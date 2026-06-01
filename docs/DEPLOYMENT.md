# Deployment Guide

This is the complete end-to-end runbook for deploying the tick data collector
to EC2. Follow the steps in order on a fresh instance.

**S3 bucket:** `trading-data-centheos` (all trading data lives here — do not change)

---

## What to do next — HMM Implementation Gate

| Milestone | Date | Action |
|-----------|------|--------|
| Clean collection started | **2026-06-01** | BTCUSDT + ETHUSDT collecting in parallel on t3.medium |
| **HMM A/B Campaign unblocked** | **2026-07-01** | 30 days of clean tick data reached — start Phase 16 |
| Phase 17 (Wave HMM) | After Phase 16 verdict | Only if `CampaignVerdict.promote is True` |

**On 2026-07-01**, return here and run the Phase 16 campaign procedure from
`implementation_plan.md §7.2 Phase 16`:

```bash
# 1. Download 30 days of tick data from S3
python notebooks/utils.py  # or use load_ticks_parquet() in a notebook

# 2. Run the HMM A/B campaign
python tools/hmm_abtest.py --symbols BTCUSDT ETHUSDT --windows 14 30

# 3. Record the CampaignVerdict in implementation_plan.md Phase 16
#    then flip Phase 16 status to [DONE — 2026-07-01]
#    If promote: true → Phase 17 is unblocked
```

Before starting: verify 30 days of data is in S3:
```bash
aws s3 ls s3://trading-data-centheos/ticks-parquet/binance/BTCUSDT/ --recursive | wc -l
# Expect ~30 date-partitioned .parquet files
```

---

## Emergency Recovery (EC2 hung / can't SSH)

Use this section whenever the instance is unresponsive. **Do not panic — tick data
on the EBS volume is never lost; the instance can always be recovered.**

### Step 1 — Connect without SSH

Try in order (stop when one works):

**A — EC2 Serial Console** (t3 instances are Nitro — this always works):
AWS Console → EC2 → your instance → Actions → **Monitor and troubleshoot →
EC2 Serial Console → Connect**
Login as `ubuntu` (no password needed if EC2 Instance Connect is enabled).

**B — AWS Systems Manager Session Manager**:
AWS Console → EC2 → your instance → **Connect → Session Manager → Connect**
_(Requires `AmazonSSMManagedInstanceCore` on the IAM role)_

**C — Stop → Start** (last resort, clears hung processes, EBS data is preserved):
AWS Console → EC2 → Instance State → **Stop** → wait until stopped → **Start**
The public IP changes — get the new one from the console before SSH-ing.

### Step 2 — Diagnose and clear disk

```bash
# Check what's full
df -h /

# Find the biggest consumers
sudo du -sh /var/lib/docker/* 2>/dev/null | sort -rh | head -10

# Clear ALL container logs immediately (zero-byte truncate — containers keep running)
for f in $(sudo find /var/lib/docker/containers -name '*-json.log' 2>/dev/null); do
    sudo truncate -s 0 "$f"
done

# Remove dangling Docker images and build cache (typically 4–8 GB)
docker system prune -af

# Verify disk recovered
df -h /
```

### Step 3 — Apply Docker daemon log rotation (one-time fix)

If `/etc/docker/daemon.json` does not exist or does not contain `log-opts`:

```bash
sudo tee /etc/docker/daemon.json > /dev/null <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "3"
  }
}
EOF
sudo systemctl restart docker
```

This caps every container's log at 150 MB total, regardless of compose settings.
**Must be applied on the existing instance — setup_ec2.sh sets this automatically
for new instances.**

### Step 4 — Pull latest code and restart

