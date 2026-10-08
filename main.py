import argparse
import email.utils
import getpass
import json
import logging
import os
import random
import re
import sys
import threading
import time
import builtins
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def default_config():
    cfg = {}
    cfg["threads"] = 10
    cfg["request_timeout"] = 8.0
    cfg["connect_timeout"] = 5.0
    cfg["normal_poll_interval"] = 5.0
    cfg["approaching_poll_interval"] = 1.0
    cfg["high_precision_interval"] = 0.05
    cfg["precision_phase_seconds"] = 10.0
    cfg["approaching_phase_seconds"] = 60.0
    cfg["max_retries"] = 3
    cfg["backoff_base"] = 0.5
    cfg["backoff_max"] = 8.0
    cfg["proxy_file"] = "proxies.txt"
    cfg["account_file"] = "accounts.json"
    cfg["timezone"] = "UTC"
    cfg["namemc_enabled"] = True
    cfg["logging_enabled"] = True
    cfg["log_dir"] = "logs"
    cfg["user_agent"] = "MinecraftNameSniper/1.0"
    cfg["namemc_request_interval"] = 3.0
    cfg["drop_safety_margin"] = 0.0
    cfg["simulation"] = False
    return cfg


def check_config(c):
    bad = []
    if not 1 <= c["threads"] <= 64:
        bad.append("threads has to be between 1 and 64")
    if c["request_timeout"] <= 0 or c["request_timeout"] > 120:
        bad.append("request_timeout has to be between 0 and 120 seconds")
    if c["connect_timeout"] <= 0 or c["connect_timeout"] > 60:
        bad.append("connect_timeout has to be between 0 and 60 seconds")
    if c["normal_poll_interval"] < 0.2:
        bad.append("normal_poll_interval has to be at least 0.2")
    if c["approaching_poll_interval"] < 0.05:
        bad.append("approaching_poll_interval has to be at least 0.05")
    if not 0.001 <= c["high_precision_interval"] <= 1.0:
        bad.append("high_precision_interval has to be between 0.001 and 1.0")
    if c["precision_phase_seconds"] <= 0 or c["precision_phase_seconds"] > 600:
        bad.append("precision_phase_seconds has to be between 0 and 600")
    if c["approaching_phase_seconds"] <= c["precision_phase_seconds"]:
        bad.append("approaching_phase_seconds has to be bigger than precision_phase_seconds")
    if c["max_retries"] < 0 or c["max_retries"] > 10:
        bad.append("max_retries has to be between 0 and 10")
    if c["backoff_base"] < 0 or c["backoff_max"] < c["backoff_base"]:
        bad.append("backoff_max has to be at least backoff_base")
    if c["timezone"] != "UTC":
        bad.append("only UTC works here")
    if c["namemc_request_interval"] < 1.0:
        bad.append("namemc_request_interval has to be at least 1 second so we dont spam namemc")
    if c["drop_safety_margin"] < 0 or c["drop_safety_margin"] > 5.0:
        bad.append("drop_safety_margin has to be between 0 and 5")
    if bad:
        raise ValueError("bad config\n  - " + "\n  - ".join(bad))


def config_from_dict(data):
    known = set(default_config().keys())
    weird = sorted(set(data) - known)
    if weird:
        raise ValueError("unknown stuff in config json " + ", ".join(weird))
    cfg = default_config()
    for k, v in data.items():
        if k in known:
            cfg[k] = v
    check_config(cfg)
    return cfg


def load_config(path="config.json"):
    p = Path(path)
    if not p.exists():
        cfg = default_config()
        check_config(cfg)
        return cfg
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("config json is broken " + str(exc))
    if not isinstance(data, dict):
        raise ValueError("config json has to be an object at the top")
    return config_from_dict(data)


name_re = re.compile(r"^[A-Za-z0-9_]{3,16}$")
profile_re = re.compile(r"^/profile/([^/?#]+)/?$")
names_re = re.compile(r"^/minecraft-names/([^/?#]+)/?$")
one_name_re = re.compile(r"^/name/([^/?#]+)/?$")


def check_username(name):
    if name is None or not str(name).strip():
        raise ValueError("name is empty")
    name = str(name).strip()
    if not 3 <= len(name) <= 16:
        raise ValueError("name has to be 3 to 16 chars long")
    if not name_re.match(name):
        raise ValueError("name can only have letters numbers and underscore")
    return name


def get_username(raw):
    if raw is None or not str(raw).strip():
        raise ValueError("you didnt type anything")
    text = str(raw).strip()
    if "://" in text or text.lower().startswith("namemc.com"):
        return _name_from_url(text)
    if "/" in text or "?" in text or " " in text:
        raise ValueError("that doesnt look right give a name or a namemc link")
    return check_username(text)


def _name_from_url(url):
    fixed = url.strip()
    if fixed.lower().startswith("namemc.com"):
        fixed = "https://" + fixed
    try:
        parsed = urllib.parse.urlparse(fixed)
    except Exception as exc:
        raise ValueError("bad link " + str(exc))
    host = (parsed.netloc or "").lower()
    if not (host == "namemc.com" or host.endswith(".namemc.com")):
        raise ValueError("that link is not namemc")
    path = parsed.path or ""
    query = urllib.parse.parse_qs(parsed.query)
    found = None
    for pat in (profile_re, names_re, one_name_re):
        m = pat.match(path)
        if m:
            found = urllib.parse.unquote(m.group(1))
            break
    if found is None:
        if path.rstrip("/") == "/search" and "q" in query and query["q"]:
            found = query["q"][0].strip()
        elif path in ("", "/"):
            raise ValueError("that namemc link has no name in it")
        else:
            raise ValueError("weird namemc link use profile name minecraft-names or search")
    nospace = (found or "").replace("-", "")
    if found and len(nospace) == 32 and all(c in "0123456789abcdefABCDEF" for c in nospace):
        raise ValueError("that link has a uuid not a name")
    if not found:
        raise ValueError("couldnt find a name in that link")
    return check_username(found)


log_lock = threading.Lock()
token_re = re.compile(r"(?i)(bearer\s+[A-Za-z0-9\-._~+/=]{8,})")


def hide_tokens(text):
    return token_re.sub("Bearer [HIDDEN]", str(text))


