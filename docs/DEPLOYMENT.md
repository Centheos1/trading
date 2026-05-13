# Deployment Guide

The app ships as a Docker Compose project. `docker compose up` is the
single entry point for every environment.

```
docker compose up -d                           # EC2: start BTCUSDT collector
docker compose --profile multi up -d           # EC2: add ETHUSDT collector
docker compose --profile ui up app             # Local: start trading UI
docker compose --profile test run --rm test    # CI: run full test suite
```

---

## Quick-start: Ship the collector to EC2 in 10 minutes

```
1. Launch EC2 t3.small (Ubuntu 24.04, IAM role with S3 write)
2. SSH in and clone the repo
3. bash scripts/setup_ec2.sh
4. nano .env  →  set S3_BUCKET=your-bucket
5. docker compose up -d
6. docker compose logs -f collector
```

Data starts flowing immediately. S3 sync runs hourly via cron.

---

## Part 1 — EC2 Tick Data Collector

### 1.1 EC2 Instance Configuration

| Parameter | Value | Notes |
|---|---|---|
| Instance type | `t3.small` | 2 vCPU, 2 GB RAM — I/O-bound, not compute-bound |
| AMI | Ubuntu 24.04 LTS | `ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*` |
| Root volume | EBS `gp3`, 30 GB | HDF5 grows ~200–400 MB/day at 2 symbols × 2 streams |
| Security group | Outbound 443 only | No inbound rules needed |
| IAM instance profile | `TradingCollectorRole` | See §1.2 |

### 1.2 IAM Instance Profile

Create a role `TradingCollectorRole` with an inline policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::YOUR-BUCKET-NAME",
        "arn:aws:s3:::YOUR-BUCKET-NAME/ticks/*"
      ]
    }
  ]
}
```

Attach this role to the EC2 instance. **No access keys in `.env`** —
the instance profile provides credentials automatically.

### 1.3 S3 Bucket Setup

```bash
aws s3 mb s3://YOUR-BUCKET-NAME --region ap-southeast-2
aws s3api put-bucket-versioning \
    --bucket YOUR-BUCKET-NAME \
    --versioning-configuration Status=Enabled
```

### 1.4 First-Time EC2 Setup

```bash
# Clone the repo
git clone https://github.com/YOUR_ORG/YOUR_REPO.git /app
cd /app

# Bootstrap: installs Docker, builds image, enables auto-start on reboot
bash scripts/setup_ec2.sh

# Set your S3 bucket (the only required config)
nano .env   # set S3_BUCKET=YOUR-BUCKET-NAME

# Start the collector
docker compose up -d
```

### 1.5 Docker Compose Services

| Service | Profile | Command | Purpose |
|---|---|---|---|
| `collector` | *(default)* | `python collect_ticks.py --symbol BTCUSDT` | Primary BTC collector |
| `collector-eth` | `multi` | `python collect_ticks.py --symbol ETHUSDT` | Add ETH collection |
| `app` | `ui` | `python main.py` | Trading UI (needs display) |
| `test` | `test` | `python -m unittest discover -s tests` | Full test suite |

```bash
# Start BTCUSDT collector (default)
docker compose up -d

# Also start ETHUSDT
docker compose --profile multi up -d

# Watch logs
docker compose logs -f
docker compose logs -f collector       # single service

# Stop everything
docker compose down

# Restart after code update
git pull && docker compose build && docker compose up -d
```

### 1.6 Auto-start on EC2 Reboot

`setup_ec2.sh` installs a systemd unit `docker-compose@trading` that runs
`docker compose up -d` on boot. No manual restart needed after an EC2
stop/start or OS update:

```bash
sudo systemctl status docker-compose@trading
sudo journalctl -u docker-compose@trading -f
```

### 1.7 Verify S3 Sync

The hourly cron at `/etc/cron.hourly/s3_sync` runs automatically.
To trigger manually:

```bash
sudo bash /etc/cron.hourly/s3_sync
cat logs/s3_sync.log
aws s3 ls s3://YOUR-BUCKET-NAME/ticks/
```

### 1.8 Download Data to Dev Machine

After data has accumulated, download it for the HMM campaign:

```bash
export S3_BUCKET=YOUR-BUCKET-NAME
bash scripts/download_ticks.sh               # BTCUSDT only
bash scripts/download_ticks.sh BTCUSDT ETHUSDT  # both symbols

