# Incident post-mortem — tick-data Parquet mirror froze for ~10 days (2026-06)

**Status:** Resolved by re-architecture on 2026-06-28 (pending redeploy authorisation).
**Severity:** High — silent, prolonged data loss on the *only* path that unblocks
Phase 16 (HMM A/B campaign) and, by extension, the live trading roadmap.
**Owner doc:** this file. Operational runbook: [`DEPLOYMENT.md`](DEPLOYMENT.md).
Engineering bar that this incident motivated: [`CODING_STANDARDS.md`](CODING_STANDARDS.md).

> Read this before touching the data pipeline **or** building the live trading
> engine. The failure modes here (silent freeze, unbounded resource growth,
> alerts that never fire, "healthy" containers serving stale/garbage data) are
> exactly the ones that are unacceptable when real money is on the line.

---

## 1. Impact

- **What broke:** The durable per-day Parquet mirror in S3
  (`s3://trading-data-centheos/ticks-parquet/...`) stopped advancing on
  **2026-06-17**. Most datasets last updated 2026-06-01; one trickled to
  2026-06-07. Discovered ~**2026-06-27/28**.
- **Data lost:** ~10+ days of continuous, analysis-ready trades + L2 depth for
  BTCUSDT and ETHUSDT. The raw HDF5 kept growing and uploading, but it was
  **corrupt** (B-tree / EOA overflow) and not reliably readable, so it is not a
  usable substitute.
- **Downstream:** The Phase 16 30-day clean-data campaign window (assumed
  2026-06-01 → 2026-07-01) is void. No usable 30-day window exists. Phase 16/17
  and the optimiser convergence work remain blocked. Data-ready date revised to
  **≈ 2026-07-29** (redeploy + 30 days).

## 2. Timeline (UTC unless noted)

| When | Event |
|---|---|
| 2026-05-13 | First EC2 collection; interrupted by HDF5 corruption, t3.small OOM, disk exhaustion from unrotated Docker logs. |
| 2026-06-01 | Migrated to t3.medium; "clean" restart declared. Mirror = separate reader tailing the live HDF5 every 15 min. |
| 2026-06-17 | Parquet mirror **froze**. HDF5 corruption advanced the flush watermark without writing; `s3_sync` kept uploading the (corrupt) HDF5 and the unchanged Parquet. Container stayed "healthy". |
| 2026-06-17 → 27 | Root disk climbed to **96 %**. OOM pressure. No alert fired (CloudWatch alarm was never created; health cron not installed on the box). |
| ~2026-06-27 | Noticed Parquet was 10 days stale via manual `aws s3 ls`. |
| 2026-06-28 | Diagnosis via AWS MCP/SSM; disk triaged (96 %→53 %); **re-architecture** designed, implemented, stress-tested. |

## 3. Root causes (there were several — all addressed)

This was not one bug. It was a fragile design plus missing guardrails.

1. **Durability coupled to a fragile single artifact (primary).**
   The "durable" mirror was a *second* process reading one ever-growing live
   HDF5 file. `libhdf5` is not safe for concurrent writer/reader without SWMR,
   and the file corrupted under sustained append load. On corruption the reader
   returned empty/garbage; the watermark advanced anyway → **the mirror froze
   while reporting success**.

2. **Silent failure.** A frozen flush produced no error the operator saw — the
   container health check only checked liveness, not data freshness.

3. **Alerting never armed.** A health script existed but the CloudWatch alarm
   was never created and the cron was not installed on the live box, so 10 days
   passed with zero pages.

4. **Disk could fill.** `MAX_H5_GB` was 8 GB with no free-disk floor; combined
   with local retention the root volume reached 96 %, causing write failures and
   OOM pressure that compounded the corruption.

5. **Unbounded per-flush work (latent, found during the fix).** The Parquet
   writer did a full read-modify-write (re-read + `drop_duplicates` + `sort`) of
   the *entire* day file on every flush. Cost grew through the day; at a 60 s
   cadence on busy depth data this is a recurring multi-hundred-MB memory spike
   — an OOM risk that would have re-introduced instability even after fixing the
   freeze. (Measured: a pandas accumulator peaked at **2.6 GB** for a 6 M-row
   day.)

