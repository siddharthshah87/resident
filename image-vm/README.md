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
`--profile=docker` (docker.io), or both `--profile=desktop,docker`. Raspberry Pi 4 (arm64):
`./stage-rpi4-firmware.sh && ./build.sh --profile=rpi4` on an arm64 host (see "Raspberry Pi 4" below).
Update a running box: `./update.sh owner@<host> <version>`.

## Layout
```
mkosi.conf                 Debian trixie, systemd-boot, UKI, packages (every group has an intent)
mkosi.conf.d/              per-architecture kernel/firmware/console (10-x86-64, 10-arm64); 00-rpi4-arch selects arm64
mkosi.profiles/{desktop,docker,rpi4}/   optional variants (rpi4: Pi 4 firmware on the ESP, arm64)
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
1. `./build.sh` → `out/updates/resident_<ver>.efi`, `resident_<ver>_<partuuid>.root-<arch>{,-verity}.raw`, `SHA256SUMS`
   (`<arch>` = `x86-64`, or `arm64` for rpi4; `mkosi.postinst` rewrites the shipped transfers' `root-x86-64` for arm64).
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

## Raspberry Pi 4 Model B (`--profile=rpi4`)
> **Status: built by CI; boot on real Pi 4 hardware not yet verified.** CI (`.github/workflows/image-rpi4.yml`,
> GitHub's `ubuntu-24.04-arm` runner) builds the image and boots that same disk on `qemu-system-aarch64 -M virt`
> with AAVMF UEFI, requiring `resident-health: healthy` and `UNEXPLAINED STATE: 0/`. That proves the arm64 kernel,
> UKI, verity root and first boot, not the Pi boot ROM → pftf → systemd-boot hand-off.

Boot chain, the same as x86 after the firmware: Pi boot ROM/EEPROM → **partition 1** (our ESP, FAT, `mkosi.repart/00-esp.conf`)
→ `start4.elf` + `config.txt` → `armstub=RPI_EFI.fd` ([pftf/RPi4](https://github.com/pftf/RPi4) UEFI, EDK2) →
`EFI/BOOT/BOOTAA64.EFI` (systemd-boot, installed by mkosi) → `EFI/Linux/resident_<ver>+3.efi` (UKI) → gpt-auto picks the
erofs + dm-verity root from `roothash=`. A/B slots, `/var`, `/etc` overlay and sysupdate are unchanged.

How the variant is put together:
- `mkosi.conf.d/00-rpi4-arch.conf` sets `Architecture=arm64` when the profile is selected. mkosi parses `mkosi.profiles/`
  *after* `mkosi.conf.d/`, so setting it only in the profile would be too late for the `[Match] Architecture=` drop-ins.
  `10-x86-64.conf` (linux-image-amd64, intel-microcode, firmware-iwlwifi, `console=ttyS0`) and `10-arm64.conf`
  (linux-image-arm64, which carries the bcm2711 device trees; `console=ttyAMA0,115200`) are each selected by architecture.
  The default build is unchanged: `mkosi summary` before and after differs only in the random seed.
- `mkosi.profiles/rpi4/mkosi.conf` adds firmware-brcm80211 (Wi-Fi, including `brcmfmac43455-sdio.raspberrypi,4-model-b.txt`)
  and bluez-firmware (Bluetooth `BCM4345C0.hcd`). `Packages=` and `KernelCommandLine=` *append* across files; an empty
  assignment would reset them.
- `./stage-rpi4-firmware.sh` downloads pftf **v1.53** (zip sha256 `ca9973e2a7a546b3df871cfb7382e656829114b6dfa424f40dc67cc90a217d88`,
  checked) and Broadcom's `LICENCE.broadcom` (pinned to raspberrypi/firmware tag 1.20260915, checked) into
  `mkosi.profiles/rpi4/mkosi.extra/efi/`. mkosi copies that to `/efi` in the build root, and `00-esp.conf`'s `CopyFiles=/efi:/`
  puts it at the **ESP root**: `start4.elf fixup4.dat config.txt RPI_EFI.fd bcm2711-rpi-4-b.dtb bcm2711-rpi-400.dtb
  bcm2711-rpi-cm4.dtb overlays/{miniuart-bt,upstream-pi4}.dtbo firmware/ Readme.md LICENCE.broadcom`. The root partition
  excludes `/efi/`. The staged tree is not in git.
- Licences (from the zip's own Readme.md and licence files): `RPI_EFI.fd` is EDK2, **BSD-2-Clause-Patent**. `start4.elf`,
  `fixup4.dat` and the `.dtb`/`.dtbo` files are Raspberry Pi boot files under **Broadcom's redistributable licence** (binary-only,
  unmodified, for use with Raspberry Pi devices; `LICENCE.broadcom` ships next to them on the ESP). `firmware/brcm/*`
  (pftf's Wi-Fi blobs; Debian's own firmware-brcm80211 is what the OS uses) is under the **Cypress** licence in `firmware/LICENCE.txt`.

### One-time steps on the Pi
1. **EEPROM bootloader 2020-09-03 or newer.** GPT disks and USB boot need it; any Pi 4 updated since late 2020 has it. Check it
   from Raspberry Pi OS with `sudo rpi-eeprom-update`, and update with `sudo rpi-eeprom-update -a && sudo reboot`. Without Pi OS:
   Raspberry Pi Imager → *Choose OS* → *Misc utility images* → *Bootloader (Pi 4 family)* → *SD Card Boot* or *USB Boot*.
   Write that to a spare SD card, boot the Pi from it once (when it finishes, the screen turns green and the activity LED blinks steadily), then power off.
2. **Flash the image.** Use `resident-rpi4_<ver>.img.zst` from the release (verify it with `SHA256SUMS-rpi4`).
   - Raspberry Pi Imager: *Choose OS* → *Use custom* → the `.img.zst` file. Skip Imager's "OS customisation"; it doesn't apply here.
   - Or: `zstd -dc resident-rpi4_<ver>.img.zst | sudo dd of=/dev/sdX bs=4M conv=fsync status=progress`.
   The disk needs **≥ 16 GB**. The layout is 512 MB ESP + 2×(3 GB root + 64 MB verity) + 4 GB `/var`, and `/var` grows to
   fill the disk on first boot.
3. **SD card vs USB SSD.** An SD card works with the factory boot order. A USB SSD is faster and wears better, but the
   EEPROM must try USB: choose Imager's *USB Boot* bootloader image in step 1, or set it from Pi OS with
   `sudo rpi-eeprom-config --edit` and `BOOT_ORDER=0xf14` (read right to left: USB, then SD, then retry). Use a powered hub or an SSD that
   draws little power.
4. **UEFI settings.** On the first boot, press **Esc** at the Raspberry Pi logo, then go to *Device Manager → Raspberry Pi
   Configuration → Advanced Configuration*:
   - *Limit RAM to 3 GB*: **Disabled**. pftf enforces 3 GB by default because of a DMA bug in the SoC; Debian's 6.x kernel
     handles it.
   - *System Table Selection*: **DeviceTree**. pftf defaults to ACPI. Ethernet, USB and SD do work under ACPI, so the image
     should come up either way. DeviceTree, with pftf's `upstream-pi4` overlay, gives Debian's kernel the full set of Pi 4
     peripherals (Wi-Fi, Bluetooth, GPIO, thermal, HDMI audio).
   - *F10* saves. pftf writes these settings back into `RPI_EFI.fd` on the ESP, so they survive reboots and image updates
     (sysupdate touches only `EFI/Linux/`). The settings can't be set in advance: they live in the firmware's own variable
     store, not in `config.txt`. Re-flashing resets them.
5. **Serial console** (optional): 3.3 V USB-UART on GPIO 14 (TXD, pin 8), GPIO 15 (RXD, pin 10) and GND (pin 6), at
   115200 8N1. pftf's `config.txt` sets `enable_uart=1` + `dtoverlay=miniuart-bt`, so this is the PL011 (`/dev/ttyAMA0`),
   which is what the kernel command line uses. HDMI is `tty0`, the primary console.

Known risks (unverified on hardware): the arm64 UKI carries every kernel module (as on x86, where it's 169 MB). Two of them
plus the firmware have to fit in the 512 MB ESP for an A/B update. The DTB is pftf's (Raspberry Pi Foundation's) plus its
`upstream-pi4` overlay, not Debian's own `broadcom/bcm2711-rpi-4-b.dtb`. If a peripheral misbehaves in DeviceTree mode,
try Debian's DTB (from `/usr/lib/linux-image-*/broadcom/`) on the ESP first.

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
