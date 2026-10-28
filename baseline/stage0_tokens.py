#!/usr/bin/env python3
"""Stage 0 baseline: mine Claude Code session logs for token and tool-call patterns.

Answers: how much of what the agent reads is *discovery* (figuring out the machine)
versus *mutation* (changing it)? That share is the primary lever an intent log /
world model attacks.
"""
import collections
import glob
import json
import os
import re
import sys

PROJECTS = os.path.expanduser("~/.claude/projects")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stage0_report.md")

DISCOVERY_TOOLS = {"Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch", "ToolSearch", "NotebookRead"}
MUTATION_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
DELEGATE_TOOLS = {"Agent", "Task"}

DISCOVERY_VERBS = {
    "ls", "cat", "head", "tail", "less", "grep", "rg", "find", "fd", "ps", "df", "du", "ss", "netstat", "which",
    "whereis", "echo", "pwd", "wc", "stat", "file", "journalctl", "free", "uptime", "top", "htop", "id", "whoami",
    "hostname", "hostnamectl", "lsblk", "lscpu", "lsusb", "lspci", "ip", "ifconfig", "dig", "nslookup", "env",
    "printenv", "type", "command", "tree", "diff", "jq", "yq", "awk", "sed", "sort", "uniq", "cut", "tr", "xargs",
    "test", "[", "true", "date", "cal", "man", "help", "readlink", "realpath", "basename", "dirname", "md5sum",
    "sha256sum", "lsof", "nproc", "uname", "dmesg", "last", "w", "who", "tailscale", "nvidia-smi", "sensors",
}
MUTATION_VERBS = {
    "rm", "mv", "cp", "mkdir", "rmdir", "touch", "chmod", "chown", "ln", "tee", "apt", "apt-get", "dpkg", "snap",
    "pip", "pip3", "npm", "npx", "pnpm", "yarn", "cargo", "go", "make", "cmake", "truncate", "dd", "mkfs", "mount",
    "umount", "useradd", "usermod", "passwd", "kill", "pkill", "killall", "reboot", "shutdown", "crontab", "ufw",
    "iptables", "nft", "update-alternatives", "ldconfig", "setcap", "install", "rsync", "scp", "curl", "wget",
}
READ_ONLY_SUB = {
    "git": {"status", "log", "diff", "show", "branch", "remote", "blame", "ls-files", "rev-parse", "describe", "tag", "fetch"},
    "docker": {"ps", "logs", "inspect", "images", "stats", "version", "info", "port", "top", "compose ls", "compose ps", "compose logs"},
    "systemctl": {"status", "list-units", "list-unit-files", "list-timers", "show", "is-active", "is-enabled", "cat", "is-failed"},
    "gh": {"pr view", "pr list", "pr diff", "issue view", "issue list", "repo view", "run list", "run view", "api"},
    "kubectl": {"get", "describe", "logs", "top"},
    "crontab": {"-l"},
    "node": set(), "python3": set(), "python": set(), "bash": set(), "sh": set(), "zsh": set(),
}
SCRIPT_RUNNERS = {"node", "python3", "python", "bash", "sh", "zsh", "uv", "pytest", "jest", "vitest", "deno", "bun", "ruby", "java"}


PREFIX_WORDS = {"sudo", "nohup", "time", "env", "exec", "cd", "export", "timeout"}


def _strip_prefix(toks):
    """Drop env assignments and wrapper words so the real verb is first."""
    while toks and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]) or toks[0] in PREFIX_WORDS):
        if toks[0] in ("cd", "export", "timeout") and len(toks) > 1:
            toks = toks[2:]
        else:
            toks = toks[1:]
    return toks


def lead_verb(cmd):
    for seg in re.split(r"\|\||&&|;|\|", cmd or ""):
        toks = _strip_prefix(seg.strip().split())
        if toks:
            return os.path.basename(toks[0])
    return "?"


def classify_bash(cmd):
    """discovery | mutation | execute (runs code/tests) | other"""
    if not cmd:
        return "other"
    if re.search(r"(?<![<>])>{1,2}\s*(?!&|/dev/null)", cmd):
        return "mutation"  # redirect to a file
    if re.search(r"\bsed\s+-i\b", cmd):
        return "mutation"
    classes = set()
    for seg in re.split(r"\|\||&&|;|\|", cmd):
        toks = _strip_prefix(seg.strip().split())
        if not toks:
            continue
        verb = os.path.basename(toks[0])
        rest = " ".join(toks[1:])
        if verb in READ_ONLY_SUB and verb not in SCRIPT_RUNNERS:
            classes.add("discovery" if any(rest.startswith(s) for s in READ_ONLY_SUB[verb]) else "mutation")
        elif verb in SCRIPT_RUNNERS:
            classes.add("execute")
        elif verb in DISCOVERY_VERBS:
            classes.add("discovery")
        elif verb in MUTATION_VERBS:
            classes.add("mutation")
        else:
            classes.add("other")
    for c in ("mutation", "execute", "discovery", "other"):
        if c in classes:
            return c
    return "other"


