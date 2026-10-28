#!/usr/bin/env python3
"""resident tree — a head agent, children in container bodies, authority that only shrinks going down.

Host (head):
  resident spawn NAME --parent P --policy FILE [--purpose TEXT]  raise a child; its policy must fit inside P's
  resident tree [--compact]                                     the family: status, budgets, score, pending approvals
  resident broker [--once]                                      serve requests coming up from bodies
  resident approvals | approve ID [--as WHO] | deny ID          the human end of escalation
  resident retire NAME                                          destroy a child and its descendants (logs dumped first)
Inside a body (agentos.py):
  resident request VERB RESOURCE [--policy-file F --purpose T] [--reason T] [--wait S]

Rules: verbs come from a fixed table; policies use only '*' wildcards; a child's capabilities, approval list and
budget must fit inside its parent's; a request outside the requester's policy (or on a verb that needs approval)
escalates to a human, naming the nearest ancestor that holds the capability; if no ancestor holds it, it is denied.
"""
import fnmatch, getpass, glob, json, os, re, shlex, subprocess, sys, time, tomllib, uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import spawn as bodies  # noqa: E402

ROOT = os.path.expanduser("~/resident")
IND = os.path.join(ROOT, "individuals")
REG = os.path.join(IND, "registry.json")
APPROVALS = os.path.join(ROOT, "approvals")
BROKER_LOG = os.path.join(ROOT, "broker.jsonl")
INTENT = os.path.expanduser("~/.local/bin/intent")
RESIDENT = os.path.expanduser("~/.local/bin/resident")
def _head_name():
    """The head is the host individual: $RESIDENT_HEAD, else whatever the registry already calls it, else 'head'."""
    if os.environ.get("RESIDENT_HEAD"):
        return os.environ["RESIDENT_HEAD"]
    try:
        return next(n for n, e in json.load(open(REG)).items() if e.get("kind") == "host")
    except (OSError, ValueError, StopIteration):
        return "head"


HEAD = _head_name()
HEAD_POLICY = os.path.join(ROOT, "policies", f"{HEAD}.toml")
OWNER = os.environ.get("RESIDENT_OWNER") or getpass.getuser()  # the human who approves

# the only host actions a child can ever cause
VERBS = {
    "container.inspect": lambda r: ["docker", "inspect", "--format",
        "{{.Name}} {{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}} since={{.State.StartedAt}}", r],
    "container.restart": lambda r: ["docker", "restart", r],
    "unit.restart": lambda r: ["systemctl", "--user", "restart", r],
    "browser.visit": lambda r: [os.path.expanduser("~/.local/bin/resident-browse"), r],
}
MUTATING = {"container.restart", "unit.restart"}
BUDGET_KEYS = ("usd_per_day", "memory_mb", "cpus")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def die(msg):
    sys.exit(f"resident: {msg}")


def log(rec):
    rec["ts"] = now()
    with open(BROKER_LOG, "a") as fh:
        fh.write(json.dumps(rec) + "\n")


# ------------------------------------------------------------------ policy

def parse_policy(text):
    p = tomllib.loads(text)
    caps = p.setdefault("capabilities", {})
    for verb, globs in caps.items():
        if verb == "spawn":
            if not isinstance(globs, bool):
                raise ValueError("capabilities.spawn must be true/false")
            continue
        if verb not in VERBS:
            raise ValueError(f"unknown verb '{verb}' (known: {', '.join(sorted(VERBS))})")
        if not isinstance(globs, list) or not all(isinstance(g, str) for g in globs):
            raise ValueError(f"capabilities.{verb} must be a list of patterns")
        for g in globs:
            if re.search(r"[?\[\]]", g):
                raise ValueError(f"{verb} '{g}': only '*' wildcards are allowed")
    p.setdefault("approval_required", {}).setdefault("verbs", [])
    p.setdefault("budget", {})
    return p


def read_policy(path):
    return parse_policy(open(path).read())


def allowed(pol, verb, resource):
    globs = pol["capabilities"].get(verb, [])
    return isinstance(globs, list) and any(fnmatch.fnmatchcase(resource, g) for g in globs)


