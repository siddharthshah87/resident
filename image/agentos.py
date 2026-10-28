#!/usr/bin/env python3
"""resident — make your machine your own assistant. Intent log, state fingerprints, why-resolver, world model.

Thesis: configuration is a cache; the intent log is the truth.
stdlib only. No sudo. Everything lives under $RESIDENT_HOME (default ~/resident).
"""
import getpass
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.expanduser(os.environ.get("RESIDENT_HOME") or os.environ.get("HOLDFAST_HOME") or os.environ.get("AGENTOS_HOME") or "~/resident")
DB = os.path.join(ROOT, "intents.db")
LAST_FP = os.path.join(ROOT, "last_fingerprint.json")
WORLD = os.path.join(ROOT, "machine.md")
WORLD_BUDGET_CHARS = 12_000  # ~3k tokens
DRIFT_INTENT_ID = 0          # reserved: changes observed outside `intent apply`
WILDCARDS = ("0.0.0.0", "[::]", "*", "")
HOME = os.path.expanduser("~")
# files the system rewrites on its own; tracking them only produces noise
ETC_IGNORE = re.compile(
    r"^/etc/(ld\.so\.cache|\.pwd\.lock|machine-info|motd|(passwd|group|shadow|gshadow|subuid|subgid)-"
    r"|cups/(printers|classes|subscriptions)\.conf(\.O)?|alternatives/|apparmor\.d/cache/|\.updated$|.*\.dpkg-(old|new|dist)$)"
)


def is_public(port_key):
    return port_key.rpartition(":")[0] in WILDCARDS


def compact_cmd(s):
    s = re.sub(r"\S*/\.nvm/versions/node/[^/]+/bin/node", "node", s)
    s = s.replace(HOME + "/", "~/")
    s = re.sub(r"\s*>>?\s*\S+\s*2>&1", "", s)
    s = re.sub(r"\s*>\s*/dev/null(\s*2>&1)?", "", s)
    return s.strip()


# ---------------------------------------------------------------- helpers

def sh(cmd, timeout=60):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def author():
    for var in ("RESIDENT_AUTHOR", "HOLDFAST_AUTHOR", "AGENTOS_AUTHOR"):  # earlier names, still honored
        if os.environ.get(var):
            return os.environ[var]
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE"):
        return "claude"
    return getpass.getuser()


def die(msg, code=1):
    print(f"resident: {msg}", file=sys.stderr)
    sys.exit(code)


