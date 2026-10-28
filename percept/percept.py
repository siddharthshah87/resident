#!/usr/bin/env python3
"""percept — perception daemon: makes the human's activity on this machine legible
to the resident agent. Consent-based, kill switch, audit trail (activity.jsonl).

Records (only on change, every POLL s):
  window  : active window app class + title (titles redacted by pattern)
  idle    : active <-> idle transitions (idle threshold IDLE_SECS) and screen lock
  system  : journald events (failed units, OOM, USB, network, logins, crashes)

Privacy by construction: titles and app names only. No screenshots, no keystrokes,
no clipboard, no URLs beyond what a title shows. 24h retention. `percept off` stops it.

Usage:
  percept run        daemon loop (what the systemd unit runs)
  percept once       one poll + summary, print what would be recorded
  percept on|off     kill switch (DISABLED file)
  percept status     state, session type, entries in last hour
  percept summary    write activity.md and print it
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.realpath(__file__))  # realpath: ~/.local/bin/percept is a symlink
LOG = os.path.join(HERE, "activity.jsonl")
SUMMARY = os.path.join(HERE, "activity.md")
DISABLED = os.path.join(HERE, "DISABLED")
CURSOR = os.path.join(HERE, "journal.cursor")
INBOX = os.path.join(os.path.dirname(HERE), "inbox")

POLL = 5
SUMMARY_EVERY = 60
IDLE_SECS = 300
RETENTION = timedelta(hours=24)
REDACT = re.compile(r"(?i)password|passcode|2fa|otp|bank|login")
UID = os.getuid()

# journald patterns -> (event, summary builder)
SYS_PATTERNS = [
    ("failed-unit", re.compile(r"^(?P<unit>\S+): Failed with result '(?P<res>[^']+)'")),
    ("oom", re.compile(r"Out of memory: Killed process (?P<pid>\d+) \((?P<name>[^)]+)\)")),
    ("oom", re.compile(r"oom-kill:.*?,task=(?P<name>[^,]+)")),
    ("oom", re.compile(r"Killed (?P<unit>\S+) due to memory (?:pressure|used)")),  # systemd-oomd
    ("usb", re.compile(r"New USB device found, idVendor=(?P<vid>\w+), idProduct=(?P<pid>\w+)")),
    ("usb", re.compile(r"USB disconnect, device number (?P<num>\d+)")),
    ("network", re.compile(r"device \((?P<dev>[^)]+)\): state change: \S+ -> (?P<st>activated|disconnected|unavailable) ")),
    ("network", re.compile(r"\((?P<dev>[^)]+)\): link (?P<st>connected|disconnected)")),
    ("login", re.compile(r"Accepted (?P<how>\w+) for (?P<user>\S+) from (?P<ip>\S+)")),
    ("login", re.compile(r"xrdp.*?login successful for user (?P<user>\S+)")),
    ("crash", re.compile(r"called for pid (?P<pid>\d+), signal (?P<sig>\d+), core limit")),
]
JOURNAL_GREP = ("Failed with result|Out of memory|oom-kill|due to memory|New USB device|USB disconnect"
                "|state change:|link (connected|disconnected)|Accepted (publickey|password)|login successful"
                "|called for pid")


def now():
    return datetime.now().astimezone().replace(microsecond=0)


def iso(dt):
    return dt.isoformat()


def parse_ts(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def redact(title):
    if not title:
        return title
    return "[redacted]" if REDACT.search(title) else title


def run(cmd, env=None, timeout=4):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, r.stdout
    except Exception:
        return 1, ""


# ---------------------------------------------------------------- session env
def session_env():
    """Find DISPLAY/XAUTHORITY/DBUS/WAYLAND_DISPLAY from the user's graphical session
    (gnome-shell env), since the daemon may run from systemd without them."""
    env = dict(os.environ)
    keys = ("DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE")
    for proc in ("gnome-shell", "Xorg", "xfce4-session", "plasmashell"):
        rc, out = run(["pgrep", "-x", proc])
        for pid in out.split():
            try:
                with open(f"/proc/{pid}/environ", "rb") as fh:
                    for kv in fh.read().split(b"\0"):
                        k, _, v = kv.partition(b"=")
                        if k.decode(errors="ignore") in keys and v:
                            env.setdefault(k.decode(), v.decode(errors="ignore"))
            except Exception:
                pass
        if "DISPLAY" in env:
            break
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{UID}/bus")
    if "DISPLAY" not in env and "WAYLAND_DISPLAY" not in env:
        env.setdefault("DISPLAY", ":1" if os.path.exists("/tmp/.X11-unix/X1") else ":0")
    if "XAUTHORITY" not in env:
        for cand in (f"/run/user/{UID}/gdm/Xauthority", os.path.expanduser("~/.Xauthority")):
            if os.path.exists(cand):
                env["XAUTHORITY"] = cand
                break
    return env


def session_type(env):
    if env.get("WAYLAND_DISPLAY"):
        return "wayland"
    if env.get("XDG_SESSION_TYPE") in ("x11", "wayland"):
        return env["XDG_SESSION_TYPE"]
    rc, out = run(["pgrep", "-x", "Xorg"])
    return "x11" if out.strip() else "unknown"


# ---------------------------------------------------------------- sensors
def screen_locked(env):
    rc, out = run(["gdbus", "call", "--session", "--dest", "org.gnome.ScreenSaver",
                   "--object-path", "/org/gnome/ScreenSaver",
                   "--method", "org.gnome.ScreenSaver.GetActive"], env=env)
    return "true" in out if rc == 0 else None


def idle_seconds(env, stype):
    rc, out = run(["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.IdleMonitor",
                   "--object-path", "/org/gnome/Mutter/IdleMonitor/Core",
                   "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime"], env=env)
    m = re.search(r"uint64 (\d+)", out)
    if rc == 0 and m:
        return int(m.group(1)) / 1000.0, "mutter-idlemonitor"
    if stype == "x11" and shutil.which("xprintidle"):
        rc, out = run(["xprintidle"], env=env)
        if rc == 0 and out.strip().isdigit():
            return int(out.strip()) / 1000.0, "xprintidle"
    return None, None


def active_window_x11(env):
    rc, out = run(["xprop", "-root", "_NET_ACTIVE_WINDOW"], env=env)
    m = re.search(r"window id # (0x[0-9a-fA-F]+)", out)
    if rc != 0 or not m or int(m.group(1), 16) == 0:
        return None, None, "xprop"
    wid = m.group(1)
    rc, out = run(["xprop", "-id", wid, "WM_CLASS", "_NET_WM_NAME", "WM_NAME"], env=env)
    app, title = None, None
    for line in out.splitlines():
        if line.startswith("WM_CLASS"):
            parts = re.findall(r'"((?:[^"\\]|\\.)*)"', line)
            app = parts[-1] if parts else None
        elif line.startswith("_NET_WM_NAME") and title is None:
            mm = re.search(r'= "(.*)"$', line)
            title = mm.group(1) if mm else None
        elif line.startswith("WM_NAME") and title is None:
            mm = re.search(r'= "(.*)"$', line)
            title = mm.group(1) if mm else None
    if title:
        title = title.encode().decode("unicode_escape", errors="ignore") if "\\" in title else title
    return app, title, "xprop"


GUI_APPS = ("firefox", "chrome", "chromium", "brave", "code", "gnome-terminal", "nautilus", "gedit",
            "libreoffice", "evince", "thunderbird", "slack", "discord", "zoom", "vlc", "spotify",
            "gnome-text-editor", "ptyxis", "kitty", "alacritty", "remmina", "anydesk")


def active_window_wayland(env):
    # Try the (usually restricted) Shell introspection API first.
    rc, out = run(["gdbus", "call", "--session", "--dest", "org.gnome.Shell",
                   "--object-path", "/org/gnome/Shell/Introspect",
                   "--method", "org.gnome.Shell.Introspect.GetWindows"], env=env)
    if rc == 0 and "has-focus" in out:
        for blob in re.split(r"\}\s*,\s*\d+:\s*\{", out):
            if "'has-focus': <true>" in blob:
                app = re.search(r"'(?:wm-class|app-id)': <'([^']*)'>", blob)
                title = re.search(r"'title': <'([^']*)'>", blob)
                return (app.group(1) if app else None), (title.group(1) if title else None), "shell-introspect"
        return None, None, "shell-introspect"
    # Fallback: titles unavailable — report the set of running GUI apps.
    rc, out = run(["ps", "-u", str(UID), "-o", "comm="])
    seen = sorted({c.strip() for c in out.splitlines() if c.strip() in GUI_APPS})
    return ("apps: " + ", ".join(seen)) if seen else None, None, "ps-fallback(no titles)"


def active_window(env, stype):
    if stype == "wayland":
        return active_window_wayland(env)
    return active_window_x11(env)


# ---------------------------------------------------------------- journal
def journal_events():
    """Read new journal lines since the saved cursor; return list of (event, summary, detail)."""
    cmd = ["journalctl", "-o", "json", "--no-pager", "-q", "--show-cursor", "-g", JOURNAL_GREP]
    cursor = None
    try:
        cursor = open(CURSOR).read().strip() or None
    except Exception:
        pass
    cmd += ["--after-cursor", cursor] if cursor else ["--since", "-5min"]
    rc, out = run(cmd, timeout=20)
    events, last_cursor = [], None
    for line in out.splitlines():
        if line.startswith("-- cursor:"):
            last_cursor = line.split(":", 1)[1].strip()
            continue
        try:
            j = json.loads(line)
        except Exception:
            continue
        msg = j.get("MESSAGE") or ""
        if not isinstance(msg, str):
            continue
        ident = j.get("SYSLOG_IDENTIFIER") or j.get("_COMM") or ""
        for ev, pat in SYS_PATTERNS:
            m = pat.search(msg)
            if not m:
                continue
            g = m.groupdict()
            if ev == "failed-unit":
                s = f"unit {g['unit']} failed ({g['res']})"
            elif ev == "oom":
                s = f"OOM kill: {g.get('name') or g.get('unit')}"
            elif ev == "usb":
                s = (f"USB added {g['vid']}:{g['pid']}" if "vid" in g else f"USB removed (dev {g['num']})")
            elif ev == "network":
                s = f"network {g['dev']} {g['st']}"
            elif ev == "login":
                s = (f"ssh login {g['user']} from {g['ip']} ({g['how']})" if "ip" in g
                     else f"xrdp login {g['user']}")
            elif ev == "crash":
                s = f"apport: crash pid {g['pid']} signal {g['sig']}"
            else:
                s = msg[:120]
            if ev == "crash" and "apport" not in ident:
                continue
            events.append((ev, s, msg.strip()[:400]))
            break
    if last_cursor:
        try:
            with open(CURSOR, "w") as fh:
                fh.write(last_cursor)
        except Exception:
            pass
    return events


def emit_inbox(summary, detail):
    os.makedirs(INBOX, exist_ok=True)
    ts = now()
    path = os.path.join(INBOX, f"{int(ts.timestamp())}-system.json")
    n = 0
    while os.path.exists(path):
        n += 1
        path = os.path.join(INBOX, f"{int(ts.timestamp())}-{n}-system.json")
    with open(path, "w") as fh:
        json.dump({"ts": iso(ts), "kind": "system", "summary": summary, "detail": detail}, fh)


# ---------------------------------------------------------------- log
def read_log():
    out = []
    try:
        with open(LOG) as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return out


def append(entries):
    """Append entries and prune anything older than RETENTION (rewrites the file)."""
    if not entries:
        return
    cutoff = now() - RETENTION
    kept = [e for e in read_log() if (parse_ts(e.get("ts", "")) or cutoff) >= cutoff]
    kept.extend(entries)
    tmp = LOG + ".tmp"
    with open(tmp, "w") as fh:
        for e in kept:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    os.replace(tmp, LOG)


def enabled():
    return not os.path.exists(DISABLED)


# ---------------------------------------------------------------- summary
def fmt_dur(secs):
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


def build_summary(stype="?", method="?"):
    t = now()
    log = read_log()
    L = [f"## Activity (percept, {t.strftime('%Y-%m-%d %H:%M %Z')}; session {stype}, via {method})"]
    if not enabled():
        L.append("percept is OFF (kill switch) — nothing recorded. `percept on` to resume.")
    # current state
    idle_entries = [e for e in log if e.get("kind") == "idle"]
    if idle_entries:
        last = idle_entries[-1]
        since = parse_ts(last["ts"])
        ago = fmt_dur((t - since).total_seconds()) if since else "?"
        L.append(f"State: {last.get('state', '?')} since {since.strftime('%H:%M') if since else '?'} ({ago})")
    else:
        L.append("State: unknown (no idle data yet)")
    # last 60 min by app, with dwell = time until next window/idle entry
    cut = t - timedelta(minutes=60)
    wins = [e for e in log if e.get("kind") in ("window", "idle")]
    wins = [e for e in wins if parse_ts(e["ts"])]
    per_app, titles = {}, {}
    for i, e in enumerate(wins):
        if e.get("kind") != "window":
            continue
        start = parse_ts(e["ts"])
        end = parse_ts(wins[i + 1]["ts"]) if i + 1 < len(wins) else t
        start = max(start, cut)
        if end <= cut:
            continue
        app = e.get("app") or "?"
        per_app[app] = per_app.get(app, 0) + (end - start).total_seconds()
        if e.get("title"):
            titles.setdefault(app, []).append(e["title"])
    L.append("Last 60 min by app:")
    if per_app:
        for app, secs in sorted(per_app.items(), key=lambda kv: -kv[1])[:8]:
            rec = titles.get(app, [])[-3:]
            rec = "; ".join(x[:60] for x in reversed(rec))
            L.append(f"- {app}: {fmt_dur(secs)}" + (f" — {rec}" if rec else ""))
    else:
        L.append("- (no window activity)")
    # system events 24h
    sys_ev = [e for e in log if e.get("kind") == "system"][-10:]
    L.append("System events (24h, newest last):" if sys_ev else "System events (24h): none")
    for e in sys_ev:
        ts = parse_ts(e["ts"])
        L.append(f"- {ts.strftime('%H:%M') if ts else '?'} {e.get('summary', '')[:90]}")
    L.append("Policy: consented by owner (intent log). Titles/app names only, redaction on, "
             "24h retention, kill switch `percept off`.")
    text = "\n".join(L)
    while len(text) > 1500 and len(L) > 4:
        # drop oldest system events first, then app rows
        idx = next((i for i in range(len(L) - 2, 1, -1) if L[i].startswith("- ")), None)
        if idx is None:
            break
        del L[idx]
        text = "\n".join(L)
    return text[:1500]


def write_summary(stype="?", method="?"):
    text = build_summary(stype, method)
    tmp = SUMMARY + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text + "\n")
    os.replace(tmp, SUMMARY)
    return text


# ---------------------------------------------------------------- daemon
class Daemon:
    def __init__(self):
        self.env = session_env()
        self.stype = session_type(self.env)
        self.method = "shell-introspect" if self.stype == "wayland" else "xprop"
        self.last_win = None       # (app, title)
        self.last_state = None     # "active" | "idle" | "locked"
        self.last_sys = {}         # summary -> ts (dedupe)
        self.last_summary = 0
        self.was_enabled = None
        # Seed from the existing log so a restart does not re-record unchanged state.
        for e in read_log():
            if e.get("kind") == "idle":
                self.last_state = e.get("state")
            elif e.get("kind") == "window":
                self.last_win = (e.get("app"), e.get("title"))

    def poll(self):
        new = []
        t = iso(now())
        if not enabled():
            if self.was_enabled is not False:
                self.was_enabled = False
                write_summary(self.stype, self.method)
            self.last_win = self.last_state = None
            journal_events()  # advance the cursor and discard: nothing from the OFF window is kept
            return new
        if self.was_enabled is False:
            self.last_summary = 0  # rewrite the summary right away on OFF -> ON
        self.was_enabled = True
        # refresh session env occasionally (login after boot)
        if self.method == "?" or int(time.time()) % 300 < POLL:
            self.env = session_env()
            self.stype = session_type(self.env)

        locked = screen_locked(self.env)
        idle, imethod = idle_seconds(self.env, self.stype)
        if locked:
            state = "locked"
        elif idle is None:
            state = self.last_state or "unknown"
        else:
            state = "idle" if idle >= IDLE_SECS else "active"
        if state != self.last_state and state != "unknown":
            new.append({"ts": t, "kind": "idle", "state": state, "idle_s": int(idle or 0),
                        "summary": f"{state}" + (f" via {imethod}" if imethod else "")})
            self.last_state = state

        if state != "locked":
            app, title, self.method = active_window(self.env, self.stype)
            title = redact(title)
            if (app or title) and (app, title) != self.last_win:
                new.append({"ts": t, "kind": "window", "app": app, "title": title,
                            "summary": f"{app}: {title}" if title else f"{app}"})
                self.last_win = (app, title)
        else:
            self.last_win = None

        for ev, summary, detail in journal_events():
            key = summary
            prev = self.last_sys.get(key)
            if prev and time.time() - prev < 60:
                continue
            self.last_sys[key] = time.time()
            new.append({"ts": t, "kind": "system", "event": ev, "summary": summary, "detail": detail})
            if ev in ("failed-unit", "oom"):
                try:
                    emit_inbox(summary, detail)
                except Exception:
                    pass
        append(new)
        if time.time() - self.last_summary >= SUMMARY_EVERY:
            write_summary(self.stype, self.method)
            self.last_summary = time.time()
        return new

    def loop(self):
        print(f"percept: session={self.stype} display={self.env.get('DISPLAY')} "
              f"wayland={self.env.get('WAYLAND_DISPLAY')} bus={self.env.get('DBUS_SESSION_BUS_ADDRESS')}",
              flush=True)
        while True:
            try:
                for e in self.poll():
                    # Never echo window titles to stdout: the journal persists longer than 24h.
                    if e["kind"] != "window":
                        print(f"percept: {e['kind']} {e.get('summary', '')}", flush=True)
            except Exception as ex:
                print(f"percept: poll error: {ex!r}", file=sys.stderr, flush=True)
            time.sleep(POLL)


# ---------------------------------------------------------------- cli
def cmd_status():
    env = session_env()
    stype = session_type(env)
    rc, out = run(["systemctl", "--user", "is-active", "percept.service"])
    svc = out.strip() or "unknown"
    log = read_log()
    hour = now() - timedelta(hours=1)
    recent = [e for e in log if (parse_ts(e.get("ts", "")) or hour) > hour]
    kinds = {}
    for e in recent:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    idle, im = idle_seconds(env, stype)
    locked = screen_locked(env)
    app, title, wm = active_window(env, stype) if not locked else (None, None, "n/a (locked)")
    print(f"percept:   {'ON' if enabled() else 'OFF (kill switch: ' + DISABLED + ')'}")
    print(f"service:   {svc}")
    print(f"session:   {stype} DISPLAY={env.get('DISPLAY')} WAYLAND={env.get('WAYLAND_DISPLAY') or '-'}")
    print(f"idle:      {fmt_dur(idle) if idle is not None else 'n/a'} via {im or 'n/a'}; locked={locked}")
    print(f"window:    {app!r} {redact(title)!r} via {wm}")
    print(f"last hour: {len(recent)} entries {kinds}  (total {len(log)}, 24h retention)")
    print(f"log:       {LOG}\nsummary:   {SUMMARY}")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "run"
    if cmd == "run":
        Daemon().loop()
    elif cmd == "once":
        d = Daemon()
        d.last_summary = 0
        for e in d.poll():
            print(json.dumps(e, ensure_ascii=False))
        print(f"--- {SUMMARY}:")
        print(open(SUMMARY).read())
    elif cmd == "off":
        open(DISABLED, "w").write(iso(now()) + "\n")
        print(f"percept OFF — {DISABLED} created; daemon idles and records nothing.")
    elif cmd == "on":
        try:
            os.remove(DISABLED)
        except FileNotFoundError:
            pass
        print("percept ON")
    elif cmd == "status":
        cmd_status()
    elif cmd == "summary":
        env = session_env()
        print(write_summary(session_type(env), "cli"))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
