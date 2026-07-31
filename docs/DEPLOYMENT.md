# Deployment Guide

This is the complete end-to-end runbook for deploying the **greenfield
Parquet-only** tick data collector to EC2. Follow the steps in order.

**S3 bucket:** `trading-data-centheos` (all trading data lives here — do not change)

---

## Greenfield architecture (2026-07)

| Concern | Design |
|---|---|
| Durable ticks | Immutable Parquet shards `ticks/{venue}/{symbol}/{dataset}/date=…/hour=…/part-*.parquet` |
| Live MD bus | ElastiCache Redis Streams `md:{venue}:{kind}` (best-effort; never SoR) |
| Process model | One container per `(venue, symbol)` — `data-btc`, `data-eth` |
| OHLCV | Yearly Parquet only; `workers=2`; bounded adapter retries that **raise** |
| This host | **Collector only** — no strategy / BookMap / uvicorn |
| CI/CD | GitHub Actions CI on push; Infra/App CD path-filtered + environment approval |

### Cross-host contracts

| Concern | Contract |
|---|---|
| Durable history | Read `s3://…/ticks/{venue}/…` and `ohlcv/{venue}/…` |
| Live market data | Subscribe ElastiCache Streams `md:{venue}:{kind}` filtered by symbol |
| BookMap overlays | Strategy `bookmap_publisher` WebSocket on the **strategy** host |
| Failure isolation | Strategy down ≠ stop collector; Redis down ≠ stop Parquet; one symbol ≠ kill others |

### Go-live gate (instance stays **stopped** until all pass)

1. Unit tests green: `python -m unittest tests.test_tick_parquet_store tests.test_collect_ticks tests.test_ohlcv_bounded_retry -v`
2. Staging dry-run ≥30 min dual-symbol; `kill -9` one symbol container → other continues; restart → no shard corruption; S3 shows new `part-*.parquet` within flush interval.
3. OHLCV: one Oanda + one Binance year append under `mem_limit`; forced SSL faults raise (no infinite loop).
4. `pipeline_health.sh` emits `PipelineHealthy=1`; forced stop → alarm within 2 periods.
5. Disk headroom > 40%; **no** `*_ticks.h5` written.
6. Archive/clean S3 via `scripts/s3_archive_and_clean.sh --dry-run` then `--execute` after review.
7. Only then: start EC2, deploy, watch 24h unattended.

### S3 archive / clean

**Use the profile that owns the bucket.** The default credential chain on the
dev laptop resolves to an unrelated AWS account (`exerp-server`, acct
`346880879164`) and fails with a bare `AccessDenied`. The bucket lives in acct
`437329951890` — profile `trading`. The script now prints the resolved identity
up front and refuses to continue if it cannot list the bucket.

```bash
./scripts/s3_archive_and_clean.sh --profile trading \
  --bucket trading-data-centheos --dry-run

# Reclaim the 6.6 TB AND stop it recurring. This is the minimum fix.
./scripts/s3_archive_and_clean.sh --profile trading \
  --bucket trading-data-centheos \
  --expire-versions --apply-lifecycle --execute

# Selective tick cleanup: delete corrupt HDF5, Glacier readable tick data,
# and prove OHLCV stayed untouched in Standard.
./scripts/s3_archive_and_clean.sh --profile trading \
  --bucket trading-data-centheos \
  --archive-ticks --dry-run
./scripts/s3_archive_and_clean.sh --profile trading \
  --bucket trading-data-centheos \
  --archive-ticks --execute

# Full-bucket archive path. Do NOT use this when OHLCV must stay Standard.
./scripts/s3_archive_and_clean.sh --profile trading \
  --bucket trading-data-centheos \
  --expire-versions --delete-corrupt-h5 --glacier --clean-live --execute
```

`--expire-versions` is the **one-off cleanup**; `--apply-lifecycle` is the
**recurrence fix**. They are separate because expiring versions today does
nothing to stop the tail re-growing tomorrow. `--apply-lifecycle` installs a
`NoncurrentVersionExpiration` rule (`--noncurrent-days`, default 14, matching
`infra/main.tf`) plus a 7-day `AbortIncompleteMultipartUpload`, without any
Glacier transition.
`--glacier` applies that same protection rule *and* the archival transition in
a single `put-bucket-lifecycle-configuration` call, so the two flags cannot
clobber each other.

