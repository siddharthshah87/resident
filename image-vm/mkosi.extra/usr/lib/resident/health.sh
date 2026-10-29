#!/bin/bash
# resident-health: is this slot good enough to bless? Any failure → exit 1 → FailureAction=reboot →
# systemd-boot counts a failed try; three failures fall back to the previous blessed UKI/slot.
set -u
fail=0
chk() { if "$@" >/dev/null 2>&1; then echo "ok   $*"; else echo "FAIL $*"; fail=1; fi; }

chk systemctl is-active dbus.service
chk busctl --system --timeout=10 call org.freedesktop.PolicyKit1 /org/freedesktop/PolicyKit1/Authority org.freedesktop.DBus.Peer Ping  # activates polkit on demand
chk systemctl is-active ssh.service
chk systemctl is-active systemd-networkd.service
chk findmnt -t overlay /etc                            # persistent /etc came up
chk findmnt /var                                       # writable partition mounted
chk test -w /var/lib/resident                           # agent state dir usable
chk test -s /etc/machine-id                            # identity persisted (etc-overlay.sh)
chk id -u agent                                        # principal exists (sysusers ran)
chk sudo -l -U agent                                   # sudoers parses (a broken sudoers.d locks the human out too)
chk systemctl start resident-world.service              # agentos.py runs under this systemd/python
chk test -s /var/lib/resident/machine.md
# browser is optional for health (it may be restarting after an update) — report only
systemctl is-active agent-browser.service >/dev/null 2>&1 && echo "ok   agent-browser" || echo "warn agent-browser not active"

if [ "$fail" -ne 0 ]; then
    echo "resident-health: UNHEALTHY — not blessing this boot" >&2
    /usr/bin/resident emit system "boot health check failed" "$(systemctl --failed --no-legend --plain)" 2>/dev/null || true
    exit 1
fi
/usr/bin/resident score 2>/dev/null | awk 'NR==1{print "resident-health: " $0; next} $2 ~ /^[1-9]/{print "resident-health:   " $0}'   # read by CI from the console
echo "resident-health: healthy; systemd-bless-boot may mark this slot good"
