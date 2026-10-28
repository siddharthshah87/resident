#!/usr/bin/env python3
"""agentd — resident agent runtime for Resident.

Watches ~/agentos/inbox/ for <epoch>-<kind>.json events, answers each with ONE
query to a persistent ClaudeSDKClient session (world model = machine.md in the
system prompt, written once per session), writes ~/agentos/outbox/<epoch>-<kind>.md,
moves the event to inbox/done/, and logs every wake to agentd/wakes.jsonl.

Usage:
  agentd run                  # daemon (systemd unit agentd.service)
  agentd ask "<question>"     # drop an ask event, wait ≤180s, print the answer
  agentd status               # last wakes summary
Env:
  AGENTD_SYSPROMPT=preset|custom   (default preset: claude_code preset + append)
"""
import asyncio
import fcntl
import json
import os
import shutil
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(os.environ.get("RESIDENT_HOME") or os.environ.get("AGENTOS_HOME") or Path.home() / "resident")
INBOX = BASE / "inbox"
DONE = INBOX / "done"
OUTBOX = BASE / "outbox"
HERE = BASE / "agentd"
WAKES = HERE / "wakes.jsonl"
LOCK = HERE / "agentd.lock"
MACHINE = BASE / "machine.md"
STATE = HERE / "state.json"

POLL_S = 2
QUERY_TIMEOUT_S = 300
# residency policy
MAX_CACHE_READ = 60_000      # recycle when last turn read > this
MAX_IDLE_S = 60 * 60         # recycle after 60 min idle
MIN_AGE_FOR_REFRESH_S = 600  # recycle on machine.md change only if session > 10 min old

PROTOCOL = (
    "You are the resident agent on this machine. The world model below is a snapshot "
    "(≤10 min old). For 'right now' metrics probe live. Read-only question → answer it; "
    "don't spend turns on `why`/`intent` unless the question is about provenance or you "
    "are about to change something. Before any change: `intent add` then "
    "`intent apply <id> -- <cmd>`; report the verify result. Answer concisely."
)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(*a):
    print(now_iso(), *a, file=sys.stderr, flush=True)


