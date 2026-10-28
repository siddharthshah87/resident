# resident — bootable image (image-vm)

> **UNTESTED.** Written 2026-10-27/28 on my-box without sudo, qemu/KVM or mkosi. Every file
> was syntax-checked (see "Validated"), nothing was built or booted. Expect the first `mkosi build`
> to need 2–5 small fixes (key spellings differ between mkosi versions; see "mkosi version").

The NUC prototype (`~/resident`) lives inside Ubuntu as a user. This directory is the same system as
an OS: a sealed image where the agent is a **principal** (uid, groups, polkit, D-Bus name, sudo
wrapper) and where **the permission model is readable text** (`/etc/resident/policy.toml`), with
the intent log seeded at first boot so nothing in the image is unexplained.

## Run it (needs sudo once)
```
sudo apt install -y mkosi systemd-boot qemu-system-x86 systemd-repart ovmf erofs-utils systemd-ukify pipx && sudo usermod -aG kvm owner
pipx install git+https://github.com/systemd/mkosi.git     # apt's mkosi is 20.2; this config targets v25+ (then: newgrp kvm)
cd ~/resident/image-vm && ./build.sh && ./boot.sh            # serial console; ssh -p 2222 owner@localhost
```
Variants: `./build.sh --profile=desktop` (cage + portals, see docs/portal-consent.md),
`--profile=docker` (docker.io), or both `--profile=desktop,docker`.
Update a running box: `./update.sh owner@<host> <version>`.

## Layout
```
mkosi.conf                 Debian trixie, systemd-boot, UKI, packages (every group has an intent)
mkosi.profiles/{desktop,docker}/   optional variants
mkosi.repart/              install-image GPT: ESP · root-A (erofs+verity) · root-A-verity · root-B (_empty) · root-B-verity · var
mkosi.postinst             /home,/srv,/opt → /var; lock root; resolv.conf; policy hash
mkosi.extra/               files copied into the image root:
  etc/resident/policy.toml            ← THE permission model (human-readable; the broker reads it)
  etc/polkit-1/rules.d/50-resident.rules  etc/dbus-1/system.d/resident.conf  etc/sudoers.d/resident
  etc/ssh/sshd_config.d/50-resident.conf  etc/fstab  etc/systemd/network/80-dhcp.network
  usr/lib/resident/                   agentos.py (staged by build.sh), privileged.sh, policy.py,
                                     firstboot.py, health.sh, etc-overlay.sh, consent-revoke.sh
  usr/lib/systemd/system/            resident-etc-overlay, resident-firstboot, resident-world.{service,timer},
                                     resident-health, agentd, percept, agent-browser
  usr/lib/{sysusers.d,tmpfiles.d}/resident.conf   the principal and its directories
  usr/lib/sysupdate.d/*.transfer     A/B update definitions      usr/lib/repart.d/30-var.conf  grow /var
  usr/share/resident/intents.json     seeded on first boot        efi/loader/loader.conf  editor=no
build.sh · boot.sh · update.sh · intents.json · docs/portal-consent.md
```

## Immutable vs writable
| path | state | why |
|---|---|---|
| `/` incl. `/usr`, `/etc` (lower) | erofs + dm-verity, read-only; two slots A/B | the image IS the configuration cache; roothash in the UKI |
| `/etc` | overlayfs: lower = image, upper = `/var/lib/resident/etc-overlay/upper` | local edits persist and `resident` can still hash /etc; `rm -rf` the upper = factory reset |
| `/var` | ext4, the only writable partition, grown to disk on first boot | agent state `/var/lib/resident`, journal, docker, `/var/home`, update staging |
| `/efi` | vfat | systemd-boot + `resident_<ver>+3.efi` UKIs |
| `/tmp`, `/run` | tmpfs | |
`/home`, `/srv`, `/opt` are symlinks into `/var`. machine-id is captured on first boot into the overlay (`etc-overlay.sh`).

## How a human gets in (break-glass)
ssh, key only: `build.sh` bakes `$RESIDENT_SSH_PUBKEY` (default `~/.ssh/id_*.pub`) into
`/etc/ssh/authorized_keys.d/owner`. `AllowUsers owner`, root locked, no passwords, `DenyUsers agent`.
your user is in group `sudo` (Debian default rule → full sudo). The bootloader editor is off, so the
serial console only gets you a login prompt. Everything a human changes should still go through
`intent add` / `intent apply` — the drift detector records what doesn't.

