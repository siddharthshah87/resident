# resident image — prototype 0

A Debian (bookworm-slim) rootfs, built with Docker, booted as a systemd container with a
**read-only root**, that **explains itself from first boot**: every service, package and config
file in the image has a recorded reason in the intent log before anyone logs in.

> Property demonstrated: `resident score` reports **0 unexplained** items on a fresh first boot,
> `why <anything>` answers from the log, and any change made outside `intent apply` is recorded as
> DRIFT and delivered to the resident agent's inbox.

## Build, run, exec

```sh
cd ~/resident
cp lib/agentos.py image/agentos.py            # image/agentos.py is a verbatim copy; re-sync before building
docker build -t resident-image:dev image/

docker run -d --name resident-dev --privileged --cgroupns=private --read-only \
  --tmpfs /run --tmpfs /run/lock --tmpfs /tmp --tmpfs /etc \
  -v resident-var:/var resident-image:dev /sbin/init

sleep 3; docker exec resident-dev systemctl is-system-running --wait      # → running
docker exec resident-dev systemctl status resident-firstboot --no-pager     # the seeder's log
docker exec resident-dev resident score                                     # → UNEXPLAINED STATE: 0/N
docker exec resident-dev intent list
docker exec resident-dev why dbus
docker exec resident-dev cat /var/lib/resident/machine.md
docker stop resident-dev && docker start resident-dev                       # /etc is rebuilt from the factory
```

A *first* boot is a boot with an empty `/var` volume (`docker volume rm resident-var` after
`docker rm -f resident-dev`). `resident-firstboot.service` is gated on
`ConditionPathExists=!/var/lib/resident/intents.db`, so later boots keep the log and only run the
world-model timer.

## Layout: what is immutable, what is a cache, what persists

| Path | Mount | Role |
|---|---|---|
| `/` (`/usr`, `/bin`, `/lib`, …) | image, `ro` | the OS. Nothing writes here; a change is a new image. |
| `/etc` | tmpfs (empty at boot) | **a cache**: `resident-init` copies `/usr/share/factory/etc` in before systemd starts, then restores `/etc/machine-id`. Everything written here is gone on reboot, which is exactly why it must be explained in the log, not relied on. |
| `/var` | named volume `resident-var` | the only persistent state: the intent log `/var/lib/resident/{intents.db,machine.md,inbox/}`, the journal, `/var/lib/dbus/machine-id`. |
| `/run`, `/run/lock`, `/tmp` | tmpfs | volatile runtime state. |
| `/var/lib/dpkg`, `/var/lib/apt` | symlinks into `/usr/share/resident/` | the package database describes the image, so it ships with the image (not in the mutable volume). `dpkg-query`/`apt-mark` keep working; `apt-get` cannot (root is read-only) — by design. |

**Factory `/etc`.** The Dockerfile snapshots the finished `/etc` to `/usr/share/factory/etc`
(minus `hostname`/`hosts`/`resolv.conf`, which the container runtime bind-mounts, and with an
empty `machine-id`). The container's `ENTRYPOINT` is `/usr/lib/resident/resident-init`: a 15-line
`sh` script that runs as PID 1 for a moment, hydrates the empty tmpfs `/etc` with `cp -a`, writes
the persistent machine-id from `/var/lib/dbus/machine-id` (minted on first boot), then
`exec /sbin/init`. A `tmpfiles.d` line `C /etc - - - - -` is also installed for the
systemd-native path; it is a no-op here because PID 1 needs `/etc/passwd` and unit symlinks before
`systemd-tmpfiles-setup` runs, which is why the hydration happens pre-init (in the VM image it
will happen in the initrd).

## What runs

| Unit | Runs as | What |
|---|---|---|
| `resident-firstboot.service` | root, `Group=agent` | once (`ConditionPathExists=!…/intents.db`): waits for `systemctl is-system-running --wait`, then `seed-intents.py` replays `intents.json`; `ExecStopPost=resident snapshot` sets the drift baseline after the unit has stopped running, so first boot itself is not drift. |
| `resident-world.timer` → `.service` | root, `Group=agent` | `OnBootSec=1min`, every 10 min: `resident world` regenerates `machine.md`, records drift under intent #0, emits `inbox/<epoch>-drift.json` / `-failed-unit.json`. |
| `agent` (system user, nologin) | — | the resident agent's principal; owns `/var/lib/resident` (mode 2770). The observers run as root because they must hash all of `/etc` (`shadow`, `opasswd`); `UMask=0002` + setgid make everything they write group-writable for `agent`. |

`RESIDENT_HOME=/var/lib/resident` is set in `/etc/environment`, the image `ENV`, and
`Environment=` on both units. `/usr/bin/{resident,intent,why}` → `/usr/lib/resident/agentos.py`.

