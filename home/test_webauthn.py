#!/usr/bin/env python3
"""Tests for home.py: a software WebAuthn authenticator against the real verification code, then end to end over HTTP.

  python3 -I test_webauthn.py            unit + end-to-end (creates two read-only container.inspect requests)
  python3 -I test_webauthn.py --unit     unit tests only

The end-to-end part runs its own server with RESIDENT_HOME_STATE=<temp dir>, so the real owner.json is never created.
"""
import hashlib, json, os, shutil, subprocess, sys, tempfile, time, urllib.error, urllib.request
OWNER_NAME = os.environ.get("RESIDENT_OWNER") or __import__("getpass").getuser()

HERE = os.path.dirname(os.path.realpath(__file__))
TMP = tempfile.mkdtemp(prefix="resident-home-test-")
os.environ["RESIDENT_HOME_STATE"] = TMP
sys.path.insert(0, HERE)
import home  # noqa: E402
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

b64e, b64d, sha256 = home.b64e, home.b64d, home.sha256
RP = "test.example.ts.net"
RESULTS = []


def check(name, fn):
    try:
        fn()
        RESULTS.append((name, True, ""))
        print(f"  PASS  {name}")
    except Exception as e:  # noqa: BLE001
        RESULTS.append((name, False, repr(e)))
        print(f"  FAIL  {name}: {e!r}")


def must_fail(fn, contains):
    try:
        fn()
    except home.VerifyError as e:
        assert contains in str(e), f"rejected, but for the wrong reason: {e}"
        return
    raise AssertionError("was accepted")


# ------------------------------------------------------------------- a minimal CBOR encoder (test side only)

def cbor(v):
    def head(mt, n):
        if n < 24:
            return bytes([mt << 5 | n])
        for ai, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
            if n < 1 << (8 * size):
                return bytes([mt << 5 | ai]) + n.to_bytes(size, "big")
        raise ValueError
    if v is False: return b"\xf4"
    if v is True: return b"\xf5"
    if v is None: return b"\xf6"
    if isinstance(v, int): return head(0, v) if v >= 0 else head(1, -1 - v)
    if isinstance(v, bytes): return head(2, len(v)) + v
    if isinstance(v, str): return head(3, len(v.encode())) + v.encode()
    if isinstance(v, list): return head(4, len(v)) + b"".join(cbor(x) for x in v)
    if isinstance(v, dict): return head(5, len(v)) + b"".join(cbor(k) + cbor(x) for k, x in v.items())
    raise TypeError(type(v))