def setup_logs(log_dir="logs", enabled=True, level=logging.INFO):
    with log_lock:
        logger = logging.getLogger("snipe")
        if getattr(logger, "ready", False):
            return logger
        logger.setLevel(level)
        logger.propagate = False
        fmt = logging.Formatter("%(asctime)s.%(msecs)03dZ %(levelname)-7s [%(threadName)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        ch = logging.StreamHandler()
        ch.setLevel(level)
        ch.setFormatter(fmt)
        logger.addHandler(ch)
        if enabled:
            d = Path(log_dir)
            d.mkdir(parents=True, exist_ok=True)
            for fname, lvl in (("application.log", level), ("errors.log", logging.WARNING)):
                fh = logging.FileHandler(d / fname, encoding="utf-8")
                fh.setLevel(lvl)
                fh.setFormatter(fmt)
                logger.addHandler(fh)
            sh = logging.FileHandler(d / "success.log", encoding="utf-8")
            sh.setLevel(logging.INFO)
            sh.setFormatter(fmt)
            winlog = logging.getLogger("snipe.win")
            winlog.setLevel(logging.INFO)
            winlog.propagate = False
            winlog.addHandler(sh)
            winlog.addHandler(ch)
        logger.ready = True
        return logger


def get_log():
    return logging.getLogger("snipe")


def log_event(logger, level, username, account, worker, op, result, detail=""):
    logger.log(level, hide_tokens(f"user={username} account={account} worker={worker} op={op} result={result} {detail}".strip()))


def log_win(username, account, detail=""):
    logging.getLogger("snipe.win").info(hide_tokens(f"SUCCESS user={username} account={account} {detail}".strip()))


net_ua = "MinecraftNameSniper/1.0"
net_timeout = 8.0
net_connect = 5.0
net_retries = 3
net_backoff = 0.5
net_backoff_top = 8.0
net_sessions = threading.local()


def setup_net(cfg):
    global net_ua, net_timeout, net_connect, net_retries, net_backoff, net_backoff_top
    net_ua = cfg["user_agent"]
    net_timeout = cfg["request_timeout"]
    net_connect = cfg["connect_timeout"]
    net_retries = cfg["max_retries"]
    net_backoff = cfg["backoff_base"]
    net_backoff_top = cfg["backoff_max"]


def get_sess():
    s = getattr(net_sessions, "s", None)
    if s is None:
        s = requests.Session()
        adapter = HTTPAdapter(max_retries=Retry(total=0), pool_connections=20, pool_maxsize=20)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        s.headers.update({"User-Agent": net_ua, "Accept": "*/*"})
        net_sessions.s = s
    return s


def wait_a_bit(n, jitter=False):
    w = min(net_backoff * (2 ** n), net_backoff_top)
    if jitter:
        w = w * (0.7 + random.random() * 0.6)
    return w


def slow_down_wait(resp, n):
    w = wait_a_bit(n)
    if resp.status_code == 429:
        try:
            ra = resp.headers.get("Retry-After")
            if ra is not None:
                w = max(w, min(float(ra), net_backoff_top))
        except ValueError:
            pass
    return w


def try_once(method, url, headers, json_body, data, params, proxies, timeout):
    try:
        resp = get_sess().request(method.upper(), url, headers=dict(headers or {}), json=json_body, data=data, params=params, proxies=proxies, timeout=(net_connect, timeout or net_timeout))
    except requests.Timeout as exc:
        return {"ok": False, "retry": True, "jitter": True, "status": None, "kind": "timeout", "msg": "timed out: " + str(exc), "text": "", "headers": {}, "url": url}
    except requests.ConnectionError as exc:
        return {"ok": False, "retry": True, "jitter": True, "status": None, "kind": "connection_error", "msg": "no connection: " + str(exc), "text": "", "headers": {}, "url": url}
    code = resp.status_code
    if code == 429:
        return {"ok": False, "retry": True, "jitter": False, "status": 429, "kind": "rate_limited", "msg": "rate limited by " + url + " " + resp.text[:300], "text": resp.text, "headers": dict(resp.headers), "url": resp.url}
    if 500 <= code <= 599:
        return {"ok": False, "retry": True, "jitter": False, "status": code, "kind": "http_5xx", "msg": "server broke http " + str(code), "text": resp.text, "headers": dict(resp.headers), "url": resp.url}
    if 200 <= code <= 299:
        return {"ok": True, "retry": False, "jitter": False, "status": code, "kind": "", "msg": "", "text": resp.text, "headers": dict(resp.headers), "url": resp.url}
    if code == 403:
        kind = "forbidden"
    else:
        kind = "http_error"
    return {"ok": False, "retry": False, "jitter": False, "status": code, "kind": kind, "msg": "HTTP " + str(code) + " for " + url + ": " + resp.text[:300], "text": resp.text, "headers": dict(resp.headers), "url": resp.url}


def do_request(method, url, headers=None, json_body=None, data=None, params=None, proxy=None, timeout=None, tries=None):
    if tries is None:
        tries = net_retries + 1
    tries = max(1, tries)
    proxies = {"http": proxy, "https": proxy} if proxy else None
    last = {"ok": False, "retry": False, "status": None, "kind": "connection_error", "msg": "no tries ran", "text": "", "headers": {}, "url": url}
    for n in range(tries):
        r = try_once(method, url, headers, json_body, data, params, proxies, timeout)
        if not r["retry"]:
            return r
        last = r
        if n >= tries - 1:
            break
        if r["status"] == 429:
            time.sleep(slow_down_wait_simple(r, n))
        else:
            time.sleep(wait_a_bit(n, jitter=r["jitter"]))
    last["ok"] = False
    return last


def slow_down_wait_simple(r, n):
    w = wait_a_bit(n)
    if r["status"] == 429:
        try:
            ra = r["headers"].get("Retry-After")
            if ra is not None:
                w = max(w, min(float(ra), net_backoff_top))
        except ValueError:
            pass
    return w


def bearer(acc):
    return {"Authorization": "Bearer " + acc["token"]}


def px_url(proxy):
    if proxy:
        return proxy["url"]
    return None


proxy_re = re.compile(r"^\s*(?:(?P<scheme>https?|socks5)://)?(?:(?P<user>[^:@/\s]+):(?P<pass>[^@/\s]+)@)?(?P<host>[^:@/\s]+):(?P<port>\d{1,5})\s*$")
cooldown_time = 300.0


def read_proxy(line):
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    m = proxy_re.match(s)
    if not m:
        raise ValueError(f"bad proxy {line.strip()}")
    scheme = m.group("scheme") or "http"
    host = m.group("host")
    port = int(m.group("port"))
    if not 1 <= port <= 65535:
        raise ValueError(f"bad proxy port in {line.strip()}")
    user = m.group("user")
    pwd = m.group("pass")
    if user and pwd:
        url = f"{scheme}://{user}:{pwd}@{host}:{port}"
    else:
        url = f"{scheme}://{host}:{port}"
    return {"raw": s, "url": url, "host": host, "port": port, "fails": 0, "wins": 0, "rest_until": 0.0}


def load_proxy_file(path):
    p = Path(path)
    if not p.exists():
        return []
    out = []
    bad = []
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        try:
            info = read_proxy(line)
            if info is not None:
                out.append(info)
        except ValueError as exc:
            bad.append(f"line {i}: {exc}")
    if bad:
        raise ValueError("proxies txt is broken\n  - " + "\n  - ".join(bad))
    seen = set()
    clean = []
    for pr in out:
        if pr["url"] not in seen:
            seen.add(pr["url"])
            clean.append(pr)
    return clean


def make_pool(items, cooldown=cooldown_time):
    return {"items": list(items or []), "i": 0, "lock": threading.Lock(), "cooldown": cooldown}


def pool_from_file(path, cooldown=cooldown_time):
    return make_pool(load_proxy_file(path), cooldown=cooldown)


def pool_ready(px):
    return time.monotonic() >= px["rest_until"]


def pool_get(pool):
    with pool["lock"]:
        items = pool["items"]
        if not items:
            return None
        for _ in range(len(items)):
            cand = items[pool["i"] % len(items)]
            pool["i"] = (pool["i"] + 1) % len(items)
            if pool_ready(cand):
                return cand
        return None


def pool_good(pool, px):
    if px is None:
        return
    with pool["lock"]:
        px["wins"] += 1
        px["fails"] = 0


def pool_bad(pool, px, dead=False):
    if px is None:
        return
    with pool["lock"]:
        px["fails"] += 1
        if dead:
            px["rest_until"] = time.monotonic() + pool["cooldown"]
        else:
            px["rest_until"] = time.monotonic() + min(pool["cooldown"], 30.0 * px["fails"])


acc_lock = threading.Lock()


def load_accounts(path):
    p = Path(path)
    if not p.exists():
        raise ValueError(f"cant find {p} run python main.py --setup first")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"accounts file is broken json {exc}")
    if not isinstance(data, list) or not data:
        raise ValueError(f"{p} has to be a list of accounts")
    accs = []
    seen = set()
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise ValueError(f"account number {i} has to have id and access_token")
        aid = str(entry.get("id", "")).strip()
        token = str(entry.get("access_token", "")).strip()
        if not aid:
            raise ValueError(f"account number {i} has no id")
        if aid in seen:
            raise ValueError(f"two accounts called {aid}")
        seen.add(aid)
        if not token or token.startswith("REPLACE_"):
            raise ValueError(f"account {aid} still has the placeholder token go get a real one")
        accs.append({"id": aid, "token": token, "notes": str(entry.get("notes", "")), "valid": None, "err": ""})
    return accs