def classify_tool(name, inp):
    if name in DISCOVERY_TOOLS:
        return "discovery"
    if name in MUTATION_TOOLS:
        return "mutation"
    if name in DELEGATE_TOOLS:
        return "delegate"
    if name == "Bash":
        return classify_bash((inp or {}).get("command", ""))
    return "other"


def result_chars(content):
    if isinstance(content, str):
        return len(content)
    n = 0
    for c in content or []:
        if isinstance(c, dict):
            if c.get("type") == "text":
                n += len(c.get("text", ""))
            elif c.get("type") == "tool_result":
                n += result_chars(c.get("content"))
    return n


def main():
    files = glob.glob(os.path.join(PROJECTS, "**", "*.jsonl"), recursive=True)
    tok = collections.Counter()
    tools = collections.Counter()
    classes = collections.Counter()
    res_chars = collections.Counter()
    bash_verbs = collections.Counter()
    per_session = []
    seen_msg = set()
    turns = 0

    for f in files:
        s_tok = collections.Counter()
        s_turns = 0
        id2class = {}
        try:
            fh = open(f, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                t = o.get("type")
                m = o.get("message") or {}
                if t == "assistant":
                    mid = m.get("id")
                    u = m.get("usage") or {}
                    if mid and mid not in seen_msg and u:
                        seen_msg.add(mid)
                        turns += 1
                        s_turns += 1
                        for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"):
                            v = u.get(k) or 0
                            tok[k] += v
                            s_tok[k] += v
                    for c in m.get("content") or []:
                        if isinstance(c, dict) and c.get("type") == "tool_use":
                            name = c.get("name", "?")
                            cls = classify_tool(name, c.get("input"))
                            tools[name] += 1
                            classes[cls] += 1
                            id2class[c.get("id")] = cls
                            if name == "Bash":
                                bash_verbs[lead_verb((c.get("input") or {}).get("command", ""))] += 1
                elif t == "user":
                    for c in (m.get("content") or []) if isinstance(m.get("content"), list) else []:
                        if isinstance(c, dict) and c.get("type") == "tool_result":
                            cls = id2class.get(c.get("tool_use_id"), "other")
                            res_chars[cls] += result_chars(c.get("content"))
        if s_turns:
            per_session.append((f, s_turns, sum(s_tok.values()), s_tok["output_tokens"]))

    total_in = tok["input_tokens"] + tok["cache_creation_input_tokens"] + tok["cache_read_input_tokens"]
    total = total_in + tok["output_tokens"]
    tool_total = sum(classes.values()) or 1
    res_total = sum(res_chars.values()) or 1
    per_session.sort(key=lambda x: -x[2])
    med = sorted(x[2] for x in per_session)
    median = med[len(med) // 2] if med else 0

    L = []
    L.append(f"# Stage 0 baseline — Claude Code on {os.uname().nodename}\n")
    L.append(f"Sessions with activity: **{len(per_session)}** (of {len(files)} log files) · assistant turns: **{turns:,}**\n")
    L.append("## Tokens (all sessions)\n")
    L.append("| | tokens |\n|---|---|")
    L.append(f"| fresh input | {tok['input_tokens']:,} |")
    L.append(f"| cache write | {tok['cache_creation_input_tokens']:,} |")
    L.append(f"| cache read | {tok['cache_read_input_tokens']:,} |")
    L.append(f"| output | {tok['output_tokens']:,} |")
    L.append(f"| **total** | **{total:,}** |")
    L.append(f"\nPer session: median {median:,} tokens, mean {total // max(len(per_session), 1):,}. Per turn: {total // max(turns, 1):,}.\n")
    L.append("## Tool calls by class\n")
    L.append("| class | calls | share | result chars | share of inbound tool volume |\n|---|---|---|---|---|")
    for cls in ("discovery", "mutation", "execute", "delegate", "other"):
        L.append(f"| {cls} | {classes[cls]:,} | {100 * classes[cls] / tool_total:.0f}% | {res_chars[cls]:,} | {100 * res_chars[cls] / res_total:.0f}% |")
    L.append(f"\n**Discovery share of inbound tool volume: {100 * res_chars['discovery'] / res_total:.0f}%** (≈{res_chars['discovery'] // 4:,} tokens). This is what a world model replaces.\n")
    L.append("## Tools\n")
    L.append(", ".join(f"{k} {v:,}" for k, v in tools.most_common(15)))
    L.append("\n\n## Top Bash verbs\n")
    L.append(", ".join(f"{k} {v:,}" for k, v in bash_verbs.most_common(25)))
    L.append("\n\n## Heaviest sessions\n")
    for f, n, t, out in per_session[:8]:
        proj = os.path.basename(os.path.dirname(f)).replace(os.path.expanduser("~").replace("/", "-") + "-", "~/").replace(os.path.expanduser("~").replace("/", "-"), "~")
        L.append(f"- {proj}: {n} turns, {t:,} tokens ({out:,} out)")
    text = "\n".join(L)
    with open(OUT, "w") as fh:
        fh.write(text + "\n")
    print(text)
    print(f"\n[written to {OUT}]", file=sys.stderr)


if __name__ == "__main__":
    main()