A lifecycle PUT replaces the **entire** document, and both this script and
`infra/main.tf` use the rule ID `expire-noncurrent-versions`. Once Terraform
manages the bucket it is authoritative — change retention there. A unit test
(`tests/test_s3_lifecycle_policy.py`) fails if the two defaults drift apart.
Use `--print-lifecycle` to review the exact document without touching AWS.

Every run ends by reading the live bucket config back and logs a loud `[WARN]`
if versioning is enabled while no noncurrent-expiry rule exists.

`--archive-ticks` is the selective path. It auto-enables
`--delete-corrupt-h5`, then copy-verifies every remaining current object under
`ticks/` and `ticks-parquet/` into `cold-archive/` with Glacier Flexible
Retrieval. Only after verification does it permanently delete the exact source
VersionId, avoiding both delete markers and fresh noncurrent source copies.
Retries reuse a matching destination; a conflict aborts rather than overwrite
Glacier data. It snapshots the `ohlcv/` object count and byte total before and
after and fails if either changes.

`--archive-ticks` is rejected when combined with broad `--glacier` or
`--clean-live`, because those modes also affect `ohlcv/`.

`--delete-corrupt-h5` permanently removes all versions of `.h5` keys marked
`ok: false` in `logs/h5_validate/report.json` (fallback: the four keys confirmed
corrupt by h5py on 2026-07-28). It is auto-enabled by `--archive-ticks`,
`--glacier`, and `--clean-live`.

Selective restore later: `scripts/s3_restore_tick_parquet.sh`.

#### OHLCV quality audit (2026-07-28)

The timestamp column of every one of the **1,915 current OHLCV Parquet
objects (16.502 GB)** was read directly from S3 and unioned across legacy-flat
and yearly layouts per exchange/symbol/timeframe. There were **zero unreadable
objects**.

| Exchange | Series | Unique candles | Finding |
|---|---:|---:|---|
| Binance | 516 | 612,198,701 | **Zero internal one-minute gaps** across each stored series; Binance is 24/7 |
| Oanda | 127 | 271,113,215 | Readable, but not certifiably gap-free without per-instrument trading calendars |

Oanda does not emit a candle for every wall-clock minute with no price change,
and its FX, metals, index, bond, and commodity instruments have different
sessions and holiday closures. A strict one-minute check therefore produces
false positives. Even after the repository's generic weekend/daily-close
suppression, long closures remain (for example recurring overnight closures
in indices and year-end holidays). The repository does not currently encode
per-instrument calendars, so do **not** claim that every Oanda gap is explained.
The data remains worth retaining and is suitable for gap-aware OHLCV research;
strategies must not assume a perfectly contiguous Oanda minute grid.

#### Measured inventory (2026-07-28) — versioning blowout

The plan's "~51 GB" was **current objects only**. Actual billed storage is
~130× larger because versioning is on with no lifecycle rule:

| | Objects / versions | Size |
|---|---|---|
| Current objects | 2,069 | **47.5 GB** |
| Noncurrent versions | 79,983 | **6,601.5 GB** |
| **Total billed** | 82,052 | **6,649.0 GB** |

Where the noncurrent bulk came from:

| Prefix | Noncurrent versions | Size | Cause |
|---|---|---|---|
| `ticks/` | 2,575 | 5,356 GB | Hourly `s3_sync` re-uploaded each whole multi-GB HDF5 file — `BTCUSDT_ticks.h5` alone has 1,237 versions / 2,726 GB |
| `ohlcv/` | 75,550 | 1,116 GB | Legacy single-file `{tf}.parquet` read-modify-write — every append rewrote the whole file |
| `ticks-parquet/` | 1,858 | 129 GB | Daily-file overwrite on each flush |

At ap-southeast-2 Standard rates (~$0.025/GB-month) that is roughly **$166/month
instead of ~$1.20/month**. Expiring noncurrent versions is free (DELETE requests
are not billed), so step 2 alone removes essentially all of it.

The greenfield design removes all three root causes: no HDF5 uploads, immutable
write-once shards (no overwrite), and yearly-only OHLCV files.

#### Remediated 2026-07-28

Run: `--expire-versions --apply-lifecycle --execute` (profile `trading`).

| | Before | After |
|---|---|---|
| Current objects | 2,069 / 47.46 GB | 2,069 / 47.46 GB |
| Noncurrent versions | 79,983 / 6,601.52 GB | **0 / 0 GB** |
| Delete markers | 4 | 0 |
| **Total billed** | **6,648.98 GB** | **47.46 GB** |

