#!/usr/bin/env bash
# Continuous tick-pipeline staleness alarm — intended for /etc/cron.hourly/.
#
# This is the guardrail for the 2026-06 freeze: a "healthy" container whose
# Parquet mirror has silently stopped advancing. It runs the in-container
# health check (collect_ticks.py --health-check), confirms the hourly S3 sync
# is still running, and on ANY problem logs at ERROR, optionally fires an
# alert, and exits non-zero so cron mail / a systemd timer surfaces it.
#
# "Files exist in S3" is NOT "the pipeline works" — these checks assert the
# mirror is producing RECENT data and is advancing.
#
# Environment variables (sourced from ${APP_DIR}/.env if present):
#   APP_DIR                    defaults to /home/ubuntu/app/trading
#   SYMBOLS                    comma-separated; default BTCUSDT
#   TICK_CONTAINER             primary writer container (default: trading-data)
#   HEALTH_MAX_STALENESS_HOURS grace after 00:00 UTC (default: 2)
#   S3_SYNC_MAX_AGE_MIN        max age of a successful s3_sync run (default: 120)
#   ALERT_WEBHOOK_URL          optional; POSTed a JSON {"text": "..."} on failure
#   ALERT_SNS_TOPIC_ARN        optional; aws sns publish on failure
#   CLOUDWATCH_NAMESPACE       optional; if set, emit a PipelineHealthy 1/0
#                              metric each run so a CloudWatch alarm can page
#                              (and "missing data" = box down also pages).
#                              Pair with scripts/create_cloudwatch_alarm.sh.

set -euo pipefail

# run-parts/cron runs with a minimal PATH that can omit /usr/local/bin, where
# the AWS CLI v2 installs (/usr/local/bin/aws -> /usr/local/aws-cli/...). If aws
# is not found, emit_metric silently skips and the CloudWatch heartbeat goes
# missing — which the alarm treats as breaching, masking the very failures it
# exists to catch. Prepend the standard locations so aws/docker always resolve.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"

APP_DIR="${APP_DIR:-/home/ubuntu/app/trading}"
LOG_FILE="${APP_DIR}/logs/pipeline_health.log"

cd "${APP_DIR}"
mkdir -p "$(dirname "${LOG_FILE}")"

if [ -f .env ]; then
    # shellcheck disable=SC1091
    set -o allexport
    source .env
    set +o allexport
fi

SYMBOLS="${SYMBOLS:-BTCUSDT}"
TICK_CONTAINER="${TICK_CONTAINER:-trading-data}"
HEALTH_MAX_STALENESS_HOURS="${HEALTH_MAX_STALENESS_HOURS:-2}"
S3_SYNC_MAX_AGE_MIN="${S3_SYNC_MAX_AGE_MIN:-120}"

log() {
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "${LOG_FILE}"
}

# Best-effort CloudWatch heartbeat: emit PipelineHealthy=1 (healthy) or 0
# (unhealthy) so an alarm can page on 0 AND on missing data (a dead box stops
# emitting, which CloudWatch treats as breaching — see create_cloudwatch_alarm.sh).
# Never let a metric failure crash the health check.
emit_disk_metric() {
    [ -n "${CLOUDWATCH_NAMESPACE:-}" ] || return 0
    command -v aws >/dev/null 2>&1 || { log "[WARN] aws CLI missing; skip DiskFreeGB"; return 0; }
    command -v df >/dev/null 2>&1 || { log "[WARN] df missing; skip DiskFreeGB"; return 0; }
    free_gb=$(df -B1 / | awk 'NR==2 {printf "%.3f", $4/1073741824}')
    if [ -z "${free_gb}" ]; then
        log "[WARN] could not read free bytes on /"
        return 0
    fi
    if aws cloudwatch put-metric-data \
            --namespace "${CLOUDWATCH_NAMESPACE}" \
            --metric-name DiskFreeGB \
            --dimensions "Host=$(hostname)" \
            --value "${free_gb}" >> "${LOG_FILE}" 2>&1; then
        log "[OK] emitted DiskFreeGB=${free_gb} to ${CLOUDWATCH_NAMESPACE}"
    else
        log "[WARN] DiskFreeGB put-metric-data failed"
    fi
}

emit_metric() {
    [ -n "${CLOUDWATCH_NAMESPACE:-}" ] || return 0
    command -v aws >/dev/null 2>&1 || { log "[WARN] aws CLI missing; skip metric"; return 0; }
    if aws cloudwatch put-metric-data \
            --namespace "${CLOUDWATCH_NAMESPACE}" \
            --metric-name PipelineHealthy \
            --dimensions "Host=$(hostname)" \
            --value "$1" >> "${LOG_FILE}" 2>&1; then
        log "[OK] emitted PipelineHealthy=$1 to ${CLOUDWATCH_NAMESPACE}"
    else
        log "[WARN] put-metric-data failed"
    fi
}

is_running() {
    docker inspect --format '{{.State.Running}}' "$1" 2>/dev/null | grep -q true
}

