#!/bin/bash
# Free health check for the paper agent (no AI). Prints "OK ..." or "DOWN: reason".
URL="https://mickyatakilt.github.io/sports-paper-agent/data.json"
REPO="Mickyatakilt/sports-paper-agent"
upd=$(curl -s -m 15 "$URL?$(date +%s)" | python3 -c "import json,sys; print(json.load(sys.stdin)['updated'])" 2>/dev/null)
if [ -z "$upd" ]; then echo "DOWN: dashboard data not reachable"; exit 1; fi
age_h=$(python3 -c "
import datetime
u=datetime.datetime.fromisoformat('$upd'); n=datetime.datetime.now(datetime.timezone.utc)
print(round((n-u).total_seconds()/3600,1))")
last=$(gh run list --repo "$REPO" --limit 1 --json status,conclusion -q '.[0] | .status+" "+.conclusion' 2>/dev/null)
case "$last" in
  *failure*|*cancelled*|*timed_out*) echo "DOWN: last cloud run failed ($last); data is ${age_h}h old"; exit 1;;
esac
if python3 -c "import sys; sys.exit(0 if $age_h > 7 else 1)"; then echo "DOWN: no successful run for ${age_h}h (should be every 3h)"; exit 1; fi
echo "OK: last update ${age_h}h ago, last run: ${last:-unknown}"
