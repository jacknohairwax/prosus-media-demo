#!/bin/bash
# Project status across all worktrees. Run: bash prosus-media-demo/status.sh
PARENT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MAIN="$PARENT/prosus-media-demo"
G="git -C $MAIN"
NOW=$(date +%s)

echo "=== WINDOWS (live heartbeat) ==="
if ls "$PARENT"/.pulse/*.status >/dev/null 2>&1; then
  printf "%-26s %-18s %-10s %s\n" WINDOW "LAST EVENT" TOOL AGE
  for f in "$PARENT"/.pulse/*.status; do
    n=$(basename "$f" .status)
    IFS=$'\t' read -r ts ev detail < "$f"
    age=$(( (NOW - $(date -j -f '%Y-%m-%d %H:%M:%S' "$ts" +%s 2>/dev/null || date -d "$ts" +%s)) / 60 ))
    state="$ev"; [ "$ev" = "Stop" ] && state="idle (waiting on you)"
    [ "$ev" = "SessionEnd" ] && state="closed"
    [ "$ev" = "PostToolUse" ] && state="working"
    printf "%-26s %-18s %-10s %sm ago\n" "$n" "$state" "${detail:--}" "$age"
  done
else
  echo "(no heartbeat yet - windows started before the hook was installed)"
fi

echo
echo "=== BRANCHES ==="
for b in win-a-collect win-b-page win-c-queries win-d-integrate win-e-trim main; do
  printf "%-18s %s\n" "$b" "$($G log -1 --format='%h %cr  %s' $b 2>/dev/null | cut -c1-58)"
done

echo
echo "=== OUTPUTS ==="
printf "%-26s %-12s %-13s %s\n" FOLDER ROWS SPEND REPORTS
for d in "$PARENT"/prosus-media-demo*; do
  [ -d "$d" ] || continue
  r=$(ls "$d"/reports/*.md 2>/dev/null | xargs -n1 basename 2>/dev/null | tr '\n' ' ')
  rows="-"; [ -f "$d/data/articles.csv" ] && rows="$(($(wc -l < "$d/data/articles.csv")-1))"
  usd="-"; [ -f "$d/runs/log.csv" ] && usd="USD $(awk -F, 'NR>1{s+=$6}END{printf "%.4f", s}' "$d/runs/log.csv")"
  printf "%-26s %-12s %-13s %s\n" "$(basename "$d")" "$rows" "$usd" "${r:--}"
done

echo
echo "=== FILE ACTIVITY (15 min) ==="
find "$PARENT" -type f -mmin -15 -not -path '*/.git/*' -not -path '*__pycache__*' -not -name '.DS_Store' -not -path '*/.pulse/*' 2>/dev/null | sed "s|$PARENT/||" | head -10
