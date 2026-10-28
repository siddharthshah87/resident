#!/bin/bash
# Kill switch for perception + portal consent (human runs this; also safe for the agent to run on itself).
#   consent-revoke.sh            → percept off, forget portal restore tokens, stop percept
# Re-granting is a human act (policy.toml [approval] "consent"): docs/portal-consent.md.
set -u
H=/var/lib/resident
touch "$H/percept/DISABLED" 2>/dev/null && echo "percept: OFF (DISABLED marker set)"
shred -u "$H"/consent/*.token 2>/dev/null && echo "portal restore tokens destroyed"
# forget portal grants in the agent's permission store (desktop variant; ignore if absent)
if [ -S /run/user/980/bus ]; then
  for table in screencast remote-desktop; do
    sudo -u agent DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/980/bus \
      busctl --user call org.freedesktop.impl.portal.PermissionStore /org/freedesktop/impl/portal/PermissionStore \
      org.freedesktop.impl.portal.PermissionStore List s "$table" 2>/dev/null | tr -d '"' | tr ' ' '\n' | grep -v '^as$\|^[0-9]*$' | while read -r id; do
        [ -n "$id" ] && sudo -u agent DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/980/bus \
          busctl --user call org.freedesktop.impl.portal.PermissionStore /org/freedesktop/impl/portal/PermissionStore \
          org.freedesktop.impl.portal.PermissionStore Delete ss "$table" "$id" && echo "forgot $table/$id"
    done
  done
fi
systemctl stop percept.service 2>/dev/null && echo "percept.service stopped (it stays enabled but idles while DISABLED exists)"
echo "revoked. $(date -u +%FT%TZ)" >> "$H/consent/history.log" 2>/dev/null || true
