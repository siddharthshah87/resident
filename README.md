# Resident

**Make your machine your own assistant.**

Resident turns a Linux box into a home for an AI agent that you own. The agent lives on your hardware,
remembers *why* every part of the machine is the way it is, changes things only through recorded and
verified intents, and raises child agents in containers whose permissions are carved out of its own.

It started as a one-night experiment on a home server (an Intel NUC running Ubuntu 24.04) in October 2026,
and it is measured: see [Stage 1](stage1/REPORT.md).

> **Status: early prototype.** It runs on one machine. Read [Safety](#safety) before trying it anywhere you care about.

## The idea

- **Config is a cache; intent is the truth.** Every service, listening port, package, container, cron line
  and `/etc` file should have a recorded reason. `why 3000` answers *"intent #1: run the shop dashboard,
  added by the owner on 2026-10-28"*, or says plainly that nothing explains it.
- **Change through intents, report outcomes.** `intent add "<goal>" --verify "<check>"`, then
  `intent apply <id> -- <command>`: the machine is fingerprinted before and after, every change is attributed
  to the intent, and what gets reported is whether the *goal* was met, not the exit code.
- **A world model instead of probing.** `machine.md` (regenerated every 10 minutes, about 2k tokens) tells the
  agent what's on the box, what's unexplained, what drifted and what failed, so it doesn't re-discover the
  machine on every question.
- **An individual is a body plus its record of reasons.** Destroy the container *and* its volume, replay the
  intent log onto a new body, and you get the same individual back: same answers to `why`, same verified
  state, lineage recorded.
- **A head agent raises children.** A child's policy must fit inside its parent's: capabilities, the list
  of verbs that need a human, and budget. Children never get the Docker socket; they ask the broker for a
  fixed set of verbs. Requests outside a child's policy escalate to a human. If nobody in the chain holds
  the capability, the request is denied.

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

## Safety

- **Approvals are not yet tied to the owner.** Any local process running as your user can run `resident approve`.
  Binding approvals to a passkey or phone is the next piece of work.
- **The head agent runs as your user.** If your user is in the `docker` group, that is root-equivalent.
  Children are contained (read-only root, memory and CPU limits, no Docker socket, a fixed verb table), but the head is not.
- **percept** records window titles and app names only: no screenshots, keystrokes or clipboard. It redacts
  credential-looking titles, keeps 24 hours, records nothing while the screen is locked, and stops with `percept off`.
- **The browser profile holds logins.** Treat it like a password store. Every visit is logged.
- Budgets (`usd_per_day`) are checked when a child is created but **not yet metered**.

## Roadmap

1. Approvals bound to the owner's passkey or phone.
2. `agentd` inside each body: every child gets its own pulse, and spend is metered against its budget.
3. Boot `image-vm/` on real hardware (a second SSD, then a Raspberry Pi); A/B updates with automatic rollback.
4. A first-boot interview whose answers become the head agent's base intents (network, owner, model, remote access, purpose).
5. End-to-end encrypted backup and one-command respawn of every individual onto new hardware.

## Name and license

*Resident* because the agent lives on the machine rather than visiting it. The CLI also answers to `agentos`
(the project's working name) for compatibility.

Copyright (C) 2026 Siddharth Shah. Licensed under the [GNU Affero General Public License v3.0 or later](LICENSE):
you may use, study and modify Resident, but if you distribute a modified version, or run one as a network
service for others, you must publish your changes under the same license.