## 4. The fix (2026-06-28)

**Durability is now decoupled from HDF5.** The live feed (`data_service.py`)
hands every trade/depth message to `tick_parquet_store.LiveParquetMirror`, which
is the system of record. HDF5 is a secondary, disposable artifact — it may
corrupt, rotate, or be deleted without affecting the durable mirror.

| Root cause | Permanent fix | Verified by |
|---|---|---|
| 1 — durability coupled to HDF5 | `LiveParquetMirror` writes Parquet straight from the feed; no HDF5 on the durability path | `tests/test_tick_parquet_store.py::TestLiveParquetMirror` (incl. restart-recovery, no-truncate, retry) |
| 2 — silent failure | Health check evaluates **Parquet freshness** (`latest_parquet_date`), not just liveness | `evaluate_pipeline_health` + `collect_ticks.py --health-check` tests |
| 3 — no alerts | `pipeline_health.sh` emits a `PipelineHealthy` CloudWatch metric; `create_cloudwatch_alarm.sh` pages on `0` **and on missing data** (`treat-missing-data breaching`) | Manual alarm creation step in `DEPLOYMENT.md` |
| 4 — disk fill | `MIN_FREE_DISK_GB` floor (default 2 GB) forces early HDF5 rotation; `MAX_H5_GB` 8→3 | `tests/test_collect_ticks.py::TestRotationReason` |
| 5 — unbounded per-flush work | Chunked-pyarrow per-day accumulator + atomic day-file overwrite; append-only writer, sort/dedup moved to the read boundary | Full-day stress test (below) |

### Stress-test evidence (a busy day, one symbol)

Simulated 1,440 flushes × ~4.2 k depth rows = **6,048,000 rows/day** through the
real `LiveParquetMirror`:

| Metric | pandas RMW accumulator (rejected) | chunked-pyarrow accumulator (shipped) |
|---|---|---|
| Peak RSS (collector hot path) | **2,644 MB** | **~350 MB / symbol** |
| Worst single flush | 627 ms | **257 ms** |
| Rows persisted (correctness) | — | **6,048,000 (exact)** |

Two symbols on the t3.medium (4 GB) → ~0.7 GB hot-path RSS, well within budget
alongside the C++ engine, Redis, and OHLCV collector. (A full-day `read()` for
analysis is ~1.5 GB and runs in the notebook on a dev machine, never on the box;
the collector and health check never read full days.)

### Durability / crash semantics now

- Hard crash loses **≤ 60 s** of ticks (the in-memory buffer since the last flush).
- On restart the day's accumulator is reloaded once from its file, so an
  overwrite never truncates a day that already has rows on disk.
- A failed write keeps rows in memory and retries on the next flush — no silent drop.
- Day files are written via temp-file + atomic `os.replace` — no torn files.

## 5. Prevention checklist (apply to ALL data/operational paths, incl. live trading)

1. **No single fragile point on the durability path.** The system of record must
   not depend on a separate reader tailing a concurrently-written, mutable file.
2. **Freshness is a first-class health signal.** "Process up" ≠ "data flowing".
   Alarm on output staleness, with `treat-missing-data = breaching` so a dead box
   still pages.
3. **Arm the alert before declaring done.** A health script with no alarm wired
   is not monitoring. Verify a test page end-to-end.
4. **Bound every per-event/per-flush cost.** No work that grows with accumulated
   data. Measure peak memory/CPU under a realistic *full-day / full-load* profile
   before shipping — not just a unit test with 5 rows.
5. **Guard the resources.** Free-disk floor, memory caps, rotation — and prove
   the guard triggers with a test.
6. **Verify post-deploy, not just on green CI.** After deploy, confirm the
   artifact actually advances in the destination (S3 day-file count climbing).

## 6. Implication for the live trading engine

Data collection is the *easy* case: append-only, replayable, no money at risk,
and we still lost 10 days silently. The live engine must clear a higher bar
before it routes real orders: end-to-end heartbeats with paging, bounded and
measured resource usage under peak load, no silent-failure paths, kill-switch on
staleness/disconnect, and post-deploy verification. The checklist in §5 is the
minimum carried forward into Phases 17–21 and any live-execution work.
