#!/usr/bin/env bash
# Detached map→pull supervisor; dual_write off; single run_queue at a time.
set -euo pipefail
cd $ODDS_DATA_DIR/backfill/5df-multibook-history-queue
LOG=logs/supervise_$(date +%Y%m%d_%H%M%S).log
STATUS=docs/schema/v2_0-backfill-supervise-live.md（未公开）
exec >>"$LOG" 2>&1
echo "START $(date -Iseconds) pid=$$"
MAX_CYCLES=${MAX_CYCLES:-8}
cycle=0
empty=0

wait_run_queue() {
  while pgrep -f 'python3 -u run_queue.py' >/dev/null 2>&1; do
    echo "wait run_queue $(date -Iseconds)"
    sleep 30
  done
}

drain_pending() {
  while true; do
    pend=$(python3 -c 'import json;print(json.load(open("queue/state.json")).get("pending",0))' 2>/dev/null || echo 0)
    if [[ "${pend:-0}" -le 0 ]]; then echo "pending empty"; break; fi
    echo "drain pending=$pend $(date -Iseconds)"
    wait_run_queue
    python3 -u run_queue.py --limit 220 || true
    sleep 2
  done
}

write_status() {
  python3 health_check.py > /tmp/hc.json || true
  {
    echo "# supervise live"
    echo
    echo "- updated: $(date -Iseconds)"
    echo "- supervise_pid: $$"
    echo "- log: \`$LOG\`"
    echo
    echo '```json'
    cat /tmp/hc.json
    echo '```'
  } > "$STATUS"
}

wait_run_queue
drain_pending
write_status

while [[ $cycle -lt $MAX_CYCLES ]]; do
  cycle=$((cycle+1))
  echo "CYCLE $cycle map $(date -Iseconds)"
  wait_run_queue
  python3 map_csl_fixtures.py --days 21 --max-new 250 || true
  python3 map_csl_fixtures.py --local-only || true
  python3 build_queue.py || true
  write_status
  pend=$(python3 -c 'import json;print(json.load(open("queue/state.json")).get("pending",0))' 2>/dev/null || echo 0)
  if [[ "${pend:-0}" -le 0 ]]; then
    empty=$((empty+1))
    echo "no new pending after map; empty_streak=$empty"
    if [[ $empty -ge 3 ]]; then
      echo "3 empty maps in a row; stop"
      break
    fi
    # try denser / wider day window next
    python3 map_csl_fixtures.py --days 28 --max-new 300 || true
    python3 build_queue.py || true
    write_status
    pend=$(python3 -c 'import json;print(json.load(open("queue/state.json")).get("pending",0))' 2>/dev/null || echo 0)
    if [[ "${pend:-0}" -le 0 ]]; then
      continue
    fi
    empty=0
  else
    empty=0
  fi
  drain_pending
  write_status
done

echo "DONE $(date -Iseconds)"
write_status
