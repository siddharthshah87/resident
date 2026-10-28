#!/usr/bin/env bash
# Stage 1 A/B: each task runs in a fresh `claude -p` session under one or more conditions.
#   world = normal config (~/.claude/CLAUDE.md imports ~/agentos/machine.md)
#   bare  = global CLAUDE.md moved aside for the duration of the run (restored by trap)
#   sys   = CLAUDE.md moved aside; machine.md injected with --append-system-prompt (cacheable position)
# Usage: CONDS="world bare" ./run.sh [first_task] [last_task]      e.g. CONDS=sys ./run.sh 1 20
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
WORK=$HERE/work
OUT=$HERE/results.jsonl
GLOBAL=$HOME/.claude/CLAUDE.md
BACKUP=$HERE/.CLAUDE.md.moved
WORLDFILE=$HOME/agentos/machine.md
FIRST=${1:-1}; LAST=${2:-20}
CONDS=${CONDS:-"world bare"}
exec 9>"$HERE/.lock"; flock -n 9 || { echo "another batch is already running — refusing to start" >&2; exit 1; }
PRE="You are helping administer this Linux machine. Answer the question below concisely (under 150 words). Read-only: do not modify anything on the system. Question: "

restore() { [ -f "$BACKUP" ] && mv -f "$BACKUP" "$GLOBAL"; return 0; }
trap restore EXIT INT TERM

run_one() { # cond id prompt
  local cond=$1 id=$2 prompt=$3 start end json present=false
  local -a extra=()
  if [ "$cond" = sys ]; then
    extra=(--append-system-prompt "# Machine context (agentos)
This box keeps an intent log. Read the world model below instead of probing with ls/ps/ss/systemctl, and follow its protocol before changing system state.

$(cat "$WORLDFILE")")
  fi
  echo "[$(date +%H:%M:%S)] task $id / $cond ..." >&2
  [ -f "$GLOBAL" ] && present=true
  start=$(date +%s.%N)
  json=$(cd "$WORK" && env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT claude -p "$PRE$prompt" \
          --output-format json \
          --allowedTools "Bash,Read,Grep,Glob" \
          --disallowedTools "Edit,Write,NotebookEdit,WebSearch,WebFetch,Agent" \
          "${extra[@]}" \
          </dev/null 2>"$WORK/stderr.$cond.$id") || true
  end=$(date +%s.%N)
  printf '%s' "$json" | python3 -I -c '
import json, sys
cond, tid, start, end, present = sys.argv[1:6]
raw = sys.stdin.read()
try:
    o = json.loads(raw)
except Exception:
    o = {"error": "nojson", "raw": raw[:800]}
o["_cond"] = cond; o["_task"] = int(tid); o["_wall"] = round(float(end) - float(start), 1)
o["_claude_md_present"] = (present == "true")
print(json.dumps(o))' "$cond" "$id" "$start" "$end" "$present" >> "$OUT"
  echo "[$(date +%H:%M:%S)]   done in $(printf '%.0f' "$(echo "$end - $start" | bc)")s" >&2
}

while IFS='|' read -r id prompt; do
  [ -z "$id" ] && continue
  { [ "$id" -lt "$FIRST" ] || [ "$id" -gt "$LAST" ]; } && continue
  for cond in $CONDS; do
    case "$cond" in
      world) restore ;;
      bare|sys) [ -f "$GLOBAL" ] && mv -f "$GLOBAL" "$BACKUP" ;;
    esac
    run_one "$cond" "$id" "$prompt"
    restore
  done
done < "$HERE/tasks.txt"
echo "[$(date +%H:%M:%S)] batch $FIRST..$LAST [$CONDS] complete → $OUT" >&2
