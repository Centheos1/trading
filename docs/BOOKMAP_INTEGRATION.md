# Bookmap Visualization Layer

Lightweight, decoupled visualization layer that streams strategy events
from the live Python / C++ execution engine to a Java add-on running
inside Bookmap. The add-on is **visualization-only** — it never sends
execution commands and has no direct dependency on the trading stack.

---

## Quick-start (mock data)

```bash
# Terminal 1 — start the mock event publisher
cd /path/to/backtest
python -m bookmap_publisher.mock_emitter --symbol BTCUSDT --interval 1.0

# In Bookmap — load the fat jar
#   Add-ons → Load from file →
#   bookmap-addon/build/libs/bookmap-addon-0.1.0+build.N-all.jar
#   Then subscribe to any instrument (e.g. BTCUSDT).
```

Within a few seconds the **Strategy Events** side panel will update live
with connection state, regime, PnL, position, and the last entry/exit.
Trade markers (▲▼ entries, ◆ exits) will appear on the heatmap chart at
the timestamp and price of each event.

The header of the side-panel shows `v0.1.0+build.N (build #N)` so you can
verify the exact build Bookmap actually loaded.

> **Reloading a new build:** Bookmap caches loaded Java classes for the
> rest of the session. Simply "removing" the add-on from the menu and
> re-loading the new jar leaves the previous classloader's daemon
> threads alive, which would normally cause two WebSocket connections.
>
> The add-on guards against this with a **JVM-wide generation token**
> (`com.trading.bookmap.ws.generation` in `System.getProperties()`).
> Each `StrategyWebSocketClient.start()` publishes a strictly-increasing
> token; older clients poll the token every 250 ms and gracefully close
> their socket + exit when they observe a newer one. The publisher will
> still see one transient "old + new" overlap (~250 ms) the first time
> you hot-reload, after which only the newest client stays connected.
>
> For a fully clean state — and to verify the build number printed in
> the startup log — **quit and restart Bookmap between version swaps**.

---

## Build numbering

`gradle fatJar` auto-increments a build counter each time it runs. The
number ends up in five places so you can always tell which build is
loaded:

| Where | Example |
|-------|---------|
| Jar filename | `bookmap-addon-0.1.0+build.42-all.jar` |
| `META-INF/MANIFEST.MF` | `Build-Number: 42`, `Build-Time: 2026-05-20T22:51:13+10:00` |
| `build-info.properties` (root of jar) | `buildNumber=42` |
| Add-on startup log | `Bookmap Strategy Add-on  version=0.1.0+build.42  build#42  built=…` |
| Side-panel header | `v0.1.0+build.42 (build #42)` |

The counter persists in `bookmap-addon/version.properties` (committed to
git so numbers are monotonic across machines).

```bash
cd bookmap-addon
gradle fatJar                       # builds; auto-bumps counter
ls build/libs/                      # confirm filename matches what you load
```

---

## 1. What each layer does

| Layer | Purpose | Lives in |
|-------|---------|----------|
| `bookmap_publisher/` | Thread-safe WebSocket publisher driven by the Python execution engine. Owns a background asyncio loop on port 8765. | `bookmap_publisher/` |
| `execution/live_runner.py` | Hosts the live execution loop. Accepts an optional `bookmap_publisher` instance; `None` (default) preserves prior behaviour exactly. | `execution/` |
| `bookmap-addon/` | Gradle Java 17 module that runs inside Bookmap. Connects to `ws://localhost:8765`, parses events, updates the side-panel and draws trade overlays on the heatmap chart. | `bookmap-addon/` |

### Why decoupled

* The Java add-on has zero dependencies on Python, the C++ engine, or
  the broker. Closing or restarting Bookmap has no effect on the trading
  engine.
* If the publisher fails to start, the engine continues without it —
  every Bookmap hook in `live_runner.py` is wrapped in a `try/except`
  that logs at `DEBUG` and never raises.
* The wire format is plain JSON; if the Python side gains a new field,
  older Java builds simply ignore it (`@JsonIgnoreProperties`).

---

## 2. Event flow

```
+--------------------+        +--------------------+        +---------------------+
| C++ OrderFlowEngine|        | BookmapPublisher    |        | StrategyWebSocket   |
|  RippleDecision    |        |  - on_intent        |        |  Client (Java)      |
|                    |        |  - on_metrics       |        |  java-websocket     |
+--------------------+        |  - on_health        |        +----------+----------+
          | set_ripple_cb     +---------+----------+                    |
          v                             |                               |
+--------------------+        +--------------------+        +---------------------+
| live_runner._gate_ |  call  | ws_server (asyncio |  WS    | EventHandler        |
| and_dispatch       +------->+ websockets:8765)   +<------>+ OverlayState        |
|                    |        +--------------------+        | MetricsState        |
| status_printer     |                                      | StrategyMetrics     |
|                    |                                      | Panel (Swing EDT)   |
+--------------------+                                      | TradeOverlayPainter |
                                                            | (heatmap chart)     |
                                                            +---------------------+
```

Three hook points inside `run_live_execute`:

1. **Intent dispatch — `_gate_and_dispatch`**. After
   `exec_mgr.on_intent(intent)` returns, the publisher receives
   `on_intent(intent, exec_mgr, symbol)`. `event_builder` maps the
   intent to an `ENTRY` or `EXIT` event.
