#!/usr/bin/env python3
"""resident home — the owner's screen: pending approvals (approved only with the owner's passkey), the family tree.

  python3 home.py [serve] [--port N]     listen on 127.0.0.1:8720 (mounted at https://<host>/resident/ by tailscale serve)
  python3 home.py enroll [--replace]     one-time 15-minute enrollment link for the owner's phone
  python3 home.py status                 enrollment state

State lives in $RESIDENT_HOME_STATE (default ~/resident/home): owner.json (the passkey's public key), enroll.json
(sha256 of the one-time token), audit.jsonl (every approval attempt). rp_id = $RESIDENT_HOME_RPID or the tailnet
DNS name. Only ES256 (P-256) passkeys with user verification are accepted.
"""
import base64, glob, hashlib, hmac, json, os, re, secrets, shlex, subprocess, sys, threading, time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))
import tree  # noqa: E402
import propose  # noqa: E402

STATE = os.environ.get("RESIDENT_HOME_STATE") or os.path.expanduser("~/resident/home")
OWNER_FILE = os.path.join(STATE, "owner.json")
ENROLL_FILE = os.path.join(STATE, "enroll.json")
AUDIT = os.path.join(STATE, "audit.jsonl")
PORT = 8720
CHALLENGE_TTL = 300
TOKEN_TTL = 900
OWNER = tree.OWNER
AID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
USED_PROPOSALS = os.path.join(STATE, "used_proposals")
OUTBOX = propose.OUTBOX
INBOX = os.environ.get("RESIDENT_INBOX") or os.path.join(
    os.path.expanduser(os.environ.get("RESIDENT_HOME") or "~/resident"), "inbox")
# how a root proposal is run: sudo the root-owned helper, which re-verifies the passkey signature itself.
# RESIDENT_ROOT_HELPER replaces this prefix in tests only (they cannot sudo).
ROOT_HELPER = os.environ.get("RESIDENT_ROOT_HELPER") or "sudo -n /usr/local/libexec/resident-root-apply"
APPLY_TIMEOUT = 420
FEED_N, FEED_CHARS, ASK_MAX = 15, 400, 2000

FLAG_UP, FLAG_UV, FLAG_AT, FLAG_ED = 0x01, 0x04, 0x40, 0x80


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def b64d(s):
    if not isinstance(s, str) or not re.fullmatch(r"[A-Za-z0-9_-]*={0,2}", s):
        raise VerifyError("bad base64url")
    return base64.urlsafe_b64decode(s.rstrip("=") + "=" * (-len(s.rstrip("=")) % 4))


def b64e(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def sha256(b):
    return hashlib.sha256(b).digest()


_rp = None


def rp_id():
    global _rp
    if os.environ.get("RESIDENT_HOME_RPID"):
        return os.environ["RESIDENT_HOME_RPID"]
    if _rp is None:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=10).stdout
        _rp = json.loads(out)["Self"]["DNSName"].rstrip(".")
    return _rp


def origin():
    return "https://" + rp_id()


# ------------------------------------------------------------------- CBOR (decode only; WebAuthn subset)

class CBORError(ValueError):
    pass


