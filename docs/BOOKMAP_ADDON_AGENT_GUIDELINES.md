# Bookmap Add-on Development — Agent Guidelines

Operational guide for coding agents (and humans) working on the
`bookmap-addon/` Java module. Captures the non-obvious behaviours,
classloader traps, coordinate systems, and runtime gotchas that this
project has learned the hard way through multiple debug sessions.

This document is the **second** source of truth for the add-on, after
the official Bookmap API:

| Priority | Source | Use for |
|---|---|---|
| 1 | <https://bookmap.com/knowledgebase/docs/API> | Canonical API surface |
| 2 | <https://github.com/BookmapAPI/DemoStrategies> | Reference patterns |
| 3 | `javap` against `/Applications/Bookmap.app/Contents/app/lib/bm-l1api.jar` | Method signatures, field types, enum values |
| 4 | This document | Project-specific operational rules |
| 5 | `docs/BOOKMAP_INTEGRATION.md` | End-to-end pipeline architecture |

If anything in this document contradicts the official Bookmap
documentation, the official docs win — open a PR to fix this file.

---

## 1. Cardinal rules

These rules are **non-negotiable**. Violating them creates regressions
that take hours to diagnose because the failure is silent.

### 1.0 Register screen-space painters from `onUserMessage`, NOT from `onInstrumentAdded`

**The single most expensive bug in this project's history (builds 7–14):**
the trade-marker and HUD painters were only registered inside
`Layer1ApiInstrumentAdapter.onInstrumentAdded(...)`. Bookmap's normal
add-on lifecycle in this version (7.7+) does **not** invoke that
callback during chart attachment, so the painters were never registered,
`createScreenSpacePainter` was never called, and 321 events were queued
to dead canvases (`activeCanvases=0`) before anyone noticed.

**The canonical hook is `Layer1ApiAdminAdapter.onUserMessage(Object data)`.**
Bookmap dispatches a
`velox.api.layer1.messages.UserMessageLayersChainCreatedTargeted` once
the layers chain is wired up and ready to accept painter modifications.
The message has a public `targetClass` field — gate the registration on
`msg.targetClass == YourAddon.class`:

```java
@Override
public void onUserMessage(Object data) {
    if (data instanceof UserMessageLayersChainCreatedTargeted) {
        UserMessageLayersChainCreatedTargeted msg =
            (UserMessageLayersChainCreatedTargeted) data;
        if (msg.targetClass == BookmapStrategyAddon.class) {
            registerOverlayPainter();
        }
    }
}
```

Make registration **idempotent** (guard with a `painterRegistered` flag)
so you can also call it defensively from `onInstrumentAdded` —
belt-and-braces against version drift in Bookmap's lifecycle order.

**Diagnostic check:** if you see lines like
`TradeOverlay: queued ENTRY ... activeCanvases=0` in
`~/bookmap-addon-debug.log` but no
`onUserMessage LayersChainCreatedTargeted ... forUs=true` lines, your
addon is loaded but Bookmap hasn't sent the layers-chain message yet
(or your override is not being seen). Confirm the class implements
`Layer1ApiAdminAdapter` and that the `@Override` is present on
`onUserMessage`.

### 1.1 Never use Jackson at runtime

Bookmap pre-loads its own (older, ~2.12) `jackson-core` on the parent
classloader. A newer `jackson-databind` shipped in our fat-jar will
crash at first deserialisation with
`NoSuchMethodError: JsonParser.getReadCapabilities()` — the JDK runtime
treats listener exceptions as fatal and silently aborts the WebSocket.

**Use** the hand-rolled `com.trading.bookmap.model.FlatJsonParser`. The
wire format is intentionally flat to keep this trivial.

### 1.2 Never use the bundled `org.java-websocket`

Bookmap also ships its own (older) copy of `java-websocket` that sends
`Sec-WebSocket-Version: 8` (a pre-RFC draft). Modern servers reject it.

**Use** `java.net.http.WebSocket` (JDK 11+). It is loaded by the
bootstrap classloader, sidesteps the conflict entirely, and always
negotiates RFC 6455.

### 1.3 Hot reload does NOT unload the previous classloader

