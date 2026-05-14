# Deployment Guide

This is the complete end-to-end runbook for deploying the tick data collector
to EC2. Follow the steps in order on a fresh instance.

**S3 bucket:** `trading-data-centheos` (all trading data lives here — do not change)

---

## Overview

```
Local Mac  ──git push──►  GitHub
                              │
                              │ git clone (SSH)
                              ▼
                         EC2 t3.small (Ubuntu 24.04)
                              │
                         docker compose up -d
                              │
                    ┌─────────┴─────────┐
                    │                   │
              collector             collector-eth
            (BTCUSDT)               (ETHUSDT)
                    │                   │
                    └─────────┬─────────┘
                              │ hourly cron
                              ▼
                   S3: trading-data-centheos
                       ticks/binance_ticks.h5
                       ticks/binance_ticks_ETHUSDT.h5
```

---

## Part 1 — One-time setup (run on your local Mac, not EC2)

### 1.1 Install AWS CLI on your Mac

```bash
brew install awscli
aws configure
# AWS Access Key ID:     <your key>
# AWS Secret Access Key: <your secret>
# Default region:        ap-southeast-2
# Default output format: json
```

### 1.2 Create the S3 bucket

> **Important:** Create the bucket from your local Mac. The EC2 IAM role
> intentionally does not have `s3:CreateBucket` — it only writes objects.
> Running `aws s3 mb` from EC2 will return `AccessDenied`.

```bash
# Create
aws s3 mb s3://trading-data-centheos --region ap-southeast-2

# Enable versioning (protects against accidental overwrites during hourly sync)
aws s3api put-bucket-versioning \
    --bucket trading-data-centheos \
    --versioning-configuration Status=Enabled

# Enable SSE-S3 encryption at rest (free, no key management required)
aws s3api put-bucket-encryption \
    --bucket trading-data-centheos \
    --server-side-encryption-configuration '{
        "Rules": [{
            "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
            "BucketKeyEnabled": true
        }]
    }'

# Lifecycle: auto-delete old versions after 30 days to control storage cost
aws s3api put-bucket-lifecycle-configuration \
    --bucket trading-data-centheos \
    --lifecycle-configuration '{
        "Rules": [{
            "ID": "expire-old-versions",
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "NoncurrentVersionExpiration": {"NoncurrentDays": 30}
        }]
    }'

# Verify
aws s3 ls s3://trading-data-centheos
```

### 1.3 Create the IAM role

In **AWS Console → IAM → Roles → Create role:**

1. Trusted entity: **AWS service → EC2**
2. Skip managed policies
3. Role name: `TradingCollectorRole`

After creating, add this inline policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::trading-data-centheos",
        "arn:aws:s3:::trading-data-centheos/*"
      ]
    }
  ]
}
```

---

## Part 2 — Launch EC2

In **AWS Console → EC2 → Launch Instance:**

| Setting | Value |
|---|---|
| Name | `trading-collector` |
| AMI | Ubuntu Server 24.04 LTS (Canonical) — **64-bit x86, not ARM** |
| Instance type | `t3.small` (2 vCPU, 2 GB RAM) |
| Key pair | Create new → RSA → `.pem` → save to `~/Downloads/` |
| Security group | New — SSH inbound from My IP only; all outbound open |
| Storage | 30 GB gp3 |
| IAM instance profile | `TradingCollectorRole` |

> If you forgot to attach the IAM role at launch, you can add it without
> stopping the instance: **EC2 console → select instance → Actions →
> Security → Modify IAM role → TradingCollectorRole**.

SSH in:

```bash
ssh -i ~/Downloads/trading-collector.pem ubuntu@<EC2-PUBLIC-IP>
```

---

## Part 3 — First-time EC2 setup (run once per instance)

### 3.1 Install AWS CLI v2

> **Note:** `sudo apt install awscli` fails on Ubuntu 24.04 — the package
> is not available in the apt repo. Install the official v2 binary instead.

```bash
sudo apt install -y unzip curl
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip awscliv2.zip
sudo ./aws/install
aws --version   # should show: aws-cli/2.x.x
```

No `aws configure` needed — the `TradingCollectorRole` instance profile
provides credentials automatically. Verify:

```bash
aws s3 ls s3://trading-data-centheos
```

### 3.2 Set up GitHub SSH authentication

> GitHub removed password authentication in 2021. Attempting to clone with
> a password returns: `remote: Invalid username or token. Password
> authentication is not supported for Git operations.`
> Use SSH instead — it never expires.

```bash
# Generate a key for this instance
ssh-keygen -t ed25519 -C "trading-ec2" -f ~/.ssh/id_ed25519 -N ""