def cbor_decode(data, off=0, depth=0):
    """Decode one CBOR item at data[off:]; return (value, next_offset). Major types 0-5 and 7 (no tags)."""
    if depth > 16:
        raise CBORError("too deep")
    if off >= len(data):
        raise CBORError("truncated")
    ib = data[off]
    mt, ai = ib >> 5, ib & 0x1F
    off += 1

    def need(n):
        if off + n > len(data):
            raise CBORError("truncated")

    if ai < 24:
        arg = ai
    elif ai in (24, 25, 26, 27):
        n = 1 << (ai - 24)
        need(n)
        arg = int.from_bytes(data[off:off + n], "big")
        if mt == 7:
            raw = data[off:off + n]
        off += n
    else:
        raise CBORError(f"unsupported additional info {ai} (indefinite lengths not allowed)")

    if mt == 0:
        return arg, off
    if mt == 1:
        return -1 - arg, off
    if mt in (2, 3):
        need(arg)
        b = bytes(data[off:off + arg])
        off += arg
        if mt == 3:
            try:
                return b.decode("utf-8"), off
            except UnicodeDecodeError:
                raise CBORError("bad utf-8")
        return b, off
    if mt == 4:
        out = []
        for _ in range(arg):
            v, off = cbor_decode(data, off, depth + 1)
            out.append(v)
        return out, off
    if mt == 5:
        out = {}
        for _ in range(arg):
            k, off = cbor_decode(data, off, depth + 1)
            if isinstance(k, (list, dict)):
                raise CBORError("unhashable map key")
            v, off = cbor_decode(data, off, depth + 1)
            if k in out:
                raise CBORError("duplicate map key")
            out[k] = v
        return out, off
    if mt == 7:
        if ai < 24 or ai == 24:
            simple = {20: False, 21: True, 22: None, 23: None}
            if arg in simple:
                return simple[arg], off
            raise CBORError(f"unsupported simple value {arg}")
        import struct
        if ai == 25:
            return struct.unpack(">e", raw)[0], off
        if ai == 26:
            return struct.unpack(">f", raw)[0], off
        return struct.unpack(">d", raw)[0], off
    raise CBORError(f"unsupported major type {mt}")


# ------------------------------------------------------------------- WebAuthn verification

class VerifyError(Exception):
    pass


def parse_auth_data(ad):
    if len(ad) < 37:
        raise VerifyError("authenticatorData too short")
    out = {"rp_id_hash": ad[:32], "flags": ad[32], "sign_count": int.from_bytes(ad[33:37], "big")}
    off = 37
    if out["flags"] & FLAG_AT:
        if len(ad) < off + 18:
            raise VerifyError("attested credential data truncated")
        out["aaguid"] = ad[off:off + 16]
        n = int.from_bytes(ad[off + 16:off + 18], "big")
        off += 18
        if n == 0 or n > 1023 or len(ad) < off + n:
            raise VerifyError("bad credential id length")
        out["cred_id"] = ad[off:off + n]
        off += n
        try:
            out["cose"], off = cbor_decode(ad, off)
        except CBORError as e:
            raise VerifyError(f"COSE key: {e}")
    if out["flags"] & FLAG_ED:
        try:
            out["extensions"], off = cbor_decode(ad, off)
        except CBORError as e:
            raise VerifyError(f"extensions: {e}")
    if off != len(ad):
        raise VerifyError("trailing bytes in authenticatorData")
    return out


def check_client_data(cdj, typ, expected_challenge, rpid):
    try:
        cd = json.loads(cdj.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise VerifyError("clientDataJSON is not JSON")
    if not isinstance(cd, dict) or cd.get("type") != typ:
        raise VerifyError(f"clientData.type is not {typ}")
    try:
        got = b64d(cd.get("challenge", ""))
    except (VerifyError, ValueError):
        raise VerifyError("clientData.challenge malformed")
    if not expected_challenge or not hmac.compare_digest(got, expected_challenge):
        raise VerifyError("challenge mismatch")
    if cd.get("origin") != "https://" + rpid:
        raise VerifyError(f"origin mismatch ({cd.get('origin')!r})")
    if cd.get("crossOrigin") is True:
        raise VerifyError("crossOrigin")
    return cd


def check_rp_and_flags(auth, rpid, need):
    if not hmac.compare_digest(auth["rp_id_hash"], sha256(rpid.encode())):
        raise VerifyError("rpIdHash mismatch")
    for bit, name in ((FLAG_UP, "UP"), (FLAG_UV, "UV"), (FLAG_AT, "AT")):
        if need & bit and not auth["flags"] & bit:
            raise VerifyError(f"flag {name} not set")


def public_key(x, y):
    try:
        return ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"), int.from_bytes(y, "big"),
                                             ec.SECP256R1()).public_key()
    except ValueError:
        raise VerifyError("public key is not a P-256 point")


