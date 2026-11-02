#!/usr/bin/env bash
# One v1 end-to-end acceptance session in the foreground:
#   ./run.sh <tag> [live_v1.py options]      e.g. ./run.sh pilot   ./run.sh s1
# Runs the driver from the v1-accept worktree venv, then grades it.
# Logs UTC start/end/rc/wall to batch.log and checks for stray processes after.
set -u
cd "$(dirname "$0")"
WT=${DOLLOS_V1_WT:-/home/seith/DollOS/DollOS/.worktrees/v1-accept}
tag="$1"; shift
t0=$(date +%s)
echo "=== $(date -u +%FT%TZ) start $tag $*" >> batch.log
uv run --project "$WT" --frozen python live_v1.py "$tag" "$@" > "driver-$tag.out" 2>&1
rc=$?
t1=$(date +%s)
echo "=== $(date -u +%FT%TZ) end $tag rc=$rc wall=$((t1-t0))s" >> batch.log
if [ -d "$tag" ]; then
  # Capture the grader's rc BEFORE any $(...) runs: in `echo "$(date) rc=$?"`
  # the command substitution resets $? (that is how dry4 logged rc=0 for a
  # grader that died with SIGSEGV, kernel log 14:31:08, and wrote nothing).
  # One retry if it fails or leaves no grade.json; both attempts are logged.
  for attempt in 1 2; do
    rm -f "$tag/grade.json" "$tag/grade.txt"
    uv run --project "$WT" --frozen python grade_v1.py "$tag" > "grade-$tag.out" 2>&1
    grc=$?
    [ -s "$tag/grade.json" ] && have=yes || have=no
    echo "=== $(date -u +%FT%TZ) graded $tag attempt=$attempt rc=$grc grade.json=$have" >> batch.log
    [ "$grc" -eq 0 ] && [ "$have" = yes ] && break
    cp "grade-$tag.out" "grade-$tag.attempt$attempt.out"
  done
  if [ "$have" != yes ]; then
    echo "GRADING FAILED for $tag (rc=$grc); see grade-$tag.attempt*.out" | tee -a batch.log
    [ "$rc" -eq 0 ] && rc=3
  fi
fi
tail -n 30 "driver-$tag.out" | cut -c1-400
echo "rc=$rc wall=$((t1-t0))s"
# Only this driver's leftovers: daemons on a config in this dir, our fixture
# http.server port, the dry-run stub, fixture scripts started under this dir.
here=$(pwd)
strays=$(pgrep -af -- "--config $here/|http\.server 18775|stub_llm\.py|$here/.*(backup_check|sync_photos)\.sh" | grep -v pgrep)
for pid in $(pgrep -f 'backup_check\.sh|sync_photos\.sh|mcp-server-fetch'); do
  case "$(readlink /proc/$pid/cwd 2>/dev/null)" in
    "$here"/*|"$WT"|"$WT"/*) strays="$strays
$(ps -o pid=,args= -p "$pid")" ;;
  esac
done
strays=$(printf '%s\n' "$strays" | sed '/^$/d' | sort -u)
if [ -n "$strays" ]; then
  echo "STRAYS: $strays" | tee -a batch.log
  # A crashed driver (host GPFs, kernel log) leaves its daemon and fixtures
  # running, which fails the next session's preflight. Kill them.
  kill $(printf '%s\n' "$strays" | awk '{print $1}') 2>/dev/null
  sleep 3
  echo "killed strays" | tee -a batch.log
else
  echo "no strays" | tee -a batch.log
fi
exit $rc
