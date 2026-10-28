# Stage 1 — does a world model (machine.md) pay for itself? my-box, 2026-10-27

20 read-only ops questions about this box, identical across conditions. All 100 answers graded correct in substance.
Fresh = one `claude -p` session per question. Resident = all 20 questions in one `--resume` session (cost de-cumulated; Claude Code reports it cumulatively).

| condition | context | cost | tokens | turns | tool calls | wall |
|---|---|---|---|---|---|---|
| **bare** (fresh) | none | **$2.07** | 1.68M | 68 | 48 | 455 s |
| **world-res** (resident) | CLAUDE.md → machine.md, written once | $2.37 | 4.41M | 47 | 27 | 350 s |
| **sys** (fresh) | machine.md via --append-system-prompt | $2.49 | 1.07M | 41 | 21 | 283 s |
| **bare-res** (resident) | none; transcript grows | $2.58 | 5.14M | 54 | 34 | 443 s |
| **world** (fresh) | CLAUDE.md → machine.md | $2.91 | 1.02M | 39 | 19 | **279 s** |

## What held

- The world model wins every efficiency metric in every pairing. Fresh: 1.65× fewer tokens, 1.74× fewer turns, 2.5× fewer tool calls, 5.4× less discovery output, **1.63× faster**. Resident: 1.15× fewer turns, 1.27× faster, 1.09× cheaper than bare-res.
- Protocol v1 taxed questions the snapshot couldn't answer (probing tasks 0.80×); v2 wording fixed it (1.42×).
- Quality defects induced by v1 (stale snapshot used for a "now" question; a rewritten cron line quoted as verbatim) were fixed and did not recur. One persistent minor omission: GNOME Remote Desktop dropped from the remote-access list in world/sys.

## What failed

- **Falsifier (≥2× tokens on the mixed set): failed at 1.65×.** On dollars it failed harder: fresh bare is the cheapest configuration; the world model costs +15% (resident) to +40% (fresh CLAUDE.md) on this workload.
- Mechanism, isolated per session: with no CLAUDE.md, every fresh session reads its whole ~19k prompt from cache and writes 0. With CLAUDE.md, ~10.1k tokens are re-written at turn 1 of every session regardless of content changes (Claude Code places memory in "dynamic system prompt sections" past the cache boundary). `--append-system-prompt` cuts that to ~6.8k, not 0. 203k rewritten tokens = the entire $0.84 gap in v2.
- Residency amortizes that tax (world-res −19% vs fresh world) but accumulates transcript: by task 20 each turn reads ~150k tokens; bare-res is +25% vs fresh bare.

## Conclusion

For short, ad-hoc questions in fresh sessions (3.4 turns avg), per-box context is dollar-negative under Claude Code's current prompt caching: the saved turns are worth less than the fixed per-session write. It is strongly positive on time, turns, provenance and findings (unattended-upgrades dead since March; 33.8 GB of reclaimable Docker images; dead test container; AnyDesk on the LAN with no owner).
If the agent is going to be resident anyway, the world model is what makes residency affordable (world-res is the cheapest resident configuration), and the cost that remains is transcript growth, which the world model + intent log exist to replace.
The tax is an artifact of `claude -p` prompt assembly, not of the model or API: a resident agent built on the Agent SDK with explicit cache breakpoints (machine.md inside the cached system prefix, short context per wake, memory in the intent log) should pay the write once per cache TTL and read it thereafter. That is the next experiment.

Spend for all runs tonight ≈ $22 including two discarded contaminated batches.
Files: tasks.txt, run.sh (world|bare|sys), run_resident.sh (world-res|bare-res), analyze.py, results.jsonl (100 rows), results.v1/v2.jsonl, answers*.md.

_Published copy: two task names in tasks.txt were generalized to avoid identifying the test machine's owner; the questions are otherwise as run._
