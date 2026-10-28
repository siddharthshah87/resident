#!/usr/bin/env bash
# Residency test: all tasks in ONE long-lived session per condition (--resume), so per-box context is written once.
#   world-res = ~/.claude/CLAUDE.md in place (imports machine.md)      bare-res = CLAUDE.md moved aside
# Usage: CONDS="world-res bare-res" ./run_resident.sh [first] [last]
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); WORK=$HERE/work; OUT=$HERE/results.jsonl
GLOBAL=$HOME/.claude/CLAUDE.md; BACKUP=$HERE/.CLAUDE.md.moved
FIRST=${1:-1}; LAST=${2:-20}; CONDS=${CONDS:-"world-res bare-res"}
exec 9>"$HERE/.lock"; flock -n 9 || { echo "another batch is already running — refusing to start" >&2; exit 1; }
PRE="You are helping administer this Linux machine. Answer the question below concisely (under 150 words). Read-only: do not modify anything on the system. Question: "
restore() { [ -f "$BACKUP" ] && mv -f "$BACKUP" "$GLOBAL"; return 0; }
trap restore EXIT INT TERM
ARGS=(--output-format json --allowedTools "Bash,Read,Grep,Glob" --disallowedTools "Edit,Write,NotebookEdit,WebSearch,WebFetch,Agent")

for cond in $CONDS; do
  case "$cond" in world-res) restore ;; bare-res) [ -f "$GLOBAL" ] && mv -f "$GLOBAL" "$BACKUP" ;; esac
  sid=""
  while IFS='|' read -r id prompt; do
    [ -z "$id" ] && continue
    { [ "$id" -lt "$FIRST" ] || [ "$id" -gt "$LAST" ]; } && continue
    present=false; [ -f "$GLOBAL" ] && present=true
    echo "[$(date +%H:%M:%S)] $cond task $id ${sid:+(resume ${sid:0:8})}" >&2
    start=$(date +%s.%N)
    if [ -z "$sid" ]; then
      json=$(cd "$WORK" && env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT claude -p "$PRE$prompt" "${ARGS[@]}" </dev/null 2>"$WORK/stderr.$cond.$id") || true
    else
      json=$(cd "$WORK" && env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT claude -p --resume "$sid" "$PRE$prompt" "${ARGS[@]}" </dev/null 2>"$WORK/stderr.$cond.$id") || true
    fi
    end=$(date +%s.%N)
    sid=$(printf '%s' "$json" | python3 -I -c 'import json,sys
try: print(json.loads(sys.stdin.read()).get("session_id",""))
except Exception: print("")')
    printf '%s' "$json" | python3 -I -c '
import json, sys
cond, tid, start, end, present = sys.argv[1:6]
raw = sys.stdin.read()
try: o = json.loads(raw)
except Exception: o = {"error": "nojson", "raw": raw[:800]}
o["_cond"] = cond; o["_task"] = int(tid); o["_wall"] = round(float(end) - float(start), 1)
o["_start"] = float(start); o["_end"] = float(end); o["_claude_md_present"] = (present == "true")
print(json.dumps(o))' "$cond" "$id" "$start" "$end" "$present" >> "$OUT"
    echo "[$(date +%H:%M:%S)]   done in $(printf '%.0f' "$(echo "$end - $start" | bc)")s" >&2
  done < "$HERE/tasks.txt"
  restore
done
echo "[$(date +%H:%M:%S)] resident batch [$CONDS] complete → $OUT" >&2
