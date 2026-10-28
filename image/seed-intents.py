#!/usr/bin/env python3
"""Seed the resident intent log from intents.json so the image explains itself from first boot.

Each entry: {"text": why, "adopt": [regex, ...], "kind": optional, "verify": optional,
             "pre": optional shell command run just before adopting (e.g. wake a D-Bus-activated service),
             "adopt_dpkg_priority": ["required", "important"]  # expands to one anchored regex at run time}
Entries are processed in order; `intent adopt` only claims state that has no owner yet, so put
specific reasons first and catch-alls last. Drives the real CLI (intent/resident) — no private API.
"""
import json
import os
import re
import subprocess
import sys


def run(*argv, check=True):
    r = subprocess.run(argv, capture_output=True, text=True)
    if check and r.returncode != 0:
        print(f"seed: {' '.join(argv)!r} failed ({r.returncode}): {r.stderr.strip()}", file=sys.stderr)
    return r.stdout


def dpkg_by_priority(prios):
    out = run("dpkg-query", "-W", "-f=${Package} ${Priority}\n", check=False)
    names = sorted(p for p, _, pr in (l.partition(" ") for l in out.splitlines()) if pr.strip() in prios)
    return ["^(" + "|".join(re.escape(n) for n in names) + ")$"] if names else []


def main():
    spec_path = sys.argv[1] if len(sys.argv) > 1 else "/usr/lib/resident/intents.json"
    os.environ.setdefault("RESIDENT_AUTHOR", "image")
    if "--no-wait" not in sys.argv:
        run("systemctl", "is-system-running", "--wait", check=False)  # explain the booted system, not a half-started one
    with open(spec_path) as fh:
        spec = json.load(fh)
    total = 0
    for e in spec:
        pats = list(e.get("adopt", []))
        if e.get("adopt_dpkg_priority"):
            pats += dpkg_by_priority(set(e["adopt_dpkg_priority"]))
        add = ["intent", "add", e["text"]] + (["--verify", e["verify"]] if e.get("verify") else [])
        iid = run(*add).strip()
        if not iid.isdigit():
            sys.exit(f"seed: intent add failed for {e['text']!r}")
        if e.get("pre"):
            subprocess.run(e["pre"], shell=True, capture_output=True)
        n = 0
        if pats:
            adopt = ["intent", "adopt"] + (["--kind", e["kind"]] if e.get("kind") else []) + [iid] + pats
            out = run(*adopt)
            n = int(out.split()[1]) if out.startswith("adopted") else 0
        total += n
        print(f"#{iid:>3} adopted {n:4d}  {e['text'][:90]}")
    run("resident", "snapshot")
    run("resident", "world", "--no-drift")
    print(f"seed: {len(spec)} intents, {total} items attributed")
    print(run("resident", "score"))


if __name__ == "__main__":
    main()