79,987 objects deleted. Estimated cost drops from ~$166/month to ~$1.20/month.
No current object was touched.

The bucket now carries a live `expire-noncurrent-versions` rule (14-day
`NoncurrentVersionExpiration`, 7-day `AbortIncompleteMultipartUpload`), verified
by reading the config back. Applying `infra/main.tf` is now a no-op for this
rule and should stay that way — Terraform is authoritative from here.

#### Step ordering is enforced

`--glacier` / `--clean-live` are **refused** while noncurrent versions remain,
unless `--expire-versions` is in the same invocation. Reasons:

- Glacier Flexible Retrieval bills a **90-day minimum per object** — archiving
  6.6 TB of junk versions locks in months of cost.
- With versioning on, `aws s3 rm` only writes **delete markers**; the "removed"
  objects survive as noncurrent versions and keep billing. Expiring is what
  actually reclaims the space.

### Start collector (prod)

```bash
# .env must set REDIS_URL=redis://<elasticache>:6379 and S3_BUCKET=…
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  --profile ohlcv up -d data-btc data-eth ohlcv-collector
```

Local laptop with bundled Redis:

```bash
docker compose --profile bundled-redis up -d data-btc data-eth
```

---

## What to do next — HMM Implementation Gate

> ⚠️ **The 2026-06-01 "clean collection" did not hold.** The Parquet mirror froze
> on 2026-06-17 and went unnoticed for ~10 days — see
> [`INCIDENT_2026-06_tick_pipeline.md`](INCIDENT_2026-06_tick_pipeline.md). The
> pipeline was re-architected on 2026-06-28 (durability decoupled from HDF5); the
> clean 30-day clock **restarts on redeploy**.

| Milestone | Date | Action |
|-----------|------|--------|
| ~~Clean collection started~~ (voided) | ~~2026-06-01~~ → froze 2026-06-17 | See incident post-mortem |
| Re-architected redeploy | ≈ **2026-06-29** (pending authorisation) | Decoupled mirror + disk floor + CloudWatch alarm |
| **HMM A/B Campaign unblocked** | ≈ **2026-07-29** | redeploy + 30 days of clean tick data — start Phase 16 |
| Phase 17 (Wave HMM) | After Phase 16 verdict | Only if `CampaignVerdict.promote is True` |

**Once 30 clean days are reached (≈ 2026-07-29)**, return here and run the Phase 16
campaign procedure from `implementation_plan.md §7.2 Phase 16`:

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
| **Parquet mirror silently froze for ~10 days (2026-06, recurrence)** — durability depended on a *separate reader* (the flush thread / `flush_from_h5`) tailing one ever-growing, corruption-prone live HDF5 file. When the HDF5 superblock/B-tree corrupted under append load, the read returned empty/garbage, the watermark advanced without writing any Parquet, and the mirror froze while the container stayed *healthy* and HDF5 kept uploading | **Rearchitected: the durable mirror is now written DIRECTLY off the live feed** (`tick_parquet_store.LiveParquetMirror`, fed in `data_service.py`). HDF5 is no longer on the durability path — it can corrupt, rotate, or be deleted without freezing Parquet. See **"Tick data durability architecture"** below |
| **Root disk hit 96% with two multi-GB HDF5 files → write failures + OOM kills** | Hard free-disk floor `MIN_FREE_DISK_GB` (default 2 GB) forces early HDF5 rotation regardless of `MAX_H5_GB`; default cap lowered to 3 GB since HDF5 is now secondary (`collect_ticks._rotation_reason`) |
| Freeze was invisible for 10 days — webhook/SNS alerts were never configured | `scripts/pipeline_health.sh` now also emits an hourly `PipelineHealthy` CloudWatch metric; `scripts/create_cloudwatch_alarm.sh` creates an alarm that pages on `0` **and on missing data** (a dead box) |
| `s3fs`/`aiobotocore` vs `botocore` version skew broke S3 Parquet listing | Keep the AWS dependency matrix compatible; verify after any bump (see `docs/CODING_STANDARDS.md` §5) |
| "Files exist in S3" was mistaken for "pipeline works" while data was 2 weeks stale | Health checks below assert the **latest day is current** and **row counts advance**, not merely that files exist |

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
                              │ LiveParquetMirror: buffered off the feed,
                              │ flushed every PARQUET_FLUSH_INTERVAL s (default 60)
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

