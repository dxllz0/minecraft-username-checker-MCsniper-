# minecraft name sniper

i made this because i kept missing name drops by like 2 seconds lol. it watches a minecraft username, counts down to when it should drop, then tries to grab it the moment it frees up.

it only uses real mojang endpoints, nothing sketchy. and it tells you straight up if it worked or not, no fake "success!!" messages.

## what it does

you give it a name (or a namemc link), it checks namemc + mojang to guess when the name drops, shows a live countdown, then at zero it fires off a bunch of claim attempts at once and checks if you actually got it.

## you need

- python 3.12 or newer
- pip install -r requirements.txt

## install

```
cd MCChecker
pip install -r requirements.txt
```

or double click run.bat if youre on windows, same thing.

## first time setup

just run this and answer the questions:

```
python main.py --setup
```

itll make config.json, accounts.json and proxies.txt for you. nothing saves until you say yes at the end.

## how to run

```
python main.py
```

it asks for a name. you can also paste a namemc link like https://namemc.com/search?q=Notch and it figures out the name itself.

some other ways:

```
python main.py --username Notch
python main.py --username Notch --target "2026-10-07T22:41:03Z"
python main.py --username Notch --no-namemc
```

want to test without actually claiming anything:

```
python main.py --simulation --seconds 5
```

that just runs the countdown with a fake target 5 seconds out so you can see the timing work. totally safe.

## accounts (read this part)

to actually claim a name the program needs your minecraft bearer token. i dont do password logins, mojang killed that years ago anyway.

how to get one: log in through the official minecraft launcher / microsoft login, grab the bearer token that talks to api.minecraftservices.com, and stick it in accounts.json like:

```json
[{"id": "account_01", "access_token": "paste_yours_here"}]
```

or just use --setup, it asks you for it and hides what you type.

tokens expire btw. if yours is dead the program will tell you to go get a new one. it never prints your token anywhere, not in console, not in logs.

## proxies (optional)

put them in proxies.txt, one per line:

```
127.0.0.1:8080
user:pass@192.168.1.10:3128
```

if it finds any it asks `use proxies? [Y/n]` when you start. answer Y or N. bad proxies get ignored automatically after they fail. no proxies is fine too, it just connects direct.

## config

config.json has all the knobs. main ones:

- threads (how many claim attempts fire at once, default 10)
- timeouts and poll speeds for the countdown phases
- namemc_request_interval (dont set this below 1 or namemc will block you)

if you mess something up it tells you exactly whats wrong on startup.

## stuff that doesnt work / isnt my fault

- namemc has no api and never tells you the exact drop time. so unless namemc literally shows a drop time, the program guesses using the 37 day rule (names free up ~37 days after someone changes away). it always says ESTIMATED vs EXACT so you know which one it is.
- mojangs old api randomly throws 403s for no reason. the program just tries the other endpoint when that happens.
- the availability check is rate limited (like 20 checks per 5 min per account) so dont expect spam.
- you need a valid token and your account cant be on name-change cooldown. theres no way around that.
- even with perfect timing someone else might click faster. it reports SUCCESS / FAILED / UNKNOWN based on what mojang actually says back.

## when stuff breaks

- namemc 403: cloudflare being annoying, wait a bit, it uses mojang instead meanwhile
- namemc 429: youre asking too fast, raise namemc_request_interval
- 401/403 on claim: token is dead, get a new one
- microsoft asking for extra verification: go log in in your browser, then get a fresh token
- no drop time found: name is taken and nobodys saying when it drops, so it just watches until its free

## files

```
main.py (the whole program, one file)
config.json (settings)
accounts.json (your tokens, keep this private)
proxies.txt (optional)
run.bat (double click this on windows)
logs/ (application.log, success.log, errors.log)
```

gl sniping
