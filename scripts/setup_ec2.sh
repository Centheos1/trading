#!/usr/bin/env bash
# EC2 bootstrap script for Ubuntu 24.04 LTS.
#
# Installs Docker + Docker Compose, clones the repo, configures .env,
# and starts the collector via docker compose up -d.
#
# Usage (run as ubuntu with sudo available):
#   curl -sSL https://raw.githubusercontent.com/.../setup_ec2.sh | bash
#   -- OR --
#   bash scripts/setup_ec2.sh
#
# Environment variables (optional):
#   REPO_URL    Git repo URL to clone (leave blank if repo already present)
#   APP_DIR     Destination directory (default: /home/ubuntu/app/trading)
#   S3_BUCKET   Pre-fill .env S3_BUCKET (can also be set interactively after)
#   SYMBOLS     Primary symbol to collect (default: BTCUSDT)

set -euo pipefail

APP_DIR="${APP_DIR:-/home/ubuntu/app/trading}"
REPO_URL="${REPO_URL:-}"
SERVICE_USER="${SERVICE_USER:-ubuntu}"

log() { echo "[setup_ec2] $(date -u +%H:%M:%SZ) $*"; }

# ---------------------------------------------------------------------------
# 1. Install Docker Engine + Docker Compose plugin
# ---------------------------------------------------------------------------
log "Installing Docker..."
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg

# Docker's official apt repo
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) \
  signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update -qq
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin

# Allow SERVICE_USER to run docker without sudo
sudo usermod -aG docker "${SERVICE_USER}"

# Configure Docker daemon with default log rotation so container logs can
# never fill the root disk, regardless of individual compose file settings.
# max-size: 50m = each container's log file caps at 50 MB
# max-file: 3   = keep 3 rotated files → max 150 MB per container total
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json > /dev/null <<'DAEMON_JSON'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "3"
  }
}
DAEMON_JSON

sudo systemctl enable docker
sudo systemctl start docker

log "Docker $(docker --version) installed."
log "Docker Compose $(docker compose version) installed."

# ---------------------------------------------------------------------------
# 2. Application directory
# ---------------------------------------------------------------------------
if [ -n "${REPO_URL}" ]; then
    log "Cloning repo to ${APP_DIR}..."
    sudo mkdir -p "${APP_DIR}"
    sudo chown "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"
    git clone "${REPO_URL}" "${APP_DIR}"
else
    log "Using existing directory: ${APP_DIR}"
    if [ "$(pwd)" != "${APP_DIR}" ]; then
        sudo mkdir -p "${APP_DIR}"
        sudo cp -r . "${APP_DIR}/"
        sudo chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"
    fi
fi

cd "${APP_DIR}"

# ---------------------------------------------------------------------------
# 3. Data and log directories (bind-mounted by docker compose)
# ---------------------------------------------------------------------------
mkdir -p data logs
log "Directories: data/ logs/ created."

# ---------------------------------------------------------------------------
# 4. .env file
# ---------------------------------------------------------------------------
if [ ! -f .env ]; then
    log "Creating .env from template..."
    cp .env.template .env
    # Pre-fill from environment variables if provided
    if [ -n "${S3_BUCKET:-}" ]; then
        sed -i "s/^S3_BUCKET=$/S3_BUCKET=${S3_BUCKET}/" .env
    fi
    if [ -n "${SYMBOLS:-}" ]; then
        sed -i "s/^SYMBOLS=BTCUSDT$/SYMBOLS=${SYMBOLS}/" .env
    fi
    log ".env created — review with: nano ${APP_DIR}/.env"
else
    log ".env already exists — reconciling against template."
fi

# Reconcile .env against the template on EVERY deploy. .env is gitignored, so a
# box otherwise keeps whatever values it was first created with — a stale .env
# then SILENTLY masks new shipped defaults (root cause of the 2026-06 freeze,
# which kept running the old 900s/8 GB settings after a redeploy). reconcile_env
# adds missing keys, forces the managed reliability tunables to the template
# value, and reports any other drift. Operator secrets (blank in the template)
# are never touched.
if [ -f scripts/reconcile_env.sh ]; then
    bash scripts/reconcile_env.sh .env .env.template \
        || log "WARN: .env reconcile reported issues (see output above)."