Bookmap reloads add-on JARs into fresh classloaders **without**
unloading the previous one. Daemon threads from the old classloader
keep running for the rest of the Bookmap session. Symptoms:

- Two WebSocket connections to the publisher
- Stale state in static fields of the old classloader
- "Old build" log lines interleaving with "new build" log lines

**Mitigation patterns this codebase already implements:**

- JVM-wide generation token (`AddonGeneration` in `System.getProperties()`)
- Older `StrategyWebSocketClient` instances poll the token every 250 ms
  and gracefully shut down when superseded.
- For verification or clean test runs: **fully quit Bookmap** (Cmd-Q,
  not just remove the add-on).

### 1.4 Never use `float`, always `double`

Project rule (see `AGENT_STRATEGY_RULES.md` §5.5) — applies to the
add-on's price coordinate maths too. `RelativeDataVerticalCoordinate`
takes a `double dataOffsetY`.

### 1.5 Never let an exception escape a JDK WebSocket listener

The JDK `WebSocket.Listener` runtime treats any uncaught throwable as
fatal: it closes the socket without sending a Close frame (server logs
see TCP RST). Wrap **every** callback body in `try { ... } catch
(Throwable t)` and log. See `StrategyWebSocketClient.InnerListener`
for the canonical pattern.

---

## 2. Build, deploy, verify workflow

### 2.1 Build

```bash
cd bookmap-addon
gradle fatJar
ls build/libs/                 # confirm bookmap-addon-0.1.0+build.N-all.jar
```

`gradle fatJar` auto-increments the build counter in
`bookmap-addon/version.properties` (committed to git so numbers stay
monotonic).

### 2.2 Deploy to Bookmap

The "remove + re-add" UI workflow leaves the previous classloader
alive (§1.3). For a clean swap:

1. **Quit Bookmap** completely (Cmd-Q on macOS).
2. Reopen Bookmap.
3. Configure add-ons → Manage add-ons → remove old "Strategy Events".
4. Add → select the new fat-jar.
5. Open the chart for the instrument you want, then Configure add-ons
   → check "Strategy Events".

### 2.3 Verify the right build is running

Six places carry the build number — they must all agree:

| Where | How to check |
|---|---|
| Jar filename | `ls bookmap-addon/build/libs/` |
| `META-INF/MANIFEST.MF` | `unzip -p file.jar META-INF/MANIFEST.MF \| grep Build` |
| `build-info.properties` (jar root) | `unzip -p file.jar build-info.properties` |
| Add-on startup log | `tail ~/bookmap-addon-debug.log` |
| Side-panel header | `v0.1.0+build.N (build #N)` |
| Chart HUD overlay | top-right of heatmap |

If any of these disagree, you're running a stale classloader.

### 2.4 Diagnostic log is the source of truth at runtime

