# agentos system browser

Headless Google Chrome running as a `systemd --user` service with a **persistent
profile** and a CDP socket on `127.0.0.1:9222`. The resident agent reads pages
through `agentos-browse`; every visit is logged.

```
agent-browser.service      systemctl --user {status,restart,stop} agent-browser.service
agentos-browse <url>                       main text (title, final URL, ≤8,000 chars)
agentos-browse <url> --screenshot [path]   PNG → shots/<epoch>.png (or path); prints the path
agentos-browse <url> --links               up to 50 links: text → href
agentos-browse --tabs                      list open targets
   flags: --keep (leave the tab open)  --timeout <ms>
curl -s 127.0.0.1:9222/json/version        is the browser up?
```

Files here:

| path | what |
|---|---|
| `profile/` | Chrome user-data-dir: cookies, logins, local storage. **Treat like a password store.** |
| `shots/` | screenshots (`<epoch>.png`) |
| `visits.jsonl` | one line per `agentos-browse` call: `{ts,url,final_url,title,intent,mode}` |
| `browse.mjs` + `agentos-browse` | the CLI (Node Playwright `connectOverCDP`; Playwright is borrowed from `~/shop-app/social-poster/node_modules`) |
| `browser-mcp.py` | stdio MCP server exposing `browse(url, mode)` for Claude Code |

## The profile holds credentials

Anything you log into inside this browser stays logged in across restarts,
because `profile/` persists. That is the point: the agent can read pages behind
logins without ever seeing a password. It also means `profile/` is as sensitive
as a password manager. It is `0700`, owned by your user, and never leaves the
box (do not back it up to shared storage, do not commit it).

## Logging into a site once, for the agent

Chrome does not share a user-data-dir between two processes, so stop the
service, run the **same binary non-headless with the same profile**, log in, quit,
and start the service again:

```
systemctl --user stop agent-browser.service
/usr/bin/google-chrome-stable --user-data-dir=~/resident/browser/profile --no-first-run
#   ... log in to the site in the window that opens, then quit Chrome (Ctrl+Q) ...
systemctl --user start agent-browser.service
agentos-browse https://the-site.example/account      # should now show the logged-in page
```

Do this at the NUC's desktop or over xrdp/gnome-remote-desktop (headless over
SSH has no display). Sites that fingerprint headless mode may still log you out
later; re-run the steps above.

## Every visit is logged

`visits.jsonl` records each URL the agent opened, where it ended up, the page
title, the mode, and `$AGENTOS_INTENT` if the caller set it (so a visit can be
traced to an intent in `intents.db`). Read it with
`tail -n 20 ~/agentos/browser/visits.jsonl | jq .`. Nothing is sent anywhere else.

## Why it's here (provenance)

Recorded as an intent in agentos (`why agent-browser`, `why 9222`). The unit
file is `~/.config/systemd/user/agent-browser.service`; the CLI is symlinked at
`~/.local/bin/agentos-browse`. Chrome flags: `--headless=new
--remote-debugging-port=9222 --remote-debugging-address=127.0.0.1
--user-data-dir=.../profile --no-first-run --disable-background-networking
--disable-sync --window-size=1280,900`. CDP is bound to loopback only; anything
with a local shell as your user can drive the browser (and therefore its
logins), which is the same trust boundary as the profile directory itself.

## Remove

```
systemctl --user disable --now agent-browser.service
rm ~/.config/systemd/user/agent-browser.service ~/.local/bin/agentos-browse
rm -rf ~/agentos/browser          # deletes the profile (logins) and visit log
```