# Print the public key — copy this entire output
cat ~/.ssh/id_ed25519.pub
```

In your browser: **github.com → Settings → SSH and GPG keys → New SSH key**
- Title: `trading-ec2`
- Paste the public key
- Click **Add SSH key**

Test the connection:

```bash
ssh -T git@github.com
# Expected: Hi Centheos1! You've successfully authenticated...
```

### 3.3 Clone the repo and run setup

```bash
mkdir -p ~/app
git clone git@github.com:Centheos1/trading.git ~/app/trading
cd ~/app/trading

# Bootstrap: installs Docker, builds the image, enables auto-start on reboot
bash scripts/setup_ec2.sh
```

### 3.4 Fix Docker group permissions

> `setup_ec2.sh` adds your user to the `docker` group, but Linux only applies
> group membership to **new** login sessions. Your current SSH session was
> already open when the script ran, so Docker commands return:
> `permission denied while trying to connect to the Docker daemon socket at
> unix:///var/run/docker.sock`

Fix — either run:

```bash
newgrp docker
```

Or disconnect and reconnect your SSH session:

```bash
exit
ssh -i ~/Downloads/trading-collector.pem ubuntu@<EC2-PUBLIC-IP>
```

Verify:

```bash
docker ps   # should show an empty table, not a permissions error
```

### 3.5 Configure .env

```bash
cp ~/app/trading/.env.template ~/app/trading/.env
nano ~/app/trading/.env
```

Set the following values:

```
S3_BUCKET=trading-data-centheos
SYMBOLS=BTCUSDT
LOG_LEVEL=INFO

# OHLCV historical collector — write Parquet straight to S3 on EC2
DATA_STORE=s3
OHLCV_EXCHANGE=all
OHLCV_TIMEFRAME=1m
OHLCV_FROM_DATE=2020-01-01
OHLCV_MODE=continuous
OHLCV_POLL_INTERVAL=3600
OHLCV_WORKERS=4

# Required for Oanda OHLCV collection
OANDA_ACCOUNT_ID=...
OANDA_ACCESS_TOKEN=...
OANDA_ACCOUNT_TYPE=practice
```

Save: `Ctrl+O` → Enter. Exit: `Ctrl+X`.

---

## Part 4 — Start data collection

```bash
cd ~/app/trading

# Build if needed
# In a second SSH session
docker compose build --progress=plain 2>&1 | tail -20

# Tick collectors — real-time WebSocket trades + L2 depth (HDF5 → S3)
docker compose up -d                              # BTCUSDT
docker compose --profile multi up -d              # ETHUSDT

# OHLCV collector — backfills 2020→now then polls hourly forever (Parquet → S3)
docker compose --profile ohlcv up -d

# Verify all three are running
docker compose ps
```

Expected output:

```
NAME                          STATUS
trading-collector-btcusdt     Up X seconds (healthy)
trading-collector-ethusdt     Up X seconds
trading-ohlcv-collector       Up X seconds
```

Watch live logs:

```bash
docker compose logs -f                       # everything together
docker compose logs -f collector             # BTCUSDT ticks only
docker compose logs -f collector-eth         # ETHUSDT ticks only
docker compose logs -f ohlcv-collector       # OHLCV historical
```

Expected tick collector log line every 10 seconds:

```
2026-05-13T10:00:00 [INFO] data_service :: Collected 842 trades, 3100 depth updates
```

Expected OHLCV collector log line during backfill:

```
2026-05-13T10:00:00 [INFO] collect_ohlcv :: [binance/BTCUSDT/1m] 2020-01-01 00:00 → 2026-05-13 10:00 — 3,159,840 new rows (12.4s)
```

> **OHLCV backfill runtime:** the first run takes ~30–40 hours to fill 6 years
> of 1-minute data for ~300 Binance perpetuals + ~100 Oanda instruments.
> It runs detached and survives SSH disconnects. After backfill it enters
> the hourly continuous loop automatically.

---

## Part 5 — Verify S3 upload

The hourly cron at `/etc/cron.hourly/s3_sync` runs automatically.
To trigger manually:

```bash
sudo bash /etc/cron.hourly/s3_sync
cat ~/app/trading/logs/s3_sync.log

# Confirm files in S3
aws s3 ls s3://trading-data-centheos/ticks/
```

Expected:

```
ticks/binance_ticks.h5
ticks/binance_ticks_ETHUSDT.h5
```

Verify OHLCV Parquet uploads (written directly by the collector when
`DATA_STORE=s3`):

```bash
aws s3 ls s3://trading-data-centheos/ohlcv/binance/ --recursive | head
aws s3 ls s3://trading-data-centheos/ohlcv/oanda/   --recursive | head
```

Expected:

```
ohlcv/binance/BTCUSDT/1m.parquet
ohlcv/binance/ETHUSDT/1m.parquet
ohlcv/binance/SOLUSDT/1m.parquet
…
ohlcv/oanda/EUR_USD/1m.parquet
ohlcv/oanda/XAU_USD/1m.parquet
ohlcv/oanda/SPX500_USD/1m.parquet
…
```

