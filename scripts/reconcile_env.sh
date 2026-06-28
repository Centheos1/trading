#!/usr/bin/env bash
# Reconcile an existing .env against .env.template on every deploy.
#
# Why this exists: .env is gitignored, so a box keeps whatever values it was
# first created with. When a shipped default changes in .env.template (e.g. the
# decoupled-mirror rearchitecture lowered PARQUET_FLUSH_INTERVAL 900 -> 60 and
# MAX_H5_GB 8 -> 3), a stale .env SILENTLY masks the new default and the
# collector keeps running the old, unsafe settings after a redeploy. That is
# exactly what happened in the 2026-06 incident. This script makes that drift
# impossible to miss and self-healing for the keys that matter most.
#
# Behaviour (idempotent; backs up .env before any write):
#   * MISSING keys  (in template, absent from .env)        -> ADDED (template value)
#   * MANAGED keys  (reliability/safety tunables, below)    -> FORCED to template
#                                                              value when they drift
#   * Other keys with a drifted value                       -> REPORTED only
#   * Operator-owned secrets (blank in the template:        -> never touched
#     S3_BUCKET, OANDA_*, BINANCE_*, ALERT_*)
#
# Usage:
#   bash scripts/reconcile_env.sh [ENV_FILE] [TEMPLATE_FILE]
#     ENV_FILE       default: .env
#     TEMPLATE_FILE  default: .env.template
#   DRY_RUN=1 bash scripts/reconcile_env.sh   # report only, write nothing
#
# Exit code: 0 on success (including when only non-managed drift was reported),
# non-zero only on a hard error (missing template, unwritable .env).

set -euo pipefail

ENV_FILE="${1:-.env}"
TEMPLATE_FILE="${2:-.env.template}"
DRY_RUN="${DRY_RUN:-0}"

# Keys the deploy ENFORCES to the template value. These are pure
# reliability/safety tunables with a single correct shipped value and no
# legitimate per-box override — a stale value here is a production hazard
# (the 2026-06 freeze ran at the old 900s / 8 GB settings). Everything NOT in
# this list is only added-if-missing and otherwise reported, never overwritten,
# so deliberate per-box choices (SYMBOLS, OANDA_ACCOUNT_TYPE, LOG_LEVEL, the
# OHLCV_* scope knobs, …) and secrets are preserved.
MANAGED_KEYS="MAX_H5_GB MIN_FREE_DISK_GB PARQUET_FLUSH_INTERVAL HEALTH_MAX_STALENESS_HOURS S3_SYNC_MAX_AGE_MIN CLOUDWATCH_NAMESPACE DATA_STORE"

log() { echo "[reconcile_env] $*"; }

is_managed() {
    case " ${MANAGED_KEYS} " in
        *" $1 "*) return 0 ;;
        *) return 1 ;;
    esac
}

if [ ! -f "${TEMPLATE_FILE}" ]; then
    echo "[reconcile_env] ERROR: template ${TEMPLATE_FILE} not found" >&2
    exit 1
fi

# A brand-new box with no .env: just seed it from the template and stop.
if [ ! -f "${ENV_FILE}" ]; then
    log "${ENV_FILE} absent — creating from ${TEMPLATE_FILE}"
    [ "${DRY_RUN}" = "1" ] || cp "${TEMPLATE_FILE}" "${ENV_FILE}"
    exit 0
fi

# First value of KEY in ENV_FILE; prints it and returns 0, or returns 1 if the
# key is absent (an empty value, e.g. "S3_BUCKET=", still counts as present).
env_get() {
    local key="$1" line
    line="$(grep -E "^${key}=" "${ENV_FILE}" | head -n 1 || true)"
    [ -n "${line}" ] || return 1
    printf '%s' "${line#*=}"
}

# Escape a replacement string for sed (delimiter '|', plus & and backslash).
sed_escape_repl() { printf '%s' "$1" | sed -e 's/[&|\\]/\\&/g'; }

ADDS=()      # "KEY=VALUE"
UPDATES=()   # "KEY|OLD|NEW"
DRIFTS=()    # "KEY|OLD|NEW"

while IFS= read -r tline || [ -n "${tline}" ]; do
    # Only KEY=VALUE lines (allow empty value); skip comments and blanks.
    [[ "${tline}" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    key="${BASH_REMATCH[1]}"
    tval="${BASH_REMATCH[2]}"

    if cur="$(env_get "${key}")"; then
        if [ "${cur}" != "${tval}" ]; then
            if is_managed "${key}"; then
                UPDATES+=("${key}|${cur}|${tval}")
            elif [ -n "${tval}" ]; then
                # Only report keys that ship a non-blank default. A blank
                # template value means the key is operator-supplied (S3_BUCKET,
                # OANDA_*, BINANCE_*, ALERT_*); a filled-in value there is
                # expected, not "drift" — and printing it would leak secrets.
                DRIFTS+=("${key}|${cur}|${tval}")
            fi
        fi
    else
        ADDS+=("${key}=${tval}")
    fi
done < "${TEMPLATE_FILE}"

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
for d in "${DRIFTS[@]:-}"; do
    [ -n "${d}" ] || continue
    k="${d%%|*}"; rest="${d#*|}"; old="${rest%%|*}"; new="${rest#*|}"
    log "[drift]  ${k}: .env='${old}' template='${new}' (left as-is; review)"
done

if [ "${#UPDATES[@]}" -eq 0 ] && [ "${#ADDS[@]}" -eq 0 ]; then
    log "OK — managed keys in sync; no keys missing."
    exit 0
fi

for u in "${UPDATES[@]:-}"; do
    [ -n "${u}" ] || continue
    k="${u%%|*}"; rest="${u#*|}"; old="${rest%%|*}"; new="${rest#*|}"
    log "[update] ${k}: '${old}' -> '${new}' (managed)"
done
for a in "${ADDS[@]:-}"; do
    [ -n "${a}" ] || continue
    log "[add]    ${a}"
done

if [ "${DRY_RUN}" = "1" ]; then
    log "DRY_RUN=1 — no changes written."
    exit 0
fi

# ---------------------------------------------------------------------------
# Apply (back up first)
# ---------------------------------------------------------------------------
backup="${ENV_FILE}.bak.$(date -u +%Y%m%dT%H%M%SZ)"
cp "${ENV_FILE}" "${backup}"
log "backed up ${ENV_FILE} -> ${backup}"

for u in "${UPDATES[@]:-}"; do
    [ -n "${u}" ] || continue
    k="${u%%|*}"; rest="${u#*|}"; new="${rest#*|}"
    sed -i -E "s|^${k}=.*|${k}=$(sed_escape_repl "${new}")|" "${ENV_FILE}"
done

for a in "${ADDS[@]:-}"; do
    [ -n "${a}" ] || continue
    printf '%s\n' "${a}" >> "${ENV_FILE}"
done

log "Done — ${#UPDATES[@]} updated, ${#ADDS[@]} added. Recreate affected"
log "containers to apply:  docker compose up -d"