`com.trading.bookmap.DiagnosticLog` writes to **`~/bookmap-addon-debug.log`**
— a stable, predictable path that does NOT depend on Bookmap's own
SLF4J configuration (which lives in a platform-specific cache dir
that's hard to find).

Use it liberally for any new code path you want to be able to debug
without a remote debugger:

```java
DiagnosticLog.log("MyComponent: did X with foo=" + foo + " bar=" + bar);
DiagnosticLog.log("MyComponent: Y failed", throwable);  // stack trace included
```

The log file is the single best diagnostic when "it's working in tests
but not in Bookmap".

---

## 3. Bookmap API surface (project-specific cheatsheet)

### 3.1 Required annotations on the add-on entry class

```java
@Layer1Attachable                                  // discoverability
@Layer1StrategyName("Strategy Events")             // user-visible name
@Layer1ApiVersion(Layer1ApiVersionValue.VERSION2)  // current API version
public final class BookmapStrategyAddon implements
        Layer1ApiFinishable,
        Layer1ApiAdminAdapter,
        Layer1ApiInstrumentAdapter,
        Layer1CustomPanelsGetter { ... }
```

Bookmap discovers the class by scanning the jar for `@Layer1Attachable`.
**`Main-Class` in MANIFEST.MF is irrelevant to Bookmap** — it is only
used for `java -jar` standalone invocations.

### 3.2 Lifecycle interfaces and when they fire

| Order | Interface / hook | When fired | What to do here |
|---|---|---|---|
| 1 | Constructor `(Layer1ApiProvider api)` | Once per add-on instance. Bookmap may create multiple instances. | Start shared services (WS client) reference-counted. |
| 2 | `Layer1ApiAdminAdapter.onUserMessage(Object data)` with `UserMessageLayersChainCreatedTargeted` (where `targetClass == YourAddon.class`) | Once Bookmap's layers chain is ready to accept painter modifications. | **Register screen-space painters here** via `Layer1ApiUserMessageModifyScreenSpacePainter`. See §1.0. |
| 3 | `Layer1ApiInstrumentAdapter.onInstrumentAdded(alias, info)` | When Bookmap subscribes the add-on to an instrument feed. **Note:** in Bookmap 7.7+ this often fires LATER than the layers-chain message and may not fire at all if the user only ticks the addon for the chart without re-opening it. Do NOT rely on it as the painter registration trigger. | Capture per-alias `pips` / `symbol` metadata. Defensive idempotent painter re-registration. |
| 4 | `Layer1CustomPanelsGetter.getCustomGuiFor(alias, indicator)` | Each time the user opens the per-chart Configure add-ons dialog. | Return the side-panel UI. |
| 5 | `Layer1ApiFinishable.finish()` | When Bookmap unloads the add-on. | Decrement refcount; tear down WS client when last instance releases. |

**Important:** `getCustomGuiFor` does **NOT** automatically dock a
panel into the running chart UI. It populates the per-chart Configure
add-ons dialog. To draw something always-visible while trading, use
the screen-space painter system (§3.4).

**Important:** Hook #3 (`onInstrumentAdded`) being missed is
historically the most common silent failure in this project. If you
see no `onInstrumentAdded` lines in the diagnostic log even after the
chart is open and ticking, that is **expected** — register your
painter from hook #2 instead.

### 3.3 Instrument metadata — do NOT assume single-symbol

`InstrumentInfo.pips` is per-instrument. With multiple charts attached,
you cannot store one global `sharedPips` — last-write-wins yields
wrong-scale markers on every chart but the most recently attached.

**Pattern (already implemented):** track per-alias maps:

```java
private static final ConcurrentMap<String, Double> ALIAS_TO_PIPS = new ConcurrentHashMap<>();
private static final ConcurrentMap<String, String> ALIAS_TO_SYMBOL = new ConcurrentHashMap<>();
```

Populate them in `onInstrumentAdded`. Look them up by `alias` inside
the painter's `createScreenSpacePainter(alias, ...)` callback.

### 3.4 Screen-space painter system (the "always visible" UI)

Two-step registration:

```java
// 1. Build a factory that creates one painter per chart canvas.
public final class MyPainter implements ScreenSpacePainterFactory {
    @Override
    public ScreenSpacePainterAdapter createScreenSpacePainter(
            String alias, String indicatorName,
            ScreenSpaceCanvasFactory canvasFactory) { ... }
}

// 2. Register the factory globally — but ONLY after the layers chain
//    is ready (see §1.0 and §3.2 hook #2).
@Override
public void onUserMessage(Object data) {
    if (data instanceof UserMessageLayersChainCreatedTargeted) {
        UserMessageLayersChainCreatedTargeted msg =
            (UserMessageLayersChainCreatedTargeted) data;
        if (msg.targetClass == MyAddon.class) {
            api.sendUserMessage(
                Layer1ApiUserMessageModifyScreenSpacePainter
                    .builder(MyAddon.class, "My Layer Name")
                    .setIsAdd(true)
                    .setScreenSpacePainterFactory(new MyPainter())
                    .build());
        }
    }
}
```

Bookmap then calls `createScreenSpacePainter(alias, ...)` for every
chart. **The same factory may be invoked many times — once per chart.**

**Do not** call `api.sendUserMessage(modifyPainterMsg)` from the
constructor or before receiving the layers-chain message — Bookmap
either drops the message or silently registers a painter that's never
hooked into any canvas (this was the root cause of the
build-7-through-14 silent failure).

#### Canvas types

```java
ScreenSpaceCanvasType.HEATMAP            // chart body, excluding price ladder
ScreenSpaceCanvasType.FULL_WINDOW        // entire chart window
ScreenSpaceCanvasType.RIGHT_OF_TIMELINE  // narrow strip after the rightmost data
```

Pick **`HEATMAP`** for trade overlays and HUD boxes — coordinates are
local to the data area and don't fight with the price ladder/SVP/COB.

#### Coordinate system (the most error-prone part)

Two axes, two anchor types:

| Coordinate | Class | Notes |
|---|---|---|
| `HORIZONTAL_DATA_ZERO` | `RelativeHorizontalCoordinate` | X = data baseline. `timeOffsetX` is in **nanoseconds**. |
| `HORIZONTAL_PIXEL_ZERO` | `RelativeHorizontalCoordinate` | X = left edge of the canvas in pixels. |
| `VERTICAL_DATA_ZERO`   | `RelativeDataVerticalCoordinate(base, ticks)` | Y = data baseline. `dataOffsetY` is in **tick units** (price ÷ pips). |
| `VERTICAL_PIXEL_ZERO`  | `RelativePixelVerticalCoordinate` | Y = bottom edge in pixels. **Y-axis is up** (larger Y = higher on screen). |

**Critical:** `RelativeDataVerticalCoordinate` expects price scaled by
the chart's pips. For BTC at $77,000 with `pips = 0.01`, you must pass
`ticks = 7,700,000` — NOT `77000`. Mismatched pips puts markers
~25–100× off-screen.

#### Pixel HUD anchored to a corner

Use the viewport callbacks on `ScreenSpacePainterAdapter` to track
canvas size and reposition the HUD on resize:

```java
@Override public void onHeatmapActivePixelsWidth(int w) { chartW = w; reposition(); }
@Override public void onHeatmapPixelsHeight(int h)      { chartH = h; reposition(); }
```

For "top-right corner" pinning:

```java
HorizontalCoordinate x1 = new RelativePixelHorizontalCoordinate(
    HORIZONTAL_PIXEL_ZERO, chartW - BOX_W - MARGIN);
VerticalCoordinate   y2 = new RelativePixelVerticalCoordinate(
    VERTICAL_PIXEL_ZERO,   chartH - MARGIN);
// (x2, y1) follow from BOX_W and BOX_H.
```

See `MetricsOverlayPainter` for the canonical implementation.

#### Live updates without flicker

`canvas.removeShape(old) + canvas.addShape(new)` is the supported
pattern for swapping a `CanvasIcon` to a fresh `PreparedImage`. Wrap
both calls in a single `synchronized` block so two updates can't
interleave.

### 3.5 Symbol filtering for events that fan out across charts

`addEvent(...)` queues a `MarkerSpec` on every active painter. With
two charts attached (e.g. ESM and BTCUSDT), a BTCUSDT event would
otherwise be drawn at nonsense prices on the ES chart.

Filter inside `applySpec`:

```java
String chartSymbol = lookup.symbolForAlias(alias);     // "BTCUSDT"
String eventSymbol = spec.symbol;                       // "BTCUSDT"
if (!chartSymbol.equalsIgnoreCase(eventSymbol)
        && !chartSymbol.toUpperCase().contains(eventSymbol.toUpperCase())) {
    return;
}
```

The `contains` fallback handles aliases that prefix the symbol with
extra metadata (`BTCUSDT@BN`).

---

## 4. Architectural patterns

### 4.1 Reference-counted shared state

Bookmap may instantiate the add-on class multiple times (discovery,
chart attachment, etc.). Per-instance fields would create one
WebSocket per instance — the publisher would see a flurry of
connect/disconnect cycles.

**Pattern:** keep the WS client, painter, and shared state in
`static final` fields. Reference-count the lifecycle in the
constructor (`refCount++`) and `finish()` (`refCount--`). Only the
last instance to release tears the WS client down.

See `BookmapStrategyAddon.ensureShared()` / `releaseShared()`.

### 4.2 Generation token for cross-classloader supersession

When Bookmap loads a new build, the old classloader's threads keep
running. To avoid two clients competing for the WebSocket:

- New client publishes a strictly-increasing token to
  `System.getProperties()` on `start()`.
- Old client polls the token every 250 ms; when it sees a higher
  value, it gracefully closes its socket and exits the reconnect loop.

See `AddonGeneration` and `StrategyWebSocketClient.pollGenerationAndMaybeShutDown`.

### 4.3 Fail-soft, never propagate

The add-on is **visualization-only** and must never crash Bookmap or
the trading engine. Every external boundary catches `Throwable`:

- `EventHandler.onEvent` — wraps the body in `try { ... } catch (Throwable)`
- `WebSocket.Listener.onText` — wraps Jackson/parser calls
- `ScreenSpacePainterAdapter.onHeatmap*` callbacks — guard repositioning
- `BookmapPublisher.on_intent` (Python) — `try/except` and log at debug

A misbehaving handler that lets an exception escape closes the WS
silently and the user sees "Connection: DISCONNECTED" with no clue why.

### 4.4 Standalone smoke-test mode

Every Bookmap-coupled component gets a parallel standalone code path
that runs without `velox.*` on the classpath. Run via:

```bash
java -jar bookmap-addon/build/libs/bookmap-addon-0.1.0+build.N-all.jar
```

The `Main-Class` in the manifest points to **`StandaloneMain`** (not
`BookmapStrategyAddon`, which `implements` velox interfaces and would
fail to load standalone with `NoClassDefFoundError`). `StandaloneMain`
wires `StrategyWebSocketClient` → `MetricsState` → `StrategyMetricsPanel`
in a plain `JFrame` so you can validate the WS pipeline end-to-end
without launching Bookmap.

When adding new bookmap-coupled functionality (e.g. a new painter):

- Keep the rendering logic in a class with **no** `velox.*` imports
  where possible.
- Add a Bookmap-only adapter that wraps it.
- Wire the standalone version into `StandaloneMain` for headless QA.

---

## 5. Common failure modes (and how to diagnose them)

| Symptom | Likely cause | Diagnostic |
|---|---|---|
| Side-panel shows live data but no chart markers, log shows `activeCanvases=0` and zero `createScreenSpacePainter` lines | Painter never registered — registering from `onInstrumentAdded` only (§1.0) | `grep onUserMessage ~/bookmap-addon-debug.log` — should include `LayersChainCreatedTargeted ... forUs=true` |
| Side-panel shows live data but no chart markers, log shows `createScreenSpacePainter` lines | Wrong pips → markers off-screen (§3.3) | `tail ~/bookmap-addon-debug.log \| grep TradeOverlay` — check `pips=` and `ticks=` |
| Markers on one chart but not another | Symbol filter rejecting the event | Check `chartSymbol` vs `eventSymbol` lines in diagnostic log |
| Two `Bookmap client connected` log lines after reload | Stale classloader still running (§1.3) | Quit Bookmap, restart |
| `NoClassDefFoundError: velox/...` running `java -jar` | `Main-Class` points to a class implementing velox interfaces | Confirm `Main-Class: com.trading.bookmap.StandaloneMain` in manifest |
| `NoSuchMethodError: JsonParser.getReadCapabilities()` | jackson-databind in fat-jar conflicts with Bookmap's jackson-core | Stick with `FlatJsonParser`; do NOT add jackson-databind |
| WebSocket disconnects after ~1s | Listener threw an exception — JDK runtime closed the socket | All listener bodies must `try { ... } catch (Throwable)` |
| Markers appear, then vanish silently | Chart scrolled past the event timestamp | Pure rendering behaviour — markers are pinned to event time |
| HUD invisible / clipped | `chartW`/`chartH` not received before first draw | Add a sane default (e.g. 800×400) so HUD renders before first viewport callback |
| Add-on shows in dialog but never on chart | Add-on not attached to that instrument's subscription | Right-click instrument tab → Configure add-ons → check "Strategy Events" |
| Mock emitter price doesn't match instrument | Default `--base-price` is BTC-aligned; ETH/ES need override | `python -m bookmap_publisher.mock_emitter --base-price 3500` (ETH) or `5800` (ES) |

---

## 6. Definition of Done for an add-on change

A change is **done** when ALL of the following are true:

| Criterion | Check |
|---|---|
| Compiles | `gradle fatJar` succeeds |
| Lints clean | No new warnings in modified files |
| Standalone smoke test works | `java -jar build/libs/...-all.jar` opens the panel and connects to mock emitter |
| Bookmap deploy works | Side-panel + HUD show the new build number |
| Diagnostic log shows expected events | `~/bookmap-addon-debug.log` has the new code paths firing |
| Multi-chart safe | If pips/symbol-aware: tested on ≥2 instruments simultaneously |
| Docs updated | `BOOKMAP_INTEGRATION.md` if the user-facing pipeline changed; this file if a new gotcha was discovered |
| Build counter advanced | New `build.N-all.jar` filename committed to the deploy notes |

---

## 7. When extending the UI

Choose the right surface for the data:

| Data | Right surface | Why |
|---|---|---|
| Per-trade marker (entry/exit/fill) | `ScreenSpacePainterFactory` on HEATMAP, data coordinates | Pins marker to event time + price; survives chart scroll |
| Always-visible aggregate state (PnL, regime, position) | `ScreenSpacePainterFactory` on HEATMAP, pixel coordinates pinned to a corner | No dialog interaction needed; resilient across Bookmap tiers |
| Configurable parameters (URL, symbol filter, refresh rate) | `Layer1CustomPanelsGetter.getCustomGuiFor` | Bookmap's standard Configure add-ons UX |
| One-shot status read on demand | Same Configure dialog panel | Acceptable for dev/debug surfaces |

**Anti-pattern:** packaging "always visible" data inside
`getCustomGuiFor` and asking users to leave a dialog open. Some
Bookmap tiers don't dock that panel into the chart at all — it only
renders inside the Configure add-ons window. Use a screen-space
painter for anything that must be visible while trading.

---

## 8. Frequently-needed `javap` recipes

When the Bookmap docs are vague, inspect the API jar directly:

```bash
JAVA_HOME=/Library/Java/JavaVirtualMachines/temurin-21.jdk/Contents/Home
JAR=/Applications/Bookmap.app/Contents/app/lib/bm-l1api.jar

# List all screen-space classes
$JAVA_HOME/bin/jar tf $JAR | grep ScreenSpace

# Inspect a specific class
$JAVA_HOME/bin/javap -classpath $JAR \
    'velox.api.layer1.layers.strategies.interfaces.ScreenSpaceCanvas$CanvasIcon'

# Inspect InstrumentInfo
$JAVA_HOME/bin/javap -classpath $JAR \
    'velox.api.layer1.data.InstrumentInfo'
```

Public field types and constructor signatures revealed by `javap` are
authoritative — the published Bookmap docs are not always up to date.

---

## 9. Pointers to in-tree examples

| Pattern | File |
|---|---|
| Add-on entry point | `bookmap-addon/src/main/java/com/trading/bookmap/BookmapStrategyAddon.java` |
| Standalone smoke-test entry | `bookmap-addon/src/main/java/com/trading/bookmap/StandaloneMain.java` |
| Trade-marker painter (data coordinates) | `bookmap-addon/src/main/java/com/trading/bookmap/render/TradeOverlayPainter.java` |
| HUD overlay painter (pixel coordinates) | `bookmap-addon/src/main/java/com/trading/bookmap/render/MetricsOverlayPainter.java` |
| Hardened JDK WebSocket client | `bookmap-addon/src/main/java/com/trading/bookmap/ipc/StrategyWebSocketClient.java` |
| Cross-classloader generation token | `bookmap-addon/src/main/java/com/trading/bookmap/AddonGeneration.java` |
| Hand-rolled JSON parser | `bookmap-addon/src/main/java/com/trading/bookmap/model/FlatJsonParser.java` |
| Diagnostic log | `bookmap-addon/src/main/java/com/trading/bookmap/DiagnosticLog.java` |
| Build-info accessor | `bookmap-addon/src/main/java/com/trading/bookmap/BuildInfo.java` |
| Python publisher | `bookmap_publisher/publisher.py` |
| Mock emitter | `bookmap_publisher/mock_emitter.py` |

When you add a new pattern that future agents will need, add it to
this table.
