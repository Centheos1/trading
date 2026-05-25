# Bookmap Visualization Layer

Lightweight, decoupled visualization layer that streams strategy events
from the live Python / C++ execution engine to a Java add-on running
inside Bookmap. The add-on is **visualization-only** — it never sends
execution commands and has no direct dependency on the trading stack.

> **For agents writing or modifying add-on code:** read
> [`BOOKMAP_ADDON_AGENT_GUIDELINES.md`](BOOKMAP_ADDON_AGENT_GUIDELINES.md)
> first. It captures the classloader traps, jackson conflicts,
> coordinate-system gotchas, and per-instrument pip scaling rules that
> are easy to get wrong and hard to debug.

---

## Quick-start (mock data)

```bash
# Terminal 1 — start the mock event publisher
cd /path/to/backtest
python -m bookmap_publisher.mock_emitter --symbol BTCUSDT --interval 1.0
# (default --base-price is 77000; pass e.g. --base-price 3500 for ETH)

# In Bookmap — load the fat jar
#   Configure add-ons → Manage add-ons → Add →
#   bookmap-addon/build/libs/bookmap-addon-0.1.0+build.N-all.jar
#   Then open the chart for the instrument and tick "Strategy Events"
#   in its Configure add-ons dialog.
```

Within a few seconds three things happen:

1. **HUD overlay** — a semi-transparent metrics box appears in the
   top-right of the heatmap, always visible while trading. Shows
   Connection, Symbol, Regime, Position, PnL, Last Entry, Last Exit.
2. **Trade markers** — ▲ (BUY entry), ▼ (SELL entry), ◆ (exit) drawn
   on the heatmap at each event's timestamp and price.
3. **Configure dialog panel** — the same metrics also render inside
   each chart's "Configure add-ons" dialog (legacy surface; the HUD
   above is the always-visible alternative).

Both the side-panel header and the HUD show `v0.1.0+build.N (build #N)`
so you can verify the exact build Bookmap actually loaded.

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
number ends up in six places so you can always tell which build is
loaded:

| Where | Example |
|-------|---------|
| Jar filename | `bookmap-addon-0.1.0+build.42-all.jar` |
| `META-INF/MANIFEST.MF` | `Build-Number: 42`, `Build-Time: 2026-05-20T22:51:13+10:00` |
| `build-info.properties` (root of jar) | `buildNumber=42` |
| Add-on startup log (`~/bookmap-addon-debug.log`) | `Add-on starting: version=0.1.0+build.42 build#42 built=…` |
| Side-panel header (Configure add-ons dialog) | `v0.1.0+build.42 (build #42)` |
| Chart HUD overlay | `v0.1.0+build.42 (build #42)` |

The counter persists in `bookmap-addon/version.properties` (committed to
git so numbers are monotonic across machines).

**Notable builds:**

* **#12** — Metrics HUD overlay added (always-visible top-right box).
* **#13** — Per-alias pips / symbol filter — fixes off-screen markers
  when multiple instruments are attached.
* **#15** — Painter registration moved from `onInstrumentAdded` to
  `onUserMessage(UserMessageLayersChainCreatedTargeted)`. Required for
  trade markers and HUD to appear at all. Builds 7–14 silently failed
  to register painters; see Agent guidelines §1.0.

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
| `bookmap-addon/` | Gradle Java 17 module that runs inside Bookmap. Connects to `ws://localhost:8765`, parses events, draws (a) per-trade markers on the heatmap, (b) an always-visible metrics HUD on the heatmap, and (c) a metrics widget inside the per-chart Configure add-ons dialog. | `bookmap-addon/` |

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
|                    |        |  - on_metrics       |        |  JDK HttpClient WS  |
+--------------------+        |  - on_health        |        +----------+----------+
          | set_ripple_cb     +---------+----------+                    |
          v                             |                               |