# Any running tick container can run the check — data/ is a shared mounted
# volume holding every symbol's files. Prefer a per-symbol container, then the
# default writer.
find_tick_container() {
    local first_sym
    first_sym="$(echo "${SYMBOLS}" | cut -d, -f1)"
    if is_running "trading-data-${first_sym}"; then
        echo "trading-data-${first_sym}"
    elif is_running "${TICK_CONTAINER}"; then
        echo "${TICK_CONTAINER}"
    else
        echo ""
    fi
}

ISSUES=()

# ---------------------------------------------------------------------------
# 1. In-container Parquet shard freshness (per symbol)
# ---------------------------------------------------------------------------
HEALTH_MAX_STALENESS_SECONDS="${HEALTH_MAX_STALENESS_SECONDS:-300}"
CONTAINER="$(find_tick_container)"
if [ -z "${CONTAINER}" ]; then
    ISSUES+=("collector container not running (checked trading-data-* / ${TICK_CONTAINER}) — no tick data is being collected")
    log "[CRITICAL] no running tick container — cannot run health check"
else
    # Prefer checking each symbol in its own container when present.
    IFS=',' read -r -a SYM_ARR <<< "${SYMBOLS}"
    for sym in "${SYM_ARR[@]}"; do
        sym="$(echo "${sym}" | tr -d ' ')"
        [ -n "${sym}" ] || continue
        c="trading-data-${sym}"
        if ! is_running "${c}"; then
            c="${CONTAINER}"
        fi
        if docker exec "${c}" python collect_ticks.py --health-check \
                --symbol "${sym}" \
                --max-staleness-seconds "${HEALTH_MAX_STALENESS_SECONDS}" \
                --data-dir /app/data >> "${LOG_FILE}" 2>&1; then
            log "[OK] shard health (${c}, symbol=${sym})"
        else
            ISSUES+=("Parquet shards unhealthy for ${sym} — see ${LOG_FILE}")
            log "[FAIL] health check failed for ${sym} via ${c}"
        fi
    done
fi

# ---------------------------------------------------------------------------
# 2. S3 sync freshness — a stalled cron means nothing is reaching S3
# ---------------------------------------------------------------------------
SYNC_LOG="${APP_DIR}/logs/s3_sync.log"
if [ -f "${SYNC_LOG}" ]; then
    now_s=$(date -u +%s)
    mtime_s=$(stat -c %Y "${SYNC_LOG}" 2>/dev/null || stat -f %m "${SYNC_LOG}")
    age_min=$(( (now_s - mtime_s) / 60 ))
    if [ "${age_min}" -gt "${S3_SYNC_MAX_AGE_MIN}" ]; then
        ISSUES+=("s3_sync.log not updated for ${age_min} min (>${S3_SYNC_MAX_AGE_MIN}) — the hourly S3 sync cron may be dead")
        log "[FAIL] s3_sync.log stale (${age_min} min old)"
    else
        log "[OK] s3_sync.log fresh (${age_min} min old)"
    fi
    # Surface a sync that ran but reported failures on its last pass.
    if tail -n 40 "${SYNC_LOG}" | grep -q "\[DONE\] uploaded=.* failed=[1-9]"; then
        ISSUES+=("last s3_sync run reported upload failures — see ${SYNC_LOG}")
        log "[FAIL] last s3_sync reported failures"
    fi
else
    ISSUES+=("no s3_sync.log at ${SYNC_LOG} — the S3 sync cron has never run")
    log "[FAIL] s3_sync.log missing"
fi

# ---------------------------------------------------------------------------
# 3. Report + alert
# ---------------------------------------------------------------------------
emit_disk_metric

if [ "${#ISSUES[@]}" -eq 0 ]; then
    log "[DONE] pipeline healthy"
    emit_metric 1
    exit 0
fi

emit_metric 0

MSG="Tick pipeline ALARM on $(hostname) — ${#ISSUES[@]} issue(s):"
for i in "${ISSUES[@]}"; do
    MSG="${MSG}"$'\n'"  - ${i}"
done
log "[ALARM] ${MSG}"
echo "${MSG}" >&2

# Optional webhook alert (Slack/Discord-style {"text": ...}). Never let a
# failed alert crash the script — it is already in the failure path.
if [ -n "${ALERT_WEBHOOK_URL:-}" ]; then
    payload=$(printf '{"text": %s}' "$(printf '%s' "${MSG}" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')")
    if curl -fsS -X POST -H 'Content-Type: application/json' \
            -d "${payload}" "${ALERT_WEBHOOK_URL}" >> "${LOG_FILE}" 2>&1; then
        log "[OK] alert webhook delivered"
    else
        log "[WARN] alert webhook POST failed"
    fi
fi

# Optional AWS SNS alert.
if [ -n "${ALERT_SNS_TOPIC_ARN:-}" ]; then
    if aws sns publish --topic-arn "${ALERT_SNS_TOPIC_ARN}" \
            --subject "Tick pipeline alarm on $(hostname)" \
            --message "${MSG}" >> "${LOG_FILE}" 2>&1; then
        log "[OK] SNS alert published"
    else
        log "[WARN] SNS publish failed"
    fi
fi

exit 1