class SoftAuthenticator:
    """A P-256 platform authenticator in software: what a phone's secure enclave does, minus the enclave."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(32)
        self.counter = 0

    def cose(self, alg=-7):
        n = self.key.public_key().public_numbers()
        return cbor({1: 2, 3: alg, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

    def auth_data(self, rpid, flags, attested=False, alg=-7):
        ad = sha256(rpid.encode()) + bytes([flags]) + self.counter.to_bytes(4, "big")
        if attested:
            ad += b"\x00" * 16 + len(self.cred_id).to_bytes(2, "big") + self.cred_id + self.cose(alg)
        return ad

    @staticmethod
    def client_data(typ, challenge, origin):
        return json.dumps({"type": typ, "challenge": b64e(challenge), "origin": origin, "crossOrigin": False}).encode()

    def create(self, challenge, rpid, origin=None, flags=0x45, alg=-7, cred_id_override=None):
        cdj = self.client_data("webauthn.create", challenge, origin or "https://" + rpid)
        att = cbor({"fmt": "none", "attStmt": {}, "authData": self.auth_data(rpid, flags, True, alg)})
        return {"rawId": cred_id_override or self.cred_id, "clientDataJSON": cdj, "attestationObject": att}

    def get(self, challenge, rpid, origin=None, flags=0x05, bump=1):
        self.counter += bump
        ad = self.auth_data(rpid, flags)
        cdj = self.client_data("webauthn.get", challenge, origin or "https://" + rpid)
        sig = self.key.sign(ad + sha256(cdj), ec.ECDSA(hashes.SHA256()))
        return {"rawId": self.cred_id, "clientDataJSON": cdj, "authenticatorData": ad, "signature": sig}


def reg(r, rp, chal):
    return home.verify_registration(rp, chal, r["rawId"], r["clientDataJSON"], r["attestationObject"])


def asrt(a, cred, rp, chal):
    return home.verify_assertion(rp, cred, chal, a["rawId"], a["clientDataJSON"], a["authenticatorData"], a["signature"])


# ------------------------------------------------------------------- unit tests

def unit():
    print("unit: CBOR")
    vectors = [("00", 0), ("17", 23), ("1818", 24), ("1a000f4240", 1000000), ("1bffffffffffffffff", 2**64 - 1),
               ("20", -1), ("3903e7", -1000), ("4401020304", b"\x01\x02\x03\x04"), ("6449455446", "IETF"),
               ("8301820203820405", [1, [2, 3], [4, 5]]), ("a26161016162820203", {"a": 1, "b": [2, 3]}),
               ("f4", False), ("f5", True), ("f6", None), ("f93e00", 1.5), ("fa47c35000", 100000.0),
               ("fb3ff199999999999a", 1.1), ("590003616263", b"abc"), ("7a00000003616263", "abc")]
    def cbor_vectors():
        for hx, want in vectors:
            got, end = home.cbor_decode(bytes.fromhex(hx))
            assert got == want and end == len(hx) // 2, (hx, got, want)
    check("CBOR RFC 8949 vectors decode with correct next offset", cbor_vectors)
    def cbor_bad():
        for hx in ("5f", "9f", "1c", "62ff", "c0", "41"):
            try:
                home.cbor_decode(bytes.fromhex(hx))
                raise AssertionError(f"accepted {hx}")
            except home.CBORError:
                pass
    check("CBOR rejects indefinite lengths, tags, truncation", cbor_bad)

    print("unit: registration")
    A = SoftAuthenticator()
    chal = os.urandom(32)
    state = {}
    def ok_reg():
        cred = reg(A.create(chal, RP), RP, chal)
        assert cred["alg"] == -7 and b64d(cred["cred_id"]) == A.cred_id and len(b64d(cred["x"])) == 32
        state["cred"] = cred
    check("valid enrollment passes", ok_reg)
    check("enroll: wrong origin fails", lambda: must_fail(lambda: reg(A.create(chal, RP, origin="https://evil.example"), RP, chal), "origin"))
    check("enroll: wrong challenge fails", lambda: must_fail(lambda: reg(A.create(os.urandom(32), RP), RP, chal), "challenge"))
    check("enroll: wrong rpId (rpIdHash) fails", lambda: must_fail(lambda: reg(A.create(chal, "evil.example", origin="https://" + RP), RP, chal), "rpIdHash"))
    check("enroll: missing UV fails", lambda: must_fail(lambda: reg(A.create(chal, RP, flags=0x41), RP, chal), "UV"))
    check("enroll: missing UP fails", lambda: must_fail(lambda: reg(A.create(chal, RP, flags=0x44), RP, chal), "UP"))
    check("enroll: wrong alg (-257) fails", lambda: must_fail(lambda: reg(A.create(chal, RP, alg=-257), RP, chal), "alg"))
    check("enroll: rawId != credId fails", lambda: must_fail(lambda: reg(A.create(chal, RP, cred_id_override=os.urandom(32)), RP, chal), "rawId"))
    def wrong_type():
        r = A.create(chal, RP)
        r["clientDataJSON"] = SoftAuthenticator.client_data("webauthn.get", chal, "https://" + RP)
        reg(r, RP, chal)
    check("enroll: clientData.type webauthn.get fails", lambda: must_fail(wrong_type, "type"))

    print("unit: assertion")
    store = home.Challenges()
    def ok_assert():
        c = store.issue("approve", "aaa111")
        n = asrt(A.get(c, RP), state["cred"], RP, store.take("approve", "aaa111")["challenge"])
        assert n == A.counter
        state["cred"]["sign_count"] = n
    check("valid assertion passes", ok_assert)
    def tampered():
        c = os.urandom(32)
        a = A.get(c, RP)
        sig = bytearray(a["signature"]); sig[-3] ^= 0x01; a["signature"] = bytes(sig)
        asrt(a, state["cred"], RP, c)
    check("tampered signature fails", lambda: must_fail(tampered, "signature"))
    def tampered_ad():
        c = os.urandom(32)
        a = A.get(c, RP)
        a["authenticatorData"] = a["authenticatorData"][:33] + (A.counter + 5).to_bytes(4, "big")
        asrt(a, state["cred"], RP, c)
    check("tampered authenticatorData (counter) fails", lambda: must_fail(tampered_ad, "signature"))
    def reused():
        c = store.issue("approve", "aaa111")
        assert store.take("approve", "aaa111")
        rec = store.take("approve", "aaa111")
        assert rec is None, "challenge could be taken twice"
        # and an assertion over a used challenge fails against whatever is live now
        c2 = store.issue("approve", "aaa111")
        asrt(A.get(c, RP), state["cred"], RP, store.take("approve", "aaa111")["challenge"])
    check("reused challenge fails", lambda: must_fail(reused, "challenge"))
    def other_aid():
        ca = store.issue("approve", "aaa111")
        store.issue("approve", "bbb222")
        asrt(A.get(ca, RP), state["cred"], RP, store.take("approve", "bbb222")["challenge"])
    check("challenge issued for a different aid fails", lambda: must_fail(other_aid, "challenge"))
    def wrong_cred():
        B = SoftAuthenticator()
        c = os.urandom(32)
        asrt(B.get(c, RP), state["cred"], RP, c)
    check("wrong credential id fails", lambda: must_fail(wrong_cred, "credential"))
    def same_id_other_key():
        B = SoftAuthenticator(); B.cred_id = A.cred_id; B.counter = A.counter
        c = os.urandom(32)
        asrt(B.get(c, RP), state["cred"], RP, c)
    check("right credential id, wrong key fails", lambda: must_fail(same_id_other_key, "signature"))
    def no_uv():
        c = os.urandom(32)
        asrt(A.get(c, RP, flags=0x01), state["cred"], RP, c)
    check("assertion without UV fails", lambda: must_fail(no_uv, "UV"))
    def wrong_origin():
        c = os.urandom(32)
        asrt(A.get(c, RP, origin="https://evil.example"), state["cred"], RP, c)
    check("assertion from wrong origin fails", lambda: must_fail(wrong_origin, "origin"))
    def wrong_rp():
        c = os.urandom(32)
        asrt(A.get(c, "evil.example", origin="https://" + RP), state["cred"], RP, c)
    check("assertion with wrong rpIdHash fails", lambda: must_fail(wrong_rp, "rpIdHash"))
    def counter_back():
        state["cred"]["sign_count"] = A.counter
        c = os.urandom(32)
        asrt(A.get(c, RP, bump=-1), state["cred"], RP, c)
    check("sign count going backwards fails", lambda: must_fail(counter_back, "sign count"))
    def expired():
        s = home.Challenges(ttl=0)
        s.issue("approve", "x")
        time.sleep(0.01)
        assert s.take("approve", "x") is None
    check("expired challenge is not returned", expired)


# ------------------------------------------------------------------- end to end

PORT = 8721


def http(path, body=None, origin=True, ctype="application/json"):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/resident/{path}",
                                 data=None if body is None else json.dumps(body).encode(), method="GET" if body is None else "POST")
    if body is not None:
        req.add_header("Content-Type", ctype)
    if origin:
        req.add_header("Origin", origin if isinstance(origin, str) else "https://" + home.rp_id())
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def make_request():
    before = {a["id"] for a in home.tree.pending()}
    r = subprocess.run(["docker", "exec", "merit-reviews", "resident", "request", "container.inspect",
                        "shop-app-dashboard-1", "--reason", "resident-home e2e test", "--wait", "5"],
                       capture_output=True, text=True, timeout=60)
    new = [a for a in home.tree.pending() if a["id"] not in before and a["requester"] == "merit-reviews"
           and a["verb"] == "container.inspect"]
    assert len(new) == 1, f"expected one new pending inspect approval, got {new}; request said {r.stdout} {r.stderr}"
    return new[0]


def e2e():
    rp = home.rp_id()
    print(f"e2e: server on :{PORT}, rp_id {rp}, state {TMP}")
    env = dict(os.environ, RESIDENT_HOME_STATE=TMP)
    srv = subprocess.Popen([sys.executable, "-I", os.path.join(HERE, "home.py"), "serve", "--port", str(PORT)], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    made = []
    try:
        for _ in range(50):
            try:
                if http("api/state", origin=False)[0] == 200:
                    break
            except OSError:
                time.sleep(0.2)
        A = SoftAuthenticator()
        st = {}

        def enroll():
            url = subprocess.run([sys.executable, "-I", os.path.join(HERE, "home.py"), "enroll"], env=env,
                                 capture_output=True, text=True, check=True).stdout.strip()
            assert url.startswith(f"https://{rp}/resident/?enroll="), url
            tok = url.split("enroll=", 1)[1]
            assert http("api/enroll/options", {"token": "nope"})[0] == 403
            code, o = http("api/enroll/options", {"token": tok})
            assert code == 200, o
            pk = o["publicKey"]
            assert pk["rp"] == {"id": rp, "name": "Resident"} and pk["pubKeyCredParams"] == [{"type": "public-key", "alg": -7}]
            assert pk["authenticatorSelection"]["userVerification"] == "required" and len(b64d(pk["challenge"])) == 32
            r = A.create(b64d(pk["challenge"]), rp)
            code, out = http("api/enroll/finish", {"token": tok, **{k: b64e(v) for k, v in r.items()}})
            assert code == 200 and out.get("enrolled"), out
            assert os.stat(os.path.join(TMP, "owner.json")).st_mode & 0o777 == 0o600
            assert not os.path.exists(os.path.join(TMP, "enroll.json")), "token not consumed"
            assert http("api/enroll/options", {"token": tok})[0] == 403, "token reusable"
            assert http("api/state", origin=False)[1]["enrolled"] is True
        check("e2e: enroll the software authenticator over HTTP (token single-use, owner.json 0600)", enroll)

        def csrf():
            assert http("api/deny", {"aid": "zzz"}, origin="https://evil.example")[0] == 403
            assert http("api/deny", {"aid": "zzz"}, ctype="text/plain")[0] == 415
        check("e2e: foreign Origin and non-JSON content type are refused", csrf)

        def approve():
            a = make_request(); made.append(a["id"])
            st["a"] = a
            code, o = http("api/approve/options", {"aid": a["id"]})
            assert code == 200, o
            pk = o["publicKey"]
            assert pk["rpId"] == rp and pk["allowCredentials"][0]["id"] == b64e(A.cred_id)
            bad = A.get(b64d(pk["challenge"]), rp)
            bad["signature"] = A.get(os.urandom(32), rp)["signature"]  # a valid signature over something else
            code, out = http("api/approve/finish", {"aid": a["id"], **{k: b64e(v) for k, v in bad.items()}})
            assert code == 403 and "signature" in out["error"], out
            assert json.load(open(os.path.join(home.tree.APPROVALS, a["id"] + ".json")))["status"] == "pending"
            # the failed attempt consumed that challenge; get a fresh one
            code, o = http("api/approve/options", {"aid": a["id"]})
            good = A.get(b64d(o["publicKey"]["challenge"]), rp)
            code, out = http("api/approve/finish", {"aid": a["id"], **{k: b64e(v) for k, v in good.items()}})
            assert code == 200 and out["response"]["status"] == "done", out
            f = json.load(open(os.path.join(home.tree.APPROVALS, a["id"] + ".json")))
            assert f["status"] == "approved" and f["decided_by"] == f"{OWNER_NAME} (passkey)", f
            resp = json.load(open(os.path.join(home.tree.IND, "merit-reviews", "uplink", "responses", a["rid"] + ".json")))
            assert resp["status"] == "done" and "shop-app-dashboard-1" in resp["output"], resp
            code, out = http("api/approve/finish", {"aid": a["id"], **{k: b64e(v) for k, v in good.items()}})
            assert code == 403, f"replay accepted: {out}"
            print(f"        approved {a['id']} by passkey → child response {a['rid']}: {resp['output'].strip()[:90]}")
        check("e2e: real escalated inspect approved with a passkey assertion (bad sig refused first, replay refused after)", approve)

        def cli_refused():
            b = make_request(); made.append(b["id"])
            r = subprocess.run([os.path.expanduser("~/.local/bin/resident"), "approve", b["id"]], env=env,
                               capture_output=True, text=True, timeout=60)
            assert r.returncode != 0 and "passkey" in r.stderr, (r.returncode, r.stdout, r.stderr)
            assert json.load(open(os.path.join(home.tree.APPROVALS, b["id"] + ".json")))["status"] == "pending"
            print(f"        CLI: {r.stderr.strip()}")
            code, out = http("api/deny", {"aid": b["id"]})
            assert code == 200 and out["approval"]["status"] == "denied", out
            assert out["approval"]["decided_by"] == f"{OWNER_NAME} (home page)"
        check("e2e: CLI `resident approve` refused once enrolled; deny via the page works", cli_refused)

        def audit_log():
            recs = [json.loads(x) for x in open(os.path.join(TMP, "audit.jsonl"))]
            ap = [r for r in recs if r["event"] == "approve"]
            assert any(r["ok"] for r in ap) and any(not r["ok"] for r in ap), ap
        check("e2e: audit.jsonl records both failed and successful approval attempts", audit_log)
    finally:
        srv.terminate()
        srv.wait(10)
        for aid in made:  # never leave a test request pending
            p = os.path.join(home.tree.APPROVALS, aid + ".json")
            if os.path.exists(p) and json.load(open(p))["status"] == "pending":
                subprocess.run([os.path.expanduser("~/.local/bin/resident"), "deny", aid], capture_output=True)


if __name__ == "__main__":
    try:
        unit()
        if "--unit" not in sys.argv:
            e2e()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    bad = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} passed" + (f"; temp state {TMP} removed" if not os.path.exists(TMP) else ""))
    sys.exit(1 if bad else 0)