def fits_inside(child, parent):
    """Violations if `child` would hold anything its parent does not. With '*'-only patterns,
    fnmatch(child_pattern_as_text, parent_pattern) is a sound containment test: a child '*' can only
    land inside a parent '*'."""
    v = []
    pc, cc = parent["capabilities"], child["capabilities"]
    if cc.get("spawn") and not pc.get("spawn"):
        v.append("spawn: parent may not spawn, so neither may the child")
    for verb, globs in cc.items():
        if verb == "spawn":
            continue
        if verb not in pc:
            v.append(f"{verb}: parent holds no such capability")
            continue
        for g in globs:
            if not any(fnmatch.fnmatchcase(g, pg) for pg in pc[verb]):
                v.append(f"{verb} '{g}': outside parent's {pc[verb]}")
    missing = set(parent["approval_required"]["verbs"]) - set(child["approval_required"]["verbs"])
    if missing:
        v.append(f"approval_required must keep the parent's {sorted(missing)}")
    for k in BUDGET_KEYS:
        if k not in child["budget"]:
            v.append(f"budget.{k} is required")
    return v


def summarize(pol):
    caps = "; ".join(f"{k} {v}" for k, v in pol["capabilities"].items() if k != "spawn")
    b = pol["budget"]
    return (f"spawn={pol['capabilities'].get('spawn', False)}; {caps or 'no capabilities'}; "
            f"approval for {pol['approval_required']['verbs']}; budget ${b.get('usd_per_day', 0):g}/day "
            f"{b.get('memory_mb', 0)}MB {b.get('cpus', 0)}cpu, max {b.get('max_children', 0)} children")


# ---------------------------------------------------------------- registry

def registry():
    try:
        return json.load(open(REG))
    except (OSError, ValueError):
        r = {HEAD: {"name": HEAD, "parent": None, "kind": "host", "policy": HEAD_POLICY, "born": now(),
                    "purpose": read_policy(HEAD_POLICY).get("individual", {}).get("purpose", "")}}
        save_registry(r)
        return r


def save_registry(r):
    os.makedirs(IND, exist_ok=True)
    tmp = REG + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(r, fh, indent=1)
    os.replace(tmp, REG)


def policy_of(name):
    return read_policy(registry()[name]["policy"])


def ancestors(name):
    r, out = registry(), []
    p = r[name]["parent"]
    while p:
        out.append(p)
        p = r[p]["parent"]
    return out


def children(name):
    return sorted(n for n, e in registry().items() if e["parent"] == name)


def budget_left(name):
    pol = policy_of(name)
    left = {k: pol["budget"].get(k, 0) for k in BUDGET_KEYS}
    kids = children(name)
    for kid in kids:
        kb = policy_of(kid)["budget"]
        for k in BUDGET_KEYS:
            left[k] -= kb.get(k, 0)
    left["children"] = pol["budget"].get("max_children", 0) - len(kids)
    return left


def host_intent(text, author):
    r = subprocess.run([INTENT, "add", text], capture_output=True, text=True, env=dict(os.environ, RESIDENT_AUTHOR=author))
    return r.stdout.strip()


# ------------------------------------------------------------------- spawn

