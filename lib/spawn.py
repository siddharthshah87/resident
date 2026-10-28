#!/usr/bin/env python3
"""resident individuals — the container is the body, the intent log is the self.

  resident spawn  NAME --owner O --purpose P [--image IMG]   birth: body from image + base intents
  resident dump   NAME                                       export the owner's intents (not the image's) as JSON
  resident replay NAME --onto NEW [--image IMG]              rebirth: new body, same self (re-applies intents)
  resident kill   NAME [--purge]                             destroy the body (keep the /var volume unless --purge)
  resident exec   NAME -- cmd...                             run inside the body
"""
import json, os, re, subprocess, sys, time
from datetime import datetime, timezone

IMAGE = "resident-image:dev"
HOME = os.path.expanduser("~/resident/individuals")
RUN = ["--privileged", "--cgroupns=private", "--read-only", "--tmpfs", "/run", "--tmpfs", "/run/lock",
       "--tmpfs", "/tmp", "--tmpfs", "/etc"]

def sh(args, check=True, env=None, inp=None):
    r = subprocess.run(args, capture_output=True, text=True, input=inp)
    if check and r.returncode:
        sys.exit(f"spawn: {' '.join(args[:4])}… failed: {r.stderr.strip()[:400]}")
    return r.stdout

def dexec(name, args, author=None):
    pre = ["docker", "exec"] + (["-e", f"RESIDENT_AUTHOR={author}"] if author else []) + [name]
    return sh(pre + args)