```bash
cd ~/app/trading
git pull

# Reinstall cron
sudo cp scripts/s3_sync.sh /etc/cron.hourly/s3_sync
sudo chmod +x /etc/cron.hourly/s3_sync

# Rebuild data service
docker compose build data
docker system prune -f --filter "until=1h"   # ← always prune after build
docker compose up -d --remove-orphans

# Rebuild ohlcv-collector (picks up log rotation + reduced verbosity)
docker compose --profile ohlcv build ohlcv-collector
docker system prune -f --filter "until=1h"
docker compose --profile ohlcv up -d --no-deps --force-recreate ohlcv-collector

# Verify
docker ps
docker compose logs --tail=20 data
docker compose --profile ohlcv logs --tail=20 ohlcv-collector | grep -v credentials
```

### Root causes and permanent fixes applied

| Root cause | Permanent fix |
|------------|--------------|
| `aiobotocore` credentials log per S3 write (1000s of lines/hour) | Silenced at WARNING in `collect_ohlcv.py` `_setup_logging` |
| Docker build cache accumulates (4–8 GB per build) | `docker system prune` runs after every `docker compose build` |
| No daemon-level log cap | `/etc/docker/daemon.json` sets 50m/3-file default for all containers |
| Compose log rotation only applies on container recreate | Daemon config applies immediately system-wide |

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
                              │
                              │ Inline (every 15 min, in-process flush thread)
                              ▼
                   data/ticks/binance/{SYMBOL}/{dataset}/YYYY-MM-DD.parquet
                              │
                              │ Hourly cron — s3_sync.sh
                              ▼
                   S3: trading-data-centheos
                       ticks/{SYMBOL}_ticks.h5            (live HDF5 snapshot)
                       ticks-parquet/binance/{SYMBOL}/…   (date-partitioned)
                       ticks/archive/binance/…            (rotated HDF5)
```

> **Why two formats?** HDF5 is the canonical store (the C++ ``TickStore``
> writes there at line-rate). Parquet is the durable, queryable mirror —
> per-day files synced to S3 so notebooks can fetch a single date window
> without downloading multi-GB HDF5 files. If the live HDF5 corrupts
> (disk full, host crash), the worst-case data loss is the last 15
> minutes — everything before that lives in Parquet/S3.

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
ssh ec2-trading

cd ~/app/trading

# Build (always prune after to reclaim build cache)
docker compose build --progress=plain > /tmp/build.log 2>&1 &
tail -f /tmp/build.log
docker system prune -f --filter "until=1h"   # reclaim build cache (4–8 GB)

# Tick collector — BTCUSDT + ETHUSDT in parallel (single container)
docker compose up -d --remove-orphans

# OHLCV collector — backfills 2020→now then polls hourly forever (Parquet → S3)
docker compose --profile ohlcv build ohlcv-collector
docker system prune -f --filter "until=1h"
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

### 5.1 Install / refresh the hourly cron

`setup_ec2.sh` installs the cron automatically on first setup.  After any
update to `scripts/s3_sync.sh`, refresh it on the running instance:

```bash
cd ~/app/trading
sudo cp scripts/s3_sync.sh /etc/cron.hourly/s3_sync
sudo chmod +x /etc/cron.hourly/s3_sync

# Verify it's there and executable
ls -l /etc/cron.hourly/s3_sync
```

Trigger an immediate sync to confirm it works:

```bash
sudo bash /etc/cron.hourly/s3_sync
cat ~/app/trading/logs/s3_sync.log

# Confirm files in S3
aws s3 ls s3://trading-data-centheos/ticks/
```

Expected:

```
ticks/BTCUSDT_ticks.h5
ticks/ETHUSDT_ticks.h5
```

And the Parquet date-partitioned mirror:

```bash
aws s3 ls s3://trading-data-centheos/ticks-parquet/binance/BTCUSDT/trades/ | head
```

Expected (one file per UTC day):

```
2026-05-31.parquet
2026-06-01.parquet
…
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
docker compose build data
docker system prune -f --filter "until=1h"   # always prune after build
docker compose up -d --remove-orphans
docker compose --profile ohlcv build ohlcv-collector
docker system prune -f --filter "until=1h"
docker compose --profile ohlcv up -d --no-deps --force-recreate ohlcv-collector
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

### Tick data — Parquet by date range (preferred)

