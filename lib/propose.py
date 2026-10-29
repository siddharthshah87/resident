#!/usr/bin/env python3
"""resident propose — the agent proposes ONE change; only the owner's passkey can approve it.

  resident propose "<summary>" --why "<one sentence>" --verify "<cmd>" [--root] -- <command…>

Writes ~/resident/approvals/<id>.json (kind "proposal", status "pending") and an outbox note, prints the id and the
home page URL. The owner approves on the phone: the passkey signs sha256(canonical(proposal)), so the signature
commits to the exact command, verify command and root flag. home/home.py then runs it through `intent apply`
(as root via /usr/local/libexec/resident-root-apply, which re-verifies the signature itself).

Env (tests): RESIDENT_APPROVALS (default ~/resident/approvals), RESIDENT_OUTBOX (default $RESIDENT_HOME/outbox).
"""
import base64, hashlib, json, os, re, secrets, shlex, subprocess, sys, time
from datetime import datetime, timezone

APPROVALS = os.environ.get("RESIDENT_APPROVALS") or os.path.expanduser("~/resident/approvals")
OUTBOX = os.environ.get("RESIDENT_OUTBOX") or os.path.join(
    os.path.expanduser(os.environ.get("RESIDENT_HOME") or "~/resident"), "outbox")
REQUESTER = os.environ.get("RESIDENT_HEAD") or "nuc"
CANON_FIELDS = ("kind", "id", "requester", "summary", "why", "command", "verify", "root", "nonce")
ID_RE = re.compile(r"[0-9a-f]{6}")
NONCE_RE = re.compile(r"[A-Za-z0-9_-]{22}")
MAX_SUMMARY = 80


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def b64e(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def canonical(p):
    """The exact bytes the owner's passkey commits to (via sha256). Must match root/resident-root-apply."""
    return json.dumps({k: p[k] for k in CANON_FIELDS}, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def challenge(p):
    return hashlib.sha256(canonical(p)).digest()


def is_proposal(a):
    return isinstance(a, dict) and a.get("kind") == "proposal"


def problems(p):
    """Why this is not a well-formed proposal ([] if it is)."""
    if not is_proposal(p):
        return ["kind is not 'proposal'"]
    out = []
    for k in ("id", "requester", "summary", "why", "command", "verify", "nonce"):
        if not isinstance(p.get(k), str) or not p[k].strip():
            out.append(f"{k} missing")
    if not isinstance(p.get("root"), bool):
        out.append("root is not a boolean")
    if out:
        return out
    if not ID_RE.fullmatch(p["id"]):
        out.append("id is not 6 hex chars")
    if not NONCE_RE.fullmatch(p["nonce"]):
        out.append("nonce is not 16 bytes of base64url")
    if len(p["summary"]) > MAX_SUMMARY or "\n" in p["summary"]:
        out.append(f"summary must be one line of at most {MAX_SUMMARY} chars")
    if "\n" in p["command"] or "\r" in p["command"]:
        out.append("command contains a newline")
    return out


def write_json(path, obj, mode=0o644):
    tmp = os.path.join(os.path.dirname(path), f".{os.path.basename(path)}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as fh:
        json.dump(obj, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def outbox(kind, text):
    """Drop a plain note for the owner (shown in the home page feed). Not an inbox event: agentd does not wake."""
    os.makedirs(OUTBOX, exist_ok=True)
    ms = int(time.time() * 1000)
    while True:
        path = os.path.join(OUTBOX, f"{ms}-{kind}.md")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            break
        except FileExistsError:
            ms += 1
    with os.fdopen(fd, "w") as fh:
        fh.write(text.rstrip() + "\n")
    return path


def create(summary, why, verify, command, root=False):
    p = {"kind": "proposal", "id": "", "ts": now(), "requester": REQUESTER, "summary": summary.strip(),
         "why": why.strip(), "command": command, "verify": verify, "root": bool(root),
         "nonce": b64e(secrets.token_bytes(16)), "status": "pending"}
    os.makedirs(APPROVALS, exist_ok=True)
    for _ in range(100):
        p["id"] = secrets.token_hex(3)
        path = os.path.join(APPROVALS, p["id"] + ".json")
        if os.path.exists(path):
            continue
        bad = problems(p)
        if bad:
            raise ValueError("; ".join(bad))
        tmp = os.path.join(APPROVALS, f".{p['id']}.{os.getpid()}.new")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "w") as fh:
            json.dump(p, fh, indent=1, ensure_ascii=False)
        try:  # appears complete or not at all, and never over an existing id
            os.link(tmp, path)
        except FileExistsError:
            continue
        finally:
            os.unlink(tmp)
        return p, path
    raise RuntimeError("could not allocate a proposal id")


def host():
    if os.environ.get("RESIDENT_HOME_RPID"):
        return os.environ["RESIDENT_HOME_RPID"]
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=10).stdout
        return json.loads(out)["Self"]["DNSName"].rstrip(".") or "<host>"
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return "<host>"


def main(argv):
    args = argv[1:] if argv and argv[0] == "propose" else argv
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if args else 2
    if "--" not in args:
        sys.exit('resident propose "<summary>" --why "<sentence>" --verify "<cmd>" [--root] -- <command…>')
    i = args.index("--")
    head, tail = args[:i], args[i + 1:]
    opts, pos, j = {}, [], 0
    while j < len(head):
        if head[j] in ("--why", "--verify"):
            if j + 1 >= len(head):
                sys.exit(f"propose: {head[j]} needs a value")
            opts[head[j]] = head[j + 1]
            j += 2
        elif head[j] == "--root":
            opts["--root"] = True
            j += 1
        elif head[j].startswith("--"):
            sys.exit(f"propose: unknown option {head[j]}")
        else:
            pos.append(head[j])
            j += 1
    summary = " ".join(pos).strip()
    command = tail[0] if len(tail) == 1 else shlex.join(tail)
    errs = []
    if not summary:
        errs.append("a summary is required")
    elif len(summary) > MAX_SUMMARY:
        errs.append(f"summary is {len(summary)} chars (max {MAX_SUMMARY})")
    if not (opts.get("--why") or "").strip():
        errs.append("--why is required")
    if not (opts.get("--verify") or "").strip():
        errs.append("--verify is required (a command that proves the goal)")
    if not command.strip():
        errs.append("a command after -- is required")
    if "\n" in command or "\r" in command:
        errs.append("the command contains a newline (one command line only)")
    if errs:
        sys.exit("propose: " + "; ".join(errs))
    try:
        p, _path = create(summary, opts["--why"], opts["--verify"], command, opts.get("--root", False))
    except ValueError as e:
        sys.exit(f"propose: {e}")
    outbox("proposal", f"Proposed: {p['summary']} — {p['why'].rstrip('.')}. Approve on your phone.")
    print(p["id"])
    print(f"https://{host()}/resident/")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