def verify_registration(rpid, expected_challenge, raw_id, cdj, att_obj):
    """Verify a navigator.credentials.create() response (attestation 'none'). Returns the credential record."""
    check_client_data(cdj, "webauthn.create", expected_challenge, rpid)
    try:
        att, end = cbor_decode(att_obj)
    except CBORError as e:
        raise VerifyError(f"attestationObject: {e}")
    if end != len(att_obj) or not isinstance(att, dict) or not isinstance(att.get("authData"), bytes):
        raise VerifyError("attestationObject has no authData")
    auth = parse_auth_data(att["authData"])
    check_rp_and_flags(auth, rpid, FLAG_UP | FLAG_UV | FLAG_AT)
    k = auth.get("cose")
    if not isinstance(k, dict):
        raise VerifyError("COSE key is not a map")
    if k.get(1) != 2:
        raise VerifyError("COSE kty is not EC2")
    if k.get(3) != -7:
        raise VerifyError(f"COSE alg {k.get(3)} is not ES256 (-7)")
    if k.get(-1) != 1:
        raise VerifyError("COSE crv is not P-256")
    x, y = k.get(-2), k.get(-3)
    if not (isinstance(x, bytes) and isinstance(y, bytes) and len(x) == 32 and len(y) == 32):
        raise VerifyError("COSE x/y are not 32 bytes")
    public_key(x, y)
    if not hmac.compare_digest(auth["cred_id"], raw_id):
        raise VerifyError("credential id does not match rawId")
    return {"cred_id": b64e(auth["cred_id"]), "x": b64e(x), "y": b64e(y), "alg": -7,
            "sign_count": auth["sign_count"], "created": now(), "aaguid": auth["aaguid"].hex(),
            "fmt": att.get("fmt")}


def verify_assertion(rpid, cred, expected_challenge, raw_id, cdj, auth_data, signature):
    """Verify a navigator.credentials.get() response against the stored credential. Returns the new sign count."""
    if not hmac.compare_digest(raw_id, b64d(cred["cred_id"])):
        raise VerifyError("unknown credential id")
    check_client_data(cdj, "webauthn.get", expected_challenge, rpid)
    auth = parse_auth_data(auth_data)
    check_rp_and_flags(auth, rpid, FLAG_UP | FLAG_UV)
    pub = public_key(b64d(cred["x"]), b64d(cred["y"]))
    try:
        pub.verify(signature, auth_data + sha256(cdj), ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError):
        raise VerifyError("bad signature")
    new, old = auth["sign_count"], int(cred.get("sign_count", 0))
    if new and old and new <= old:
        raise VerifyError(f"sign count did not increase ({new} <= {old}): possible cloned authenticator")
    return new


class Challenges:
    """Server-side challenges: 32 random bytes, bound to (kind, key), expiring, single use."""

    def __init__(self, ttl=CHALLENGE_TTL):
        self.ttl, self.lock, self.items = ttl, threading.Lock(), {}

    def issue(self, kind, key, challenge=None, **extra):
        """challenge=None → 32 random bytes; a proposal passes sha256(canonical) so the signature commits to it."""
        c = challenge if challenge is not None else secrets.token_bytes(32)
        with self.lock:
            t = time.time()
            self.items = {k: v for k, v in self.items.items() if v["exp"] > t}
            self.items[(kind, key)] = {"challenge": c, "exp": t + self.ttl, **extra}
        return c

    def take(self, kind, key):
        """Remove and return the live challenge record for (kind, key), or None. A second take gets None."""
        with self.lock:
            v = self.items.pop((kind, key), None)
        return v if v and v["exp"] > time.time() else None


# ------------------------------------------------------------------- state files

