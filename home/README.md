# resident home

The owner's screen, and the first screen of the agent home. It shows what the family of agents is waiting on and
the family tree, and it is the only place an escalated request can be **approved** once a passkey is enrolled.

- `home.py` — stdlib HTTP server on `127.0.0.1:8720`, unit `resident-home.service` (host intent #31), mounted
  tailnet-only at `https://<your-machine>.<your-tailnet>.ts.net/resident/` by `tailscale serve --set-path /resident`
  (serve strips the `/resident` prefix; the server accepts either form).
- `page.html` — the single self-contained page (inline CSS/JS, light/dark, strict CSP with a per-response nonce).
- `test_webauthn.py` — software authenticator + end-to-end test: `python3 -I test_webauthn.py` (`--unit` to skip e2e).
- State: `owner.json` (passkey public key, 0600), `enroll.json` (sha256 of the one-time token), `audit.jsonl`
  (every enrollment/approval attempt, ok or not, with the reason).

## Enrolling the phone

1. On the NUC: `python3 ~/resident/home/home.py enroll` — prints a one-time link, valid 15 minutes.
2. Open it in Safari on the iPhone (which must be on the tailnet), tap **Enroll this device**, confirm with Face ID.
3. `python3 ~/resident/home/home.py status` shows the enrolled credential. To move to a new phone:
   `home.py enroll --replace`.

## Approving

A child's out-of-policy request lands in `~/resident/approvals/` as pending and appears on the page. **Approve
(passkey)** asks the server for a 32-byte challenge bound to that approval id (5 minutes, single use), the phone signs
it after Face ID, and the server checks: credential id, `webauthn.get`, challenge, origin, rpIdHash, user-present and
user-verified flags, the ES256 signature with the enrolled public key, and the sign counter. Only then does it call
`tree.decide(..., passkey=True)`. **Deny** needs no passkey (denying is always safe), and `resident deny` still works.
Once `owner.json` exists, `resident approve` on the CLI refuses and points here. Before enrollment it still works
(bootstrap).

## Threat model, honestly

What this buys: a child agent (in its container body) cannot approve its own escalation, and neither can anything
that cannot produce a signature from the owner's phone — no token to steal, no CLI to call. Approvals are bound to a
specific request, cannot be replayed, and are audited.

What it does not: the gate lives in files owned by your user. Any process running as your user on the host —
including the head agent — can edit `tree.py`, delete or swap `owner.json`, point `RESIDENT_HOME_STATE` elsewhere, or
write an approval file directly. Against the head agent this is a tripwire, not a wall. Full separation needs the
agent daemon to run as its own Unix user that cannot write the approval code or state (the distro image does this:
user `agent`, uid 980, with privileged actions behind polkit/sudoers that require an intent).

## Proposals: the agent acts, the owner's passkey decides

`resident propose "<summary>" --why "<sentence>" --verify "<cmd>" [--root] -- <command>` (lib/propose.py) writes
`approvals/<id>.json` with `kind: "proposal"` and a 16-byte nonce. On the page it shows summary, why, the check, a
**runs as root** badge and the exact command. For a proposal the WebAuthn challenge is not random: it is
`sha256(canonical)` over kind, id, requester, summary, why, command, verify, root and nonce, so the passkey signs
exactly what will run. `finish` re-reads the file and refuses if it changed since `options`; home keeps its own
id:nonce replay list (`used_proposals`). On success the assertion is stored in the proposal, a host intent is
created (author = owner) and the command runs via `intent apply <iid> --verify <verify> -- …`; non-root as the
owner, root as `sudo -n /usr/local/libexec/resident-root-apply <proposal>`. The result (done/failed, exit,
verify_ok, output tail, intent) goes back into the file and to `outbox/<ms>-result.md`.

`root/resident-root-apply` is self-contained (stdlib + cryptography, `python3 -I`) and installed root-owned by
`sudo ~/resident/root/install.sh` together with `/etc/resident/owner.json` (public key only), the root-owned replay
list `/var/lib/resident-root/used` and one sudoers line. It re-verifies the owner's signature over the proposal
itself, so nothing the agent can write under `~/resident` lets it run a command as root that the owner did not sign.
Limit, honestly: the page that shows the owner what they are signing is served by your user-owned code; a
compromised head agent could show one command and ask for a signature over another. The helper stops forged and
replayed approvals, not a lying page. Re-run `install.sh` after `home.py enroll --replace`.

The page also has a conversation feed (last 15 outbox messages) and a composer (`/api/ask` → `inbox/<ms>-ask.json`).
Tests: `python3 -I test_proposals.py` (temp state everywhere; the root path runs the real helper in
`--selftest-config` mode).