# ----------------------------------------------------------------------------- agent
class Resident:
    """One persistent SDK session, recycled per the residency policy."""

    def __init__(self, mode: str):
        self.mode = mode  # preset | custom
        self.client = None
        self.session_id = None
        self.started = 0.0
        self.last_used = 0.0
        self.machine_mtime = 0.0
        self.last_cache_read = 0
        self.prev_cost = 0.0
        self.turn_index = 0  # assistant messages so far in this session

    # -- policy
    def needs_new_session(self):
        if self.client is None:
            return True, "no-session"
        t = time.time()
        if self.last_cache_read > MAX_CACHE_READ:
            return True, f"cache_read>{MAX_CACHE_READ}"
        if t - self.last_used > MAX_IDLE_S:
            return True, "idle>60min"
        try:
            mt = MACHINE.stat().st_mtime
        except FileNotFoundError:
            mt = 0
        if mt != self.machine_mtime and t - self.started > MIN_AGE_FOR_REFRESH_S:
            return True, "machine.md-changed"
        return False, "resident"

    # -- options
    def _options(self):
        from claude_agent_sdk import ClaudeAgentOptions

        world = MACHINE.read_text() if MACHINE.exists() else "(machine.md missing)"
        append = PROTOCOL + "\n\n# World model (machine.md)\n\n" + world
        if self.mode == "custom":
            system_prompt = (
                "You are Claude, an ops agent with shell access (Bash, Read, Grep, Glob) "
                "on the Linux machine " + socket.gethostname() + ". Use tools to verify when needed, "
                "then answer in plain Markdown.\n\n" + append
            )
        else:
            system_prompt = {"type": "preset", "preset": "claude_code", "append": append}
        return ClaudeAgentOptions(
            system_prompt=system_prompt,
            allowed_tools=["Bash", "Read", "Grep", "Glob"],
            disallowed_tools=["Edit", "Write", "NotebookEdit", "WebSearch", "WebFetch"],
            setting_sources=[],  # do NOT load ~/.claude/CLAUDE.md (world model would load twice)
            cwd=str(BASE),
            max_turns=40,
        )

    async def start(self):
        from claude_agent_sdk import ClaudeSDKClient

        await self.stop()
        self.machine_mtime = MACHINE.stat().st_mtime if MACHINE.exists() else 0
        self.client = ClaudeSDKClient(self._options())
        await self.client.connect()
        self.started = self.last_used = time.time()
        self.session_id = None
        self.last_cache_read = 0
        self.prev_cost = 0.0
        self.turn_index = 0
        log("new session", self.mode)

    async def stop(self):
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception as e:  # noqa
                log("disconnect error", e)
        self.client = None

    # -- one wake
    async def wake(self, prompt: str):
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

        fresh, reason = self.needs_new_session()
        if fresh:
            await self.start()
        texts, per_turn, result = [], [], None
        t0 = time.time()
        await self.client.query(prompt)
        async for msg in self.client.receive_response():
            if isinstance(msg, AssistantMessage):
                u = msg.usage or {}
                # the SDK emits one AssistantMessage per content block; count API turns once
                if per_turn and msg.message_id and msg.message_id == per_turn[-1].get("mid"):
                    for b in msg.content:
                        if isinstance(b, TextBlock) and b.text.strip():
                            texts.append(b.text)
                    continue
                self.turn_index += 1
                per_turn.append({
                    "mid": msg.message_id,
                    "i": self.turn_index,
                    "cc": u.get("cache_creation_input_tokens", 0),
                    "cr": u.get("cache_read_input_tokens", 0),
                    "in": u.get("input_tokens", 0),
                    "out": u.get("output_tokens", 0),
                })
                for b in msg.content:
                    if isinstance(b, TextBlock) and b.text.strip():
                        texts.append(b.text)
            elif isinstance(msg, ResultMessage):
                result = msg
        self.last_used = time.time()
        answer = (result.result if result and result.result else "\n\n".join(texts[-1:])) or "(no answer)"
        if result:
            self.session_id = result.session_id
        if per_turn:
            self.last_cache_read = per_turn[-1]["cr"]
        u = (result.usage if result else None) or {}
        cost = (result.total_cost_usd or 0.0) if result else 0.0
        rec = {
            "session_id": self.session_id,
            "new_session": fresh,
            "session_reason": reason,
            "sysprompt": self.mode,
            "num_turns": result.num_turns if result else None,
            "duration_ms": result.duration_ms if result else int((time.time() - t0) * 1000),
            "total_cost_usd": cost,
            "cost_delta_usd": round(cost - self.prev_cost, 6),
            "is_error": result.is_error if result else True,
            "usage": {
                "input": u.get("input_tokens", 0),
                "cache_creation": u.get("cache_creation_input_tokens", 0),
                "cache_read": u.get("cache_read_input_tokens", 0),
                "output": u.get("output_tokens", 0),
            },
            "per_turn": per_turn,
        }
        self.prev_cost = cost
        return answer, rec


# ----------------------------------------------------------------------------- daemon
def event_prompt(ev: dict) -> str:
    kind = ev.get("kind", "ask")
    summary = ev.get("summary", "").strip()
    detail = (ev.get("detail") or "").strip()
    if kind == "ask":
        return f"{summary}\n\n{detail}".strip()
    if kind == "failed-unit":
        head = "A systemd unit failed. Diagnose root cause from logs (read-only) and propose the fix; do not change anything."
    elif kind == "drift":
        head = "Drift was recorded (state changed outside `intent apply`). Explain what changed, likely cause, and whether it should be adopted into an intent."
    else:
        head = "System event. Assess and summarise what, if anything, needs attention."
    return f"[{kind}] {head}\n\nSummary: {summary}\n\nDetail:\n{detail}".strip()


def pending_events():
    return sorted(p for p in INBOX.glob("*.json") if p.is_file())


