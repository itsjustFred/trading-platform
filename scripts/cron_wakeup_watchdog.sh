#!/usr/bin/env bash
set -u

cd /home/ubuntu/Desktop/trading-platform || exit 1
LOG_DIR=/home/ubuntu/Desktop/trading-platform/logs
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/cron_wakeup_watchdog.log"
STATUS="$LOG_DIR/cron_wakeup_watchdog_status.json"

log() {
  printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG"
}

while true; do
  log 'checking Hermes gateway and cron jobs'
  gateway_status=$(hermes cron status 2>&1) || true
  printf '%s\n' "$gateway_status" >> "$LOG"

  cron_json=$(hermes cron list --all 2>&1) || true
  printf '%s\n' "$cron_json" >> "$LOG"

  cron_raw="$LOG_DIR/cron_wakeup_watchdog_cron.json"
  printf '%s\n' "$cron_json" > "$cron_raw"
  # Use mktemp for safe temporary file
  failed_ids_file=$(mktemp)
  trap 'rm -f "$failed_ids_file"' EXIT
  if python3 - "$cron_raw" "$failed_ids_file" <<'PY'
import json, sys
path = sys.argv[1]
failed_ids_path = sys.argv[2]
try:
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
except Exception:
    data = {}
jobs = data.get('jobs', []) if isinstance(data, dict) else []
with open(failed_ids_path, 'w', encoding='utf-8') as f:
    for job in jobs:
        if isinstance(job, dict) and job.get('enabled') and job.get('last_status') == 'error':
            job_id = job.get('job_id')
            if job_id:
                print(job_id, file=f)
PY
  then
    while read -r job_id; do
      [ -n "$job_id" ] || continue
      log "cron job $job_id last_status=error; restarting"
      hermes cron run --accept-hooks "$job_id" >> "$LOG" 2>&1 || log "restart failed for $job_id"
    done < "$failed_ids_file"
  else
    log 'could not inspect cron jobs for failed statuses'
  fi
path = sys.argv[1]
try:
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
except Exception:
    data = {}
jobs = data.get('jobs', []) if isinstance(data, dict) else []
for job in jobs:
    if isinstance(job, dict) and job.get('enabled') and job.get('last_status') == 'error':
        job_id = job.get('job_id')
        if job_id:
            print(job_id)
PY
  then
    while read -r job_id; do
      [ -n "$job_id" ] || continue
      log "cron job $job_id last_status=error; restarting"
      hermes cron run --accept-hooks "$job_id" >> "$LOG" 2>&1 || log "restart failed for $job_id"
    done < /tmp/cron_failed_ids.txt
  else
    log 'could not inspect cron jobs for failed statuses'
  fi

  if case "$gateway_status" in *"Gateway is running"*) true;; *) false;; esac; then
    printf '{"ok":true,"checked_at":"%s"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$STATUS"
  else
    log 'gateway is not running; attempting to start it'
    hermes gateway start >> "$LOG" 2>&1 || true
    printf '{"ok":false,"checked_at":"%s"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$STATUS"
  fi

  sleep 120
done