def write_private(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(obj, fh, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load(path):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return None


def audit(rec):
    rec = {"ts": now(), **rec}
    os.makedirs(STATE, exist_ok=True)
    fd = os.open(AUDIT, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def new_enroll_token(replace=False):
    tok = secrets.token_urlsafe(24)
    write_private(ENROLL_FILE, {"sha256": hashlib.sha256(tok.encode()).hexdigest(), "expires": time.time() + TOKEN_TTL,
                                "replace": bool(replace), "created": now()})
    return tok


def token_ok(tok):
    e = load(ENROLL_FILE)
    if not e or not isinstance(tok, str) or not tok:
        return None
    if not hmac.compare_digest(hashlib.sha256(tok.encode()).hexdigest(), e.get("sha256", "")):
        return None
    if time.time() > e.get("expires", 0):
        return None
    if os.path.exists(OWNER_FILE) and not e.get("replace"):
        return None
    return e


# ------------------------------------------------------------------- views

_tree_cache = {"t": 0, "text": ""}
_tree_lock = threading.Lock()


def tree_cached(ttl=20):
    with _tree_lock:
        if time.time() - _tree_cache["t"] > ttl:
            try:
                _tree_cache["text"] = tree.tree_text(compact=False)
            except Exception as e:  # the page must render even if docker is slow
                _tree_cache["text"] = f"(tree unavailable: {e!r})"
            _tree_cache["t"] = time.time()
        return _tree_cache["text"]


def lineage(name):
    try:
        return list(reversed([name] + tree.ancestors(name)))
    except (KeyError, TypeError):
        return [name]


def feed(n=FEED_N):
    """The last n outbox messages, oldest first: what the agent (and the approval loop) said to the owner."""
    files = []
    for f in glob.glob(os.path.join(OUTBOX, "*.md")):
        try:
            files.append((os.stat(f).st_mtime, os.path.basename(f), f))
        except OSError:
            pass
    out = []
    for mt, name, f in sorted(files)[-n:]:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                text = fh.read(8192)
        except OSError:
            continue
        text = re.split(r"\n---\n_agentd ", text, maxsplit=1)[0].strip()
        stem = name[:-3]
        out.append({"ts": datetime.fromtimestamp(mt, timezone.utc).isoformat(timespec="seconds"),
                    "kind": stem.split("-", 1)[1] if "-" in stem else "",
                    "text": text[:FEED_CHARS] + ("…" if len(text) > FEED_CHARS else "")})
    return out


def state():
    pend = []
    for a in tree.pending():
        if propose.is_proposal(a):
            pend.append({"kind": "proposal"} | {k: a.get(k, "") for k in ("id", "ts", "requester", "summary", "why",
                                                                           "verify", "command")}
                        | {"root": a.get("root") is True, "valid": not propose.problems(a)})
            continue
        pend.append({"kind": "request"} | {k: a.get(k, "") for k in ("id", "ts", "requester", "verb", "resource",
                                                                       "holder", "reason", "why_escalated")}
                    | {"lineage": lineage(a.get("requester", ""))})
    return {"enrolled": os.path.exists(OWNER_FILE), "owner": OWNER, "rp_id": rp_id(), "pending": pend,
            "feed": feed(), "tree": tree_cached()}


# ------------------------------------------------------------------- proposals (the agent asks to change something)

def proposal_path(aid):
    return os.path.join(tree.APPROVALS, aid + ".json")


def load_proposal(aid):
    p = load(proposal_path(aid))
    if not propose.is_proposal(p) or p.get("id") != aid:
        return None
    return p


def used_proposal(key, record=False):
    """home's own replay list (id:nonce). The root helper keeps its own, root-owned one."""
    os.makedirs(STATE, exist_ok=True)
    fd = os.open(USED_PROPOSALS, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "r+") as fh:
        seen = key in {x.strip() for x in fh}
        if record and not seen:
            fh.write(key + "\n")
        return seen


def apply_argv(p, path, iid):
    """The `intent apply` command line for an approved proposal (root → the sudo'ed, self-verifying helper)."""
    inner = f"{ROOT_HELPER} {shlex.quote(os.path.abspath(path))}" if p["root"] else p["command"]
    return [tree.INTENT, "apply", str(iid), "--verify", p["verify"], "--", inner]


def first_error(out, exit_code, verify_ok, verify):
    if exit_code == 0 and not verify_ok:
        return f"the check did not pass ({verify})"
    lines = [x.strip() for x in out.splitlines() if x.strip() and not x.startswith(("[intent #", "resident-root-apply: proposal "))
             and not re.match(r"\s+\S+\s+(added|removed|modified)\s", x)]
    for x in lines:
        if re.search(r"error|fail|refused|denied|not found|no such|cannot|permission|password", x, re.I):
            return x[:200]
    return lines[0][:200] if lines else f"exit {exit_code}, check {'passed' if verify_ok else 'failed'}"


def execute_proposal(path, p):
    """Run an approved proposal through `intent apply` (fingerprint before/after, verify) and record the result."""
    env = dict(os.environ, RESIDENT_AUTHOR=OWNER)
    env["PATH"] = os.path.expanduser("~/.local/bin") + ":" + env.get("PATH", "/usr/bin:/bin")
    for k in ("CLAUDECODE", "CLAUDE_CODE"):
        env.pop(k, None)
    res = {"intent": None, "exit": None, "verify_ok": False, "output": ""}
    try:
        r = subprocess.run([tree.INTENT, "add", f"{p['summary']} — {p['why']} (approved by passkey)"], env=env,
                           capture_output=True, text=True, timeout=60)
        iid = r.stdout.strip()
        if r.returncode or not iid.isdigit():
            raise RuntimeError(f"intent add failed: {(r.stderr or r.stdout).strip()[:300]}")
        res["intent"] = int(iid)
        argv = apply_argv(p, path, iid)
        res["apply"] = shlex.join(argv[1:])
        try:
            r = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, errors="replace", timeout=APPLY_TIMEOUT,
                               cwd=os.path.expanduser("~"))
            out, rc = r.stdout, r.returncode
        except subprocess.TimeoutExpired as ex:
            out, rc = (ex.stdout or b"").decode("utf-8", "replace") if isinstance(ex.stdout, bytes) else (ex.stdout or ""), 124
            out += f"\n(timed out after {APPLY_TIMEOUT}s)"
        m = re.search(r"\[intent #\d+\] exit=(-?\d+) changes=(\d+) verify=(PASS|FAIL|none)", out)
        res.update(exit=int(m.group(1)) if m else rc, verify_ok=bool(m and m.group(3) == "PASS"),
                   changes=int(m.group(2)) if m else None, output=out[-3000:])
        ok = rc == 0 and res["exit"] == 0 and res["verify_ok"]
    except Exception as ex:  # noqa: BLE001 — record any failure in the file, never leave it 'approved' silently
        res["output"] = (res["output"] + f"\n{ex!r}")[-3000:]
        ok = False
    res["status"] = "done" if ok else "failed"
    res["finished_at"] = now()
    p = dict(p)  # record against the verified in-memory proposal, never whatever the file says now
    p.update(status=res["status"], exit=res["exit"], verify_ok=res["verify_ok"], output=res["output"],
             intent=res["intent"], finished_at=res["finished_at"])
    propose.write_json(path, p)
    if ok:
        msg = f"Done: {p['summary']}. Verified."
    else:
        msg = f"Failed: {p['summary']} — {first_error(res['output'], res['exit'], res['verify_ok'], p['verify'])}"
    res["message"] = msg
    propose.outbox("result", msg)
    return p, res


def ask(text, who):
    """The owner's message to the resident agent: an inbox event (agentd answers into the outbox)."""
    os.makedirs(INBOX, exist_ok=True)
    ms = int(time.time() * 1000)
    ev = {"ts": now(), "kind": "ask", "summary": text[:80], "detail": text}
    while os.path.exists(os.path.join(INBOX, f"{ms}-ask.json")):
        ms += 1
    name = f"{ms}-ask.json"
    tmp = os.path.join(INBOX, f".{name}.tmp")
    with open(tmp, "w") as fh:
        json.dump(ev, fh)
    os.replace(tmp, os.path.join(INBOX, name))
    audit({"event": "ask", "chars": len(text), "file": name, "tailscale_user": who})
    return name


CH = Challenges()
OWNER_LOCK = threading.Lock()


def api(path, body, hdr):
    """Returns (status, json). body is the parsed JSON object."""
    who = hdr.get("Tailscale-User-Login", "")
    if path == "/api/enroll/options":
        e = token_ok(body.get("token"))
        if not e:
            return 403, {"error": "enrollment link is invalid, expired, or already used (run: python3 home.py enroll)"}
        uid = secrets.token_bytes(16)
        c = CH.issue("enroll", e["sha256"], user_id=uid)
        return 200, {"publicKey": {
            "rp": {"id": rp_id(), "name": "Resident"},
            "user": {"id": b64e(uid), "name": OWNER, "displayName": OWNER},
            "challenge": b64e(c),
            "pubKeyCredParams": [{"type": "public-key", "alg": -7}],
            "authenticatorSelection": {"residentKey": "preferred", "userVerification": "required"},
            "attestation": "none", "timeout": 120000}}
    if path == "/api/enroll/finish":
        e = token_ok(body.get("token"))
        if not e:
            audit({"event": "enroll", "ok": False, "reason": "bad token", "tailscale_user": who})
            return 403, {"error": "enrollment link is invalid, expired, or already used"}
        ch = CH.take("enroll", e["sha256"])
        try:
            if not ch:
                raise VerifyError("no live challenge (expired or already used)")
            cred = verify_registration(rp_id(), ch["challenge"], b64d(body.get("rawId", "")),
                                       b64d(body.get("clientDataJSON", "")), b64d(body.get("attestationObject", "")))
        except (VerifyError, ValueError) as ex:
            audit({"event": "enroll", "ok": False, "reason": str(ex), "tailscale_user": who})
            return 400, {"error": f"enrollment rejected: {ex}"}
        cred["user_id"] = b64e(ch["user_id"])
        cred["owner"] = OWNER
        with OWNER_LOCK:
            if os.path.exists(OWNER_FILE) and not e.get("replace"):
                return 409, {"error": "already enrolled"}
            write_private(OWNER_FILE, cred)
            try:
                os.remove(ENROLL_FILE)
            except FileNotFoundError:
                pass
        audit({"event": "enroll", "ok": True, "cred_id": cred["cred_id"], "aaguid": cred["aaguid"],
               "replace": bool(e.get("replace")), "tailscale_user": who})
        return 200, {"ok": True, "enrolled": True}

    if path == "/api/ask":
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return 400, {"error": "text required"}
        if len(text) > ASK_MAX:
            return 413, {"error": f"at most {ASK_MAX} characters"}
        return 200, {"ok": True, "event": ask(text.strip(), who)}

    aid = body.get("aid")
    if not isinstance(aid, str) or not AID_RE.fullmatch(aid):
        return 400, {"error": "bad aid"}
    prop = load_proposal(aid)
    if prop is not None and path in ("/api/approve/options", "/api/approve/finish"):
        return api_proposal(path, aid, body, who)
    is_pending = any(a["id"] == aid for a in tree.pending())
    if path == "/api/approve/options":
        cred = load(OWNER_FILE)
        if not cred:
            return 409, {"error": "no passkey enrolled yet"}
        if not is_pending:
            return 404, {"error": f"{aid} is not pending"}
        c = CH.issue("approve", aid)
        return 200, {"publicKey": {"rpId": rp_id(), "challenge": b64e(c),
                                   "allowCredentials": [{"type": "public-key", "id": cred["cred_id"]}],
                                   "userVerification": "required", "timeout": 120000}}
    if path == "/api/approve/finish":
        ch = CH.take("approve", aid)
        try:
            with OWNER_LOCK:
                cred = load(OWNER_FILE)
                if not cred:
                    raise VerifyError("no passkey enrolled")
                if not ch:
                    raise VerifyError("no live challenge for this approval (expired, used, or never issued)")
                if not is_pending:
                    raise VerifyError("not pending")
                n = verify_assertion(rp_id(), cred, ch["challenge"], b64d(body.get("rawId", "")),
                                     b64d(body.get("clientDataJSON", "")), b64d(body.get("authenticatorData", "")),
                                     b64d(body.get("signature", "")))
                cred["sign_count"] = n
                cred["last_used"] = now()
                write_private(OWNER_FILE, cred)
        except (VerifyError, ValueError) as ex:
            audit({"event": "approve", "aid": aid, "ok": False, "reason": str(ex), "tailscale_user": who})
            return 403, {"error": f"approval rejected: {ex}"}
        try:
            a, resp = tree.decide(aid, True, who=f"{OWNER} (passkey)", passkey=True)
        except SystemExit as ex:
            audit({"event": "approve", "aid": aid, "ok": False, "reason": f"decide: {ex}", "tailscale_user": who})
            return 409, {"error": str(ex)}
        audit({"event": "approve", "aid": aid, "ok": True, "verb": a["verb"], "resource": a["resource"],
               "result": resp.get("status"), "tailscale_user": who})
        _tree_cache["t"] = 0
        return 200, {"approval": a, "response": resp}
    if path == "/api/deny":
        try:
            a, resp = tree.decide(aid, False, who=f"{OWNER} (home page)")
        except SystemExit as ex:
            return 409, {"error": str(ex)}
        audit({"event": "deny", "aid": aid, "ok": True, "tailscale_user": who})
        _tree_cache["t"] = 0
        return 200, {"approval": a, "response": resp}
    return 404, {"error": "no such endpoint"}


def api_proposal(path, aid, body, who):
    if path == "/api/approve/options":
        cred = load(OWNER_FILE)
        if not cred:
            return 409, {"error": "no passkey enrolled yet"}
        p = load_proposal(aid)
        if not p or p.get("status") != "pending":
            return 404, {"error": f"{aid} is not pending"}
        bad = propose.problems(p)
        if bad:
            return 400, {"error": "malformed proposal: " + "; ".join(bad)}
        # its own challenge kind: a proposal's signature can never be spent on a child request (or vice versa)
        c = CH.issue("approve-proposal", aid, challenge=propose.challenge(p))
        return 200, {"publicKey": {"rpId": rp_id(), "challenge": b64e(c),
                                   "allowCredentials": [{"type": "public-key", "id": cred["cred_id"]}],
                                   "userVerification": "required", "timeout": 120000}}
    # finish
    ch = CH.take("approve-proposal", aid)
    path_ = proposal_path(aid)
    try:
        with OWNER_LOCK:
            cred = load(OWNER_FILE)
            if not cred:
                raise VerifyError("no passkey enrolled")
            if not ch:
                raise VerifyError("no live challenge for this approval (expired, used, or never issued)")
            p = load_proposal(aid)  # the CURRENT file: what runs is what was signed, or nothing runs
            if not p:
                raise VerifyError("proposal disappeared")
            if p.get("status") != "pending":
                raise VerifyError(f"not pending (status {p.get('status')!r})")
            bad = propose.problems(p)
            if bad:
                raise VerifyError("malformed proposal: " + "; ".join(bad))
            cur = propose.challenge(p)
            if not hmac.compare_digest(ch["challenge"], cur):
                raise VerifyError("the proposal changed after approval started (challenge mismatch); review it again")
            key = f"{aid}:{p['nonce']}"
            if used_proposal(key):
                raise VerifyError("replay: this proposal was already approved once")
            raw, cdj, ad, sig = (b64d(body.get(k, "")) for k in ("rawId", "clientDataJSON", "authenticatorData",
                                                                    "signature"))
            n = verify_assertion(rp_id(), cred, cur, raw, cdj, ad, sig)
            cred["sign_count"] = n
            cred["last_used"] = now()
            write_private(OWNER_FILE, cred)
            used_proposal(key, record=True)
            p.update(status="approved", approved_by=f"{OWNER} (passkey)", approved_at=now(),
                     assertion={"rawId": b64e(raw), "clientDataJSON": b64e(cdj), "authenticatorData": b64e(ad),
                                "signature": b64e(sig)})
            propose.write_json(path_, p)
    except (VerifyError, ValueError) as ex:
        audit({"event": "approve", "aid": aid, "kind": "proposal", "ok": False, "reason": str(ex),
               "tailscale_user": who})
        return 403, {"error": f"approval rejected: {ex}"}
    audit({"event": "approve", "aid": aid, "kind": "proposal", "ok": True, "root": p["root"],
           "command_sha256": hashlib.sha256(p["command"].encode()).hexdigest()[:16], "tailscale_user": who})
    p, res = execute_proposal(path_, p)
    audit({"event": "proposal-result", "aid": aid, "status": res["status"], "exit": res["exit"],
           "verify_ok": res["verify_ok"], "intent": res["intent"]})
    _tree_cache["t"] = 0
    return 200, {"approval": {k: v for k, v in p.items() if k != "assertion"}, "result": res,
                 "response": {"status": res["status"]}}


PAGE = open(os.path.join(os.path.dirname(os.path.realpath(__file__)), "page.html")).read() \
    if os.path.exists(os.path.join(os.path.dirname(os.path.realpath(__file__)), "page.html")) else ""


class Handler(BaseHTTPRequestHandler):
    server_version = "resident-home"
    sys_version = ""

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def route(self):
        p = self.path.split("?", 1)[0].split("#", 1)[0]
        if p == "/resident" or p.startswith("/resident/"):
            p = p[len("/resident"):]
        return p or "/"

    def send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def origin_ok(self):
        o = self.headers.get("Origin")
        return o is None or o == origin()

    def do_GET(self):
        if not self.origin_ok():
            return self.send(403, {"error": "bad origin"})
        p = self.route()
        if p in ("/", "/index.html"):
            nonce = secrets.token_urlsafe(16)
            csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; connect-src 'self'; "
                   "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            return self.send(200, PAGE.replace("__NONCE__", nonce).encode(), "text/html; charset=utf-8",
                             {"Content-Security-Policy": csp})
        if p == "/api/state":
            return self.send(200, state())
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.origin_ok():
            return self.send(403, {"error": "bad origin"})
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return self.send(415, {"error": "Content-Type must be application/json"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = -1
        if n < 0 or n > 65536:
            return self.send(413, {"error": "body too large"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self.send(400, {"error": "body must be a JSON object"})
        try:
            code, out = api(self.route(), body, self.headers)
        except Exception as ex:
            code, out = 500, {"error": f"internal error: {ex!r}"}
        self.send(code, out)


def main(argv):
    cmd = argv[0] if argv and not argv[0].startswith("-") else "serve"
    if cmd == "enroll":
        replace = "--replace" in argv
        if os.path.exists(OWNER_FILE) and not replace:
            sys.exit("home: a passkey is already enrolled; use `enroll --replace` to replace it")
        tok = new_enroll_token(replace)
        print(f"https://{rp_id()}/resident/?enroll={tok}")
        print(f"(one-time link, expires in {TOKEN_TTL // 60} minutes; open it on the owner's phone)", file=sys.stderr)
        return 0
    if cmd == "status":
        c = load(OWNER_FILE)
        e = load(ENROLL_FILE)
        print(f"owner: {OWNER}\nrp_id: {rp_id()}\nstate: {STATE}")
        print(f"enrolled: {'yes, since ' + c['created'] + ' (cred ' + c['cred_id'][:12] + '…, sign_count ' + str(c['sign_count']) + ')' if c else 'no'}")
        if e:
            left = e["expires"] - time.time()
            print(f"enrollment token: {'live, ' + str(int(left // 60)) + ' min left' if left > 0 else 'expired'}"
                  + (" (replace)" if e.get("replace") else ""))
        return 0
    if cmd == "serve":
        port = int(argv[argv.index("--port") + 1]) if "--port" in argv else int(os.environ.get("RESIDENT_HOME_PORT", PORT))
        rp_id()
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        srv.daemon_threads = True
        print(f"resident home on http://127.0.0.1:{port}/ (rp_id {rp_id()}, state {STATE})", flush=True)
        srv.serve_forever()
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