> **`.env` is gitignored — reconcile it on every deploy.** Because `.env`
> persists per-box, a stale file silently masks new shipped defaults. This was
> the root cause of the 2026-06 freeze: after a redeploy the mirror kept running
> the old `PARQUET_FLUSH_INTERVAL=900` / `MAX_H5_GB=8` settings even though the
> rearchitecture had lowered them to `60` / `3`. `setup_ec2.sh` now runs
> `scripts/reconcile_env.sh` automatically, but run it by hand after any
> `git pull` that changes `.env.template`:
>
> ```bash
> cd ~/app/trading
> DRY_RUN=1 bash scripts/reconcile_env.sh   # preview: [update]/[add]/[drift]
> bash scripts/reconcile_env.sh             # apply (backs up .env first)
> docker compose --profile ohlcv up -d      # recreate to pick up new values
> ```
>
> It **forces** the managed reliability tunables (`MAX_H5_GB`,
> `MIN_FREE_DISK_GB`, `PARQUET_FLUSH_INTERVAL`, `HEALTH_MAX_STALENESS_HOURS`,
> `S3_SYNC_MAX_AGE_MIN`, `CLOUDWATCH_NAMESPACE`, `DATA_STORE`) to the template
> value, **adds** any missing keys, and **reports** other drift without touching
> operator secrets (`S3_BUCKET`, `OANDA_*`, `BINANCE_*`, `ALERT_*`). Verify the
> live values inside the container with
> `docker compose exec data env | grep -E 'PARQUET_FLUSH|MAX_H5|MIN_FREE'`.

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

`setup_ec2.sh` installs both hourly crons as **symlinks** into `/etc/cron.hourly`
pointing at the repo scripts, so a `git pull` keeps them current automatically:

```bash
cd ~/app/trading
sudo chmod +x scripts/s3_sync.sh scripts/pipeline_health.sh
sudo ln -sf "$PWD/scripts/s3_sync.sh"        /etc/cron.hourly/s3_sync
sudo ln -sf "$PWD/scripts/pipeline_health.sh" /etc/cron.hourly/zz_pipeline_health
ls -l /etc/cron.hourly/            # both should be symlinks -> ~/app/trading/scripts/...
```

> **Why symlinks, not `cp` (2026-06 follow-up).** The crons used to be one-time
> copies. A later `git pull` then left `/etc/cron.hourly/zz_pipeline_health`
> running a **stale** script from before the CloudWatch heartbeat existed: it
> ran green every hour but never emitted `PipelineHealthy`, so the alarm sat
> permanently in `ALARM` on missing data and could never page on a real outage.
> Symlinks make the installed cron *be* the repo file. If you ever `cp` again,
> re-run a deploy or the `ln -sf` above. Confirm the heartbeat is actually
> flowing (not just that the cron is green):
>
> ```bash
> grep -c emit_metric /etc/cron.hourly/zz_pipeline_health      # must be > 0
> aws cloudwatch get-metric-statistics --namespace Trading/Pipeline \
>   --metric-name PipelineHealthy --dimensions Name=Host,Value=$(hostname) \
>   --start-time "$(date -u -d '6 hours ago' +%FT%TZ)" \
>   --end-time "$(date -u +%FT%TZ)" --period 3600 --statistics Minimum
> # Expect ~1 datapoint per hour, all = 1.0
> ```

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

> Code is baked into the Docker image (the `data` service mounts only `./data`
> and `./logs`, not the source). A bare `docker restart` re-runs the **old**
> image — you must `build` then `up -d` to recreate the container. (The systemd
> path in `scripts/collector@.service` runs source directly from `/app`, so there
> a `git pull` + `systemctl restart collector@<SYMBOL>` is enough.)

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

**Then run the Data pipeline health checks below.** A deploy is not complete
until they pass — "containers are up" is not "the pipeline works."

### Data pipeline health checks (MANDATORY after every deploy and rotation)

These assert the pipeline is producing **recent** data and is **advancing** —
the checks that would have caught the 2026-06 freeze on day one. Run all four.

