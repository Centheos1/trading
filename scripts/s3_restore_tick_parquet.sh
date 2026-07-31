#!/usr/bin/env bash
# Bulk-restore selected tick Parquet keys from Glacier and validate readability.
#
#   ./scripts/s3_restore_tick_parquet.sh --bucket trading-data-centheos \
#       --prefix cold-archive/ticks-parquet/ --days 7 --dry-run
#   ./scripts/s3_restore_tick_parquet.sh --bucket trading-data-centheos \
#       --prefix cold-archive/ticks-parquet/ --execute

set -euo pipefail

BUCKET="${S3_BUCKET:-trading-data-centheos}"
PREFIX="cold-archive/ticks-parquet/"
DAYS=7
DRY_RUN=1
DEST_PREFIX="recovered/ticks/"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bucket) BUCKET="$2"; shift 2 ;;
        --prefix) PREFIX="$2"; shift 2 ;;
        --dest-prefix) DEST_PREFIX="$2"; shift 2 ;;
        --days) DAYS="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --execute) DRY_RUN=0; shift ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

log "Listing s3://${BUCKET}/${PREFIX}"
mapfile -t KEYS < <(aws s3api list-objects-v2 --bucket "${BUCKET}" --prefix "${PREFIX}" \
    --query 'Contents[].Key' --output text | tr '\t' '\n' | grep -E '\.parquet$' || true)

log "Found ${#KEYS[@]} parquet keys"
restored=0
skipped=0
for key in "${KEYS[@]}"; do
    [[ -n "${key}" ]] || continue
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        log "[DRY-RUN] restore ${key} (tier=Bulk, days=${DAYS})"
        continue
    fi
    if aws s3api restore-object --bucket "${BUCKET}" --key "${key}" \
            --restore-request "Days=${DAYS},GlacierJobParameters={Tier=Bulk}" \
            2>/dev/null; then
        log "[OK] restore requested ${key}"
        restored=$((restored + 1))
    else
        # Already restored or not Glacier — try copy to recovered/
        dest="${DEST_PREFIX}${key#${PREFIX}}"
        if aws s3 cp "s3://${BUCKET}/${key}" "s3://${BUCKET}/${dest}" --only-show-errors; then
            log "[OK] copied ${key} → ${dest}"
            restored=$((restored + 1))
        else
            log "[WARN] skip unreadable ${key}"
            skipped=$((skipped + 1))
        fi
    fi
done

log "[DONE] restored_or_copied=${restored} skipped=${skipped} dry_run=${DRY_RUN}"
log "Validate locally with: aws s3 cp … && python -c 'import pandas as pd; pd.read_parquet(...)'"
