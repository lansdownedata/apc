#!/usr/bin/env bash
# Run the portal's cron jobs against a local dev server, the way cron-job.org does in prod.
#   scripts/local_cron.sh                 # the five-minute set once
#   scripts/local_cron.sh all             # every job once
#   scripts/local_cron.sh loop            # five-minute set every 5 min, hourly set each hour
#   scripts/local_cron.sh run-touchpoints # one named job
set -euo pipefail
cd "$(dirname "$0")/.."
KEY=$(grep -E '^CRON_SECRET=' .env | cut -d= -f2-)
BASE=${CRON_BASE:-http://127.0.0.1:8000}
FAST=(run-touchpoints run-tasks monitor-dispatch gnet-farm-in)
HOURLY=(reconcile-payments retry-qbo-sync retry-la-sync cleaning-holds)
DAILY=(charge-due-balances recognize-due-revenue deposit-report unconfirmed-trips-report complaint-report review-report)
hit() { printf '%-26s ' "$1"; curl -s -o /dev/null -w '%{http_code}\n' -X POST -H "X-Cron-Key: $KEY" "$BASE/cron/$1/"; }
case "${1:-fast}" in
  fast) for j in "${FAST[@]}"; do hit "$j"; done ;;
  all) for j in "${FAST[@]}" "${HOURLY[@]}" "${DAILY[@]}"; do hit "$j"; done ;;
  loop) n=0; while true; do date; for j in "${FAST[@]}"; do hit "$j"; done
        if (( n % 12 == 0 )); then for j in "${HOURLY[@]}"; do hit "$j"; done; fi
        n=$((n+1)); sleep 300; done ;;
  *) hit "$1" ;;
esac