```bash
cd ~/app/trading
TODAY=$(date -u +%Y-%m-%d); YDAY=$(date -u -d 'yesterday' +%Y-%m-%d)

# 1. Latest Parquet day is current (today or yesterday), per dataset.
for ds in trades depth_snapshots depth_updates; do
  echo "== $ds =="
  aws s3 ls "s3://trading-data-centheos/ticks-parquet/binance/BTCUSDT/$ds/" | tail -3
done
# FAIL if the newest file is older than $YDAY → flush or sync is stalled.

# 2. The local mirror's newest day is current (the file the flush thread writes
#    before s3_sync uploads it). Independent of S3.
docker compose exec data python3 -c "
from tick_parquet_store import TickParquetStore
s = TickParquetStore(root='/app/data/ticks')
for ds in ('trades','depth_snapshots','depth_updates'):
    print(ds, s.latest_parquet_date('binance','BTCUSDT',ds))
"
# FAIL if any dataset's newest day is older than $YDAY.

# 3. The mirror flush thread is actually writing (not a silent no-op).
docker compose logs data --since 5m | grep -E 'Parquet mirror flush' | tail
# Expect periodic "Parquet mirror flush: BTCUSDT.trades=... " lines, every
# --parquet-flush-interval seconds (default 60).

# 4. The hourly S3 sync cron is alive and succeeding.
tail -n 20 ~/app/trading/logs/s3_sync.log
# Expect a recent "[OK] ticks-parquet/ tree synced" line within the last hour.
ls -l /etc/cron.hourly/s3_sync
```

If any check fails, see "Recover a stalled / gapped Parquet mirror" in
Troubleshooting before assuming the deploy succeeded.

### Automated staleness alarm (runs hourly — no manual action needed)

`scripts/pipeline_health.sh` automates checks #1–#4 and runs **every hour**
(installed by `setup_ec2.sh` at `/etc/cron.hourly/zz_pipeline_health`, after
`s3_sync`). It runs the in-container check
(`collect_ticks.py --health-check`), confirms the S3 sync cron is alive, and on
any problem logs at ERROR, optionally fires an alert, and exits non-zero (so
cron mail surfaces it). This is the standing guardrail against the freeze — you
should never again learn about a stalled mirror two weeks late.

Install / refresh it on a running instance after a code pull:

```bash
cd ~/app/trading
sudo cp scripts/pipeline_health.sh /etc/cron.hourly/zz_pipeline_health
sudo chmod +x /etc/cron.hourly/zz_pipeline_health

# Run it once now to confirm it works; tail the result.
sudo bash /etc/cron.hourly/zz_pipeline_health; echo "exit=$?"
tail -n 30 ~/app/trading/logs/pipeline_health.log
```

Optional alerting (set in `.env`; otherwise failures surface via cron mail and
`logs/pipeline_health.log` only):

| `.env` variable | Effect |
|---|---|
| `ALERT_WEBHOOK_URL` | POST `{"text": "..."}` to a Slack/Discord-style webhook on failure |
| `ALERT_SNS_TOPIC_ARN` | `aws sns publish` on failure (add `sns:Publish` to `TradingCollectorRole`) |
| `CLOUDWATCH_NAMESPACE` | Emit an hourly `PipelineHealthy` 1/0 metric for a CloudWatch alarm (needs `cloudwatch:PutMetricData`) |
| `HEALTH_MAX_STALENESS_HOURS` | Grace after 00:00 UTC before yesterday's day-file is stale (default 2) |
| `S3_SYNC_MAX_AGE_MIN` | Max age of a successful `s3_sync` run before its cron is flagged dead (default 120) |

> The alarm runs the check inside the collector container, so "container not
> running" is itself reported as a CRITICAL issue — the most important signal of
> all. A transient HDF5 open contention could in rare cases cause a one-off
> false alarm; a real freeze stays failing every hour.

**CloudWatch paging (recommended — survives a fully dead box).** Webhook/SNS
alerts above only fire while the box is alive to run the cron. To page even when
the instance is down, set `CLOUDWATCH_NAMESPACE` in `.env` (the health cron then
emits `PipelineHealthy=1/0` hourly) and create the alarm once:

```bash
ALERT_EMAIL=you@example.com ./scripts/create_cloudwatch_alarm.sh
# Then confirm the SNS subscription email. The alarm pages when PipelineHealthy
# < 1 for 2h OR the metric goes MISSING (treat-missing-data=breaching) — i.e. a
# dead box pages too. This is the real backstop for the 10-day silent freeze.
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

If a file is corrupt it no longer matters for durability: the Parquet mirror in
S3 (`s3://${S3_BUCKET}/ticks-parquet/binance/{SYMBOL}/`) is written straight off
the live feed (see below), so it holds everything except at most the last
`PARQUET_FLUSH_INTERVAL` seconds (default 60 s) of in-memory buffer. Stop the
symbol, delete the corrupt HDF5, and restart — fresh HDF5 collection resumes and
the mirror never paused:

