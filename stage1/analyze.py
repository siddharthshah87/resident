#!/usr/bin/env python3
"""Summarize Stage 1 results.jsonl: tokens, cost, turns, tool calls per condition; each condition vs `bare`."""
import collections, json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "baseline"))
from stage0_tokens import classify_tool  # noqa: E402

PROJ = os.path.expanduser("~/.claude/projects/" + os.path.join(HERE, "work").replace("/", "-"))
EXPECT_MD = {"world": True, "bare": False, "sys": False, "world-res": True, "bare-res": False}
from datetime import datetime, timezone
def _epoch(ts):
    try: return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception: return None

def session_tools(sid, start=None, end=None):
    f = os.path.join(PROJ, f"{sid}.jsonl"); calls = collections.Counter(); chars = collections.Counter(); id2 = {}
    turn1 = None; seen = set()
    if not os.path.exists(f): return calls, chars, turn1
    for line in open(f, encoding="utf-8", errors="replace"):
        try: o = json.loads(line)
        except ValueError: continue
        if start is not None:
            e = _epoch(o.get("timestamp", ""))
            if e is None or e < start - 2 or e > end + 2: continue
        m = o.get("message") or {}
        if o.get("type") == "assistant":
            u = m.get("usage") or {}
            if u and m.get("id") not in seen:
                seen.add(m.get("id"))
                if turn1 is None: turn1 = (u.get("cache_creation_input_tokens", 0), u.get("cache_read_input_tokens", 0))
            for c in m.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    cls = classify_tool(c.get("name"), c.get("input")); calls[cls] += 1; id2[c.get("id")] = cls
        elif o.get("type") == "user" and isinstance(m.get("content"), list):
            for c in m["content"]:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    cont = c.get("content"); n = len(cont) if isinstance(cont, str) else sum(len(x.get("text", "")) for x in cont or [] if isinstance(x, dict))
                    chars[id2.get(c.get("tool_use_id"), "other")] += n
    return calls, chars, turn1

rows = [json.loads(l) for l in open(os.path.join(HERE, "results.jsonl")) if l.strip()]
by = collections.defaultdict(dict)
for r in rows: by[r["_task"]][r["_cond"]] = r
# `claude -p --resume` reports total_cost_usd cumulatively over the session: de-cumulate resident conditions
for c in ("world-res", "bare-res"):
    prev = 0.0
    for t in sorted(by):
        r = by[t].get(c)
        if not r or r.get("total_cost_usd") is None: continue
        cum = r["total_cost_usd"]; r["total_cost_usd"] = round(cum - prev, 4); prev = cum
conds = [c for c in ("world", "sys", "bare", "world-res", "bare-res") if any(c in by[t] for t in by)]

def tok(r):
    u = r.get("usage") or {}
    return sum(u.get(k, 0) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))

agg = {c: collections.Counter() for c in conds}
print("| task | cond | tokens | turn1 write/read | cost $ | turns | tools | discovery | disc chars | wall s |")
print("|---|---|---|---|---|---|---|---|---|---|")
for t in sorted(by):
    for c in conds:
        r = by[t].get(c)
        if not r: continue
        if r.get("_claude_md_present") is not None and r["_claude_md_present"] != EXPECT_MD[c]:
            print(f"| {t} | {c} | INVALID: CLAUDE.md present={r['_claude_md_present']} |"); continue
        if r.get("error") or r.get("is_error"):
            print(f"| {t} | {c} | ERROR {str(r.get('error') or r.get('result'))[:60]} |"); continue
        calls, chars, t1 = session_tools(r.get("session_id", ""), r.get("_start"), r.get("_end")); u = r.get("usage") or {}
        v = dict(tokens=tok(r), cost=r.get("total_cost_usd", 0) or 0, turns=r.get("num_turns", 0), tools=sum(calls.values()),
                 disc=calls["discovery"], dchars=chars["discovery"], wall=r.get("_wall", 0), n=1,
                 cw=u.get("cache_creation_input_tokens", 0), cr=u.get("cache_read_input_tokens", 0), t1w=(t1 or (0, 0))[0])
        for k, x in v.items(): agg[c][k] += x
        print(f"| {t} | {c} | {v['tokens']:,} | {v['t1w']:,}/{(t1 or (0,0))[1]:,} | {v['cost']:.3f} | {v['turns']} | {v['tools']} | {v['disc']} | {v['dchars']:,} | {v['wall']:.0f} |")
print()
def compare(c, base):
    w, b = agg[c], agg[base]
    print(f"**{c} vs {base} — {w['n']} tasks per condition**\n")
    print(f"| metric | {c} | {base} | {base} ÷ {c} |\n|---|---|---|---|")
    for k, lab in (("tokens", "total tokens"), ("cost", "cost $"), ("turns", "turns"), ("tools", "tool calls"), ("disc", "discovery calls"),
                   ("dchars", "discovery chars"), ("wall", "wall seconds"), ("cw", "cache-write tokens"), ("t1w", "turn-1 cache-write")):
        fmt = (lambda x: f"{x:.2f}") if k == "cost" else (lambda x: f"{x:,.0f}")
        ratio = (b[k] / w[k]) if w[k] else float("nan")
        print(f"| {lab} | {fmt(w[k])} | {fmt(b[k])} | {ratio:.2f}× |")
    print()
pairs = [(c, "bare") for c in ("world", "sys") if c in conds and "bare" in conds]
if "world-res" in conds and "bare-res" in conds: pairs.append(("world-res", "bare-res"))
if "bare-res" in conds and "bare" in conds: pairs.append(("bare-res", "bare"))
if "world-res" in conds and "world" in conds: pairs.append(("world-res", "world"))
for c, base in pairs:
    if agg[c]["n"] and agg[base]["n"]: compare(c, base)
with open(os.path.join(HERE, "answers.md"), "w") as fh:
    tasks = dict(l.split("|", 1) for l in open(os.path.join(HERE, "tasks.txt")) if "|" in l)
    for t in sorted(by):
        fh.write(f"## Task {t}: {tasks.get(str(t), '').strip()}\n\n")
        for c in conds:
            r = by[t].get(c)
            if r: fh.write(f"**{c}** ({tok(r):,} tok, {r.get('num_turns', '?')} turns):\n\n{(r.get('result') or r.get('error') or '')[:1200]}\n\n")
print(f"[answers → {os.path.join(HERE, 'answers.md')}]")
