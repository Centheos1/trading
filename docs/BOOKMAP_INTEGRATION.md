# Bookmap Visualization Layer

Lightweight, decoupled visualization layer that streams strategy events
from the live Python / C++ execution engine to a Java add-on running
inside Bookmap. The add-on is **visualization-only** — it never sends
execution commands and has no direct dependency on the trading stack.

The MVP delivers the proof-of-concept end-to-end connection. Real
trade markers are rendered behind a placeholder coordinate mapper; the
metrics panel and IPC transport are production-quality.

---

## 1. What each layer does

| Layer | Purpose | Lives in |
|-------|---------|----------|
| `bookmap_publisher/` | Thread-safe WebSocket publisher driven by the Python execution engine. Owns a background asyncio loop on port 8765. | `bookmap_publisher/` |
| `execution/live_runner.py` | Hosts the live execution loop. Now accepts an optional `bookmap_publisher` instance; passing `None` (default) preserves prior behaviour exactly. | `execution/` |
| `bookmap-addon/` | Gradle Java 11 module that runs inside Bookmap. Connects to `ws://localhost:8765`, parses events, updates the Swing metrics panel, and draws trade overlays. | `bookmap-addon/` |

### Why decoupled

* The Java add-on has zero dependencies on Python, the C++ engine, or
  the broker. Closing or restarting Bookmap has no effect on the
  trading engine.
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
+--------------------+                                      +---------------------+
```

Three hook points inside `run_live_execute`:

1. **Intent dispatch — `_gate_and_dispatch`**. After
   `exec_mgr.on_intent(intent)` returns, the publisher receives
   `on_intent(intent, exec_mgr, symbol)`. `event_builder` maps the
   intent to an `ENTRY` or `EXIT` event. Cancel / rearm / prepare
   intents are intentionally ignored — they do not represent a
   chart-visible action.
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
| `ENTRY` | `timestamp`, `price`, `side`, `label`. `qty` is `0.0` in the MVP — see TODO in `event_builder.entry_from_intent`. |
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
`INFO` is quieter. To verify with a one-liner:

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
gradle build
# Produces:
#   build/libs/bookmap-addon-0.1.0.jar      (thin)
#   build/libs/bookmap-addon-0.1.0-all.jar  (fat jar with java-websocket + jackson)
```

Toolchain auto-provisioning is enabled via the foojay resolver —
Gradle downloads a JDK 11 the first time the build runs if one is not
already installed.

To smoke-test the add-on stand-alone (opens a Swing window connected
to the publisher):

```bash
java -jar build/libs/bookmap-addon-0.1.0-all.jar
```

To load into Bookmap proper, drop `bookmap-addon-0.1.0-all.jar` into
Bookmap's add-on directory (typically `~/BookmapAddons/` on macOS) and
restart Bookmap. The Bookmap-specific `Layer` registration step is
still TODO — see §8.

---

## 6. Activating live publishing from execution mode

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

---

## 7. Full-implementation guide (resolve before V1 cut)

The MVP populates every field in the wire schema, but some fields use
placeholder values clearly marked `# TODO: full mapping` in
`bookmap_publisher/event_builder.py`. Each TODO is the next unit of
work:

1. **`entry_from_intent` qty**. The `ExecutionManager` sizes orders
   after the intent is published. Publish an `ENTRY` follow-up keyed
   to the broker fill (or wait for the order callback) so the qty
   reflects what was actually filled.
2. **`exit_from_intent` PnL**. Hook into
   `ExecutionManager._session_realized_pnl` deltas (recorded on each
   `_execute_intent_exit` confirmed fill) so the EXIT event carries
   realised PnL in its `label`.
3. **`metric_from_exec_mgr` regime / risk budget**. Extend the
   periodic hook to also emit METRIC events sourced from
   `ofe_engine.get_strategy_snapshot()` — Wave regime, Tide bias,
   risk-budget percentage, consumed ES.
4. **`TradeOverlayRenderer.CoordinateMapper`**. Wire the placeholder
   `mapTimestampToX` / `mapPriceToY` into the real Bookmap chart
   viewport once the SDK adapter is available.

These four TODOs are deliberately additive: none of them require
re-architecting the event schema or the IPC transport.

---

## 8. Remaining Bookmap API integration points

The MVP intentionally stops short of integrating Bookmap's proprietary
SDK because that jar is not part of the public Gradle dependency tree.
The following hooks are stubbed in code with `// TODO: Bookmap canvas
API` comments:

1. **`BookmapStrategyAddon` registration**. Implement the Bookmap
   `BookmapAddon` / `Layer` interface and annotate the class with the
   SDK's `@LayerSettings`. The lifecycle methods `onLoad` /
   `onUnload` / `onTimestamp` / `onPaint` already exist on the class
   and need only to be wired through the SDK base class.
2. **`TradeOverlayRenderer.CoordinateMapper`**. Replace the default
   `(-1, -1)` mapper with one bound to the Bookmap viewport's
   `timeToScreenCoordinate(long ts)` and `priceToScreenCoordinate
   (double price)` callbacks.
3. **Side panel embedding**. Bookmap surfaces side panels via its
   `Layer` API; add a method that returns
   `BookmapStrategyAddon.getMetricsPanel()` (already public) so the
   chart picks it up automatically.

Once these three points are wired the add-on becomes a fully-fledged
Bookmap layer with zero further changes to the IPC, state, or
rendering code in the add-on tree.