```bash
docker compose stop data
rm ~/app/trading/data/ticks/BTCUSDT_ticks.h5    # or ETHUSDT_ticks.h5
docker compose up -d data
docker compose logs -f data
```

### Tick data durability architecture (decoupled mirror)

> **Read this before touching the tick pipeline.** It encodes the fix for two
> separate 2026-06 freezes.

The durable store is the **per-day Parquet mirror**, written **directly from the
live WebSocket feed** by `tick_parquet_store.LiveParquetMirror`:

```
Binance WS ─► data_service.TickDataCollector
                 ├─► C++ OrderFlowEngine ─► HDF5  (secondary, rotated, disposable)
                 └─► LiveParquetMirror   ─► per-day Parquet ─► s3_sync ─► S3 (DURABLE)
```

- The collector tees every trade/depth message into an in-memory buffer
  (`add_trade` / `add_depth`). A flush thread moves the buffer into a per-day
  **chunked-pyarrow accumulator** and atomically (temp-file + `os.replace`)
  overwrites that day's single Parquet file every `PARQUET_FLUSH_INTERVAL`
  seconds and on clean shutdown.
- **Bounded write cost (the second 2026-06 hardening).** The writer never
  re-reads/dedups/sorts the day file per flush (that RMW peaked at ~2.6 GB RSS
  for a 6 M-row day). Appending is `pa.concat_tables` (adds a chunk, no copy);
  the reader (`TickParquetStore.read`) sorts + de-duplicates. Measured on a full
  simulated busy day (6 M depth rows/symbol): worst flush 257 ms, **~0.35 GB
  RSS/symbol**. Past-day accumulators are evicted once the UTC day rolls over.
- **Crash-safe.** A hard crash loses ≤ `PARQUET_FLUSH_INTERVAL` s of ticks; on
  restart the day's accumulator is reloaded once from disk so an overwrite never
  truncates a day that already has rows; a failed write keeps rows in memory and
  retries next flush (never a silent drop); atomic rename means no torn files.
- **HDF5 is no longer on the durability path.** It is still written by the C++
  engine (order-flow analysis needs a store) but it can corrupt, rotate, or be
  deleted with zero effect on the Parquet mirror.
- This removes the entire failure class behind the 10-day freeze: there is no
  longer a *separate reader* tailing a single growing corruption-prone file, no
  watermark to strand, and no silent "advance-without-write" path.

Why the old design failed (do not reintroduce it): durability used to flow
`HDF5 ─► flush_from_h5 (separate reader) ─► Parquet`. A corrupt HDF5 made the
reader return empty rows, the watermark advanced past them, and the mirror froze
while everything looked healthy. `flush_from_h5` / `--backfill-parquet` still
exist **for manual recovery from archived HDF5 files only** — never on the hot path.

### HDF5 file grew past the rotation limit

The collector auto-rotates HDF5 when it exceeds `--max-h5-gb` (default 3 GB)
**or** when free disk drops below `MIN_FREE_DISK_GB` (default 2 GB): it archives
the old file to `s3://${S3_BUCKET}/ticks/archive/…`, deletes the local copy, and
exits with code 75. Docker `restart: unless-stopped` (and systemd
`Restart=on-failure`) start a fresh process with a new empty HDF5. Verify:

```bash
docker compose logs data --tail 50 | grep -E 'rotation|archive|Free disk'
aws s3 ls s3://trading-data-centheos/ticks/archive/binance/ | tail
```

> **Rotation is now safe for the mirror.** Because the mirror is fed off the live
> feed (not by reading HDF5), rotating or deleting the HDF5 does not pause or gap
> Parquet — the buffer keeps filling and flushing across the restart. There is no
> watermark to strand and no gap-day backfill to run. Still run the health checks
> after a deploy to confirm the mirror is advancing.

### Recover a stalled / gapped Parquet mirror

Symptoms: health check #1 shows the newest Parquet day is old; or the live mirror
flush log line is missing; or the mirror has historical gaps from a past freeze
(the 2026-06 incident).