def db():
    os.makedirs(ROOT, exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS intents(
            id INTEGER PRIMARY KEY, created TEXT, author TEXT, text TEXT,
            status TEXT DEFAULT 'active', verify TEXT);
        CREATE TABLE IF NOT EXISTS runs(
            id INTEGER PRIMARY KEY, intent_id INT, started TEXT, finished TEXT,
            command TEXT, exit_code INT, verify_cmd TEXT, verify_ok INT,
            before_json TEXT, after_json TEXT);
        CREATE TABLE IF NOT EXISTS changes(
            id INTEGER PRIMARY KEY, intent_id INT, run_id INT, kind TEXT, key TEXT,
            action TEXT, detail TEXT, ts TEXT);
        CREATE INDEX IF NOT EXISTS idx_changes_key ON changes(kind, key);
        """
    )
    c.execute(
        "INSERT OR IGNORE INTO intents(id, created, author, text, status) VALUES(?,?,?,?,?)",
        (DRIFT_INTENT_ID, now(), "system", "UNTRACKED DRIFT — changed outside `intent apply`", "drift"),
    )
    c.commit()
    return c


# ------------------------------------------------------------- collectors
# Each returns {key: detail}. Keys are what `why` resolves against.

def collect_services():
    out = {}
    for line in sh("systemctl list-units --type=service --state=running --no-legend --plain").splitlines():
        p = line.split()
        if p:
            out[p[0]] = "running"
    for line in sh("systemctl list-unit-files --type=service --state=enabled --no-legend --plain").splitlines():
        p = line.split()
        if p:
            out.setdefault(p[0], "enabled")
    return out


def collect_user_services():
    out = {}
    for line in sh("systemctl --user list-units --type=service --state=running --no-legend --plain").splitlines():
        p = line.split()
        if p:
            out[p[0]] = "running"
    for line in sh("systemctl --user list-unit-files --type=service --state=enabled --no-legend --plain").splitlines():
        p = line.split()
        if p:
            out.setdefault(p[0], "enabled")
    return out


def collect_ports():
    out = {}
    for line in sh("ss -tlnH").splitlines():
        p = line.split()
        if len(p) < 4:
            continue
        addr, _, port = p[3].rpartition(":")
        if addr.startswith("[fe80") or addr.startswith("fe80"):
            continue  # link-local noise (rygel/mDNS)
        try:
            pn = int(port)
        except ValueError:
            continue
        if pn >= 32768:
            continue  # ephemeral
        out[f"{addr}:{pn}"] = "listening"
    return out


def collect_packages():
    out = {}
    for line in sh("dpkg-query -W -f='${binary:Package}\\t${Version}\\n'").splitlines():
        if "\t" in line:
            k, v = line.split("\t", 1)
            out[k] = v
    return out


def collect_containers():
    out = {}
    for line in sh("docker ps -a --format '{{json .}}'").splitlines():
        try:
            o = json.loads(line)
        except ValueError:
            continue
        out[o.get("Names", "?")] = f"{o.get('Image', '')} | {o.get('State', '')} | {o.get('Ports', '')}"
    return out


def collect_cron():
    out = {}
    for line in sh("crontab -l 2>/dev/null").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out["crontab:" + hashlib.sha1(s.encode()).hexdigest()[:10]] = s
    if os.path.isdir("/etc/cron.d"):
        for f in sorted(os.listdir("/etc/cron.d")):
            out[f"cron.d/{f}"] = "system"
    return out


def collect_timers():
    out = {}
    for line in sh("systemctl list-timers --all --no-legend --plain").splitlines():
        p = line.split()
        if len(p) >= 2:
            out[p[-2]] = p[-1]  # UNIT -> ACTIVATES
    return out


def collect_etc():
    out = {}
    for dp, _dn, fn in os.walk("/etc"):
        for f in fn:
            p = os.path.join(dp, f)
            if ETC_IGNORE.search(p):
                continue
            try:
                if os.path.islink(p) or not os.path.isfile(p):
                    continue
                st = os.stat(p)
                if os.access(p, os.R_OK) and st.st_size < 2_000_000:
                    with open(p, "rb") as fh:
                        h = hashlib.sha256(fh.read()).hexdigest()[:16]
                else:
                    h = f"unreadable:{int(st.st_mtime)}:{st.st_size}"
                out[p] = h
            except OSError:
                continue
    return out


COLLECTORS = {
    "service": collect_services,
    "user-service": collect_user_services,
    "port": collect_ports,
    "package": collect_packages,
    "container": collect_containers,
    "cron": collect_cron,
    "timer": collect_timers,
    "etc": collect_etc,
}


def fingerprint():
    return {k: f() for k, f in COLLECTORS.items()}


def diff(before, after):
    out = []
    for kind in COLLECTORS:
        b, a = before.get(kind, {}), after.get(kind, {})
        for k in sorted(a.keys() - b.keys()):
            out.append((kind, k, "added", a[k]))
        for k in sorted(b.keys() - a.keys()):
            out.append((kind, k, "removed", b[k]))
        for k in sorted(a.keys() & b.keys()):
            if a[k] != b[k]:
                out.append((kind, k, "modified", a[k]))
    return out


def install_birth():
    """Approximate install time: birth of / else oldest mtime in /etc."""
    s = sh("stat -c %W / 2>/dev/null").strip()
    try:
        t = int(s)
        if t > 0:
            return t
    except ValueError:
        pass
    ts = [os.stat(os.path.join("/etc", f)).st_mtime for f in os.listdir("/etc") if os.path.isfile(os.path.join("/etc", f))]
    return min(ts) if ts else 0


# ----------------------------------------------------------------- intents

def intent_add(text, verify=None):
    c = db()
    cur = c.execute(
        "INSERT INTO intents(created, author, text, verify) VALUES(?,?,?,?)", (now(), author(), text, verify)
    )
    c.commit()
    return cur.lastrowid


def intent_get(c, iid):
    it = c.execute("SELECT * FROM intents WHERE id=?", (iid,)).fetchone()
    if not it:
        die(f"no intent #{iid}")
    return it


def record_changes(c, iid, rid, changes, ts):
    c.executemany(
        "INSERT INTO changes(intent_id, run_id, kind, key, action, detail, ts) VALUES(?,?,?,?,?,?,?)",
        [(iid, rid, k, key, act, det, ts) for k, key, act, det in changes],
    )


def intent_apply(iid, command, verify=None):
    c = db()
    it = intent_get(c, iid)
    before = fingerprint()
    started = now()
    rc = subprocess.call(command, shell=True)
    after = fingerprint()
    finished = now()
    vcmd = verify or it["verify"]
    vok = None
    if vcmd:
        vok = 1 if subprocess.call(vcmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0 else 0
    cur = c.execute(
        "INSERT INTO runs(intent_id, started, finished, command, exit_code, verify_cmd, verify_ok, before_json, after_json)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (iid, started, finished, command, rc, vcmd, vok, json.dumps(before), json.dumps(after)),
    )
    rid = cur.lastrowid
    changes = diff(before, after)
    record_changes(c, iid, rid, changes, finished)
    c.commit()
    save_last_fp(after)
    return rc, vcmd, vok, changes


def intent_adopt(iid, patterns, kinds=None):
    """Retroactively attribute existing state matching any regex to an intent."""
    c = db()
    intent_get(c, iid)
    fp = fingerprint()
    rx = [re.compile(p) for p in patterns]
    ts = now()
    adopted = []
    for kind, items in fp.items():
        if kinds and kind not in kinds:
            continue
        for key, det in items.items():
            if any(r.search(key) or r.search(det or "") for r in rx):
                # re-attributable if it has no owner yet or is only known as DRIFT (intent 0)
                if not c.execute("SELECT 1 FROM changes WHERE kind=? AND key=? AND intent_id<>?", (kind, key, DRIFT_INTENT_ID)).fetchone():
                    adopted.append((kind, key, "adopted", det))
    record_changes(c, iid, None, adopted, ts)
    c.commit()
    return adopted


def intent_verify(iid):
    c = db()
    it = intent_get(c, iid)
    if not it["verify"]:
        return None
    return subprocess.call(it["verify"], shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0


def intent_list(status=None):
    c = db()
    q = "SELECT i.*, (SELECT COUNT(*) FROM changes ch WHERE ch.intent_id=i.id) AS n FROM intents i WHERE id>0"
    args = ()
    if status:
        q += " AND status=?"
        args = (status,)
    return c.execute(q + " ORDER BY id", args).fetchall()


def intent_show(iid):
    c = db()
    it = intent_get(c, iid)
    runs = c.execute("SELECT * FROM runs WHERE intent_id=? ORDER BY id", (iid,)).fetchall()
    ch = c.execute("SELECT * FROM changes WHERE intent_id=? ORDER BY kind, key", (iid,)).fetchall()
    return it, runs, ch


def intent_close(iid, status="done"):
    c = db()
    intent_get(c, iid)
    c.execute("UPDATE intents SET status=? WHERE id=?", (status, iid))
    c.commit()


def intent_edit(iid, text=None, verify=None):
    c = db()
    intent_get(c, iid)
    if text is not None:
        c.execute("UPDATE intents SET text=? WHERE id=?", (text, iid))
    if verify is not None:
        c.execute("UPDATE intents SET verify=? WHERE id=?", (verify, iid))
    c.commit()


# ---------------------------------------------------------------------- why

def why(thing):
    c = db()
    base = (
        "SELECT ch.kind, ch.key, ch.action, ch.ts, ch.detail, i.id AS iid, i.text, i.author, i.created, i.status "
        "FROM changes ch JOIN intents i ON i.id=ch.intent_id WHERE {} ORDER BY ch.ts DESC"
    )
    stages = [("ch.key=?", (thing,)), ("ch.key LIKE ?", (f"%:{thing}",))]  # exact, then port number
    if not thing.isdigit() and len(thing) >= 3:  # substrings only for names, never bare numbers
        stages += [("ch.key LIKE ?", (f"%{thing}%",)), ("ch.detail LIKE ?", (f"%{thing}%",))]
    for cond, args in stages:
        rows = c.execute(base.format(cond), args).fetchall()
        if rows:
            return rows
    return []


# -------------------------------------------------------------------- score

def score(fp=None):
    """Unexplained state: what exists on this box with no recorded intent."""
    fp = fp or fingerprint()
    c = db()
    explained = {(r["kind"], r["key"]) for r in c.execute("SELECT DISTINCT kind, key FROM changes")}
    birth = install_birth()
    manual = set(sh("apt-mark showmanual").split())

    scope = {
        "service": [k for k, v in fp["service"].items() if v == "running"],
        # only user units the owner installed (~/.config/systemd/user), not the stock GNOME session services
        "user-service": [k for k, v in fp.get("user-service", {}).items()
                         if v == "running" and os.path.exists(os.path.join(HOME, ".config/systemd/user", k))],
        "port": [k for k in fp["port"] if is_public(k)],
        "container": list(fp["container"]),
        "cron": list(fp["cron"]),
        "package": [k for k in fp["package"] if k in manual],
        "etc": [],
    }
    for p in fp["etc"]:
        try:
            if os.stat(p).st_mtime > birth + 86400:
                scope["etc"].append(p)
        except OSError:
            pass

    rows = []
    tot_n = tot_u = 0
    for kind, keys in scope.items():
        n = len(keys)
        unexplained = [k for k in keys if (kind, k) not in explained]
        rows.append((kind, n, len(unexplained), unexplained))
        tot_n += n
        tot_u += len(unexplained)
    return rows, tot_n, tot_u


# ---------------------------------------------------------- drift / world

def save_last_fp(fp):
    with open(LAST_FP, "w") as fh:
        json.dump(fp, fh)


def load_last_fp():
    try:
        with open(LAST_FP) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


INBOX = os.path.join(ROOT, "inbox")


def emit_event(kind, summary, detail=""):
    """Drop an event for the resident agent (agentd) — the event bus is a directory of JSON files."""
    os.makedirs(INBOX, exist_ok=True)
    path = os.path.join(INBOX, f"{int(datetime.now().timestamp())}-{kind}.json")
    with open(path, "w") as fh:
        json.dump({"ts": now(), "kind": kind, "summary": summary, "detail": detail}, fh)
    return path


def detect_drift(fp):
    """Compare to the last saved fingerprint; attribute unattributed changes to DRIFT and notify agentd."""
    last = load_last_fp()
    save_last_fp(fp)
    if last is None:
        return []
    changes = diff(last, fp)
    if changes:
        c = db()
        record_changes(c, DRIFT_INTENT_ID, None, changes, now())
        c.commit()
        lines = [f"{k} {act}: {key}" for k, key, act, _d in changes[:30]]
        emit_event("drift", f"{len(changes)} change(s) outside intent apply", "\n".join(lines))
    return changes


def failed_units():
    out = []
    for line in sh("systemctl --failed --no-legend --plain").splitlines():
        p = line.split()
        if p:
            out.append(p[0])
    for line in sh("systemctl --user --failed --no-legend --plain").splitlines():
        p = line.split()
        if p:
            out.append("user/" + p[0])
    return out


def _clip(items, n):
    items = list(items)
    return items[:n] + ([f"… +{len(items) - n} more"] if len(items) > n else [])


def world_model(fp=None, drift=None):
    fp = fp or fingerprint()
    rows, tot_n, tot_u = score(fp)
    c = db()
    intents = c.execute("SELECT * FROM intents WHERE id>0 ORDER BY id DESC LIMIT 20").fetchall()
    host = sh("hostnamectl --static").strip() or "unknown"
    os_name = ""
    for line in sh("cat /etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            os_name = line.split("=", 1)[1].strip('"')
    uptime = sh("uptime -p").strip()
    load = sh("cut -d' ' -f1-3 /proc/loadavg").strip()
    mem = sh("free -h | awk '/^Mem/{print $3\"/\"$2\" used\"}'").strip()
    disk = sh("df -h / | awk 'NR==2{print $3\"/\"$2\" used (\"$5\")\"}'").strip()
    ip_ts = sh("tailscale ip -4 2>/dev/null").strip()
    cores = sh("nproc").strip()
    last_dpkg = sh("tail -1 /var/log/dpkg.log 2>/dev/null | cut -c1-16").strip()

    L = []
    L.append(f"# {host} — machine.md (snapshot {now()}; regenerated every 10 min, so live numbers may be stale)")
    L.append("")
    L.append(f"{os_name} · {cores} cores · {uptime} · load {load} · mem {mem} · disk {disk}"
             + (f" · tailscale {ip_ts}" if ip_ts else "") + (f" · last package change {last_dpkg}" if last_dpkg else ""))
    L.append("")
    L.append(f"**Unexplained state: {tot_u}/{tot_n}** ({(100 * tot_u / tot_n) if tot_n else 0:.0f}% of tracked items have no recorded intent)")
    for kind, n, u, _ in rows:
        L.append(f"- {kind}: {u}/{n} unexplained")
    L.append("")

    L.append("## Containers")
    status = dict(l.split("\t", 1) for l in sh("docker ps -a --format '{{.Names}}\t{{.Status}}'").splitlines() if "\t" in l)
    for k, v in sorted(fp["container"].items()):
        L.append(f"- {k}: {v}" + (f" ({status[k]})" if k in status else ""))
    L.append("")

    L.append("## Public listening ports (wildcard-bound)")
    pub = sorted({int(k.rpartition(":")[2]) for k in fp["port"] if is_public(k)})
    L.append(", ".join(str(p) for p in pub) or "(none)")
    other = sorted(k for k in fp["port"] if not is_public(k))
    if other:
        L.append("Other bound: " + ", ".join(_clip(other, 12)))
    L.append("")

    L.append("## Running services")
    svc = sorted(k[:-8] if k.endswith(".service") else k for k, v in fp["service"].items() if v == "running")
    L.append(", ".join(svc))
    usvc = sorted(k[:-8] if k.endswith(".service") else k for k, v in fp.get("user-service", {}).items() if v == "running")
    if usvc:
        L.append("User services: " + ", ".join(usvc))
    L.append("")

    L.append("## User cron (verbatim lines from `crontab -l`)")
    for k, v in sorted(fp["cron"].items()):
        if k.startswith("crontab:"):
            L.append(f"- `{v}`")
    L.append("")
    L.append("## User systemd timers (`systemctl --user`)")
    for line in sh("systemctl --user list-timers --all --no-legend --plain").splitlines():
        p = line.split()
        if len(p) >= 2:
            L.append(f"- {p[-2]} → {p[-1]}")
    L.append("")

    L.append("## Recent intents")
    if not intents:
        L.append("(none yet — run `intent add \"…\"` before changing anything)")
    for it in intents:
        L.append(f"- #{it['id']} [{it['status']}] {it['created'][:10]} {it['author']}: {it['text']}")
    L.append("")

    tree_py = os.path.join(os.path.dirname(os.path.realpath(__file__)), "tree.py")
    if os.path.exists(tree_py) and os.path.exists(os.path.join(ROOT, "individuals", "registry.json")):
        t = sh(f"{sys.executable} {tree_py} tree --compact", timeout=90).strip()
        if t:
            L.append("## Individuals (head and children; `resident tree` for detail)")
            L.append(t)
            L.append("")
    failed = failed_units()
    if failed:
        L.append("## FAILED units")
        L.append(", ".join(failed))
        L.append("")
    act = os.path.join(ROOT, "percept", "activity.md")
    if os.path.exists(act) and not os.path.exists(os.path.join(ROOT, "percept", "DISABLED")):
        try:
            L.append(open(act).read().strip()[:1600])
            L.append("")
        except OSError:
            pass
    if os.path.exists(os.path.join(ROOT, "browser", "visits.jsonl")) or sh("systemctl --user is-active agent-browser.service").strip() == "active":
        L.append("## Agent browser")
        L.append("Headless Chromium (persistent profile) is available: `resident-browse <url>` prints page text, `--screenshot`, `--links`. Visits are logged.")
        L.append("")
    if drift:
        L.append("## Drift since last check (changed outside `intent apply`)")
        for k, key, act, det in _clip(drift, 25):
            L.append(f"- {k} {act}: {key}")
        L.append("")

    L.append("## Protocol for agents")
    L.append("1. Start from this file; it answers most inventory questions without probing. It is a snapshot: for anything 'right now' (load, memory, process state, recent logs) probe live and say which you used.")
    L.append("2. Read-only question → answer it. Run `why <thing>` only when the question is about provenance or you are about to change something; don't spend turns on `why`/`intent` for a lookup.")
    L.append("3. Before any change: `intent add \"<goal>\" [--verify \"<check>\"]` → `intent apply <id> -- <command>`. Quote system state verbatim (cron lines above are verbatim).")
    L.append("4. Report the verify result, not the exit code.")
    L.append("5. Remote-access methods on this box include anything in: ssh, xrdp, gnome-remote-desktop, anydesk, tailscale — check the running-services list, not just ports.")
    L.append("6. Events for the resident agent arrive as JSON files in ~/resident/inbox/ (kinds: ask, drift, failed-unit, system); answers go to ~/resident/outbox/.")

    text = "\n".join(L)
    if len(text) > WORLD_BUDGET_CHARS:
        text = text[: WORLD_BUDGET_CHARS - 40] + "\n…(truncated to budget)"
    return text


FAILED_SEEN = os.path.join(ROOT, "failed_seen.json")


def write_world(check_drift=True):
    fp = fingerprint()
    drift = detect_drift(fp) if check_drift else []
    try:
        seen = set(json.load(open(FAILED_SEEN)))
    except (OSError, ValueError):
        seen = set()
    cur = set(failed_units())
    for u in sorted(cur - seen):
        emit_event("failed-unit", f"{u} failed", sh(f"systemctl {'--user ' if u.startswith('user/') else ''}status {u.removeprefix('user/')} --no-pager -n 15"))
    with open(FAILED_SEEN, "w") as fh:
        json.dump(sorted(cur), fh)
    text = world_model(fp, drift)
    os.makedirs(ROOT, exist_ok=True)
    with open(WORLD, "w") as fh:
        fh.write(text + "\n")
    return text, drift


# ---------------------------------------------------------------------- CLI

USAGE = """resident — make your machine your own assistant.
(config is a cache; intent is the truth)

  intent add "<goal>" [--verify "<cmd>"]        record an intent, prints its id
  intent apply <id> [--verify "<cmd>"] -- <cmd>  snapshot → run → snapshot → attribute changes
  intent adopt <id> [--kind k] <regex>...       attribute existing state to an intent
  intent list [--all] | show <id> | close <id> | verify <id>
  intent edit <id> ["<text>"] [--verify "<cmd>"]  reword an intent / set its check
  why <thing>                                   which intent explains a port/service/file/package/container
  resident score                                 unexplained-state report
  resident world [--no-drift]                    regenerate machine.md (and detect drift)
  resident snapshot                              save current fingerprint as the drift baseline
  resident emit <kind> "<summary>" ["<detail>"]  drop an event for the resident agent (inbox/)
  resident spawn|tree|broker|approvals|approve|deny|retire   the family of individuals (lib/tree.py)
  resident request VERB RESOURCE [...]           inside a body: ask the parent (see lib/tree.py)
"""


def main(argv):
    prog = os.path.basename(argv[0])
    args = argv[1:]
    if prog in ("intent", "why"):
        args = [prog] + args
    if not args or args[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    cmd = args[0]

    if cmd == "intent":
        if len(args) < 2:
            print(USAGE)
            return 2
        sub = args[1]
        rest = args[2:]
        if sub == "add":
            verify = None
            if "--verify" in rest:
                i = rest.index("--verify")
                verify = rest[i + 1]
                rest = rest[:i] + rest[i + 2:]
            if not rest:
                die("intent add needs text")
            iid = intent_add(" ".join(rest), verify)
            print(iid)
            return 0
        if sub == "apply":
            if "--" not in rest:
                die("intent apply <id> [--verify cmd] -- <command>")
            i = rest.index("--")
            tail = rest[i + 1:]
            head = rest[:i]
            command = tail[0] if len(tail) == 1 else shlex.join(tail)  # keep quoting so `sh -c "a > b"` survives
            verify = None
            if "--verify" in head:
                j = head.index("--verify")
                verify = head[j + 1]
                head = head[:j] + head[j + 2:]
            iid = int(head[0])
            rc, vcmd, vok, changes = intent_apply(iid, command, verify)
            print(f"\n[intent #{iid}] exit={rc} changes={len(changes)}" + (f" verify={'PASS' if vok else 'FAIL'} ({vcmd})" if vcmd else " verify=none"))
            for k, key, act, det in changes[:40]:
                print(f"  {k:9} {act:8} {key}")
            if len(changes) > 40:
                print(f"  … +{len(changes) - 40} more")
            return 0 if (rc == 0 and vok in (None, 1)) else 1
        if sub == "adopt":
            kinds = None
            if "--kind" in rest:
                i = rest.index("--kind")
                kinds = {rest[i + 1]}
                rest = rest[:i] + rest[i + 2:]
            iid = int(rest[0])
            adopted = intent_adopt(iid, rest[1:], kinds)
            print(f"adopted {len(adopted)} items into intent #{iid}")
            for k, key, _a, _d in adopted[:40]:
                print(f"  {k:9} {key}")
            if len(adopted) > 40:
                print(f"  … +{len(adopted) - 40} more")
            return 0
        if sub == "list":
            for it in intent_list(None if "--all" in rest else "active"):
                v = " ✓verify" if it["verify"] else ""
                print(f"#{it['id']:<4} [{it['status']}] {it['created'][:10]} {it['author']:<9} {it['text']}  ({it['n']} items{v})")
            return 0
        if sub == "show":
            it, runs, ch = intent_show(int(rest[0]))
            print(f"#{it['id']} [{it['status']}] {it['created']} by {it['author']}\n  {it['text']}")
            if it["verify"]:
                print(f"  verify: {it['verify']}")
            for r in runs:
                v = "" if r["verify_ok"] is None else (" verify=PASS" if r["verify_ok"] else " verify=FAIL")
                print(f"  run {r['id']} {r['started']} exit={r['exit_code']}{v}: {r['command']}")
            for x in ch:
                print(f"  {x['kind']:9} {x['action']:8} {x['key']}")
            return 0
        if sub == "close":
            intent_close(int(rest[0]), rest[1] if len(rest) > 1 else "done")
            return 0
        if sub == "verify":
            ok = intent_verify(int(rest[0]))
            print("no verify defined" if ok is None else ("PASS" if ok else "FAIL"))
            return 0 if ok else 1
        if sub == "edit":
            verify = None
            if "--verify" in rest:
                i = rest.index("--verify")
                verify = rest[i + 1]
                rest = rest[:i] + rest[i + 2:]
            iid = int(rest[0])
            intent_edit(iid, " ".join(rest[1:]) or None, verify)
            return 0
        print(USAGE)
        return 2

    if cmd == "why":
        if len(args) < 2:
            die("why <thing>")
        thing = args[1]
        rows = why(thing)
        if not rows:
            print(f"UNEXPLAINED: nothing in the intent log mentions '{thing}'. It predates the log or changed outside `intent apply`.")
            return 1
        seen = set()
        for r in rows:
            if r["iid"] in seen:
                continue
            seen.add(r["iid"])
            tag = "DRIFT" if r["iid"] == DRIFT_INTENT_ID else f"intent #{r['iid']} [{r['status']}]"
            print(f"{r['kind']} {r['key']} — {r['action']} {r['ts'][:16]} by {r['author']} → {tag}: {r['text']}")
        return 0

    if cmd == "score":
        rows, tn, tu = score()
        print(f"UNEXPLAINED STATE: {tu}/{tn} ({(100 * tu / tn) if tn else 0:.0f}%)")
        for kind, n, u, unexplained in rows:
            print(f"  {kind:9} {u:>4}/{n:<4} " + ", ".join(_clip([k.split('/')[-1] if kind == 'etc' else k for k in unexplained], 8)))
        return 0

    if cmd == "world":
        text, drift = write_world(check_drift="--no-drift" not in args)
        print(text)
        if drift:
            print(f"\n[drift: {len(drift)} unattributed change(s) recorded]", file=sys.stderr)
        return 0

    if cmd == "snapshot":
        save_last_fp(fingerprint())
        print(f"saved {LAST_FP}")
        return 0

    if cmd in ("tree", "broker", "approvals", "approve", "deny", "retire"):
        os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.realpath(__file__)), "tree.py")] + args)

    if cmd in ("request", "response"):
        up = os.path.join(ROOT, "uplink")
        if not os.path.isdir(os.path.join(up, "requests")):
            die("this body has no uplink (it has no parent)")
        if cmd == "response":
            p = os.path.join(up, "responses", f"{args[1]}.json")
            print(open(p).read() if os.path.exists(p) else "(no answer yet)")
            return 0
        rest = args[2:]
        opts = {}
        for flag in ("--policy-file", "--purpose", "--reason", "--wait"):
            if flag in rest:
                i = rest.index(flag)
                opts[flag] = rest[i + 1]
                del rest[i:i + 2]
        verb, resource = args[1], (rest[0] if rest else "")
        rid = f"{int(time.time())}-{os.urandom(3).hex()}"
        req = {"id": rid, "ts": now(), "verb": verb, "resource": resource, "reason": opts.get("--reason", "")}
        if verb == "spawn":
            req["payload"] = {"policy": open(opts["--policy-file"]).read() if "--policy-file" in opts else "",
                              "purpose": opts.get("--purpose", "")}
        iid = intent_add(f"request {rid} to parent: {verb} {resource}" + (f" — {req['reason']}" if req["reason"] else ""))
        tmp = os.path.join(up, "requests", f".{rid}.tmp")
        with open(tmp, "w") as fh:
            json.dump(req, fh)
        os.replace(tmp, os.path.join(up, "requests", f"{rid}.json"))
        resp_p, t0 = os.path.join(up, "responses", f"{rid}.json"), time.time()
        while time.time() - t0 < float(opts.get("--wait", "30")) and not os.path.exists(resp_p):
            time.sleep(1)
        if not os.path.exists(resp_p):
            print(f"{rid}: sent, no answer yet (`resident response {rid}`)")
            return 3
        resp = json.load(open(resp_p))
        print(json.dumps(resp, indent=1))
        if resp.get("status") in ("done", "denied", "failed"):
            intent_close(iid, resp["status"])
        return {"done": 0, "pending": 2}.get(resp.get("status"), 1)

    if cmd in ("spawn", "dump", "replay", "kill", "exec"):
        os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.realpath(__file__)), "spawn.py")] + args)

    if cmd == "emit":
        if len(args) < 3:
            die('resident emit <kind> "<summary>" ["<detail>"]')
        print(emit_event(args[1], args[2], args[3] if len(args) > 3 else ""))
        return 0

    print(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