2. **Periodic metrics — `status_printer`**. The existing 30-second
   status loop additionally calls `bookmap_publisher.on_metrics(...)`,
   which emits a `METRIC` (PnL) and a `POSITION` event drawn from
   `exec_mgr` state.
3. **Connection lifecycle**. `bookmap_publisher.on_health(...)` fires
   once when the WebSocket feed is up and once during shutdown.

Mock mode (`python -m bookmap_publisher.mock_emitter`) drives the same
WebSocket fan-out with synthetic data, so the Java add-on cannot tell
the two apart.

---

## 3. Wire schema

All events share a single schema (`bookmap_publisher/event_builder.py`
and `model/StrategyEvent.java`):

```json
{
  "type":      "ENTRY | EXIT | METRIC | POSITION | HEALTH",
  "symbol":    "BTCUSDT",
  "timestamp": 1716123456789,
  "price":     67250.5,
  "side":      "BUY | SELL | \"\"",
  "qty":       0.25,
  "label":     "human-readable summary",
  "name":      "metric name (METRIC only)",
  "value":     "metric value (METRIC only)"
}
```

Per-type usage:

| Type | Required fields |
|------|-----------------|
| `ENTRY` | `timestamp`, `price`, `side`, `label`. `qty` is `0.0` in the MVP — see §6.1. |
| `EXIT`  | `timestamp`, `price`, `side`, `qty`, `label`. |
| `METRIC`| `name`, `value`, `label` (optional). |
| `POSITION` | `side`, `qty`, `label` (or `label = "Flat"` when flat). |
| `HEALTH` | `name = "Connection"`, `value = "CONNECTED" \| "DISCONNECTED" \| "DEGRADED"`. |

---

## 4. Running the mock publisher

```bash
# Local Python (no docker)
python -m bookmap_publisher.mock_emitter --symbol BTCUSDT --interval 1.0

# Docker (uses the same image as the strategy service)
docker compose --profile bookmap up bookmap-publisher
```

The publisher logs every event it emits at `DEBUG` level; default
`INFO` is quieter. To verify the WebSocket stream independently:

```bash
pip install websockets
python -c "
import asyncio, websockets, json
async def main():
    async with websockets.connect('ws://localhost:8765') as ws:
        for _ in range(5):
            print(json.loads(await ws.recv()))
asyncio.run(main())
"
```

---

## 5. Building the Java add-on

```bash
cd bookmap-addon
gradle fatJar
# Produces:
#   build/libs/bookmap-addon-0.1.0.jar      (thin)
#   build/libs/bookmap-addon-0.1.0-all.jar  (fat jar with all dependencies)
```

Requirements: Bookmap 7.7+ installed at `/Applications/Bookmap.app`
(default on macOS). The build reads `bm-l1api.jar` from the Bookmap
app bundle as a `compileOnly` dependency. Override the path via:

```bash
gradle fatJar -PbookmapLib=/path/to/Bookmap.app/Contents/app/lib
```

Toolchain auto-provisioning is enabled via the foojay resolver —
Gradle downloads a JDK 17 automatically if one is not already installed.

To smoke-test stand-alone (opens a Swing window connected to the
publisher without launching Bookmap):

```bash
java -jar build/libs/bookmap-addon-0.1.0-all.jar
```

---

## 6. Full-implementation guide (remaining before V1 cut)

The MVP populates every field in the wire schema but a few use
placeholder values. Each item below is a self-contained unit of work
in `bookmap_publisher/event_builder.py`:

### 6.1 `entry_from_intent` qty
The `ExecutionManager` sizes orders **after** the intent is published.
Publish an `ENTRY` follow-up keyed to the broker fill (or wait for the
order callback) so the `qty` field reflects what was actually filled.

### 6.2 `exit_from_intent` PnL
Hook into `ExecutionManager._session_realized_pnl` deltas (recorded on
each `_execute_intent_exit` confirmed fill) so the EXIT event carries
realised PnL in its `label`.

### 6.3 `metric_from_exec_mgr` extended metrics
Extend the periodic hook to emit additional METRIC events sourced from
`ofe_engine.get_strategy_snapshot()` — Wave regime, Tide bias,
risk-budget percentage, consumed ES.

---

## 7. Activating live publishing from execution mode

The trading engine remains 100% backwards-compatible: the
`bookmap_publisher` parameter on `run_live_execute` defaults to
`None`. To enable Bookmap streaming, construct a publisher and thread
it through:

```python
from bookmap_publisher import make_publisher
from execution.live_runner import run_live_execute

publisher = make_publisher()   # binds ws://localhost:8765, returns None on failure
try:
    return run_live_execute(
        symbol=symbol,
        exec_mgr=exec_mgr,
        ofe_module=orderflow_engine,
        websockets_module=websockets,
        bookmap_publisher=publisher,
    )
finally:
    if publisher is not None:
        publisher.stop()
```

`make_publisher()` is the convenience constructor; pass `host=` /
`port=` / `symbol_default=` to override. A return value of `None`
means the WebSocket server could not bind — the engine should still
run, just without Bookmap.
