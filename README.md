# Resident

**Make your machine your own assistant.**

Resident is a **Linux distribution**. You install it on a computer you own — a mini-PC, an old desktop,
a Raspberry Pi — and that computer becomes a home for an AI assistant. You talk to it, give it tasks,
and it does them on that machine.

> **Status: early prototype.** The bootable image is being built and tested now. Today the core runs
> on an existing Debian or Ubuntu machine and as a container image (see [Quick start](#quick-start-the-host-layer-debianubuntu-no-root)).

## In plain words

- **It's your machine.** The assistant lives on hardware you own, not in a company's cloud. Your files,
  apps and logins stay on it.
- **You hold the keys.** Passwords, tokens and API keys are stored on the machine and sealed to it.
  Nothing is shared unless you allow it.
- **It just does the work.** Writing code, running its own apps, keeping backups, fixing what breaks —
  it handles that without asking.
- **It asks before anything that matters:** sending data off the machine, using one of your accounts,
  spending money, deleting things, or doing something you haven't allowed before.
- **Only your phone can say yes.** Approvals need a passkey on your phone. Agents can ask; they can't approve.
- **You choose where copies go:** only this machine, your own GitHub, or your own cloud drive.
- **It can always tell you why.** Every app, service and setting on the machine has a recorded reason.
  Ask *"why is this running?"* and it answers — or tells you plainly that nothing explains it.

## Why a distro, and not an app

An app runs inside an operating system built for people. To be useful it ends up with the keys to
everything, and nothing on the machine records what it did or why.

Resident is the operating system itself, built for an assistant from the first boot:

- the base system is **read-only**, so the assistant can't quietly change it;
- every change is **recorded with a reason** and can be undone;
- the assistant runs as **its own user**, with only the permissions you grant;
- each helper it creates gets **its own sandbox**, with less access than the one that created it.

You get the convenience of assistants like Meta's Muse, without handing your machine and your keys to someone else.

## What runs differently from other distros

Underneath it is ordinary Debian: the same kernel, packages and systemd. What's different is how the
machine is put together and who it's built for.

| | a typical distro | Resident |
|---|---|---|
| **System files** | Writable; anything with root can change them | **Read-only and verified** (erofs + dm-verity): a changed byte fails verification at boot |
| **Updates** | Packages changed in place | **Whole-image A/B updates**: the new version goes into the spare slot; if it fails to boot three times, the machine falls back on its own |
| **Boot** | Bootloader + kernel + initrd as separate files | **One unified boot image** (systemd-boot + UKI), so the kernel, its command line and the root's hash travel together |
| **`/etc`** | Accumulates edits forever | **Rebuilt from the image at every boot**; local changes live in a recorded overlay |
| **Who runs it** | A human at a keyboard | **An assistant** (`agentd`) running as its own user, `agent`, from first boot |
| **Permissions** | sudo: all or nothing | **A readable policy file** plus polkit rules: the assistant may manage only its own services, and privileged steps need a recorded intent |
| **Memory of changes** | Shell history, if anything | **An intent log**: every service, port, package, schedule and config file has a reason, and `why` answers for any of them |
| **Approvals** | Typing a password | **A passkey on your phone**; agents can ask, only the passkey can approve |
| **More software** | `apt install` into the live system | **Helpers in sandboxed containers**, each with less access than its parent, or signed system extensions |
| **Interface** | A desktop | **A conversation with the assistant**, on your phone or in a browser; a desktop is optional |

Status, plainly: the read-only verified root, the unified boot image and A/B partition layout are built and
boot in a VM. The intent log, helpers, broker and passkey approvals run today on the prototype host. The
assistant-as-its-own-user and A/B update rollback are written but not yet exercised on the bootable image.

## How it works

- **Config is a cache; intent is the truth.** Every service, listening port, package, container, cron line
  and `/etc` file should have a recorded reason. `why 3000` answers *"intent #1: run the shop dashboard,
  added by the owner on 2026-10-28"*.
- **Change through intents, report outcomes.** `intent add "<goal>" --verify "<check>"`, then
  `intent apply <id> -- <command>`: the machine is fingerprinted before and after, every change is attributed
  to the intent, and what gets reported is whether the *goal* was met, not the exit code.
- **A world model instead of probing.** `machine.md`, regenerated every 10 minutes and about 2k tokens, tells
  the assistant what's on the machine, what's unexplained, what drifted and what failed.
- **An individual is a body plus its record of reasons.** Destroy the container *and* its volume, replay the
  record onto a new body, and the same individual comes back.
- **A head agent raises helpers.** A helper's permissions must fit inside its parent's. Requests outside a
  helper's permissions go to you; if nobody above it holds the permission, the request is denied.

## What's here

| path | what |
|---|---|
| `lib/agentos.py` | `resident` / `intent` / `why`: intent log (SQLite), fingerprints, drift, `machine.md`, event bus |
| `lib/spawn.py` | individuals: `spawn`, `dump`, `replay`, `kill`, `exec` |
| `lib/tree.py` | head and children: policy subset checks, broker, approvals, `tree`, `retire` |
| `agentd/` | resident runtime on the Claude Agent SDK: one long-lived session, woken by events |
| `percept/` | consented activity awareness: active window title, idle/lock state, system events (X11/GNOME) |
| `browser/` | headless Chromium as a service: `resident-browse <url>`, visits logged, MCP server |
| `image/` | container image: Debian, read-only root, `/etc` rebuilt from factory, every part explained at first boot |
| `image-vm/` | bootable image recipe (mkosi): UKI + systemd-boot, dm-verity A/B roots, sysupdate, the agent as a polkit principal. **Not yet booted.** |
| `examples/policies/` | a head and three children, including one that over-reaches on purpose |
| `contrib/systemd/` | user units for the timer, broker, agentd, percept and browser |
| `stage1/` | the A/B harness and report |

## Quick start: the host layer (Debian/Ubuntu, no root)

```sh
git clone https://github.com/siddharthshah87/resident ~/resident
mkdir -p ~/.local/bin
for n in resident intent why; do ln -s ~/resident/lib/agentos.py ~/.local/bin/$n; done

resident score              # how much of your machine has no recorded reason (expect ~100%)
resident world              # write ~/resident/machine.md
why 22                      # ...and start explaining it
i=$(intent add "Keep a note in /tmp" --verify "test -f /tmp/note") && intent apply $i -- touch /tmp/note
```

To have every Claude Code session start from the world model, add `@~/resident/machine.md` to
`~/.claude/CLAUDE.md`. Measured trade-offs are below.

## A family of agents (Docker)

```sh
docker build -t resident-image:dev ~/resident/image
mkdir -p ~/resident/policies && cp ~/resident/examples/policies/head.toml ~/resident/policies/
resident spawn ops --parent head --policy ~/resident/examples/policies/ops.toml
cp ~/resident/contrib/systemd/resident-broker.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now resident-broker

docker exec -i ops sh -c 'cat > /tmp/p.toml' < ~/resident/examples/policies/ops-reviews.toml
docker exec ops resident request spawn ops-reviews --policy-file /tmp/p.toml   # a grandchild, raised by ops
docker exec ops-reviews resident request container.inspect shop-app-web      # outside its policy → escalates
resident tree
resident approvals && resident approve <id>      # or: resident deny <id>
```

## Measurements (Stage 1)

The same 20 read-only ops questions about one machine, run under five configurations; all 100 answers were
correct. Full method, caveats and numbers: [stage1/REPORT.md](stage1/REPORT.md).

| configuration | cost | turns | time |
|---|---|---|---|
| no world model, fresh session per question | **$2.07** | 68 | 455 s |
| world model, one resident session | $2.37 | 47 | 350 s |
| world model via system prompt, fresh sessions | $2.49 | 41 | 283 s |
| no world model, one resident session | $2.58 | 54 | 443 s |
| world model via `CLAUDE.md`, fresh sessions | $2.91 | 39 | **279 s** |

The world model wins on turns, tool calls and time in every pairing. On dollars it **loses** for short questions
in fresh sessions, because injected context is re-written to the prompt cache at the start of every session.
A resident runtime on the Agent SDK writes it once. In a small follow-up (5 questions) that brought the marginal
cost per question below the no-context baseline ($0.077 vs $0.098). The original ≥2× token target was **not**
met (1.65×). The report says so.

## Using Claude with Resident

Resident's assistant runs on the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview), which runs the
unmodified Claude Code binary. Anthropic's [terms](https://code.claude.com/docs/en/legal-and-compliance) shape how that works:

- **Bring your own API key.** Each owner uses their own Anthropic API key (or Amazon Bedrock, Google Cloud or Microsoft
  Foundry credentials), billed to them. Resident does not offer "sign in with your Claude account" and does not store
  Claude.ai credentials: Anthropic does not allow third-party products built on the Agent SDK to do that.
- **Shipped as published.** The Claude Code binary is included unmodified, with its sign-in methods intact. Usage is
  never resold or routed through anyone else's account.
- **Naming.** Resident runs Claude Code and is powered by Claude; it is not made or endorsed by Anthropic.
- **Health data.** Anthropic's BAA covers Claude Code only under a Claude for Enterprise HIPAA configuration or Zero Data
  Retention, and not Remote Control or the mobile app. Don't point Resident at patient information without that in place.

## Safety

- **Approvals are not yet tied to the owner.** Any local process running as your user can run `resident approve`.
  Binding approvals to a passkey or phone is the next piece of work.
- **The head agent runs as your user.** If your user is in the `docker` group, that is root-equivalent.
  Children are contained (read-only root, memory and CPU limits, no Docker socket, a fixed verb table), but the head is not.
- **percept** records window titles and app names only: no screenshots, keystrokes or clipboard. It redacts
  credential-looking titles, keeps 24 hours, records nothing while the screen is locked, and stops with `percept off`.
- **The browser profile holds logins.** Treat it like a password store. Every visit is logged.

## Roadmap

1. Approvals bound to the owner's passkey or phone.
2. `agentd` inside each body: every child gets its own pulse.
3. Boot `image-vm/` on real hardware (a second SSD, then a Raspberry Pi); A/B updates with automatic rollback.
4. A first-boot interview whose answers become the head agent's base intents (network, owner, model, remote access, purpose).
5. End-to-end encrypted backup and one-command respawn of every individual onto new hardware.

## Name and license

*Resident* because the agent lives on the machine rather than visiting it. The CLI also answers to `agentos`
(the project's working name) for compatibility.

Copyright (C) 2026 Siddharth Shah. Licensed under the [GNU Affero General Public License v3.0 or later](LICENSE):
you may use, study and modify Resident, but if you distribute a modified version, or run one as a network
service for others, you must publish your changes under the same license.