> With the decoupled mirror there is **no watermark on the live path** — the
> mirror writes straight from the feed. A *current* stall therefore means the
> collector or its flush thread isn't running, or writes are failing. Step 2
> below uses `--backfill-parquet` only to recover **historical gap days** from
> archived HDF5 — it is not part of the live durability path.

1. **Confirm the collector and its flush thread are alive.** A live stall is now
   a process problem, not a watermark problem:

```bash
docker compose ps data                                   # Up + healthy?
docker compose logs data --since 5m | grep -E 'Parquet mirror flush|write failed'
# Expect periodic "Parquet mirror flush: ..." lines. "write failed" → disk/perms.
```
   If the container is down/looping, fix the cause and `docker compose up -d data`;
   the buffer resumes flushing on its own. No re-flush command is needed.

2. **Backfill historical gap days from the rotated archives.** Days lost to a
   past freeze may survive in the HDF5 archives at `ticks/archive/`. Download
   each and flush into an *isolated* root (so the live mirror is untouched),
   then sync to S3:

```bash
aws s3 ls s3://trading-data-centheos/ticks/archive/binance/         # list archives
aws s3 cp s3://trading-data-centheos/ticks/archive/binance/<file>.h5 /tmp/arch.h5
docker compose exec data python collect_ticks.py --backfill-parquet \
    --symbol BTCUSDT --h5-path /tmp/arch.h5 --parquet-root /tmp/backfill
aws s3 sync /tmp/backfill/binance/BTCUSDT \
    s3://trading-data-centheos/ticks-parquet/binance/BTCUSDT
```

> A mid-write HDF5 *snapshot* may have an unreadable chunk index for some
> datasets; `flush_from_h5` skips unreadable ranges and logs them at ERROR
> rather than failing. The archived/closed files are self-consistent and recover
> fully. Re-running any backfill is safe — daily Parquet writes de-duplicate.

### EC2 disk usage budget

With the rotation + Parquet-cleanup design, expected steady-state local
disk usage per symbol:

| Data | Local retention | Permanent home |
|------|----------------|----------------|
| HDF5 live buffer | ≤ 3 GB per symbol (auto-rotated) | S3 archive |
| Parquet (recent) | 7 days per symbol | S3 (`ticks-parquet/`) |
| Parquet (older)  | Deleted by `s3_sync.sh` after S3 sync | S3 |

For 2 symbols (BTCUSDT + ETHUSDT) peak local usage is ~6 GB HDF5 +
~1–2 GB Parquet. On a 30 GB root that leaves comfortable headroom, and the
`MIN_FREE_DISK_GB` floor guarantees the box can never wedge itself full
(the 2026-06 96%-disk incident).

### HDF5 rotation cadence (`MAX_H5_GB`, `MIN_FREE_DISK_GB`)

Rotation keeps local disk bounded. Since the rearchitecture HDF5 is a
*secondary, disposable* artifact (Parquet is the durable store), so the bias is
toward **smaller, more frequent** rotation: smaller files corrupt less and free
disk faster.

- **Two independent triggers** (`collect_ticks._rotation_reason`): the size cap
  `MAX_H5_GB`, or the free-disk floor `MIN_FREE_DISK_GB`. Either one archives +
  deletes the live HDF5 and restarts clean. The floor is the hard guard against
  a full disk regardless of how the size cap is set.
- **One knob per deploy path.** `MAX_H5_GB` and `MIN_FREE_DISK_GB` in `.env`
  drive both `docker compose` (`--max-h5-gb ${MAX_H5_GB:-3}`, `MIN_FREE_DISK_GB`
  env) and the direct/systemd invocation (`collect_ticks.py` defaults them from
  `$MAX_H5_GB` / `$MIN_FREE_DISK_GB`). Don't hardcode them in the systemd unit.
- **Sizing rule.** Keep `n_symbols × MAX_H5_GB × 1.3` below free space (the ×1.3
  covers Parquet + the snapshot/archive temp copy made during rotation). The
  collector logs this budget at startup — check `docker compose logs data | grep
  "Disk budget"` after a deploy.
- **Defaults: `MAX_H5_GB=3`, `MIN_FREE_DISK_GB=2`** on a 30 GB root with 2
  symbols. Raise `MAX_H5_GB` only after growing the EBS volume. Because the
  mirror no longer depends on HDF5, frequent rotation has **no** gap-day cost.