async def daemon():
    for d in (INBOX, DONE, OUTBOX, HERE):
        d.mkdir(parents=True, exist_ok=True)
    lock_fd = open(LOCK, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another agentd holds the lock; exiting")
        return 1
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        os.environ.pop(k, None)
    res = Resident(os.environ.get("AGENTD_SYSPROMPT", "preset"))
    log("agentd up; watching", INBOX)
    while True:
        evs = pending_events()
        if not evs:
            # idle housekeeping: drop a session idle > 60 min so the next wake re-reads the world
            if res.client is not None and time.time() - res.last_used > MAX_IDLE_S:
                await res.stop()
            await asyncio.sleep(POLL_S)
            continue
        for path in evs:
            await handle(res, path)


async def handle(res: Resident, path: Path):
    try:
        ev = json.loads(path.read_text())
    except Exception as e:  # noqa
        log("bad event", path, e)
        shutil.move(str(path), DONE / (path.stem + ".bad.json"))
        return
    kind = ev.get("kind", "ask")
    stem = path.stem  # <epoch>-<kind>
    out = OUTBOX / f"{stem}.md"
    log("wake", stem, "|", (ev.get("summary") or "")[:80])
    t0 = time.time()
    try:
        answer, rec = await asyncio.wait_for(res.wake(event_prompt(ev)), QUERY_TIMEOUT_S)
    except Exception as e:  # noqa (timeout, SDK/process errors)
        log("wake failed", stem, repr(e))
        answer = f"agentd error: {e!r}"
        rec = {"session_id": res.session_id, "new_session": False, "error": repr(e),
               "duration_ms": int((time.time() - t0) * 1000)}
        await res.stop()
    rec = {"ts": now_iso(), "kind": kind, "event": stem, **rec}
    with WAKES.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    body = f"# {kind}: {ev.get('summary','')}\n\n{answer}\n\n---\n_agentd {rec['ts']} session={rec.get('session_id')} new={rec.get('new_session')} turns={rec.get('num_turns')} cost_delta=${rec.get('cost_delta_usd')}_\n"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(body)
    os.replace(tmp, out)  # atomic: `ask` waits for the .md
    if ev.get("reply_to"):
        try:
            Path(ev["reply_to"]).write_text(body)
        except Exception as e:  # noqa
            log("reply_to failed", e)
    shutil.move(str(path), DONE / path.name)
    STATE.write_text(json.dumps({"last_wake": rec["ts"], "session_id": rec.get("session_id")}))


# ----------------------------------------------------------------------------- cli
def cli_ask(question: str, timeout: int = 180) -> int:
    INBOX.mkdir(parents=True, exist_ok=True)
    epoch = int(time.time() * 1000)
    name = f"{epoch}-ask"
    ev = {"ts": now_iso(), "kind": "ask", "summary": question, "detail": "", "reply_to": ""}
    tmp = INBOX / f".{name}.json.tmp"
    tmp.write_text(json.dumps(ev))
    os.replace(tmp, INBOX / f"{name}.json")
    out = OUTBOX / f"{name}.md"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if out.exists():
            print(out.read_text())
            return 0
        time.sleep(0.5)
    print(f"agentd: timed out after {timeout}s waiting for {out}", file=sys.stderr)
    return 2


def cli_status() -> int:
    if not WAKES.exists():
        print("no wakes yet")
        return 0
    rows = [json.loads(l) for l in WAKES.read_text().splitlines() if l.strip()]
    print(f"{'ts':20} {'kind':11} {'new':3} {'turns':5} {'cc':>7} {'cr':>8} {'out':>5} {'cost_d':>8} {'ms':>7}  event")
    for r in rows[-20:]:
        u = r.get("usage", {})
        print(f"{r['ts'][:19]:20} {r['kind']:11} {str(r.get('new_session'))[:1]:3} {str(r.get('num_turns')):5} "
              f"{u.get('cache_creation',0):>7} {u.get('cache_read',0):>8} {u.get('output',0):>5} "
              f"{r.get('cost_delta_usd',0):>8.4f} {r.get('duration_ms',0):>7}  {r.get('event')}")
    return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "run"
    if cmd == "run":
        return asyncio.run(daemon()) or 0
    if cmd == "ask":
        if len(argv) < 3:
            print('usage: agentd ask "<question>"', file=sys.stderr)
            return 1
        return cli_ask(" ".join(argv[2:]))
    if cmd == "status":
        return cli_status()
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
