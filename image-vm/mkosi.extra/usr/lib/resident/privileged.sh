#!/bin/bash
# /usr/lib/resident/privileged.sh — the agent's only root path (sudoers.d/resident).
#
#   RESIDENT_INTENT=<id> sudo /usr/lib/resident/privileged.sh install <pkg>...
#   RESIDENT_INTENT=<id> sudo /usr/lib/resident/privileged.sh restart|start|stop|reload <unit>
#
# Refuses without an intent id that exists and is active in the intent log; consults
# /etc/resident/policy.toml (policy.py) for every verb/resource; appends one line per decision to
# /var/log/resident/privileged.log (+ journal). The caller is expected to wrap the whole thing in
# `intent apply <id> -- ...` so the resulting changes are attributed in the intent log.
# Exit: 0 ok · 1 denied/failed · 2 usage · 3 approval required (ask a human).
set -euo pipefail

LOG=/var/log/resident/privileged.log
POLICY_PY=/usr/lib/resident/policy.py
DB="${RESIDENT_HOME:-/var/lib/resident}/intents.db"
CALLER="${SUDO_USER:-$(id -un)}"

log() {  # log <decision> <msg...>
    local line
    line="$(date -u +%FT%TZ) intent=${RESIDENT_INTENT:-none} caller=${CALLER} decision=$1 verb=${VERB:-?} args=[${ARGS:-}] $2"
    echo "$line" >> "$LOG" 2>/dev/null || true
    logger -t resident-privileged -- "$line" 2>/dev/null || true
}
deny() { log DENY "$1"; echo "resident-privileged: DENIED: $1" >&2; exit "${2:-1}"; }

[ "$(id -u)" -eq 0 ] || { echo "run via sudo" >&2; exit 2; }
[ $# -ge 2 ] || { echo "usage: privileged.sh install <pkg>... | start|stop|restart|reload <unit>" >&2; exit 2; }
VERB=$1; shift
ARGS="$*"

# 1. intent id: required, numeric, exists, active (read-only sqlite open: no journal files as root)
[[ "${RESIDENT_INTENT:-}" =~ ^[0-9]+$ ]] || deny "RESIDENT_INTENT=<id> is required (intent add \"...\" first)" 2
[ "$RESIDENT_INTENT" -gt 0 ] || deny "intent #0 is DRIFT, not a reason" 2
INTENT_TEXT=$(python3 - "$DB" "$RESIDENT_INTENT" <<'PY'
import sqlite3, sys
db, iid = sys.argv[1], int(sys.argv[2])
try:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    r = c.execute("SELECT text, status FROM intents WHERE id=?", (iid,)).fetchone()
except sqlite3.Error as e:
    sys.exit(f"intent db unreadable: {e}")
if not r: sys.exit(f"no intent #{iid}")
if r[1] != "active": sys.exit(f"intent #{iid} is {r[1]}, not active")
print(r[0])
PY
) || deny "$INTENT_TEXT"

# 2. policy check per resource, then act
case "$VERB" in
    install)
        for pkg in "$@"; do
            msg=$("$POLICY_PY" install "$pkg") || { rc=$?; deny "install $pkg: $msg" "$rc"; }
        done
        log ALLOW "intent=\"$INTENT_TEXT\" → apt-get install $*"
        export DEBIAN_FRONTEND=noninteractive
        apt-get install -y --no-install-recommends -- "$@"
        ;;
    start|stop|restart|reload)
        [ $# -eq 1 ] || deny "exactly one unit" 2
        msg=$("$POLICY_PY" "$VERB" "$1") || { rc=$?; deny "$VERB $1: $msg" "$rc"; }
        log ALLOW "intent=\"$INTENT_TEXT\" → systemctl $VERB $1"
        systemctl "$VERB" -- "$1"
        ;;
    reboot|poweroff|remove|purge|autoremove|enable|disable|mask|useradd|usermod|passwd|nft|ufw|iptables|sysupdate)
        deny "'$VERB' is on the approval list in /etc/resident/policy.toml — ask a human (resident emit ask ...)" 3
        ;;
    *)
        deny "unknown verb '$VERB'" 2
        ;;
esac
log DONE "exit=0"
