# Perception consent via xdg-desktop-portal (desktop variant)

`percept` reads window titles and idle state. On Wayland that is only possible through
`xdg-desktop-portal` (ScreenCast for window/output metadata, RemoteDesktop only if the agent is ever
allowed to act, which it is not today). Portals ask the *seat owner* with a dialog. We want: one
explicit human grant, persisted, revocable in one command.

## Who runs the portal
The agent has its own user session: `/var/lib/systemd/linger/agent` (tmpfiles) → `user@980.service`
→ `/run/user/980`. The desktop variant starts `cage` (kiosk compositor) + `xdg-desktop-portal` +
`xdg-desktop-portal-wlr` in that session (`systemctl --user -M agent@ start resident-session.target`,
unit to be added with the variant). percept and the non-headless browser join via `WAYLAND_DISPLAY=wayland-0`.

## Granting once (human act — policy.toml [approval] "consent")
1. Human at the NUC runs `resident-consent grant` (small Python/GLib client; not written tonight):
   `org.freedesktop.portal.ScreenCast.CreateSession` → `SelectSources` with
   `persist_mode=2` (persistent) and `types=1|2` (monitors+windows), then `Start`.
2. The portal shows the chooser dialog **once**; the human picks the output/windows.
3. The `Start` response carries `restore_token`. The client writes it to
   `/var/lib/resident/consent/screencast.token` (0600 agent) and records the act:
   `RESIDENT_AUTHOR=owner intent add "consent: percept may read window titles via ScreenCast"`.
4. From then on percept passes `restore_token` in `SelectSources`; no dialog. The portal persists the
   grant in the agent's permission store (`org.freedesktop.impl.portal.PermissionStore`, tables
   `screencast` / `remote-desktop`, GVDB under `/var/lib/resident/.local/share/flatpak/db/`).
   percept only consumes PipeWire *metadata* (node names = window titles) — never frames.

## Kill switch (three layers, any one suffices)
- `percept off` → `/var/lib/resident/percept/DISABLED`; the daemon idles, records nothing.
- `/usr/lib/resident/consent-revoke.sh` → also shreds the token files and `Delete`s the permission
  store rows, so the next attempt shows the dialog again (i.e., needs a human).
- `systemctl stop xdg-desktop-portal.service` in the agent session (or stop `resident-session.target`).
The agent cannot re-grant: the dialog is on the seat, and `consent` is on the approval list.

## Not done tonight / to verify
- Exact PermissionStore table/id names (`flatpak permissions screencast` on a desktop will show them).
- `xdg-desktop-portal-wlr` only supports monitor/output ScreenCast; window-list metadata on wlroots
  needs the `wlr-foreign-toplevel-management` protocol instead (percept already has a `ps`-based fallback).
- xdg-desktop-portal in a lingering session with no `XDG_CURRENT_DESKTOP`: set it in
  `/var/lib/resident/.config/environment.d/` and `/etc/xdg-desktop-portal/portals.conf` (`default=wlr;gtk`).