# Verify
python -c "import h5py; f=h5py.File('data/binance_ticks.h5'); print(list(f.keys()))"
```

### 1.9 Running the HMM Campaign (after ≥30 days of data)

```bash
source .venv/bin/activate
python tools/hmm_abtest.py \
    --symbols BTCUSDT,ETHUSDT \
    --windows 30d,60d \
    --seed 42
# Results in reports/hmm_campaign_summary_*.md
```

---

## Part 2 — Local Development

### 2.1 Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (macOS / Windows)
  or Docker Engine (Linux)
- Cursor IDE with Dev Containers extension (optional — for in-container editing)

On macOS Apple Silicon, Docker Desktop uses Rosetta 2 to run `linux/amd64`
images. The C++ engine compiled inside the container is the **same binary
that runs on EC2**.

### 2.2 Run Tests (offscreen, no display needed)

```bash
docker compose --profile test run --rm test
```

Or via the dev container in Cursor (auto-detected from `.devcontainer/`):

```bash
python -m unittest discover -s tests -v   # inside container terminal
```

### 2.3 Run the Trading UI Locally

**macOS — XQuartz required:**

```bash
# 1. Install XQuartz: https://www.xquartz.org/
# 2. XQuartz → Preferences → Security → ✅ "Allow connections from network clients"
# 3. In a terminal (not inside Docker):
xhost +localhost

# 4. Set DISPLAY in .env or export it:
export DISPLAY=host.docker.internal:0

# 5. Start the UI service
docker compose --profile ui up app
```

**Linux (including EC2 with X11 forwarding):**

```bash
# X11 socket is mounted automatically via docker-compose.yml
docker compose --profile ui up app
```

**macOS native (fastest for UI iteration):**

Run the app directly outside Docker with the local Metal backend:

```bash
source .venv/bin/activate
python main.py   # choose 'ui' mode
```

### 2.4 Open in Cursor Dev Container

1. Open the project folder in Cursor.
2. Click **"Reopen in Container"** when prompted (detected from `.devcontainer/`).
3. First open builds the image (~5–10 minutes); subsequent opens reuse cache.
4. `postCreateCommand` rebuilds the C++ engine against the mounted workspace.

### 2.5 Common Commands

```bash
# Build / rebuild image (after Dockerfile changes)
docker compose build

# Interactive shell in the dev image
docker run --rm -it -v "$(pwd)":/app \
    $(docker compose config --images | head -1) bash

# Run a single test file
docker compose --profile test run --rm test \
    python -m unittest tests.test_collect_ticks -v

# C++ unit tests
docker compose --profile test run --rm test bash -c \
    "cd backtestingCpp/orderflow/build && ./test_ripple && ./test_schemas"
```

### 2.6 CI/CD Integration

```yaml
# GitHub Actions example
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: docker compose --profile test run --rm test
        env:
          QT_QPA_PLATFORM: offscreen
```

---

## Part 3 — EC2 GPU Instance (Phase 9 — UI Deployment)

> Phase 9 (Qt Quick/QML migration) is not yet started. This section will
> be completed when Phase 9A ships.

Target: `g5.xlarge` (NVIDIA A10G), Ubuntu 24.04, NICE DCV.
A `Dockerfile.gpu` will extend `Dockerfile.dev` with the NVIDIA Container
Toolkit and `QSG_RHI_BACKEND=opengl`. The compose file will gain a
`gpu` profile:

```bash
docker compose --profile gpu up app   # future Phase 9 command
```

---

## Troubleshooting

### `docker compose build` fails: "cannot find Boost"
The Dockerfile installs `libboost-all-dev` from apt. If you're building
manually outside Docker:
```bash
sudo apt-get install libboost-all-dev
```

### `orderflow_engine` not found at runtime
```bash
# Check the build output inside the container
docker compose --profile test run --rm test \
    find backtestingCpp/orderflow/build -name 'orderflow_engine*.so'
```

### Qt platform plugin error: "xcb"
```bash
# Force offscreen for tests — already set in docker-compose.yml
export QT_QPA_PLATFORM=offscreen
```

### S3 upload fails: "Unable to locate credentials"
- Verify the EC2 instance has `TradingCollectorRole` attached under
  **Actions → Security → Modify IAM role** in the EC2 console.
- No access keys should be in `.env`.

### Container keeps restarting
```bash
docker compose logs collector --tail 50   # read the crash reason
```

### Healthcheck fails
The `collector` healthcheck checks that `data/binance_ticks.h5` was
modified within the last 2 minutes. If the file doesn't exist yet
(first run), wait 30 s for the `start_period` to pass.
