#!/usr/bin/env python3
"""Tests for the propose → passkey-approve → apply → verify → report loop.

  python3 -I test_proposals.py

Everything runs against temp state: RESIDENT_HOME_STATE, RESIDENT_APPROVALS, RESIDENT_OUTBOX, RESIDENT_INBOX and
RESIDENT_HOME (intent DB) all point into a temp dir, the server listens on :8722, the owner is a software
authenticator, and root proposals go through the REAL root helper in --selftest-config mode (we cannot sudo).
The real owner.json, approvals, intents.db, inbox and outbox are never touched.
"""
import hashlib, importlib.machinery, importlib.util, json, os, shutil, subprocess, sys, tempfile, time
import urllib.error, urllib.request
OWNER_NAME = os.environ.get("RESIDENT_OWNER") or __import__("getpass").getuser()
SAMPLE_PROPOSAL = os.path.join(os.path.expanduser("~"), "resident", "approvals", "abc123.json")

HERE = os.path.dirname(os.path.realpath(__file__))
RES = os.path.dirname(HERE)
HELPER = os.path.join(RES, "root", "resident-root-apply")
RESIDENT = os.path.join(RES, "lib", "agentos.py")
TMP = tempfile.mkdtemp(prefix="resident-proposals-test-")
D = {k: os.path.join(TMP, k) for k in ("state", "approvals", "outbox", "inbox", "rhome", "rootcfg", "work")}
for d in D.values():
    os.makedirs(d)
os.makedirs(os.path.join(D["rootcfg"], "used.d"), mode=0o700)
os.environ.update(RESIDENT_HOME_STATE=D["state"], RESIDENT_APPROVALS=D["approvals"], RESIDENT_OUTBOX=D["outbox"],
                  RESIDENT_INBOX=D["inbox"], RESIDENT_HOME=D["rhome"],
                  RESIDENT_ROOT_HELPER=f"/usr/bin/python3 -I {HELPER} --selftest-config {D['rootcfg']}",
                  PATH=os.path.expanduser("~/.local/bin") + ":" + os.environ.get("PATH", "/usr/bin:/bin"))
for k in ("CLAUDECODE", "CLAUDE_CODE"):
    os.environ.pop(k, None)
sys.path.insert(0, HERE)
import home  # noqa: E402
import propose  # noqa: E402  (home put lib/ on sys.path)
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

assert home.tree.APPROVALS == D["approvals"] and propose.OUTBOX == D["outbox"] and home.INBOX == D["inbox"]
b64e, b64d, sha256 = home.b64e, home.b64d, home.sha256
RESULTS = []
PORT = 8722


def check(name, fn):
    try:
        fn()
        RESULTS.append((name, True))
        print(f"  PASS  {name}")
    except Exception as e:  # noqa: BLE001
        RESULTS.append((name, False))
        print(f"  FAIL  {name}: {e!r}")


def cbor(v):
    def head(mt, n):
        if n < 24:
            return bytes([mt << 5 | n])
        for ai, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
            if n < 1 << (8 * size):
                return bytes([mt << 5 | ai]) + n.to_bytes(size, "big")
    if isinstance(v, int): return head(0, v) if v >= 0 else head(1, -1 - v)
    if isinstance(v, bytes): return head(2, len(v)) + v
    if isinstance(v, str): return head(3, len(v.encode())) + v.encode()
    if isinstance(v, dict): return head(5, len(v)) + b"".join(cbor(k) + cbor(x) for k, x in v.items())
    raise TypeError(type(v))


class SoftAuthenticator:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(32)
        self.counter = 0

    def xy(self):
        n = self.key.public_key().public_numbers()
        return n.x.to_bytes(32, "big"), n.y.to_bytes(32, "big")

    def create(self, challenge, rpid):
        x, y = self.xy()
        ad = sha256(rpid.encode()) + b"\x45" + self.counter.to_bytes(4, "big") + b"\x00" * 16 + \
            len(self.cred_id).to_bytes(2, "big") + self.cred_id + cbor({1: 2, 3: -7, -1: 1, -2: x, -3: y})
        cdj = json.dumps({"type": "webauthn.create", "challenge": b64e(challenge), "origin": "https://" + rpid}).encode()
        return {"rawId": self.cred_id, "clientDataJSON": cdj,
                "attestationObject": cbor({"fmt": "none", "attStmt": {}, "authData": ad})}

    def get(self, challenge, rpid, origin=None):
        self.counter += 1
        ad = sha256(rpid.encode()) + b"\x05" + self.counter.to_bytes(4, "big")
        cdj = json.dumps({"type": "webauthn.get", "challenge": b64e(challenge),
                          "origin": origin or "https://" + rpid, "crossOrigin": False}).encode()
        sig = self.key.sign(ad + sha256(cdj), ec.ECDSA(hashes.SHA256()))
        return {"rawId": self.cred_id, "clientDataJSON": cdj, "authenticatorData": ad, "signature": sig}