def make_acc_pool(accs):
    if not accs:
        raise ValueError("no accounts")
    return {"items": accs, "i": 0, "lock": threading.Lock()}


def acc_next(pool):
    with pool["lock"]:
        items = pool["items"]
        for _ in range(len(items)):
            cand = items[pool["i"] % len(items)]
            pool["i"] = (pool["i"] + 1) % len(items)
            if cand["valid"] is not False:
                return cand
        cand = items[pool["i"] % len(items)]
        pool["i"] = (pool["i"] + 1) % len(items)
        return cand


def acc_flag(acc, valid, err=""):
    with acc_lock:
        acc["valid"] = valid
        acc["err"] = err


def read_yes_no(raw, default=True):
    s = raw.strip().lower()
    if not s:
        return default
    if s in ("y", "yes"):
        return True
    if s in ("n", "no"):
        return False
    return None


def ask_yes_no(msg, default=True):
    hint = "Y/n" if default else "y/N"
    while True:
        try:
            raw = input(f"{msg} [{hint}]: ")
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            raise SystemExit(1)
        val = read_yes_no(raw, default=default)
        if val is None:
            print("just type Y or N")
            continue
        return val


def ask_int(msg, default, low, high):
    while True:
        try:
            raw = input(f"{msg} [{default}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            raise SystemExit(1)
        if not raw:
            return default
        try:
            val = int(raw)
        except ValueError:
            print(f"give a whole number between {low} and {high}")
            continue
        if not low <= val <= high:
            print(f"give a whole number between {low} and {high}")
            continue
        return val


def ask_float(msg, default, low, high):
    while True:
        try:
            raw = input(f"{msg} [{default}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            raise SystemExit(1)
        if not raw:
            return default
        try:
            val = float(raw)
        except ValueError:
            print(f"give a number between {low} and {high}")
            continue
        if not low <= val <= high:
            print(f"give a number between {low} and {high}")
            continue
        return val


challenge_words = ("interaction_required", "additional_verification", "AADSTS50076", "AADSTS50079", "verification", "mfa", "conditional access")


def is_challenge(text):
    low = text.lower()
    for w in challenge_words:
        if w in low:
            return True
    return False


def parse_profile_body(text, aid):
    try:
        data = json.loads(text) if text else None
    except Exception:
        raise ValueError(f"mojang sent weird stuff for {aid}")
    if not isinstance(data, dict) or "id" not in data or "name" not in data:
        raise ValueError(f"mojang sent weird stuff for {aid}")
    return data


def check_login(acc, proxy=None):
    me_url = "https://api.minecraftservices.com/minecraft/profile"
    r = do_request("GET", me_url, headers=bearer(acc), proxy=proxy, tries=2)
    if not r["ok"]:
        if r["status"] in (401, 403) and is_challenge(r["msg"]):
            raise ValueError(f"account {acc['id']} needs extra verification go log in in your browser then get a new token and try again")
        if r["status"] in (401, 403):
            raise ValueError(f"account {acc['id']} login failed http {r['status']} token is bad or old go get a new one")
        raise ValueError(f"checking {acc['id']} broke ({r['kind']}) {r['msg']}")
    data = parse_profile_body(r["text"], acc["id"])
    info = {"acc": acc["id"], "uuid": str(data["id"]), "name": str(data["name"]), "can_change": None}
    r2 = do_request("GET", "https://api.minecraftservices.com/minecraft/profile/namechange-info", headers=bearer(acc), proxy=proxy, tries=1)
    if r2["ok"]:
        try:
            payload = json.loads(r2["text"]) if r2["text"] else None
            if isinstance(payload, dict):
                info["can_change"] = bool(payload.get("nameChangeAllowed", True))
        except Exception:
            pass
    acc_flag(acc, True, "")
    return info


def flag_account(acc, exc):
    msg = str(exc)[:300]
    if "401" in msg or "403" in msg or "login failed" in msg or "verification" in msg:
        acc_flag(acc, False, msg)


wait_days = 37

date_re = re.compile(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)")
drop_re = re.compile(r"(?i)(?:drop(?:s|ped)?|available)(?:\s+(?:at|on|in))?\s*:?\s*(.{0,80})")


def make_info(user, free, drop, exact, src, detail, bit=""):
    if drop is not None and drop.tzinfo is None:
        drop = drop.replace(tzinfo=timezone.utc)
    return {"user": user, "free": free, "drop": drop, "exact": exact, "src": src, "detail": detail, "bit": bit, "at": datetime.now(timezone.utc)}


def info_label(info):
    if info["drop"] is None:
        return "UNKNOWN"
    if info["exact"]:
        return "EXACT TIMESTAMP"
    return "ESTIMATED TIMESTAMP"


def make_namemc(wait_between=3.0, cache_time=60.0):
    return {"wait": max(1.0, wait_between), "ttl": cache_time, "saved": {}, "last": 0.0, "lock": threading.Lock()}


def guess_drop(last_change):
    if last_change.tzinfo:
        base = last_change
    else:
        base = last_change.replace(tzinfo=timezone.utc)
    return base + timedelta(days=wait_days)


def namemc_lookup(m, username, use_namemc=True):
    key = username.lower()
    with m["lock"]:
        hit = m["saved"].get(key)
        if hit and (time.monotonic() - hit[0]) < m["ttl"]:
            return hit[1]
    res = nm_fresh(m, username, use_namemc)
    with m["lock"]:
        m["saved"][key] = (time.monotonic(), res)
    return res


def nm_slow_down(m):
    with m["lock"]:
        wait = m["wait"] - (time.monotonic() - m["last"])
    if wait > 0:
        time.sleep(wait)
    with m["lock"]:
        m["last"] = time.monotonic()


def nm_fresh(m, username, use_namemc):
    page = None
    note = ""
    if use_namemc:
        try:
            page = nm_pages(m, username)
            note = "got the namemc page"
        except ValueError as exc:
            note = f"namemc broke ({exc}) using mojang instead"
    try:
        taken = nm_taken(username)
        note2 = "mojang says taken" if taken else "mojang says maybe free"
    except Exception as exc:
        taken = None
        note2 = f"mojang broke ({exc})"
    exact = None
    last = None
    free = None
    bit = ""
    if page:
        free, exact, last, bit = read_page(username, page)
    if taken is None and free is not None:
        taken = not free
    if taken is not None:
        avail = not taken
    else:
        avail = free
    srcs = []
    if page is not None:
        srcs.append("NameMC")
    if taken is not None:
        srcs.append("Mojang")
    src = "+".join(srcs) if srcs else "none"
    if avail is True:
        return make_info(username, True, datetime.now(timezone.utc), False, src or "Mojang", f"looks free right now {note} {note2}".strip(), bit[:500])
    if exact is not None:
        return make_info(username, False, exact, True, "NameMC", f"namemc says it drops then {note2}".strip(), bit[:500])
    if last is not None:
        return make_info(username, False, guess_drop(last), False, (src or "NameMC") + "+37 day guess", f"guess last change {last.isoformat()} plus {wait_days} days namemc never promises this {note} {note2}".strip(), bit[:500])
    if taken is True:
        return make_info(username, False, None, False, src or "unknown", f"name is taken but no idea when it drops {note} {note2} not making up a time".strip(), bit[:500])
    return make_info(username, None, None, False, src or "unknown", f"no clue everything broke {note} {note2}".strip(), bit[:500])


def nm_pages(m, username):
    nm_slow_down(m)
    err = None
    urls = ["https://namemc.com/minecraft-names/" + quote(username), "https://namemc.com/search?q=" + quote(username)]
    for url in urls:
        r = do_request("GET", url, headers={"Accept": "text/html"}, tries=2)
        if r["ok"] and r["text"] and len(r["text"]) > 500:
            return r["text"]
        if r["status"] == 404:
            err = f"namemc has no page for {username}"
            continue
        if r["status"] == 403:
            raise ValueError("namemc blocked us try again later")
        if r["kind"] == "rate_limited" or r["status"] == 429:
            raise ValueError("namemc says slow down")
        err = r["msg"]
    raise ValueError(str(err) if err else "namemc fetch broke")


def nm_taken(username):
    r = do_request("GET", f"https://api.mojang.com/users/profiles/minecraft/{quote(username)}", tries=2)
    if r["status"] == 200:
        return True
    if r["status"] == 400 or "No account" in r["msg"]:
        return False
    r2 = do_request("GET", "https://api.minecraftservices.com/minecraft/profile/lookup/name/" + quote(username), tries=2)
    if r2["status"] == 200:
        return True
    if r2["status"] == 404:
        return False
    if r2["status"] is None or r2["status"] >= 500 or r2["kind"] in ("timeout", "connection_error", "rate_limited"):
        raise ValueError(r2["msg"])
    return None


def scrape_text(html):
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    try:
        return BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", text)


def read_page(username, html):
    text = scrape_text(html)
    if text is None:
        return None, None, None, html[:500]
    bit = text[:2000]
    low = text.lower()
    free = None
    if re.search(r"(?i)\bavailable\b", text) and re.search(r"(?i)(availability|status)\s*:?\s*available", text):
        free = True
    elif re.search(r"(?i)available\s+(now|!)", text) and username.lower() in low:
        free = True
    if re.search(r"(?i)(unavailable|taken|not available|already (taken|in use))", text) and (re.search(r"(?i)unavailable", text) or "taken" in low):
        free = False
    exact = find_drop_date(text)
    last = None
    hist = re.search(r"(?i)(?:changed|history|last\s+seen|updated)\s*(?:at|on)?\s*:?\s*(.{0,60})", text)
    if hist:
        iso = date_re.search(hist.group(0))
        if iso:
            last = read_date(iso.group(1))
    if last is None:
        dates = [d for d in [read_date(mm.group(1)) for mm in date_re.finditer(text)] if d is not None]
        if dates and exact is None:
            now = datetime.now(timezone.utc)
            old = [d for d in dates if d <= now]
            if old:
                last = max(old)
    return free, exact, last, bit


def find_drop_date(text):
    m = drop_re.search(text)
    if m:
        iso = date_re.search(m.group(0) + " " + text[max(0, m.start() - 200):m.end() + 200])
        if iso:
            got = read_date(iso.group(1))
            if got is not None:
                return got
    for im in date_re.finditer(text):
        if "drop" in text[max(0, im.start() - 120):im.end() + 120].lower():
            got = read_date(im.group(1))
            if got is not None:
                return got
    return None


def read_date(s):
    try:
        from dateutil import parser as dp
        dt = dp.isoparse(s.replace(" ", "T") if " " in s and "T" not in s else s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def utc_now():
    return datetime.now(timezone.utc)


def fmt_time(dt, ms=True):
    dt = dt.astimezone(timezone.utc)
    base = dt.strftime("%Y-%m-%d %H:%M:%S")
    if ms:
        return f"{base}.{dt.microsecond // 1000:03d} UTC"
    return base + " UTC"


def fmt_left(secs):
    if secs < 0:
        secs = 0.0
    ms = int(round(secs * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


date_places = ("https://api.minecraftservices.com/publickeys", "https://api.mojang.com/users/profiles/minecraft/Notch", "https://sessionserver.mojang.com/session/minecraft/hasJoined")


def make_clock(get=None):
    return {"diff": 0.0, "lock": threading.Lock(), "n": 0, "get": get}


def clock_now(c):
    with c["lock"]:
        d = c["diff"]
    return utc_now() + timedelta(seconds=d)


def one_sample(do, url, timeout):
    before = time.monotonic()
    hdr = do(url, timeout)
    after = time.monotonic()
    if not hdr:
        return None
    try:
        server = email.utils.parsedate_to_datetime(hdr)
    except Exception:
        return None
    if server.tzinfo is None:
        server = server.replace(tzinfo=timezone.utc)
    return server.timestamp() + (after - before) / 2.0 - utc_now().timestamp()


def clock_sync(c, timeout=5.0, tries=3):
    do = c["get"] or get_date
    diffs = []
    used = ""
    for url in date_places[:tries]:
        try:
            d = one_sample(do, url, timeout)
        except Exception:
            continue
        if d is None:
            continue
        diffs.append(d)
        used = url
    with c["lock"]:
        if diffs:
            diffs.sort()
            c["diff"] = diffs[len(diffs) // 2]
            c["n"] += 1
        return {"diff": c["diff"], "hits": len(diffs), "where": used}


def get_date(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "MinecraftNameSniper/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.headers.get("Date")
    except Exception:
        pass
    try:
        req.get_method = lambda: "HEAD"
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.headers.get("Date")
    except Exception:
        return None


def make_countdown(target, clock=None, warn_at=60.0, hurry_at=10.0, slow_wait=5.0, mid_wait=1.0, fast_wait=0.05):
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    target = target.astimezone(timezone.utc)
    if clock is None:
        clock = make_clock()
    left = (target - clock_now(clock)).total_seconds()
    return {"target": target, "clock": clock, "warn": warn_at, "hurry": hurry_at, "slow": slow_wait, "mid": mid_wait, "fast": fast_wait, "end": time.monotonic() + max(0.0, left)}


def cd_left(cd):
    return max(0.0, cd["end"] - time.monotonic())


def cd_now(cd):
    return cd["target"] - timedelta(seconds=cd_left(cd))


def cd_phase(cd):
    r = cd_left(cd)
    if r <= 0:
        return "target"
    if r <= cd["hurry"]:
        return "precision"
    if r <= cd["warn"]:
        return "approaching"
    return "normal"


def cd_wait(cd):
    r = cd_left(cd)
    if r <= 0:
        return 0.0
    if r <= 1.0:
        return min(0.005, r)
    if r <= cd["hurry"]:
        return min(cd["fast"], r)
    if r <= cd["warn"]:
        return min(cd["mid"], r)
    return min(cd["slow"], r)


def make_monitor(name, func=None):
    return {"name": name, "func": func, "state": "UNKNOWN", "lock": threading.Lock(), "hist": []}


def mon_set(m, new, msg=""):
    with m["lock"]:
        old = m["state"]
        if old == new and not msg:
            return False
        m["state"] = new
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        m["hist"].append((ts, new, msg))
    line = f"[{ts}] [{new}] {m['name']} {msg}".rstrip()
    if m["func"] is not None:
        try:
            m["func"](old, new, line)
        except Exception:
            pass
    return True


def mon_get(m):
    with m["lock"]:
        return m["state"]


def phase_for(left, hurry, warn):
    if left <= 0:
        return "ATTEMPTING"
    if left <= hurry:
        return "READY"
    if left <= warn:
        return "APPROACHING"
    return "MONITORING"


def make_result(outcome, name, acc, code, msg, got=None, tries=0):
    return {"outcome": outcome, "name": name, "acc": acc, "code": code, "msg": msg, "got": got, "tries": tries}


def parse_status_body(text):
    try:
        data = json.loads(text) if text else None
    except Exception as exc:
        return None, f"mojang sent junk {exc}"
    st = str(data.get("status", "")).upper() if isinstance(data, dict) else ""
    return (st or None), f"mojang says {st or 'dunno'}"


def check_avail(name, acc, proxy=None):
    link = f"https://api.minecraftservices.com/minecraft/profile/name/{name}/available"
    r = do_request("GET", link, headers=bearer(acc), proxy=px_url(proxy), tries=1)
    if not r["ok"]:
        if r["status"] in (401, 403):
            flag_account(acc, r["msg"])
            return None, f"login broke http {r['status']} token is bad"
        if r["status"] == 404:
            return parse_status_body(r["text"])
        if r["status"] == 429 or r["kind"] == "rate_limited":
            return None, "mojang says slow down"
        if r["kind"] in ("timeout", "connection_error"):
            return None, f"net broke ({r['kind']})"
        if r["status"] and 500 <= r["status"] <= 599:
            return None, f"mojang broke http {r['status']}"
        return None, f"check broke {r['msg']}"
    return parse_status_body(r["text"])


def claim(name, acc, proxy=None, timeout=None):
    where = "https://api.minecraftservices.com/minecraft/profile/name/" + name
    r = do_request("PUT", where, headers=bearer(acc), proxy=px_url(proxy), timeout=timeout or 8.0, tries=1)
    if not r["ok"]:
        if r["status"] in (401, 403):
            flag_account(acc, r["msg"])
            return make_result("FAILED", name, acc["id"], r["status"], f"login broke http {r['status']}")
        if r["status"] == 429:
            return make_result("UNKNOWN", name, acc["id"], 429, "rate limited go check by hand")
        if r["status"] == 400:
            return make_result("FAILED", name, acc["id"], 400, f"mojang said no {r['msg'][:200]}")
        if r["status"] == 404:
            return make_result("FAILED", name, acc["id"], 404, "no profile found")
        if r["kind"] in ("timeout", "connection_error"):
            return make_result("UNKNOWN", name, acc["id"], None, "net died during claim go check by hand")
        if r["status"] and 500 <= r["status"] <= 599:
            return make_result("UNKNOWN", name, acc["id"], r["status"], f"mojang broke http {r['status']} go check by hand")
        return make_result("UNKNOWN", name, acc["id"], r["status"], f"claim broke {r['msg']}")
    got = None
    try:
        data = json.loads(r["text"]) if r["text"] else None
        if isinstance(data, dict) and data.get("name"):
            got = str(data["name"])
    except Exception:
        got = None
    return make_result("SUCCESS", name, acc["id"], r["status"], f"mojang said ok http {r['status']}", got)


def verify(name, acc, proxy=None):
    px = px_url(proxy)
    r = do_request("GET", "https://api.minecraftservices.com/minecraft/profile", headers=bearer(acc), proxy=px, tries=2)
    if r["ok"]:
        try:
            data = json.loads(r["text"]) if r["text"] else None
            if isinstance(data, dict) and str(data.get("name", "")).lower() == name.lower():
                return make_result("SUCCESS", name, acc["id"], 200, f"yep account {acc['id']} owns {data.get('name')} now", str(data.get("name")))
        except Exception:
            pass
    elif r["status"] in (401, 403):
        flag_account(acc, r["msg"])
        return make_result("UNKNOWN", name, acc["id"], r["status"], "cant check login broke during check")
    r2 = do_request("GET", f"https://api.minecraftservices.com/minecraft/profile/lookup/name/{name}", proxy=px, tries=2)
    if r2["ok"]:
        return make_result("FAILED", name, acc["id"], 200, "someone else has it we didnt get it")
    if r2["status"] == 404:
        return make_result("UNKNOWN", name, acc["id"], 404, "lookup says nothing go check by hand")
    if not r2["ok"]:
        return make_result("UNKNOWN", name, acc["id"], r2["status"], f"check was unclear {r2['kind'] or r2['status']} go look by hand")
    return make_result("UNKNOWN", name, acc["id"], None, "check was unclear")


def one_try(state, wid):
    if state["done"].is_set():
        return None
    proxy = pool_get(state["pool"])
    with state["lock"]:
        state["tries"] += 1
        n = state["tries"]
    try:
        res = claim(state["name"], state["acc"], proxy, timeout=state["timeout"])
        res["tries"] = n
        if res["outcome"] == "SUCCESS":
            pool_good(state["pool"], proxy)
        else:
            pool_bad(state["pool"], proxy)
    except Exception as exc:
        res = make_result("UNKNOWN", state["name"], state["acc"]["id"], None, f"worker {wid} broke {exc}")
    with state["lock"]:
        state["results"].append(res)
        if res["outcome"] == "SUCCESS":
            state["done"].set()
    return res


def pick_winner(state):
    for want in ("SUCCESS", "UNKNOWN", "FAILED"):
        for r in state["results"]:
            if r["outcome"] == want:
                r["tries"] = state["tries"]
                return r
    return make_result("UNKNOWN", state["name"], state["acc"]["id"], None, "nothing even ran")


def run_snipe(name, acc, pool, workers=10, timeout=8.0):
    workers = max(1, min(64, workers))
    state = {"name": name, "acc": acc, "pool": pool, "timeout": timeout, "lock": threading.Lock(), "tries": 0, "done": threading.Event(), "results": []}
    exe = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="snipe")
    jobs = [exe.submit(one_try, state, i) for i in range(workers)]
    exe.shutdown(wait=True)
    outs = []
    for f in jobs:
        try:
            outs.append(f.result())
        except Exception:
            pass
    with state["lock"]:
        state["results"].extend([o for o in outs if o is not None and o not in state["results"]])
    return pick_winner(state)


bad_words = ("REPLACE_", "CHANGE_ME", "PASTE_", "YOUR_TOKEN", "REPLACE_WITH")


def is_bad_token(token):
    s = (token or "").strip()
    if not s:
        return True
    return any(w in s.upper() for w in bad_words)


def check_proxy_list(text):
    good = []
    bad = []
    seen = set()
    for i, line in enumerate(text.splitlines(), start=1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        try:
            info = read_proxy(line)
            if info is not None and info["url"] not in seen:
                seen.add(info["url"])
                good.append(info)
        except ValueError as exc:
            bad.append(f"line {i} ({s}): {exc}")
    return good, bad


def make_config(threads, timeout, namemc_on, logs_on):
    data = default_config()
    data["threads"] = threads
    data["request_timeout"] = timeout
    data["namemc_enabled"] = namemc_on
    data["logging_enabled"] = logs_on
    check_config(data)
    return data


def file_state(path, kind):
    if not path.exists():
        return "missing"
    try:
        if kind == "config":
            load_config(path)
        elif kind == "accounts":
            load_accounts(path)
        elif kind == "proxies":
            load_proxy_file(path)
        return "ok"
    except Exception as exc:
        return f"broken ({str(exc)[:100]})"


def ask_for_accounts(ask_w, get_pw, out):
    out("--- accounts ---")
    out("paste your minecraft token for each account")
    accs = []
    seen = set()
    n = 1
    while True:
        did = f"account_{n:02d}"
        rid = ask_w(f"account name [{did}]: ").strip() or did
        if rid in seen:
            out(f"{rid} is taken pick another")
            continue
        token = get_pw(f"token for {rid}: ").strip()
        if is_bad_token(token):
            out("that looks empty or PRACTICE paste a real one")
            continue
        accs.append({"id": rid, "access_token": token})
        seen.add(rid)
        n += 1
        if not ask_yes_no("add another account", default=False):
            break
    return accs


def ask_for_proxies(px_path, ask_w, out):
    out("--- proxies you can skip this ---")
    if not ask_yes_no("use proxies", default=True):
        out("ok no proxies direct connection it is")
        return []
    keep = []
    try:
        keep = load_proxy_file(px_path)
    except Exception:
        keep = []
    if keep and ask_yes_no(f"keep the {len(keep)} proxies already in the file", default=True):
        return [p["raw"] for p in keep]
    out("paste one proxy per line like host port or user pass at host port")
    out("empty line when youre done")
    pasted = []
    while True:
        line = ask_w("proxy> ")
        if not line.strip():
            break
        pasted.append(line)
    good, bad = check_proxy_list("\n".join(pasted))
    for e in bad:
        out(f"  bad {e}")
    if bad and not good:
        out("none of those worked going without proxies")
        return []
    if bad and not ask_yes_no(f"keep the {len(good)} good ones and drop the rest", default=True):
        out("ok dropping all of them")
        return []
    out(f"got {len(good)} proxies")
    return [p["raw"] for p in good]


def save_all(cfg_path, acc_path, px_path, cfg_data, accs, lines):
    cfg_path.write_text(json.dumps(cfg_data, indent=4) + "\n", encoding="utf-8")
    acc_path.write_text(json.dumps(accs, indent=4) + "\n", encoding="utf-8")
    try:
        os.chmod(acc_path, 0o600)
    except Exception:
        pass
    head = "# one proxy per line host port or user pass at host port\n# lines with hashtag and empty lines dont count\n"
    px_path.write_text(head + "".join(x + "\n" for x in lines), encoding="utf-8")


def wizard(cfg_path="config.json", ask=None, secret=None, out=None):
    if ask is None:
        ask = input
    if out is None:
        out = print

    def get_pw(prompt):
        if secret is not None:
            return secret(prompt)
        if sys.stdin.isatty():
            try:
                return getpass.getpass(prompt)
            except Exception:
                pass
        return ask(prompt)

    def ask_w(prompt):
        return ask(prompt)

    cfg_path = Path(cfg_path)
    acc_name = "accounts.json"
    px_name = "proxies.txt"
    try:
        old = load_config(cfg_path)
        acc_name = old["account_file"]
        px_name = old["proxy_file"]
    except ValueError:
        pass
    if str(cfg_path.parent) not in ("", "."):
        base = cfg_path.parent
    else:
        base = Path(".")
    acc_path = base / acc_name
    px_path = base / px_name

    out("+================================================================+")
    out("|              MINECRAFT NAME SNIPER - FIRST TIME SETUP           |")
    out("+================================================================+")
    out("this makes config json accounts json and proxies txt for you")
    out("just hit enter to keep whats in brackets nothing saves till the end")
    out("")
    out(f"  config   {cfg_path} ({file_state(cfg_path, 'config')})")
    out(f"  accounts {acc_path} ({file_state(acc_path, 'accounts')})")
    out(f"  proxies  {px_path} ({file_state(px_path, 'proxies')})")
    out("")

    real_input = builtins.input
    builtins.input = ask_w
    try:
        threads = ask_int("how many workers", 10, 1, 64)
        timeout = ask_float("timeout in seconds", 8.0, 1.0, 120.0)
        namemc_on = ask_yes_no("use namemc", default=True)
        logs_on = ask_yes_no("save logs to files", default=True)
        out("")
        accs = ask_for_accounts(ask_w, get_pw, out)
        out("")
        lines = ask_for_proxies(px_path, ask_w, out)
        out("")
        out("--- summary ---")
        out(f"  workers {threads} timeout {timeout} namemc {namemc_on} logs {logs_on}")
        out(f"  accounts {len(accs)} proxies {len(lines)}")
        if not ask_yes_no("save all this", default=True):
            out("ok threw it all away nothing saved")
            builtins.input = real_input
            return 1
        save_all(cfg_path, acc_path, px_path, make_config(threads, timeout, namemc_on, logs_on), accs, lines)
    except (EOFError, KeyboardInterrupt, SystemExit):
        builtins.input = real_input
        out("\nquit nothing saved")
        return 1
    builtins.input = real_input

    load_config(cfg_path)
    load_accounts(acc_path)
    out("")
    out("done everything saved")
    out(f"  made {cfg_path} {acc_path} {px_path}")
    out("run it with  python main.py")
    out("practice the timing with  python main.py --simulation --seconds 5 --username Notch")
    return 0


banner = "+================================================================+\n|                  MINECRAFT NAME SNIPER :D                        |\n+================================================================+"

clear_screen = "\033[H\033[J"


def read_args(argv=None):
    p = argparse.ArgumentParser(description="minecraft name sniper")
    p.add_argument("--config", default="config.json")
    p.add_argument("--username", default=None)
    p.add_argument("--url", default=None)
    p.add_argument("--simulation", action="store_true")
    p.add_argument("--seconds", type=float, default=5.0)
    p.add_argument("--target", default=None)
    p.add_argument("--no-namemc", action="store_true")
    p.add_argument("--proxies", action="store_true")
    p.add_argument("--no-proxies", action="store_true")
    p.add_argument("--setup", action="store_true")
    return p.parse_args(argv)


def ansi_ok():
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
            return True
        except Exception:
            return False
    return sys.stdout.isatty()


def ask_name():
    print(banner)
    print("type a minecraft name or a namemc link")
    seen = set()
    while True:
        try:
            raw = input("Username: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            raise SystemExit(1)
        if raw in seen:
            print("you already typed that try another one")
            continue
        seen.add(raw)
        try:
            return get_username(raw)
        except ValueError as exc:
            print(f"nope {exc}")


def show_box(name, src, kind, target, now, left, workers, nproxies, acc, status):
    if target:
        drop_s = fmt_time(target)
    else:
        drop_s = "UNKNOWN watching till its free"
    if left is not None:
        left_s = fmt_left(left)
    else:
        left_s = "--:--:--.---"
    lines = ["+================================================================+", "|                   MINECRAFT NAME SNIPER                        |", "+================================================================+"]
    lines.append(f"| Target:       {name:<44} |")
    lines.append(f"| Source:       {src:<44} |")
    lines.append(f"| Drop:         {kind:<44} |")
    lines.append(f"| Target UTC:   {drop_s:<44} |")
    lines.append(f"| Current UTC:  {fmt_time(now):<44} |")
    lines.append(f"| Remaining:    {left_s:<44} |")
    lines.append(f"| Threads:      {workers:<44} |")
    lines.append(f"| Proxies:      {nproxies:<44} |")
    lines.append(f"| Account:      {acc:<44} |")
    lines.append(f"| Status:       {status:<44} |")
    lines.append("+================================================================+")
    return "\n".join(lines)


def read_iso(s):
    from dateutil import parser as dp
    t = dp.isoparse(s)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t


def load_proxies_or_direct(cfg, args):
    try:
        pool = pool_from_file(cfg["proxy_file"])
    except ValueError as exc:
        print(f"proxies broke\n{exc}")
        return None
    if len(pool["items"]) == 0 and Path(cfg["proxy_file"]).exists():
        print(f"no good proxies in {cfg['proxy_file']} going direct")
    elif len(pool["items"]) == 0:
        print(f"no {cfg['proxy_file']} going direct")
    else:
        use_them = pick_proxy_choice(pool, cfg, args)
        if use_them:
            print(f"ok using {len(pool['items'])} proxies")
        else:
            print("ok no proxies going direct")
            pool = make_pool([])
    return pool


def pick_proxy_choice(pool, cfg, args):
    if args.no_proxies:
        return False
    if args.proxies:
        return True
    if sys.stdin.isatty():
        return ask_yes_no(f"found {len(pool['items'])} proxies in {cfg['proxy_file']} use them", default=True)
    return True


def load_accs_or_setup(cfg, args):
    try:
        return make_acc_pool(load_accounts(cfg["account_file"]))
    except Exception as exc:
        print(f"accounts broke {exc}")
        if not sys.stdin.isatty():
            return None
        if not ask_yes_no("want to run the setup thing now", default=True):
            return None
        code = wizard(args.config)
        if code != 0:
            return None
        print("nice now starting with your new stuff")
        try:
            new_cfg = load_config(args.config)
        except ValueError as exc2:
            print(f"config broke\n{exc2}")
            return None
        for k, v in new_cfg.items():
            cfg[k] = v
        try:
            return make_acc_pool(load_accounts(cfg["account_file"]))
        except Exception as exc2:
            print(f"still broke {exc2}")
            return None


def check_all_logins(pool_of_accs, proxies):
    ok = 0
    last_err = ""
    first = None
    for a in pool_of_accs["items"]:
        got = pool_get(proxies)
        if got:
            px = got["url"]
        else:
            px = None
        try:
            s = check_login(a, proxy=px)
            print(f"  ok {a['id']} is {s['name']}")
            ok += 1
            if first is None:
                first = a
            break
        except Exception as exc:
            acc_flag(a, False, str(exc)[:200])
            last_err = str(exc)
            print(f"  bad {a['id']} {exc}")
    return ok, last_err, first


def find_target(args, cfg, username, watch, log):
    if args.target:
        try:
            target = read_iso(args.target)
        except Exception as exc:
            print(f"that target date is broken {exc}")
            return None, None, None, 2
        mon_set(watch, "DROP_ESTIMATED", f"manual target {fmt_time(target)}")
        return target, "typed by you", "MANUAL", 0
    print(f"asking namemc about {username}")
    m = make_namemc(cfg["namemc_request_interval"])
    try:
        res = namemc_lookup(m, username, use_namemc=True)
    except Exception as exc:
        print(f"namemc broke {exc}")
        return None, None, None, 2
    print(f"[INFO] {res['detail']}")
    log.info("namemc user=%s avail=%s kind=%s src=%s %s", username, res["free"], info_label(res), res["src"], res["detail"])
    if res["free"] is True:
        mon_set(watch, "READY", "looks free going for it right now")
        return datetime.now(timezone.utc), res["src"], info_label(res), 0
    if res["drop"] is not None:
        mon_set(watch, "DROP_ESTIMATED", f"{info_label(res)} {fmt_time(res['drop'])} from {res['src']}")
        print(f"[DROP] {info_label(res)}: {fmt_time(res['drop'])}")
        return res["drop"], res["src"], info_label(res), 0
    print("no idea when it drops just watching till its free ctrl c to stop")
    mon_set(watch, "MONITORING", "no drop time just watching")
    return None, res["src"], info_label(res), 0


def on_phase_change(watch, phase, want, left, timer, clock):
    if want not in ("APPROACHING", "READY"):
        return timer
    mon_set(watch, want, f"{phase} phase {fmt_left(left)} left")
    try:
        clock_sync(clock)
        return resync_timer(timer, clock)
    except Exception:
        return timer


def maybe_resync(timer, clock, last_sync, left, warn_at):
    if time.monotonic() - last_sync <= 60 or left <= warn_at:
        return timer, last_sync
    try:
        clock_sync(clock)
        timer = resync_timer(timer, clock)
    except Exception:
        pass
    return timer, time.monotonic()


def show_countdown(box_args, left, phase, watch, use_clear):
    now = box_args["now"]
    timer = box_args["timer"]
    st = mon_get(watch)
    if st == "MONITORING":
        st = phase.upper()
    box = show_box(box_args["user"], box_args["src"], box_args["kind"], timer["target"], now, left, box_args["workers"], box_args["nprox"], box_args["acc"], st)
    if use_clear:
        sys.stdout.write(clear_screen + box + "\n")
        sys.stdout.flush()
    else:
        print(f"[T-{fmt_left(left)}] {phase} now={fmt_time(now)}")


def run_countdown(username, src, kind, target, cfg, proxies, me, watch, log, clock, acc_name):
    timer = make_countdown(target + timedelta(seconds=cfg["drop_safety_margin"]), clock=clock, warn_at=cfg["approaching_phase_seconds"], hurry_at=cfg["precision_phase_seconds"], slow_wait=cfg["normal_poll_interval"], mid_wait=cfg["approaching_poll_interval"], fast_wait=cfg["high_precision_interval"])
    mon_set(watch, "MONITORING", f"counting down to {fmt_time(timer['target'])}")
    last_phase = ""
    last_shown = 0.0
    last_sync = time.monotonic()
    use_clear = ansi_ok() or sys.stdout.isatty()
    PRACTICE = cfg["simulation"]
    while True:
        try:
            left = cd_left(timer)
            now = cd_now(timer)
            phase = cd_phase(timer)
            want = phase_for(left, cfg["precision_phase_seconds"], cfg["approaching_phase_seconds"])
            if phase != last_phase:
                timer = on_phase_change(watch, phase, want, left, timer, clock)
                last_phase = phase
            timer, last_sync = maybe_resync(timer, clock, last_sync, left, cfg["approaching_phase_seconds"])
            speed = 0.5 if left > 60 else (0.1 if left > 10 else 0.05)
            if time.monotonic() - last_shown >= speed or left <= 0:
                last_shown = time.monotonic()
                show_countdown({"now": now, "timer": timer, "user": username, "src": src, "kind": kind, "workers": cfg["threads"], "nprox": len(proxies["items"]), "acc": acc_name}, left, phase, watch, use_clear)
            if not PRACTICE and 0 < left <= cfg["approaching_phase_seconds"] and int(time.monotonic() * 2) % 10 == 0:
                quick_check(username, me, proxies, log)
            if left <= 0:
                break
            time.sleep(cd_wait(timer))
        except KeyboardInterrupt:
            print("\nok stopped")
            return 130
    return 0


def try_claim_now(username, acc_name, watch, log, cfg, proxies, pool_of_accs):
    print()
    print(f"[GO] time hit {fmt_time(datetime.now(timezone.utc))} trying to grab it")
    mon_set(watch, "ATTEMPTING", "trying to grab it now")
    log_event(log, logging.INFO, username, acc_name, "main", "claim_start", "started", "")
    if cfg["simulation"]:
        time.sleep(0.15)
        print("[CHECK] PRACTICE check no real claim happened")
        print("[WIN] PRACTICE run worked timing was fine")
        mon_set(watch, "SUCCESS", "PRACTICE run done no real claim")
        return 0
    acc = acc_next(pool_of_accs)
    got = run_snipe(username, acc, proxies, workers=cfg["threads"], timeout=cfg["request_timeout"])
    log_event(log, logging.INFO, username, acc["id"], "snipe", "claim", got["outcome"], f"http={got['code']} {got['msg']} tries={got['tries']}")
    print(f"[{got['outcome']}] {got['msg']} (tries={got['tries']})")
    print("[CHECK] seeing if it worked")
    fin = verify(username, acc, pool_get(proxies))
    print(f"[{fin['outcome']}] {fin['msg']}")
    log_event(log, logging.INFO, username, acc["id"], "main", "verify", fin["outcome"], fin["msg"])
    if fin["outcome"] == "SUCCESS":
        mon_set(watch, "SUCCESS", "we got it lets gooo")
        log_win(username, acc["id"], fin["msg"])
        return 0
    if fin["outcome"] == "FAILED":
        mon_set(watch, "FAILED", fin["msg"])
        return 3
    if got["outcome"] == "FAILED":
        mon_set(watch, "FAILED", got["msg"] + " / " + fin["msg"])
    else:
        mon_set(watch, "UNKNOWN", got["msg"] + " / " + fin["msg"])
    if got["outcome"] == "FAILED" and fin["outcome"] == "UNKNOWN":
        return 3
    return 4


def main(argv=None):
    args = read_args(argv)
    pretend = bool(args.simulation)
    if args.setup:
        return wizard(args.config)
    try:
        cfg = load_config(args.config)
    except ValueError as exc:
        print(f"config broke\n{exc}")
        return 2
    if pretend:
        cfg["simulation"] = True
    setup_logs(cfg["log_dir"], enabled=cfg["logging_enabled"])
    setup_net(cfg)
    log = get_log()
    try:
        if args.username:
            username = get_username(args.username)
        elif args.url:
            username = get_username(args.url)
        elif not sys.stdin.isatty():
            print("give a name with --username or --url")
            return 2
        else:
            username = ask_name()
    except ValueError as exc:
        print(f"nope {exc}")
        return 2
    proxies = load_proxies_or_direct(cfg, args)
    if proxies is None:
        return 2
    pool_of_accs = None
    acc_name = "none (PRACTICE run)" if pretend else "?"
    me = None
    if not pretend:
        pool_of_accs = load_accs_or_setup(cfg, args)
        if pool_of_accs is None:
            return 2
        print("checking logins")
        ok, last_err, me = check_all_logins(pool_of_accs, proxies)
        if ok == 0:
            print(f"no working accounts last problem {last_err}")
            print("go get a new token and put it in accounts json")
            return 2
        acc_name = me["id"]
    else:
        me = {"id": "myacc", "token": "PRACTICE", "notes": "", "valid": None, "err": ""}
    watch = make_monitor(username, func=lambda o, n, line: (print(line), log.info(line)))
    target = None
    kind = "UNKNOWN"
    src = "PRACTICE" if pretend else ("NameMC" if cfg["namemc_enabled"] and not args.no_namemc else "direct")
    if pretend:
        if args.target:
            try:
                target = read_iso(args.target)
            except Exception as exc:
                print(f"that target date is broken {exc}")
                return 2
        else:
            target = datetime.now(timezone.utc) + timedelta(seconds=max(1.0, float(args.seconds)))
        kind = "PRACTICE"
        mon_set(watch, "DROP_ESTIMATED", f"PRACTICE target {fmt_time(target)}")
        print(f"[DROP] PRACTICE target {fmt_time(target)}")
    elif args.target:
        target, src, kind, code = find_target(args, cfg, username, watch, log)
        if code != 0:
            return code
    elif cfg["namemc_enabled"] and not args.no_namemc:
        target, src, kind, code = find_target(args, cfg, username, watch, log)
        if code != 0:
            return code
    else:
        print("namemc is off just watching till its free ctrl c to stop")
        mon_set(watch, "MONITORING", "watching")
    clock = make_clock()
    print("checking the real time")
    s = clock_sync(clock)
    if s["hits"]:
        print(f"  my clock is off by {s['diff']:+.3f}s got {s['hits']} answers from {s['where'] or 'somewhere'}")
    else:
        print("  couldnt reach anyone trusting this pc clock")
    if target is None:
        return watch_loop(username, me, proxies, cfg, watch, acc_name, src)
    if target > datetime.now(timezone.utc) - timedelta(seconds=1) or pretend:
        rc = run_countdown(username, src, kind, target, cfg, proxies, me, watch, log, clock, acc_name)
        if rc != 0:
            return rc
    else:
        print("that time already passed going for it right now")
    return try_claim_now(username, acc_name, watch, log, cfg, proxies, pool_of_accs)


def resync_timer(timer, clock):
    return make_countdown(timer["target"], clock=clock, warn_at=timer["warn"], hurry_at=timer["hurry"], slow_wait=timer["slow"], mid_wait=timer["mid"], fast_wait=timer["fast"])


last_check = 0.0


def quick_check(username, acc, proxies, log):
    global last_check
    if time.monotonic() - last_check < 15.0:
        return
    last_check = time.monotonic()
    try:
        st, detail = check_avail(username, acc, pool_get(proxies))
        if st == "AVAILABLE":
            log.info("presnipe check user=%s looks FREE waiting for go time", username)
    except Exception:
        pass


def watch_tick(username, me, proxies, cfg, watch, acc_name, src):
    st, detail = check_avail(username, me, pool_get(proxies))
    now = datetime.now(timezone.utc)
    box = show_box(username, src, "WATCHING", None, now, None, cfg["threads"], len(proxies["items"]), acc_name, mon_get(watch))
    if sys.stdout.isatty():
        sys.stdout.write(clear_screen + box + f"\n[watch] {detail}\n")
        sys.stdout.flush()
    else:
        print(f"[watch] {detail}")
    if st != "AVAILABLE":
        return None
    print("[GO] its free going for it")
    mon_set(watch, "ATTEMPTING", "saw it free going for it")
    got = run_snipe(username, me, proxies, workers=cfg["threads"], timeout=cfg["request_timeout"])
    print(f"[{got['outcome']}] {got['msg']}")
    fin = verify(username, me, pool_get(proxies))
    print(f"[{fin['outcome']}] {fin['msg']}")
    if fin["outcome"] == "SUCCESS":
        mon_set(watch, "SUCCESS", "got it from watching")
        log_win(username, me["id"], fin["msg"])
        return 0
    if fin["outcome"] == "FAILED":
        return 3
    return 4


def watch_loop(username, me, proxies, cfg, watch, acc_name, src):
    print(f"watching every {cfg['normal_poll_interval']}s ctrl c to stop")
    fails = 0
    while True:
        try:
            done = watch_tick(username, me, proxies, cfg, watch, acc_name, src)
            if done is not None:
                return done
            fails = 0
        except KeyboardInterrupt:
            print("\nok stopped watching")
            return 130
        except Exception as exc:
            fails += 1
            print(f"[watch] oops ({fails}) {exc}")
            if fails > 20:
                print("too much broke quitting")
                return 1
        time.sleep(cfg["normal_poll_interval"])


try:
    main()
except KeyboardInterrupt:
    print("bye")
