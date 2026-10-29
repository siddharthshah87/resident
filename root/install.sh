#!/bin/bash
# Install (or refresh) Resident's root helper. Run once, and again after replacing the passkey:
#   sudo ~/resident/root/install.sh
# It copies resident-root-apply to /usr/local/libexec (root:root 0755), copies ONLY the public half of the owner's
# passkey to /etc/resident/owner.json (root 0644), creates /var/lib/resident-root (0700, replay list), and allows
# exactly this sudo rule:  <user> ALL=(root) NOPASSWD: /usr/local/libexec/resident-root-apply
# Review resident-root-apply before running this: from then on it is what runs owner-approved commands as root.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "install.sh: run with sudo: sudo $0" >&2
  exit 1
fi
OWNER_USER="${SUDO_USER:?run this with sudo from the owner account}"
if [ "$OWNER_USER" = root ]; then
  echo "install.sh: run it as the owner via sudo (SUDO_USER is root)" >&2
  exit 1
fi
OWNER_HOME="$(getent passwd "$OWNER_USER" | cut -d: -f6)"
SRC_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
HELPER_SRC="$SRC_DIR/resident-root-apply"
OWNER_SRC="${RESIDENT_HOME_STATE:-$OWNER_HOME/resident/home}/owner.json"
ORIGIN_HOST="${RESIDENT_HOST:-$(tailscale status --json 2>/dev/null | /usr/bin/python3 -I -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null)}"
[ -n "$ORIGIN_HOST" ] || { echo "install.sh: cannot find this machine's Tailscale name; set RESIDENT_HOST=<name>.<tailnet>.ts.net" >&2; exit 1; }
HELPER=/usr/local/libexec/resident-root-apply
SUDOERS=/etc/sudoers.d/resident-root-apply

[ -f "$HELPER_SRC" ] || { echo "install.sh: missing $HELPER_SRC" >&2; exit 1; }
[ -f "$OWNER_SRC" ] || { echo "install.sh: no enrolled passkey at $OWNER_SRC (enroll first: python3 ~/resident/home/home.py enroll)" >&2; exit 1; }
/usr/bin/python3 -I -c 'import cryptography' 2>/dev/null || { echo "install.sh: root's python3 lacks cryptography (apt install python3-cryptography)" >&2; exit 1; }

echo "== helper"
install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 "$HELPER_SRC" "$HELPER.new"
/usr/bin/python3 -I -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$HELPER.new"
mv -f "$HELPER.new" "$HELPER"
echo "   installed $HELPER (root:root 0755, sha256 $(sha256sum "$HELPER" | cut -c1-16))"

echo "== owner public key"
install -d -o root -g root -m 0755 /etc/resident
TMPK="$(mktemp /etc/resident/.owner.json.XXXXXX)"
/usr/bin/python3 -I - "$OWNER_SRC" "$ORIGIN_HOST" "$TMPK" <<'PY'
import base64, json, re, sys
src, host, out = sys.argv[1:]
c = json.load(open(src))
def b64d(s):
    if not isinstance(s, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", s):
        sys.exit(f"install.sh: {src}: field is not base64url")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
if c.get("alg", -7) != -7:
    sys.exit("install.sh: passkey is not ES256")
cred, x, y = b64d(c.get("cred_id")), b64d(c.get("x")), b64d(c.get("y"))
if not (1 <= len(cred) <= 1023 and len(x) == 32 and len(y) == 32):
    sys.exit("install.sh: owner.json has bad key sizes")
from cryptography.hazmat.primitives.asymmetric import ec
ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()).public_key()
pub = {"cred_id": c["cred_id"], "x": c["x"], "y": c["y"], "alg": -7, "rp_id": host, "origin": "https://" + host,
       "owner": c.get("owner", ""), "created": c.get("created", "")}   # public fields only; never sign_count etc.
with open(out, "w") as fh:
    json.dump(pub, fh, indent=1)
print(f"   cred {c['cred_id'][:12]}… rp_id {host}")
PY
chown root:root "$TMPK"; chmod 0644 "$TMPK"
mv -f "$TMPK" /etc/resident/owner.json
echo "   wrote /etc/resident/owner.json (root 0644)"

echo "== replay list and log"
install -d -o root -g root -m 0700 /var/lib/resident-root
touch /var/log/resident-root.log; chown root:root /var/log/resident-root.log; chmod 0600 /var/log/resident-root.log
echo "   /var/lib/resident-root (0700), /var/log/resident-root.log (0600)"

echo "== sudoers"
TMPS="$(mktemp /etc/sudoers.d/.resident-root-apply.XXXXXX)"
printf '%s ALL=(root) NOPASSWD: %s\n' "$OWNER_USER" "$HELPER" > "$TMPS"
chown root:root "$TMPS"; chmod 0440 "$TMPS"
if ! visudo -cf "$TMPS" >/dev/null; then
  rm -f "$TMPS"; echo "install.sh: generated sudoers rule failed visudo -cf; nothing changed" >&2; exit 1
fi
mv -f "$TMPS" "$SUDOERS"
visudo -c >/dev/null || { rm -f "$SUDOERS"; echo "install.sh: sudoers as a whole failed visudo -c; rule removed" >&2; exit 1; }
echo "   $SUDOERS: $(cat "$SUDOERS")"

echo "== check"
if sudo -n -u "$OWNER_USER" sudo -n -l "$HELPER" >/dev/null 2>&1; then
  echo "   $OWNER_USER may run $HELPER as root without a password"
fi
out="$(sudo -n -u "$OWNER_USER" sudo -n "$HELPER" /nonexistent 2>&1 || true)"
case "$out" in
  *"cannot open proposal"*) echo "   self-test passed: the helper runs as root and refuses an unverifiable proposal" ;;
  *) echo "   self-test UNEXPECTED: $out" ;;
esac
echo "done. Root proposals approved by passkey now run via: sudo -n $HELPER <proposal.json>"