For any analysis bound to a date window, fetch only the Parquet day-files
you need — no multi-GB HDF5 download required:

```python
import os
os.environ["S3_BUCKET"]   = "trading-data-centheos"
os.environ["AWS_PROFILE"] = "default"   # or whichever profile has read access

from notebooks.utils import load_ticks_parquet
data = load_ticks_parquet(
    "BTCUSDT",
    from_date="2026-05-15",
    to_date="2026-05-29",
    datasets=("trades",),
    refresh=True,          # downloads missing day-files from S3
)
trades = data["trades"]    # int64 ms timestamp + price/quantity/is_buyer_maker
```

Downloaded day-files are cached at
``data/cache/ticks_parquet/binance/{SYMBOL}/{dataset}/``; subsequent
calls without ``refresh=True`` read directly from cache.

### Tick data (HDF5) — full archive download

Use this only when you need the entire historical archive (e.g. the
HMM campaign that scans the full history at once):

```bash
# On your local Mac
export S3_BUCKET=trading-data-centheos
bash scripts/download_ticks.sh BTCUSDT ETHUSDT

# Verify
python -c "import h5py; f=h5py.File('data/ticks/BTCUSDT_ticks.h5'); print(list(f.keys()))"
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
The healthcheck waits for `data/ticks/{SYMBOL}_ticks.h5` to be written.
The file is created on the first trade receipt. Wait 30 seconds for the
`start_period` to pass before checking status.

### S3 sync shows FAILED in logs

```bash
cat ~/app/trading/logs/s3_sync.log
aws s3 ls s3://trading-data-centheos/ticks/
aws s3 ls s3://trading-data-centheos/ticks-parquet/ --recursive | head
```

### Verify a live HDF5 isn't corrupt

```bash
docker compose exec data python3 -c "
import h5py, sys
for sym in ('BTCUSDT', 'ETHUSDT'):
    try:
        f = h5py.File(f'/app/data/ticks/{sym}_ticks.h5', 'r', locking=False)
        print(f'{sym} OK — datasets:', {k: f[sym][k].shape for k in f[sym]})
        f.close()
    except (OSError, FileNotFoundError) as e:
        print(f'{sym} FAILED: {e}', file=sys.stderr)
"
```

If a file is corrupt, the Parquet mirror in S3
(`s3://${S3_BUCKET}/ticks-parquet/binance/{SYMBOL}/`) still holds
everything older than the last 15 minutes. Stop the symbol, delete the
corrupt HDF5, and restart — fresh HDF5 collection resumes immediately
and the Parquet mirror keeps writing without interruption:

```bash
docker compose stop data
rm ~/app/trading/data/ticks/BTCUSDT_ticks.h5    # or ETHUSDT_ticks.h5
docker compose up -d data
docker compose logs -f data
```

### HDF5 file grew past the rotation limit

The collector auto-rotates HDF5 when it exceeds `--max-h5-gb` (default
8 GB): archives the old file to `s3://${S3_BUCKET}/ticks/archive/…`,
deletes the local copy, and exits with code 75. Docker
`restart: unless-stopped` (and systemd `Restart=on-failure`) start a
fresh process with a new empty HDF5. Verify after a rotation:

```bash
docker compose logs data --tail 50 | grep -E 'rotation|archive'
aws s3 ls s3://trading-data-centheos/ticks/archive/binance/ | tail
```

### EC2 disk usage budget

With the rotation + Parquet-cleanup design, expected steady-state local
disk usage per symbol:

| Data | Local retention | Permanent home |
|------|----------------|----------------|
| HDF5 live buffer | ≤ 8 GB per symbol (auto-rotated) | S3 archive |
| Parquet (recent) | 7 days per symbol | S3 (`ticks-parquet/`) |
| Parquet (older)  | Deleted by `s3_sync.sh` after S3 sync | S3 |

For 2 symbols (BTCUSDT + ETHUSDT) peak local usage is ~16 GB HDF5 +
~1–2 GB Parquet. A 30–50 GB EC2 root volume leaves comfortable headroom.