def wait_firstboot(name, need=15, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = subprocess.run(["docker", "exec", name, "sh", "-c", "test -f /var/lib/resident/intents.db && intent list --all | wc -l"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip().isdigit() and int(r.stdout.strip()) >= need:
            st = subprocess.run(["docker", "exec", name, "systemctl", "show", "-p", "ActiveState", "--value", "resident-firstboot"], capture_output=True, text=True).stdout.strip()
            if st in ("inactive", "active"):
                return time.time() - t0
        time.sleep(2)
    sys.exit(f"spawn: {name} did not finish first boot in {timeout}s")

def body(name, image, extra=()):
    sh(["docker", "run", "-d", "--name", name] + RUN + ["-v", f"{name}-var:/var"] + list(extra) + [image, "/sbin/init"])
    secs = wait_firstboot(name)
    print(f"[{name}] body up, first boot seeded in {secs:.0f}s")

def spawn(name, owner, purpose, image=IMAGE, lineage=None):
    body(name, image)
    meta = {"name": name, "owner": owner, "purpose": purpose, "born": datetime.now(timezone.utc).isoformat(timespec="seconds"), "image": image, "lineage": lineage or []}
    dexec(name, ["sh", "-c", f"cat > /var/lib/resident/individual.json"], ) if False else None
    sh(["docker", "exec", "-i", name, "sh", "-c", "cat > /var/lib/resident/individual.json"], inp=json.dumps(meta, indent=1))
    if not lineage:  # base intents are born once; a replayed body inherits them from the dump
        dexec(name, ["intent", "add", f"Individual '{name}': body spawned from {image} for {owner}. Purpose: {purpose}"], author=owner)
        dexec(name, ["intent", "add", f"Base policy for '{name}': acts only through `intent apply`; read-only questions need no intent; owner approval for package removal, reboot, firewall and account changes"], author=owner)
    # host-side attribution
    os.makedirs(HOME, exist_ok=True)
    i = sh(["intent", "add", f"Individual '{name}' (resident container body, volume {name}-var): {purpose}"], env=None).strip()
    subprocess.run(["intent", "adopt", i, "--kind", "container", f"^{re.escape(name)}$"], capture_output=True, text=True)
    print(f"[{name}] born: owner={owner} host-intent=#{i}")
    return meta

DUMP_PY = r'''
import json, sqlite3, os
H = next(d for d in (os.environ.get("RESIDENT_HOME"), os.environ.get("HOLDFAST_HOME"), os.environ.get("AGENTOS_HOME"), "/var/lib/resident", "/var/lib/holdfast", "/var/lib/agentos") if d and os.path.exists(d + "/intents.db"))
c = sqlite3.connect(H + "/intents.db"); c.row_factory = sqlite3.Row
out = []
for it in c.execute("SELECT * FROM intents WHERE id>0 AND author<>'image' ORDER BY id"):
    runs = [dict(r) for r in c.execute("SELECT command, verify_cmd FROM runs WHERE intent_id=? ORDER BY id", (it["id"],))]
    adopted = [dict(r) for r in c.execute("SELECT kind, key FROM changes WHERE intent_id=? AND action='adopted'", (it["id"],))]
    out.append({"id": it["id"], "text": it["text"], "author": it["author"], "status": it["status"], "verify": it["verify"], "created": it["created"], "runs": runs, "adopted": adopted})
meta = {}
try: meta = json.load(open(H + "/individual.json"))
except Exception: pass
print(json.dumps({"individual": meta, "intents": out}))
'''

def dump(name):
    data = json.loads(dexec(name, ["python3", "-c", DUMP_PY]))
    os.makedirs(HOME, exist_ok=True)
    p = os.path.join(HOME, f"{name}.json")
    json.dump(data, open(p, "w"), indent=1)
    print(f"[{name}] dumped {len(data['intents'])} intents → {p}")
    return data

def replay(name, new, image=IMAGE):
    p = os.path.join(HOME, f"{name}.json")
    alive = subprocess.run(["docker", "inspect", name], capture_output=True).returncode == 0
    data = dump(name) if alive else json.load(open(p))
    if not alive: print(f"[{name}] body is gone; replaying from {p}")
    meta = data["individual"]
    spawn(new, meta.get("owner", "unknown"), meta.get("purpose", ""), image, lineage=meta.get("lineage", []) + [name])
    for it in data["intents"]:
        add = ["intent", "add"] + (["--verify", it["verify"]] if it.get("verify") else []) + [it["text"]]
        nid = dexec(new, add, author=it["author"]).strip()
        for r in it["runs"]:
            args = ["intent", "apply", nid] + (["--verify", r["verify_cmd"]] if r.get("verify_cmd") else []) + ["--", r["command"]]
            subprocess.run(["docker", "exec", "-e", f"RESIDENT_AUTHOR={it['author']}", new] + args, capture_output=True, text=True)
        for a in it["adopted"]:
            subprocess.run(["docker", "exec", new, "intent", "adopt", nid, "--kind", a["kind"], f"^{re.escape(a['key'])}$"], capture_output=True, text=True)
        if it["status"] != "active":
            dexec(new, ["intent", "close", nid, it["status"]])
        print(f"[{new}] replayed #{it['id']}→#{nid}: {it['text'][:70]}")
    dexec(new, ["resident", "snapshot"]); dexec(new, ["resident", "world"])
    print(f"[{new}] rebirth complete; lineage {meta.get('lineage', []) + [name]}")

def kill(name, purge=False):
    sh(["docker", "rm", "-f", name])
    if purge: sh(["docker", "volume", "rm", f"{name}-var"])
    print(f"[{name}] body destroyed" + (" and volume purged" if purge else f" (volume {name}-var kept)"))

def main(a):
    if not a or a[0] in ("-h", "--help"): print(__doc__); return 0
    if a[0] == "spawn" and "--parent" in a:  # a child in the tree: policy-checked by tree.py
        os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.realpath(__file__)), "tree.py")] + a)
    cmd, rest = a[0], a[1:]
    def opt(k, default=None):
        return rest[rest.index(k) + 1] if k in rest else default
    if cmd == "spawn": spawn(rest[0], opt("--owner", os.environ.get("USER")), opt("--purpose", ""), opt("--image", IMAGE))
    elif cmd == "dump": dump(rest[0])
    elif cmd == "replay": replay(rest[0], opt("--onto"), opt("--image", IMAGE))
    elif cmd == "kill": kill(rest[0], "--purge" in rest)
    elif cmd == "exec": print(dexec(rest[0], rest[rest.index("--") + 1:] if "--" in rest else rest[1:]), end="")
    else: print(__doc__); return 2
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