def enc(a):
    return {k: b64e(v) for k, v in a.items()}


def run(argv, **kw):
    return subprocess.run(argv, capture_output=True, text=True, timeout=120, **kw)


def resident(*args):
    return run([sys.executable, "-I", RESIDENT] + list(args))


def outbox_texts():
    return [open(os.path.join(D["outbox"], f)).read() for f in sorted(os.listdir(D["outbox"]))]


# ------------------------------------------------------------------- canonical form + CLI

def load_helper():
    sys.dont_write_bytecode = True  # keep root/ free of artifacts
    loader = importlib.machinery.SourceFileLoader("resident_root_apply", HELPER)
    spec = importlib.util.spec_from_loader("resident_root_apply", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def unit_canonical():
    print("canonical form")
    p = {"kind": "proposal", "id": "a1b2c3", "ts": "x", "requester": "nuc", "summary": "Fix café — ünïcode",
         "why": "w", "command": "echo 'hi' \"there\"", "verify": "true", "root": True, "nonce": b64e(os.urandom(16)),
         "status": "pending", "assertion": {"x": 1}}
    spec = json.dumps({k: p[k] for k in ("kind", "id", "requester", "summary", "why", "command", "verify", "root",
                                         "nonce")}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    check("propose.canonical matches the contract byte-for-byte (ts/status/assertion excluded)",
          lambda: (lambda: None)() if propose.canonical(p) == spec else (_ for _ in ()).throw(AssertionError(propose.canonical(p))))
    h = load_helper()
    def same():
        assert h.canonical(p) == spec and propose.challenge(p) == hashlib.sha256(spec).digest()
        q = dict(p, root=False)
        assert propose.canonical(q) != spec
    check("root helper computes the identical canonical bytes; root flag changes them", same)
    def self_contained():
        src = open(HELPER).read()
        assert "resident/" not in "".join(l for l in src.splitlines() if l.startswith(("import", "from", "sys.path")))
        assert "sys.path" not in src and src.startswith("#!/usr/bin/python3 -I\n")
    check("root helper imports nothing from ~/resident (stdlib + cryptography only, python3 -I)", self_contained)


def cli():
    print("resident propose (CLI)")
    def refusals():
        cases = [(["only summary", "--verify", "true", "--", "true"], "--why"),
                 (["s", "--why", "w", "--", "true"], "--verify"),
                 (["s", "--why", "w", "--verify", "true", "--"], "command"),
                 (["x" * 81, "--why", "w", "--verify", "true", "--", "true"], "max 80"),
                 (["s", "--why", "w", "--verify", "true", "--", "echo a\necho b"], "newline"),
                 (["--why", "w", "--verify", "true", "--", "true"], "summary")]
        for argv, want in cases:
            r = resident("propose", *argv)
            assert r.returncode != 0 and want in r.stderr, (argv, r.returncode, r.stderr)
        assert not os.listdir(D["approvals"]), "a refused proposal left a file"
    check("refuses: no --why, no --verify, no command, summary > 80, newline in command, no summary", refusals)
    def ok():
        r = resident("propose", "Restart the printer service", "--why", "Printing stopped working.", "--verify",
                     "systemctl is-active --quiet cups", "--root", "--", "systemctl", "restart", "cups")
        assert r.returncode == 0, r.stderr
        aid, url = r.stdout.split()
        assert len(aid) == 6 and int(aid, 16) >= 0 and url.startswith("https://") and url.endswith("/resident/"), r.stdout
        p = json.load(open(os.path.join(D["approvals"], aid + ".json")))
        assert {k for k in p} == {"kind", "id", "ts", "requester", "summary", "why", "command", "verify", "root",
                                  "nonce", "status"}, p
        assert p["kind"] == "proposal" and p["status"] == "pending" and p["root"] is True and p["requester"] == "nuc"
        assert p["command"] == "systemctl restart cups" and len(b64d(p["nonce"])) == 16
        notes = outbox_texts()
        assert notes == ["Proposed: Restart the printer service — Printing stopped working. Approve on your phone.\n"], notes
        assert not os.listdir(D["inbox"]), "propose must not wake agentd"
        r = resident("approvals")
        assert aid in r.stdout and "proposes (as root)" in r.stdout, r.stdout
        r = resident("deny", aid)  # CLI deny works for proposals
        assert r.returncode == 0 and json.load(open(os.path.join(D["approvals"], aid + ".json")))["status"] == "denied"
        assert outbox_texts()[-1].startswith("Denied: Restart the printer service"), outbox_texts()
    check("valid proposal: file per contract, id + URL printed, outbox note, no inbox event; CLI deny works", ok)
    def cli_approve_refused():
        r = resident("propose", "Harmless", "--why", "w", "--verify", "true", "--", "true")
        aid = r.stdout.split()[0]
        env = dict(os.environ, RESIDENT_HOME_STATE=os.path.join(TMP, "never-enrolled"))  # even before enrollment
        r = run([sys.executable, "-I", RESIDENT, "approve", aid], env=env)
        assert r.returncode != 0 and "passkey" in r.stderr, r.stderr
        assert json.load(open(os.path.join(D["approvals"], aid + ".json")))["status"] == "pending"
        resident("deny", aid)
    check("`resident approve <proposal>` is refused even with no passkey enrolled", cli_approve_refused)


# ------------------------------------------------------------------- root helper (selftest mode)

RP_T = "test.example.ts.net"


def helper_tests():
    print("root helper (--selftest-config, runs as this user)")
    A, B = SoftAuthenticator(), SoftAuthenticator()
    cfgdir = os.path.join(TMP, "hcfg")
    os.makedirs(os.path.join(cfgdir, "used.d"))
    x, y = A.xy()
    json.dump({"cred_id": b64e(A.cred_id), "x": b64e(x), "y": b64e(y), "alg": -7, "rp_id": RP_T,
               "origin": "https://" + RP_T}, open(os.path.join(cfgdir, "owner.json"), "w"))
    n = [0]

    def make(cmd, signer=A, root=True, mutate=None, assertion=True, origin=None):
        n[0] += 1
        p = {"kind": "proposal", "id": f"{n[0]:06x}", "ts": "t", "requester": "nuc", "summary": f"helper test {n[0]}",
             "why": "w", "command": cmd, "verify": "true", "root": root, "nonce": b64e(os.urandom(16)),
             "status": "approved"}
        if assertion:
            p["assertion"] = enc(signer.get(propose.challenge(p), RP_T, origin))
        if mutate:
            mutate(p)
        path = os.path.join(D["work"], f"h{n[0]}.json")
        json.dump(p, open(path, "w"))
        return path

    def helper(path, cfg=cfgdir):
        return run(["/usr/bin/python3", "-I", HELPER] + (["--selftest-config", cfg] if cfg else []) + [path])

    def mark(name):
        return os.path.join(D["work"], name)

    st = {}

    def valid():
        f = make(f"echo helper-ran && touch {mark('valid')}")
        r = helper(f)
        assert r.returncode == 0 and "helper-ran" in r.stdout and os.path.exists(mark("valid")), (r.returncode, r.stderr)
        st["valid"] = f
    check("valid owner-signed root proposal runs (output streamed, exit 0)", valid)

    def refused(f, why, marker=None):
        r = helper(f)
        assert r.returncode == 77 and "REFUSED" in r.stderr and why in r.stderr, (r.returncode, r.stderr)
        if marker:
            assert not os.path.exists(mark(marker)), "command ran anyway"

    def tamper(field, value):
        return lambda p: p.__setitem__(field, value)
    check("tampered command refused", lambda: refused(make(f"touch {mark('t1')}", mutate=tamper("command", f"touch {mark('t1')}; id")), "signature does not cover", "t1"))
    check("tampered verify refused", lambda: refused(make(f"touch {mark('t2')}", mutate=tamper("verify", "false")), "signature does not cover", "t2"))
    check("tampered summary refused", lambda: refused(make(f"touch {mark('t2b')}", mutate=tamper("summary", "something nicer")), "signature does not cover", "t2b"))
    check("root flag flipped false→true after signing refused", lambda: refused(make(f"touch {mark('t3')}", root=False, mutate=tamper("root", True)), "signature does not cover", "t3"))
    check("non-root proposal refused", lambda: refused(make(f"touch {mark('t4')}", root=False), "not marked root", "t4"))
    check("replay of a used approval refused", lambda: refused(st["valid"], "replay"))
    check("assertion by another passkey refused", lambda: refused(make(f"touch {mark('t5')}", signer=B), "unknown credential", "t5"))
    def same_id_other_key():
        C = SoftAuthenticator(); C.cred_id = A.cred_id
        refused(make(f"touch {mark('t6')}", signer=C), "bad signature", "t6")
    check("right credential id, wrong key refused", same_id_other_key)
    check("missing assertion refused", lambda: refused(make(f"touch {mark('t7')}", assertion=False), "no owner assertion", "t7"))
    check("kind != proposal refused", lambda: refused(make(f"touch {mark('t8')}", mutate=tamper("kind", "request")), "kind", "t8"))
    check("missing kind refused", lambda: refused(make(f"touch {mark('t8b')}", mutate=lambda p: p.pop("kind")), "kind", "t8b"))
    check("wrong origin refused", lambda: refused(make(f"touch {mark('t9')}", origin="https://evil.example"), "origin", "t9"))
    def not_json():
        f = mark("garbage.json"); open(f, "w").write("root:x:0:0:secret")
        r = helper(f)
        assert r.returncode == 77 and "secret" not in r.stderr + r.stdout, r.stderr
    check("non-JSON file refused without echoing its content", not_json)
    def exit_code():
        r = helper(make("echo out; exit 3"))
        assert r.returncode == 3 and "out" in r.stdout, (r.returncode, r.stderr)
    check("command's exit code is passed through", exit_code)
    def clean_env():
        r = helper(make("echo PATH=$PATH; echo PWD=$(pwd); echo SECRET=${RESIDENT_ROOT_HELPER:-none}"))
        assert "PATH=/usr/sbin:/usr/bin:/sbin:/bin" in r.stdout and "PWD=/" in r.stdout and "SECRET=none" in r.stdout, r.stdout
    check("command runs with clean env (fixed PATH, no inherited vars) in /", clean_env)
    def not_root():
        r = helper(make(f"touch {mark('t10')}"), cfg=None)
        assert r.returncode == 77 and "must run as root" in r.stderr and not os.path.exists(mark("t10")), r.stderr
    check("without --selftest-config a non-root caller is refused", not_root)
    def logged():
        recs = [json.loads(l) for l in open(os.path.join(cfgdir, "root.log"))]
        assert any(r["result"] == "ran" and r.get("exit") == 0 for r in recs)
        assert sum(r["result"] == "refused" for r in recs) >= 10, recs
        used = open(os.path.join(cfgdir, "used.d", "used")).read().split()
        assert len(used) == len(set(used)) == sum(r["result"] == "ran" for r in recs), used
    check("one log line per run; used list holds each run once", logged)


# ------------------------------------------------------------------- end to end over HTTP

def http(path, body=None, origin=True, ctype="application/json", raw=False):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/resident/{path}",
                                 data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST")
    if body is not None:
        req.add_header("Content-Type", ctype)
    if origin:
        req.add_header("Origin", origin if isinstance(origin, str) else "https://" + home.rp_id())
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            data = r.read()
            return r.status, (data.decode() if raw else json.loads(data))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def e2e():
    rp = home.rp_id()
    print(f"e2e: server on :{PORT}, rp_id {rp}, temp {TMP}")
    srv = subprocess.Popen([sys.executable, "-I", os.path.join(HERE, "home.py"), "serve", "--port", str(PORT)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    A = SoftAuthenticator()
    try:
        for _ in range(100):
            try:
                if http("api/state", origin=False)[0] == 200:
                    break
            except OSError:
                time.sleep(0.2)

        def enroll():
            url = run([sys.executable, "-I", os.path.join(HERE, "home.py"), "enroll"], check=True).stdout.strip()
            tok = url.split("enroll=", 1)[1]
            code, o = http("api/enroll/options", {"token": tok})
            r = A.create(b64d(o["publicKey"]["challenge"]), rp)
            code, out = http("api/enroll/finish", {"token": tok, **enc(r)})
            assert code == 200 and out["enrolled"], out
            assert os.path.exists(os.path.join(D["state"], "owner.json"))
            x, y = A.xy()  # the root helper's (selftest) copy of the PUBLIC key, as install.sh would write it
            cred = json.load(open(os.path.join(D["state"], "owner.json")))
            json.dump({"cred_id": cred["cred_id"], "x": cred["x"], "y": cred["y"], "alg": -7, "rp_id": rp,
                       "origin": "https://" + rp}, open(os.path.join(D["rootcfg"], "owner.json"), "w"))
            assert b64d(cred["x"]) == x and b64d(cred["y"]) == y
        check("e2e: enroll the software authenticator in temp state", enroll)

        def new(summary, cmd, verify, root=False):
            r = resident("propose", summary, "--why", "Because the test says so.", "--verify", verify,
                         *(["--root"] if root else []), "--", cmd)
            assert r.returncode == 0, r.stderr
            return r.stdout.split()[0]

        def pfile(aid):
            return json.load(open(os.path.join(D["approvals"], aid + ".json")))

        def options(aid):
            code, o = http("api/approve/options", {"aid": aid})
            assert code == 200, o
            return b64d(o["publicKey"]["challenge"])

        st = {}
        proof = os.path.join(D["work"], "proof")

        def approve_nonroot():
            aid = new("Create the proof file", f"touch {proof}", f"test -f {proof}")
            code, s = http("api/state", origin=False)
            card = next(a for a in s["pending"] if a["id"] == aid)
            assert card["kind"] == "proposal" and card["root"] is False and card["command"] == f"touch {proof}"
            assert card["why"] and card["verify"] == f"test -f {proof}" and card["valid"]
            ch = options(aid)
            assert ch == propose.challenge(pfile(aid)), "challenge is not sha256(canonical)"
            a = A.get(ch, rp)
            code, out = http("api/approve/finish", {"aid": aid, **enc(a)})
            assert code == 200, out
            res = out["result"]
            assert res["status"] == "done" and res["verify_ok"] and res["exit"] == 0, res
            assert os.path.exists(proof)
            p = pfile(aid)
            assert p["status"] == "done" and p["verify_ok"] is True and p["exit"] == 0 and p["intent"] == res["intent"], p
            assert p["approved_by"] == f"{OWNER_NAME} (passkey)" and p["approved_at"]
            assert set(p["assertion"]) == {"rawId", "clientDataJSON", "authenticatorData", "signature"}
            assert b64d(p["assertion"]["signature"]) == a["signature"]
            assert outbox_texts()[-1] == "Done: Create the proof file. Verified.\n", outbox_texts()[-1]
            r = run([os.path.expanduser("~/.local/bin/intent"), "show", str(p["intent"])])
            assert "Create the proof file — Because the test says so. (approved by passkey)" in r.stdout, r.stdout
            assert f"by {OWNER_NAME}" in r.stdout and "verify=PASS" in r.stdout and f"touch {proof}" in r.stdout, r.stdout
            assert not any(x["id"] == aid for x in http("api/state", origin=False)[1]["pending"])
            st.update(aid=aid, a=a)
            print(f"        {aid} → intent #{p['intent']}: {r.stdout.splitlines()[0]}")
        check("e2e: non-root proposal approved by passkey → intent apply → verified done, outbox + intent recorded",
              approve_nonroot)

        def replay():
            aid, a = st["aid"], st["a"]
            code, out = http("api/approve/finish", {"aid": aid, **enc(a)})
            assert code in (403, 404) and "not pending" in out["error"] or "challenge" in out["error"], out
            p = pfile(aid)  # an attacker resets the file to pending: the challenge would be identical again
            p["status"] = "pending"
            for k in ("assertion", "exit", "verify_ok", "output", "intent"):
                p.pop(k, None)
            propose.write_json(os.path.join(D["approvals"], aid + ".json"), p)
            os.remove(proof)
            assert options(aid) == b64d(a["clientDataJSON"] and json.loads(a["clientDataJSON"])["challenge"])
            code, out = http("api/approve/finish", {"aid": aid, **enc(a)})
            assert code == 403 and "replay" in out["error"], out
            assert not os.path.exists(proof), "replayed proposal ran"
            resident("deny", aid)
        check("e2e: replayed assertion refused (after done, and after resetting the file to pending)", replay)

        def tamper():
            evil = os.path.join(D["work"], "evil")
            aid = new("Harmless looking change", f"touch {os.path.join(D['work'], 'fine')}", "true")
            ch = options(aid)
            path = os.path.join(D["approvals"], aid + ".json")
            p = pfile(aid)
            p["command"] = f"touch {evil}"
            propose.write_json(path, p)
            code, out = http("api/approve/finish", {"aid": aid, **enc(A.get(ch, rp))})
            assert code == 403 and "changed" in out["error"], out
            assert not os.path.exists(evil) and pfile(aid)["status"] == "pending"
            # signing the tampered version's challenge does not help either: options must be issued for it,
            # and what the owner then signs IS the tampered command (shown on the page) — that is the design.
            code, out = http("api/deny", {"aid": aid})
            assert code == 200 and pfile(aid)["status"] == "denied", out
            assert outbox_texts()[-1].startswith("Denied: Harmless looking change"), outbox_texts()[-1]
            st["tampered"] = aid
        check("e2e: command changed after options were issued → finish refused, nothing ran; deny works", tamper)

        def root_via_helper():
            rproof = os.path.join(D["work"], "rootproof")
            aid = new("Create the root proof file", f"touch {rproof}", f"test -f {rproof}", root=True)
            card = next(a for a in http("api/state", origin=False)[1]["pending"] if a["id"] == aid)
            assert card["root"] is True
            code, out = http("api/approve/finish", {"aid": aid, **enc(A.get(options(aid), rp))})
            assert code == 200 and out["result"]["status"] == "done", out
            assert os.path.exists(rproof)
            res = out["result"]
            assert res["apply"].rstrip("'").endswith(f"--selftest-config {D['rootcfg']} {os.path.join(D['approvals'], aid + '.json')}"), res["apply"]
            assert "verified (owner passkey)" in pfile(aid)["output"], pfile(aid)["output"]
            recs = [json.loads(l) for l in open(os.path.join(D["rootcfg"], "root.log"))]
            assert recs[-1]["id"] == aid and recs[-1]["result"] == "ran" and recs[-1]["exit"] == 0, recs
            assert outbox_texts()[-1] == "Done: Create the root proof file. Verified.\n"
        check("e2e: root proposal → intent apply → helper (selftest) re-verifies the signature → done", root_via_helper)

        def root_default_command():
            env = {k: v for k, v in os.environ.items() if k != "RESIDENT_ROOT_HELPER"}
            code = ("import sys,json; sys.path.insert(0, sys.argv[1]); import home; "
                    "print(json.dumps(home.apply_argv({'root': True, 'command': 'x', 'verify': 'v'}, sys.argv[2], 7)))")
            r = run([sys.executable, "-I", "-c", code, HERE, SAMPLE_PROPOSAL], env=env)
            argv = json.loads(r.stdout)
            assert argv[1:] == ["apply", "7", "--verify", "v", "--", "sudo -n /usr/local/libexec/resident-root-apply "
                                + SAMPLE_PROPOSAL], argv
            argv2 = json.loads(run([sys.executable, "-I", "-c", code.replace("True", "False"), HERE, "/p"], env=env).stdout)
            assert argv2[-1] == "x", argv2
        check("e2e: production root command is `intent apply N --verify V -- sudo -n /usr/local/libexec/resident-root-apply <path>`",
              root_default_command)

        def root_failure_reported():
            aid = new("Fails on purpose", "echo boom: cannot do it >&2; exit 4", "true", root=True)
            code, out = http("api/approve/finish", {"aid": aid, **enc(A.get(options(aid), rp))})
            assert code == 200 and out["result"]["status"] == "failed" and out["result"]["exit"] == 4, out
            p = pfile(aid)
            assert p["status"] == "failed" and p["exit"] == 4
            assert outbox_texts()[-1] == "Failed: Fails on purpose — boom: cannot do it\n", outbox_texts()[-1]
        check("e2e: failing command → status failed, exit recorded, 'Failed: … — <first error line>'", root_failure_reported)

        def verify_failure():
            aid = new("Command ok but goal not met", "true", "test -f /nonexistent/xyz")
            code, out = http("api/approve/finish", {"aid": aid, **enc(A.get(options(aid), rp))})
            assert code == 200 and out["result"]["status"] == "failed" and out["result"]["verify_ok"] is False, out
            assert "the check did not pass" in outbox_texts()[-1]
        check("e2e: command exits 0 but verify fails → failed", verify_failure)

        def ask_and_feed():
            code, out = http("api/ask", {"text": "  Why is the fan loud?  " + "x" * 100})
            assert code == 200, out
            f = os.path.join(D["inbox"], out["event"])
            ev = json.load(open(f))
            assert ev["kind"] == "ask" and ev["detail"].startswith("Why is the fan loud?") and len(ev["summary"]) == 80
            assert set(ev) == {"ts", "kind", "summary", "detail"} and out["event"].endswith("-ask.json")
            assert http("api/ask", {"text": "x" * 2001})[0] == 413
            assert http("api/ask", {"text": "   "})[0] == 400
            assert http("api/ask", {"text": "hi"}, origin="https://evil.example")[0] == 403
            assert http("api/ask", {"text": "hi"}, ctype="text/plain")[0] == 415
            assert len(os.listdir(D["inbox"])) == 1
            open(os.path.join(D["outbox"], "9999999999999-ask.md"), "w").write(
                "# ask: q\n\n<script>alert(1)</script> answer " + "y" * 600 + "\n\n---\n_agentd footer_\n")
            feed = http("api/state", origin=False)[1]["feed"]
            assert feed[-1]["text"].startswith("# ask: q") and len(feed[-1]["text"]) <= 401 and feed[-1]["kind"] == "ask"
            assert "_agentd" not in feed[-1]["text"] and len(feed) <= 15
            assert any(m["text"].startswith("Done: Create the proof file") for m in feed)
            code, page = http("", raw=True, origin=False)
            assert code == 200 and "Conversation" in page and "api/ask" in page.replace("'ask'", "api/ask") and "textContent" in page
        check("e2e: /api/ask writes an inbox ask event (limits + CSRF checks); feed shows last outbox messages", ask_and_feed)

        def old_requests_unchanged():
            aid = "c0ffee"
            json.dump({"id": aid, "ts": "t", "requester": "merit-reviews", "rid": "r1", "verb": "container.inspect",
                       "resource": "x", "holder": "merit", "reason": "", "why_escalated": "w", "status": "pending"},
                      open(os.path.join(D["approvals"], aid + ".json"), "w"))
            card = next(a for a in http("api/state", origin=False)[1]["pending"] if a["id"] == aid)
            assert card["kind"] == "request" and card["verb"] == "container.inspect" and "lineage" in card
            code, o = http("api/approve/options", {"aid": aid})
            assert code == 200 and len(b64d(o["publicKey"]["challenge"])) == 32  # random, as before
            os.remove(os.path.join(D["approvals"], aid + ".json"))
        check("e2e: child-request approvals (no kind) still listed and challenged as before", old_requests_unchanged)

        def kind_swap():
            aid = new("Looks harmless", "true", "true")
            ch = options(aid)
            path = os.path.join(D["approvals"], aid + ".json")
            json.dump({"id": aid, "ts": "t", "requester": "merit-reviews", "rid": "r2", "verb": "container.inspect",
                       "resource": "x", "holder": "merit", "reason": "", "why_escalated": "w", "status": "pending"},
                      open(path, "w"))
            code, out = http("api/approve/finish", {"aid": aid, **enc(A.get(ch, rp))})
            assert code == 403 and "challenge" in out["error"], out
            assert json.load(open(path))["status"] == "pending"
            os.remove(path)
        check("e2e: a proposal's signature cannot be spent on a child request swapped into its file", kind_swap)
    finally:
        srv.terminate()
        srv.wait(10)


if __name__ == "__main__":
    try:
        unit_canonical()
        cli()
        helper_tests()
        e2e()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    bad = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} passed")
    sys.exit(1 if bad else 0)
