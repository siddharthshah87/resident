# agentd — resident agent runtime

One persistent Claude Agent SDK session (`ClaudeSDKClient`) answers events dropped
into `~/agentos/inbox/`. The world model (`~/agentos/machine.md`) is placed in the
system prompt once per session instead of being re-injected via `~/.claude/CLAUDE.md`
on every `claude -p` call (stage1 finding: ~10.1k tokens re-written per fresh session).

## Layout
- `agentd.py` — daemon + CLI (venv shebang; `~/.local/bin/agentd` symlinks here)
- `.venv/` — `claude-agent-sdk` (pip)
- `wakes.jsonl` — one line per wake (ts, kind, session_id, new_session, num_turns,
  duration_ms, total_cost_usd (cumulative per session), cost_delta_usd, usage
  {input, cache_creation, cache_read, output}, per_turn [{i, cc, cr, in, out}])
- `agentd.log` — daemon stderr; `state.json` — last wake
- unit: `~/.config/systemd/user/agentd.service` (Restart=always, flock single instance)

## Event contract (consumer)
`~/agentos/inbox/<epoch>-<kind>.json`:
```json
{"ts":"<iso>","kind":"ask|drift|failed-unit|system","summary":"<one line>","detail":"<text>","reply_to":"<optional path>"}
```
Poll every 2 s. Each event = one agent query. File moves to `inbox/done/`, answer goes to
`~/agentos/outbox/<epoch>-<kind>.md` (+ `reply_to` if given). Non-`ask` kinds get a
read-only diagnose/explain preamble.

## CLI
```
agentd ask "Is anything listening on 3000?"   # drops ask event, waits ≤180 s, prints answer
agentd status                                  # last 20 wakes table
agentd run                                     # daemon (what the unit runs)
```

## Agent configuration
- `allowed_tools=[Bash, Read, Grep, Glob]`, `disallowed_tools=[Edit, Write, NotebookEdit, WebSearch, WebFetch]`,
  default permission mode (no bypass), `max_turns=40`, `cwd=~/agentos`.
- `setting_sources=[]` → `~/.claude/CLAUDE.md` is NOT loaded (no double world model).
- `system_prompt` = claude_code preset + append(protocol + machine.md). `AGENTD_SYSPROMPT=custom`
  swaps in a fully custom string prompt (no Claude Code preset). See REPORT below for the measurement.

## Residency policy (new session when any holds)
- no session / previous wake errored
- last turn `cache_read_input_tokens` > 60 000
- idle > 60 min
- `machine.md` mtime changed AND session older than 10 min

## Ops
```
systemctl --user status agentd            # running?
tail -f ~/agentos/agentd/agentd.log
systemctl --user restart agentd           # forces a new session (re-reads machine.md)
```