+--------------------+        +--------------------+        +---------------------+
| live_runner._gate_ |  call  | ws_server (asyncio |  WS    | EventHandler        |
| and_dispatch       +------->+ websockets:8765)   +<------>+ OverlayState        |
|                    |        +--------------------+        | MetricsState        |
| status_printer     |                                      | StrategyMetricsPanel|
|                    |                                      |  (Swing, Configure  |
+--------------------+                                      |   add-ons dialog)   |
                                                            | TradeOverlayPainter |
                                                            |  (heatmap markers)  |
                                                            | MetricsOverlayPainter
                                                            |  (heatmap HUD)      |
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
java -jar build/libs/bookmap-addon-0.1.0+build.N-all.jar
```

The fat-jar's `Main-Class` is `com.trading.bookmap.StandaloneMain` —
a velox-free entry point that wires `StrategyWebSocketClient` →
`MetricsState` → `StrategyMetricsPanel` in a plain `JFrame`. The
Bookmap-coupled `BookmapStrategyAddon` class cannot be a `Main-Class`
because it `implements` `velox.api.layer1.*` interfaces that are
`compileOnly` and therefore absent from the fat-jar at runtime
(running it directly fails with `NoClassDefFoundError: velox/...`).

---

## 5a. Multi-instrument behaviour and per-alias scaling

Bookmap's `RelativeDataVerticalCoordinate` expects price as **tick
units** (`price ÷ pips`). Each instrument has its own pip size — BTC
on Binance is `0.01`, ES futures is `0.25`, etc. With multiple charts
attached simultaneously, **a single shared pips value cannot correctly
position markers on all of them**.

The add-on therefore tracks per-alias maps:

```java
private static final ConcurrentMap<String, Double> ALIAS_TO_PIPS;
private static final ConcurrentMap<String, String> ALIAS_TO_SYMBOL;
```

Populated in `Layer1ApiInstrumentAdapter.onInstrumentAdded` and looked
up by `alias` inside the painter's `createScreenSpacePainter(alias,
…)` callback. Events also carry their own `symbol`; a symbol filter
(`BookmapStrategyAddon` → `TradeOverlayPainter.symbolsMatch`) ensures
a BTC event only renders on charts whose alias maps to `BTCUSDT`.

The mock emitter's `--base-price` similarly defaults to `77000` (BTC
range). For other instruments, override it so markers land in the
visible price band:

```bash
python -m bookmap_publisher.mock_emitter --symbol ETHUSDT --base-price 3500
python -m bookmap_publisher.mock_emitter --symbol ESM6  --base-price 5800
```

## 5b. Diagnostics

The add-on writes to a stable, predictable log file at
**`~/bookmap-addon-debug.log`** (separate from Bookmap's own SLF4J
output, which lives in a platform-specific cache directory that is
hard to find). Tail it during testing:

```bash
tail -f ~/bookmap-addon-debug.log
```

Useful entries (in the order they should appear during a healthy startup):

| Line prefix | Meaning |
|---|---|
| `Add-on starting: version=…` | Class load confirmed; verify the build number matches what you intended to deploy |
| `WS client starting (generation=…)` | WS supervisor thread started |
| `WS open uri=ws://localhost:8765 gen=…` | WebSocket connected |
| `onUserMessage LayersChainCreatedTargeted targetClass=… forUs=true` | **Critical**: Bookmap signals our layers chain is ready. This is the painter-registration trigger (build #15+). |
| `registerOverlayPainter: sent ModifyScreenSpacePainter messages …` | Painter factory handed off to Bookmap |
| `TradeOverlay: createScreenSpacePainter alias=… backlog=N` | Bookmap created a chart canvas for this alias and is asking for our painter |
| `onInstrumentAdded alias=… pips=… symbol=…` | Per-alias metadata captured. May fire before or after the layers-chain message; in some Bookmap builds it does not fire at all (see Agent guidelines §1.0). |
| `TradeOverlay: queued ENTRY symbol=… price=… activeCanvases=N` | An event arrived. If `activeCanvases=0` after the layers-chain message, registration didn't take effect. |
| `TradeOverlay: drew ENTRY alias=… pips=… ticks=…` | A marker was successfully placed; `ticks` should look reasonable for the instrument |
| `WS close code=… reason='…'` | Disconnect (pair with the Python publisher log to see the cause) |

**Diagnostic decision tree** when markers are missing:

1. No `onUserMessage LayersChainCreatedTargeted` line at all? → Bookmap
   is not dispatching to this addon. Confirm the addon is ticked for
   the chart in **Settings → Configure add-ons (per-chart)**, the JAR
   is the latest build, and the add-on class still implements
   `Layer1ApiAdminAdapter`.
2. `LayersChainCreatedTargeted` lines present but `forUs=false` only?
   → Bookmap is messaging *other* addons. Wait — yours should arrive
   too. If it never does, the `@Layer1Attachable` annotation may have
   been stripped or the JAR has the wrong main class.
3. `forUs=true` present but no `registerOverlayPainter: sent …` line?
   → Either `painterRegistered` was already true (idempotent guard) or
   `api == null`. Check the line directly above for the skip reason.
4. `registerOverlayPainter: sent …` present but no
   `createScreenSpacePainter` lines? → Bookmap accepted the painter
   factory but isn't asking for canvases. The chart may not be
   subscribed to a real instrument, or the chart was opened
   before the registration arrived (try opening a fresh chart tab).
5. `createScreenSpacePainter` present but events show
   `activeCanvases=0`? → Race condition: events arrived before the
   canvas was activated. Subsequent events should show the correct
   count.
6. Events show `activeCanvases≥1` but `pips=` is wrong or markers are
   off-screen? → See §5a (per-instrument scaling).

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