fi

# ---------------------------------------------------------------------------
# 5. Build the Docker image
# ---------------------------------------------------------------------------
log "Building collector Docker image..."
# Collector host only: data-btc, data-eth, ohlcv. Never strategy/BookMap here.
docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile ohlcv build

docker system prune -f --filter "until=1h"

log "Image built."

# ---------------------------------------------------------------------------
# 6. Install docker compose as a systemd service so it auto-starts on reboot
# ---------------------------------------------------------------------------
log "Installing docker-compose@trading systemd service (collector profile)..."
sudo tee /etc/systemd/system/docker-compose@trading.service > /dev/null <<SERVICE
[Unit]
Description=Docker Compose — Trading Collector (%i)
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${APP_DIR}
# Collector EC2: per-symbol tick services + OHLCV. Strategy is profile-gated
# off this host. REDIS_URL must point at ElastiCache (see .env / infra/).
ExecStart=/usr/bin/docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile ohlcv up -d data-btc data-eth ohlcv-collector
ExecStop=/usr/bin/docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile ohlcv down
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable docker-compose@trading.service

log "docker-compose@trading.service enabled (starts on boot)."

# ---------------------------------------------------------------------------
# 7. Install hourly S3 sync cron (runs on the host, not in the container)
# ---------------------------------------------------------------------------
# SYMLINK, not copy: /etc/cron.hourly was previously a one-time `cp`, so a later
# `git pull` left the cron running a STALE script (the 2026-06 follow-up: the
# installed health check predated the CloudWatch heartbeat and silently never
# emitted, leaving the alarm permanently breaching). A symlink to the repo file
# means every `git pull` updates the cron job too. run-parts follows symlinks
# and accepts these names (no dot/extension).
if [ -f scripts/s3_sync.sh ]; then
    sudo chmod +x scripts/s3_sync.sh
    sudo ln -sf "${APP_DIR}/scripts/s3_sync.sh" /etc/cron.hourly/s3_sync
    log "Hourly S3 sync cron linked → ${APP_DIR}/scripts/s3_sync.sh."
fi

# ---------------------------------------------------------------------------
# 8. Install hourly pipeline-health alarm (runs AFTER s3_sync)
# ---------------------------------------------------------------------------
# run-parts executes /etc/cron.hourly/* in lexical order, so the "zz_" prefix
# ensures the health check runs after s3_sync each hour. It asserts the Parquet
# mirror is producing RECENT data (the guardrail for the 2026-06 freeze) and
# alerts on failure — see scripts/pipeline_health.sh and docs/DEPLOYMENT.md.
if [ -f scripts/pipeline_health.sh ]; then
    sudo chmod +x scripts/pipeline_health.sh
    sudo ln -sf "${APP_DIR}/scripts/pipeline_health.sh" /etc/cron.hourly/zz_pipeline_health
    log "Hourly pipeline-health alarm linked → ${APP_DIR}/scripts/pipeline_health.sh."
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
log "Setup complete."
echo ""
echo "┌─────────────────────────────────────────────────────────────┐"
echo "│  Next steps                                                  │"
echo "│                                                              │"
echo "│  1. Edit .env:  nano ${APP_DIR}/.env                        │"
echo "│     Set S3_BUCKET=your-bucket-name                          │"
echo "│                                                              │"
echo "│  2. Set REDIS_URL to ElastiCache (required in prod).         │"
echo "│     Confirm go-live gate in docs/DEPLOYMENT.md.              │"
echo "│                                                              │"
echo "│  3. Start collector (no strategy on this host):              │"
echo "│     docker compose -f docker-compose.yml \\                   │"
echo "│       -f docker-compose.prod.yml --profile ohlcv up -d       │"
echo "│                                                              │"
echo "│  4. Watch logs:                                              │"
echo "│     docker compose logs -f data-btc data-eth ohlcv-collector │"
echo "│                                                              │"
echo "│  5. Verify S3 tick shards (after flush + sync):              │"
echo "│     aws s3 ls s3://YOUR-BUCKET/ticks/                        │"
echo "└─────────────────────────────────────────────────────────────┘"
echo ""