def spawn_child(name, parent, policy_text, purpose="", author=OWNER):
    r = registry()
    if parent not in r:
        return {"ok": False, "violations": [f"unknown parent '{parent}'"]}
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,40}", name or ""):
        return {"ok": False, "violations": ["name must be lowercase letters, digits and '-'"]}
    if name in r or subprocess.run(["docker", "inspect", name], capture_output=True).returncode == 0:
        return {"ok": False, "violations": [f"'{name}' already exists"]}
    if parent != HEAD and not name.startswith(parent + "-"):
        return {"ok": False, "violations": [f"a child of '{parent}' must be named '{parent}-…'"]}
    try:
        child = parse_policy(policy_text)
    except (ValueError, tomllib.TOMLDecodeError) as e:
        return {"ok": False, "violations": [f"policy does not parse: {e}"]}
    ppol = policy_of(parent)
    v = [] if ppol["capabilities"].get("spawn") else [f"'{parent}' may not spawn"]
    v += fits_inside(child, ppol)
    left = budget_left(parent)
    for k in BUDGET_KEYS:
        if child["budget"].get(k, 0) > left[k] + 1e-9:
            v.append(f"budget.{k} {child['budget'].get(k)} exceeds what '{parent}' has left ({left[k]:g})")
    if left["children"] < 1:
        v.append(f"'{parent}' has no child slots left")
    if v:
        log({"event": "spawn-denied", "name": name, "parent": parent, "violations": v})
        return {"ok": False, "violations": v}

    d = os.path.join(IND, name)
    for sub in ("uplink/requests", "uplink/responses", "uplink/done"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    pol_path = os.path.join(d, "policy.toml")
    with open(pol_path, "w") as fh:
        fh.write(policy_text)
    b = child["budget"]
    extra = ["--memory", f"{int(b['memory_mb'])}m", "--cpus", f"{b['cpus']}",
             "-v", f"{d}/uplink:/var/lib/resident/uplink", "--label", f"resident.parent={parent}"]
    try:
        bodies.body(name, bodies.IMAGE, extra)
    except SystemExit as e:
        return {"ok": False, "violations": [f"body failed to start: {e}"]}

    lineage = list(reversed([parent] + ancestors(parent))) + [name]
    purpose = purpose or child.get("individual", {}).get("purpose", "")
    meta = {"name": name, "parent": parent, "lineage": lineage, "purpose": purpose, "born": now(), "image": bodies.IMAGE}
    subprocess.run(["docker", "exec", "-i", name, "sh", "-c", "cat > /var/lib/resident/policy.toml"], input=policy_text, text=True)
    subprocess.run(["docker", "exec", "-i", name, "sh", "-c", "cat > /var/lib/resident/individual.json"], input=json.dumps(meta, indent=1), text=True)
    env = ["-e", f"RESIDENT_AUTHOR={parent}"]
    subprocess.run(["docker", "exec"] + env + [name, "intent", "add",
                    f"Individual '{name}': born of '{parent}' (lineage {' > '.join(lineage)}). Purpose: {purpose}"], capture_output=True)
    subprocess.run(["docker", "exec"] + env + [name, "intent", "add",
                    f"Policy for '{name}', a subset of '{parent}': {summarize(child)}"], capture_output=True)
    i = host_intent(f"Individual '{name}', child of '{parent}': {purpose} | {summarize(child)}", author)
    subprocess.run([INTENT, "adopt", i, "--kind", "container", f"^{re.escape(name)}$"], capture_output=True)
    r = registry()
    r[name] = {"name": name, "parent": parent, "kind": "container", "policy": pol_path, "purpose": purpose,
               "born": meta["born"], "host_intent": i}
    save_registry(r)
    log({"event": "spawned", "name": name, "parent": parent, "intent": i})
    return {"ok": True, "intent": i, "lineage": lineage}


# ------------------------------------------------------------------ broker

def respond(name, rid, resp):
    p = os.path.join(IND, name, "uplink", "responses", f"{rid}.json")
    with open(p + ".tmp", "w") as fh:
        json.dump(resp, fh, indent=1)
    os.replace(p + ".tmp", p)


def execute(requester, verb, resource, approved_by=None):
    cmd = shlex.join(VERBS[verb](resource))
    via = f"; approved by {approved_by}" if approved_by else "; within its own policy"
    i = None
    if verb in MUTATING:
        i = host_intent(f"'{requester}' requested {verb} {resource}{via}", requester)
        r = subprocess.run([INTENT, "apply", i, "--", cmd], capture_output=True, text=True,
                           env=dict(os.environ, RESIDENT_AUTHOR=requester))
        subprocess.run([INTENT, "close", i], capture_output=True)
    else:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
    return {"status": "done" if r.returncode == 0 else "failed", "exit": r.returncode, "intent": i,
            "approved_by": approved_by,
            # mutating runs end with the intent summary; reads matter from the top
            "output": (r.stdout + r.stderr)[-3000:] if verb in MUTATING else (r.stdout + r.stderr)[:3000]}


def handle(requester, req):
    verb, res = req.get("verb", ""), req.get("resource", "")
    if res.startswith("-") or "\n" in res:
        return {"status": "denied", "reason": "malformed resource"}
    if verb == "spawn":
        pl = req.get("payload") or {}
        out = spawn_child(res, requester, pl.get("policy", ""), pl.get("purpose", ""), author=requester)
        return {"status": "done" if out["ok"] else "denied", **out}
    if verb not in VERBS:
        return {"status": "denied", "reason": f"unknown verb '{verb}'"}
    pol = policy_of(requester)
    mine = allowed(pol, verb, res)
    if mine and verb not in pol["approval_required"]["verbs"]:
        return execute(requester, verb, res)
    chain = [requester] + ancestors(requester)
    holder = requester if mine else next((a for a in chain[1:] if allowed(policy_of(a), verb, res)), None)
    if not holder:
        return {"status": "denied", "reason": f"no one in {' > '.join(reversed(chain))} holds {verb} on {res}"}
    aid = uuid.uuid4().hex[:6]
    why = "this verb needs a human" if holder == requester else f"outside '{requester}' policy; '{holder}' holds it"
    a = {"id": aid, "ts": now(), "requester": requester, "rid": req["id"], "verb": verb, "resource": res,
         "holder": holder, "reason": req.get("reason", ""), "why_escalated": why, "status": "pending"}
    os.makedirs(APPROVALS, exist_ok=True)
    with open(os.path.join(APPROVALS, f"{aid}.json"), "w") as fh:
        json.dump(a, fh, indent=1)
    return {"status": "pending", "approval": aid, "holder": holder, "why": why}


def broker_once():
    n = 0
    for name, e in registry().items():
        if e["kind"] == "host":
            continue
        q = os.path.join(IND, name, "uplink", "requests")
        for f in sorted(os.listdir(q)) if os.path.isdir(q) else []:
            if not f.endswith(".json"):
                continue
            p = os.path.join(q, f)
            try:
                req = json.load(open(p))
            except ValueError:
                continue
            req.setdefault("id", f[:-5])
            try:
                resp = handle(name, req)
            except Exception as ex:  # a bad request must never kill the broker
                resp = {"status": "failed", "reason": repr(ex)}
            resp.update({"id": req["id"], "verb": req.get("verb"), "resource": req.get("resource"), "ts": now()})
            respond(name, req["id"], resp)
            os.replace(p, os.path.join(IND, name, "uplink", "done", f))
            log({"event": "request", "from": name, **{k: resp.get(k) for k in ("id", "verb", "resource", "status", "approval")}})
            n += 1
    return n


def pending():
    out = []
    for f in sorted(glob.glob(os.path.join(APPROVALS, "*.json"))):
        a = json.load(open(f))
        if a["status"] == "pending":
            out.append(a)
    return out


def decide(aid, approve, who=OWNER):
    p = os.path.join(APPROVALS, f"{aid}.json")
    if not os.path.exists(p):
        die(f"no approval {aid}")
    a = json.load(open(p))
    if a["status"] != "pending":
        die(f"approval {aid} is already {a['status']}")
    resp = execute(a["requester"], a["verb"], a["resource"], approved_by=who) if approve else \
        {"status": "denied", "reason": f"denied by {who}"}
    resp.update({"id": a["rid"], "verb": a["verb"], "resource": a["resource"], "ts": now(), "approval": aid})
    respond(a["requester"], a["rid"], resp)
    a.update(status="approved" if approve else "denied", decided_by=who, decided=now())
    with open(p, "w") as fh:
        json.dump(a, fh, indent=1)
    log({"event": "decision", "approval": aid, "status": a["status"], "by": who})
    return a, resp


def retire(name):
    if name == HEAD:
        die("the head cannot be retired")
    for kid in children(name):
        retire(kid)
    if subprocess.run(["docker", "inspect", name], capture_output=True).returncode == 0:
        bodies.dump(name)
        bodies.kill(name, purge=True)
    r = registry()
    e = r.pop(name)
    save_registry(r)
    if e.get("host_intent"):
        subprocess.run([INTENT, "close", e["host_intent"], "retired"], capture_output=True)
    log({"event": "retired", "name": name})
    print(f"[{name}] retired (log dumped to individuals/{name}.json)")


# -------------------------------------------------------------------- view

def short_score(text):
    m = re.search(r"(\d+)/(\d+)", text or "")
    return f"unexplained {m.group(1)}/{m.group(2)}" if m else ""


def tree_text(compact=False):
    r = registry()
    pend = {}
    for a in pending():
        pend[a["requester"]] = pend.get(a["requester"], 0) + 1
    lines = []

    def walk(name, prefix, last, depth):
        e, pol = r[name], policy_of(name)
        b = pol["budget"]
        if e["kind"] == "host":
            state = "head · host"
            score = "" if compact else short_score(subprocess.run([RESIDENT, "score"], capture_output=True, text=True).stdout)
        else:
            state = subprocess.run(["docker", "inspect", "--format", "{{.State.Status}}", name],
                                   capture_output=True, text=True).stdout.strip() or "gone"
            score = short_score(subprocess.run(["docker", "exec", name, "resident", "score"], capture_output=True,
                                               text=True).stdout) if state == "running" else ""
        kids = children(name)
        parts = [f"{name} [{state}]", f"${b.get('usd_per_day', 0):g}/day", f"{int(b.get('memory_mb', 0))}MB",
                 f"{b.get('cpus', 0):g} cpu", f"children {len(kids)}/{b.get('max_children', 0)}"]
        if score:
            parts.append(score)
        if pend.get(name):
            parts.append(f"PENDING APPROVAL ×{pend[name]}")
        lines.append((prefix + ("└─ " if last else "├─ ") if depth else "") + " · ".join(parts))
        sub = (prefix + ("   " if last else "│  ")) if depth else ""
        if not compact:
            bar = "│  " if kids else "   "
            lines.append(sub + bar + f"purpose: {e.get('purpose', '')[:100]}")
            caps = [f"{k} {v}" for k, v in pol["capabilities"].items() if k != "spawn" and v]
            lines.append(sub + bar + "may: " + ("spawn; " if pol["capabilities"].get("spawn") else "") + "; ".join(caps))
            lines.append(sub + bar + f"needs a human for: {', '.join(pol['approval_required']['verbs']) or '-'}")
        for i, k in enumerate(kids):
            walk(k, sub, i == len(kids) - 1, depth + 1)

    walk(HEAD, "", True, 0)
    n = sum(pend.values())
    if n:
        lines.append(f"{n} pending approval(s): `resident approvals`")
    return "\n".join(lines)


def main(a):
    if not a or a[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = a[0], a[1:]

    def opt(k, d=None):
        return rest[rest.index(k) + 1] if k in rest else d

    if cmd == "spawn":
        pf = opt("--policy")
        if not pf:
            die("--policy FILE is required")
        out = spawn_child(rest[0], opt("--parent", HEAD), open(pf).read(), opt("--purpose", ""),
                          author=os.environ.get("RESIDENT_AUTHOR", OWNER))
        if not out["ok"]:
            print(f"[{rest[0]}] DENIED")
            for x in out["violations"]:
                print(f"  - {x}")
            return 1
        print(f"[{rest[0]}] born; lineage {' > '.join(out['lineage'])}; host intent #{out['intent']}")
    elif cmd == "tree":
        print(tree_text(compact="--compact" in rest))
    elif cmd == "broker":
        if "--once" in rest:
            print(f"processed {broker_once()} request(s)")
        else:
            while True:
                broker_once()
                time.sleep(2)
    elif cmd == "approvals":
        rows = pending()
        for x in rows:
            print(f"{x['id']}  {x['requester']} asks {x['verb']} {x['resource']} — {x['why_escalated']}"
                  + (f" (reason: {x['reason']})" if x["reason"] else ""))
        if not rows:
            print("(no pending approvals)")
    elif cmd in ("approve", "deny"):
        a, resp = decide(rest[0], cmd == "approve", opt("--as", OWNER))
        print(f"{a['id']}: {a['status']} by {a['decided_by']} → {resp['status']}"
              + (f" (host intent #{resp['intent']})" if resp.get("intent") else ""))
    elif cmd == "retire":
        retire(rest[0])
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