## The seeded-intent contract (`image/intents.json`)

A JSON list, replayed in order on first boot with `RESIDENT_AUTHOR=image`:

```json
{"text": "<why this exists>",            // becomes `intent add "<text>"`
 "adopt": ["<regex>", ...],              // `intent adopt <id> [--kind k] <regex>...` against the live fingerprint
 "kind": "package|service|etc|cron|…",   // optional: restrict the regexes to one kind
 "verify": "<shell>",                    // optional: stored as the intent's --verify check
 "pre": "<shell>",                       // optional: run just before adopting (wake D-Bus-activated units)
 "adopt_dpkg_priority": ["required","important"]}  // expands to ^(pkg|pkg|…)$ from dpkg-query at run time
```

`intent adopt` only claims state that has no owner yet, so specific reasons come first and three
honest catch-alls come last (Debian required/important packages; auto-installed dependencies;
"/etc as shipped by the packages"). Adding a package or unit to the Dockerfile without an entry here
shows up as unexplained in `resident score` — the score is the test.

Scope notes: `score` counts packages from `apt-mark showmanual` (the ten installed by the
Dockerfile) and `/etc` files newer than the root's birth + 1 day; inside a fresh container the
root's birth is *now*, so the `etc` row reads `0/0` for the first day. Every `/etc` file is
nonetheless attributed (`why /etc/environment` works), and drift detection hashes all of `/etc`
regardless of scope.

## Demo: the guarantee

```sh
# a change made behind the agent's back …
docker exec resident-dev sh -c 'echo x > /etc/resident-drift-test.conf'
docker exec resident-dev resident world | grep -A3 '^## Drift'   # → "etc added: /etc/resident-drift-test.conf", [drift: 1 …]
docker exec resident-dev sh -c 'cat /var/lib/resident/inbox/*-drift.json'
docker exec resident-dev why /etc/resident-drift-test.conf        # → … → DRIFT: UNTRACKED DRIFT — changed outside `intent apply`

# … versus a change made the resident way
docker exec resident-dev sh -c 'i=$(intent add "test: add a config") && intent apply $i -- sh -c "echo y > /etc/resident-ok.conf"'
docker exec resident-dev why /etc/resident-ok.conf                # → … → intent #N [active]: test: add a config

docker exec resident-dev sh -c 'echo z > /usr/bin/z'             # → Read-only file system
```

## What the real VM image adds

- **A/B root partitions** (dm-verity, `systemd-boot` + UKI), `/etc` hydrated from the factory in the
  initrd, `/var` on its own partition; the intent log is the only thing that must survive an update,
  and the update itself is an intent (`intent apply N -- resident-update …`, with `--verify`).
- **Resident agent units** that need credentials or a display and are therefore not in this image:
  `agentd.service` (Claude Agent SDK session watching `inbox/`), `percept.service`,
  `agent-browser.service` (headless Chromium + CDP), all `User=agent`.
- **polkit rules for the `agent` principal**: allow `systemctl {start,stop,restart}` of units the agent
  owns, `hostnamectl`/`timedatectl` reads, nothing else; writes to `/etc` only through `intent apply`,
  which becomes a privileged helper that refuses to run without an intent id.
- `systemd-networkd` + `systemd-resolved` instead of the runtime's bind-mounted `hosts`/`resolv.conf`;
  `systemd-timesyncd`; sandboxing (`ProtectSystem=strict`, `ReadWritePaths=/var/lib/resident`) on the
  observer units; `console-getty` replaced by the agent's own session.
- `apt-daily*.timer` are masked: on the first long-running container they fired after ~6 h, failed on the
  read-only root (`cannot create /var/lib/apt/daily_lock`), and `resident world` delivered a `failed-unit`
  event to the inbox — the guarantee working as intended, but the unit should not exist in this image.
- `resident-world` should save the fingerprint hashes with a fixed identity so a root `docker exec`
  and the timer never disagree (today, files unreadable to the caller are keyed by mtime/size).

## Files

```
image/
  Dockerfile              debian:bookworm-slim + systemd, dbus, python3, iproute2, procps, ca-certificates, curl, jq, less
  agentos.py              verbatim copy of ../lib/agentos.py (re-sync before building)
  resident-init            PID-1 shim: hydrate tmpfs /etc from /usr/share/factory/etc, restore machine-id, exec systemd
  seed-intents.py         replays intents.json through the real `intent`/`resident` CLIs on first boot
  intents.json            the seeded-intent contract (18 entries)
  units/                  resident-firstboot.service, resident-world.service, resident-world.timer
```

Image size: ~152 MB (`docker images resident-image:dev`).