## How the agent is a principal
Declared in **`/etc/resident/policy.toml`**; each enforcement file cites the section it implements.
| it can | mechanism |
|---|---|
| run as `agent` (uid 980, groups systemd-journal, adm, docker) | `sysusers.d/resident.conf` |
| start/stop/restart/reload its own units `^(resident-\|agentd\|percept\|agent-browser\|shop-app)` | polkit `50-resident.rules` (`manage-units` + unit/verb lookup) |
| own `org.resident.Agent`, receive systemd1/login1 signals, be `Ask`ed by anyone local | `dbus-1/system.d/resident.conf` |
| `apt-get install <pkg>` and `systemctl restart <own unit>` as root — **only** with `RESIDENT_INTENT=<active id>`, through `privileged.sh`, audited to `/var/log/resident/privileged.log` | `sudoers.d/resident` → `privileged.sh` → `policy.py` |
| read the journal, drop events in `inbox/`, write under `/var/lib/resident`, `/tmp` | groups + tmpfiles |
| **cannot** | reboot/poweroff/suspend (polkit NO), enable/disable units, change network, remove packages, touch users/keys/sudoers/polkit/policy, log in via ssh, be re-granted perception by itself — all on `[approval]`; the wrapper exits 3 and the agent emits an `ask` event |

Example: `intent add "need jq for log parsing"` → `RESIDENT_INTENT=7 intent apply 7 -- sudo /usr/lib/resident/privileged.sh install jq`.

## Perception consent
See `docs/portal-consent.md`: one human grant via xdg-desktop-portal ScreenCast with `persist_mode=2`,
restore token in `/var/lib/resident/consent/`, kill switch `percept off` or `consent-revoke.sh`.

## Update / rollback
1. `./build.sh` → `out/updates/resident_<ver>.efi`, `resident_<ver>_<partuuid>.root-x86-64{,-verity}.raw`, `SHA256SUMS`.
2. `./update.sh host ver` stages into `/var/lib/resident/updates`, runs `systemd-sysupdate update`: UKI
   installed as `resident_<ver>+3.efi`, root+verity written into the `_empty` (or oldest) slot; the
   running version is `ProtectVersion=%A`.
3. Reboot. systemd-boot picks the newest UKI; `roothash=` in it selects the new slot via gpt-auto.
4. `resident-health.service` (`RequiredBy=boot-complete.target`, `FailureAction=reboot`) checks dbus,
   polkit, ssh, networkd, `/etc` overlay, `/var`, sudoers, and that `resident world` runs. Healthy →
   `systemd-bless-boot` renames `+3-0` → clean name. Unhealthy → reboot; after 3 tries systemd-boot
   falls back to the previous (blessed) UKI and slot. `/var` (state) is shared across slots; `/etc`
   edits persist across updates (and may shadow new defaults — the intent log says which).
Manual: `bootctl set-oneshot <entry>`; `systemd-sysupdate vacuum` frees old slots.

## Known gaps (honest list)
- **mkosi version:** keys are v25 (`[Build] ToolsTree`, `[Runtime]`, `SplitArtifacts=uki,partitions`,
  `UnifiedKernelImageFormat`). apt's 20.2 uses `[Host] QemuMem/QemuKvm`, `SplitArtifacts=yes`. `mkosi summary` tells you fast.
- Verity + A/B + sysupdate `@u` naming: the `_<partuuid>` in the staged file names is what lets sysupdate set
  the new partition's UUID to match the roothash; check `man sysupdate.d` on the build host for `@u`.
- `agentd.py` hardcodes `~/resident` → bridged by a symlink `/var~/resident → /var/lib/resident`;
  `percept.py` writes next to itself → needs a `PERCEPT_HOME` env read (unit already sets it). Neither component was modified.
- agentd needs credentials (`/var/lib/resident/credentials/claude.env` with `ANTHROPIC_API_KEY=`) and a venv
  (`python3 -m venv /var/lib/resident/agentd/.venv && .venv/bin/pip install claude-agent-sdk`, as `agent`); the unit is
  Condition-gated until both exist. `resident-browse` needs Playwright (`npm i playwright-core` in `/var/lib/resident/browser`).
- `/var` unencrypted (browser profile + credentials). Add `Encrypt=tpm2` to `mkosi.repart/30-var.conf` later. No Secure Boot yet.
- `systemd-sysupdate` binary location in Debian (`systemd` vs `systemd-boot` package): `dpkg -S systemd-sysupdate` after build.
- desktop variant lacks the `resident-session.target` unit that starts cage+portals in the agent session.

## Validated (on the NUC, no sudo)
`bash -n` on every `.sh`/postinst; `python3 -m py_compile` on `.py`; `tomllib` loads `policy.toml`; `intents.json` parses;
`node --check` on `50-resident.rules`; `xmllint`/python XML parse of the dbus policy; unit/repart/transfer keys reviewed by eye
against systemd.unit/systemd.repart/sysupdate.d. Not validated: anything mkosi, booting, polkit semantics at runtime.