---

## Part 6 — Ongoing operations

### Auto-start on reboot

The `docker-compose@trading` systemd service auto-starts both collectors
on EC2 reboot — no manual action needed. Verify:

```bash
sudo systemctl status docker-compose@trading
docker compose ps
```

### Update after a code push

```bash
cd ~/app/trading
git pull
docker compose build
docker compose up -d
docker compose --profile multi up -d
```

### Check logs after a restart

```bash
docker compose logs --tail 50 collector
docker compose logs --tail 50 collector-eth
```

### Stop collection temporarily

```bash
docker compose down                      # stops all services
docker compose up -d                     # restart BTCUSDT
docker compose --profile multi up -d     # restart ETHUSDT
```

---

## Part 7 — Download data to dev machine

### Tick data (HDF5) — needed for HMM campaign

```bash
# On your local Mac
export S3_BUCKET=trading-data-centheos
bash scripts/download_ticks.sh BTCUSDT ETHUSDT

# Verify
python -c "import h5py; f=h5py.File('data/binance_ticks.h5'); print(list(f.keys()))"
```

### OHLCV (Parquet) — needed for backtests / PCA / cross-asset analysis

There are two ways to read OHLCV from a dev machine:

**Option A — read directly from S3** (no download; recommended for large
multi-symbol analyses). In `.env`:

```
DATA_STORE=s3
S3_BUCKET=trading-data-centheos
```

`tide_backtest.load_ohlcv()` and any PCA/correlation tooling will read
Parquet straight from S3 via `s3fs` + `pyarrow`.

**Option B — download specific symbols locally** (faster for repeated
backtests on one symbol):

```bash
# A single symbol
bash scripts/download_ohlcv.sh --exchange binance --symbol BTCUSDT

# All Binance OHLCV (~30 GB)
bash scripts/download_ohlcv.sh --exchange binance

# All Oanda OHLCV (~8 GB)
bash scripts/download_ohlcv.sh --exchange oanda

# Verify
python -c "import pandas as pd; df=pd.read_parquet('data/ohlcv/binance/BTCUSDT/1m.parquet'); print(df.tail())"
```

When `DATA_STORE=local_parquet` (default in `.env.template`), all
backtests automatically read from `data/ohlcv/…` first and fall back
to the legacy `data/{exchange}.h5` if the symbol isn't present.

### Run the HMM campaign (after ≥30 days of data)

```bash
source .venv/bin/activate
python tools/hmm_abtest.py \
    --symbols BTCUSDT,ETHUSDT \
    --windows 30d,60d \
    --seed 42
# Results written to reports/hmm_campaign_summary_*.md
```

---

## Part 8 — Local development

### Run tests (Linux parity, no display needed)

```bash
docker compose --profile test run --rm test
```

### Open in Cursor dev container

1. Open project in Cursor → click **"Reopen in Container"** when prompted
2. First open builds the image (~5–10 min); subsequent opens reuse cache

### Run trading UI (native macOS — fastest for UI iteration)

```bash
source .venv/bin/activate
python main.py   # choose 'ui' mode — uses Metal GPU directly
```

---

## Part 9 — EC2 GPU instance (Phase 9 — future)

> Not yet implemented. Target: `g5.xlarge` (NVIDIA A10G), Ubuntu 24.04,
> NICE DCV remote display. A `Dockerfile.gpu` will extend `Dockerfile.dev`
> with the NVIDIA Container Toolkit and `QSG_RHI_BACKEND=opengl`.

---

## Troubleshooting

### `aws: command not found` on Ubuntu 24.04
The apt package is unavailable. Use the official v2 installer — see §3.1.

### `make_bucket failed: AccessDenied`
The EC2 IAM role cannot create buckets — by design. Create the bucket from
your local Mac — see §1.2.

### `permission denied: /var/run/docker.sock`
Your session pre-dates the Docker group change from `setup_ec2.sh`. Run
`newgrp docker` or reconnect your SSH session — see §3.4.

### `Authentication failed` cloning from GitHub
GitHub no longer accepts passwords. Set up SSH key authentication — see §3.2.

### `Unable to locate credentials` on EC2
The IAM instance profile is not attached. Go to EC2 console →
**Actions → Security → Modify IAM role → TradingCollectorRole**.
No instance restart required.

### Container keeps restarting

```bash
docker compose logs collector --tail 50
```

### Healthcheck failing on first start
The healthcheck waits for `data/binance_ticks.h5` to be written. This file
is created on first trade receipt. Wait 30 seconds for the `start_period`
to pass before checking status.

### S3 sync shows FAILED in logs

```bash
cat ~/app/trading/logs/s3_sync.log
aws s3 ls s3://trading-data-centheos/ticks/
```
