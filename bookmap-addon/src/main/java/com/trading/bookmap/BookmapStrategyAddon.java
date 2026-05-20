package com.trading.bookmap;

import com.trading.bookmap.config.AddonConfig;
import com.trading.bookmap.ipc.EventHandler;
import com.trading.bookmap.ipc.StrategyWebSocketClient;
import com.trading.bookmap.model.EventType;
import com.trading.bookmap.model.StrategyEvent;
import com.trading.bookmap.render.TradeOverlayPainter;
import com.trading.bookmap.render.TradeOverlayRenderer;
import com.trading.bookmap.state.MetricsSnapshot;
import com.trading.bookmap.state.MetricsState;
import com.trading.bookmap.state.OverlayState;
import com.trading.bookmap.ui.StrategyMetricsPanel;

import velox.api.layer1.Layer1ApiAdminAdapter;
import velox.api.layer1.Layer1ApiFinishable;
import velox.api.layer1.Layer1ApiInstrumentAdapter;
import velox.api.layer1.Layer1ApiProvider;
import velox.api.layer1.Layer1CustomPanelsGetter;
import velox.api.layer1.annotations.Layer1ApiVersion;
import velox.api.layer1.annotations.Layer1ApiVersionValue;
import velox.api.layer1.annotations.Layer1Attachable;
import velox.api.layer1.annotations.Layer1StrategyName;
import velox.api.layer1.data.InstrumentInfo;
import velox.api.layer1.messages.indicators.Layer1ApiUserMessageModifyScreenSpacePainter;
import velox.gui.StrategyPanel;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import javax.swing.JFrame;
import javax.swing.SwingUtilities;
import java.awt.Graphics2D;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Bookmap add-on entry point — visualises strategy events streamed
 * from the local Python publisher over ws://localhost:8765.
 *
 * <p>Bookmap discovers this class via the {@link Layer1Attachable}
 * annotation, constructs it by injecting a {@link Layer1ApiProvider},
 * and eventually calls {@link #finish()} when the add-on is disabled.
 *
 * <h3>Singleton sharing</h3>
 * Bookmap may instantiate this class multiple times (during discovery,
 * for each chart attachment, etc.). To avoid one WebSocket connection
 * per instance — which the Python publisher would see as multiple rapid
 * connect/disconnect cycles — the WS client, painter, and shared state
 * are held in <i>static</i> fields with reference-counted lifecycle.
 * Only the first instance starts the WS supervisor; only the last
 * instance to finish() tears it down.
 *
 * <h3>Visual outputs</h3>
 * <ol>
 *   <li><b>Side panel</b> — a "Strategy Events" metrics widget per
 *       attachment, all driven by the shared {@link MetricsState}.</li>
 *   <li><b>Chart overlays</b> — ENTRY (▲▼) and EXIT (◆) markers drawn
 *       on the heatmap via the shared
 *       {@link velox.api.layer1.layers.strategies.interfaces.ScreenSpacePainterFactory}.</li>
 * </ol>
 *
 * <p>For local smoke-testing without launching Bookmap, run:
 * <pre>
 *   java -jar build/libs/bookmap-addon-0.1.0+build.N-all.jar
 * </pre>
 */
@Layer1Attachable
@Layer1StrategyName("Strategy Events")
@Layer1ApiVersion(Layer1ApiVersionValue.VERSION2)
public final class BookmapStrategyAddon implements
        Layer1ApiFinishable,
        Layer1ApiAdminAdapter,
        Layer1ApiInstrumentAdapter,
        Layer1CustomPanelsGetter {

    private static final Logger LOG = LoggerFactory.getLogger(BookmapStrategyAddon.class);

    // -------------------------------------------------- shared (per JVM)
    private static final Object SHARED_LOCK = new Object();
    private static final AtomicInteger INSTANCE_SEQ = new AtomicInteger();
    private static final OverlayState SHARED_OVERLAY = new OverlayState();
    private static final MetricsState SHARED_METRICS = new MetricsState();
    private static final TradeOverlayPainter SHARED_PAINTER = new TradeOverlayPainter();
    private static final Set<StrategyMetricsPanel> ACTIVE_PANELS =
        ConcurrentHashMap.newKeySet();
    private static volatile double sharedPricePips = 1.0;
    private static StrategyWebSocketClient sharedWs;
    private static int refCount = 0;
    private static boolean painterRegistered = false;

    // -------------------------------------------------- per-instance
    private final int instanceId;
    private final Layer1ApiProvider api;
    private final AddonConfig config;
    private final StrategyMetricsPanel metricsPanel;
    private final TradeOverlayRenderer overlayRenderer;

    /**
     * Bookmap injection constructor. Each new instance increments the
     * shared reference count; only the first instance actually starts
     * the WebSocket supervisor.
     *
     * @param api Bookmap provider; {@code null} in standalone smoke-test mode.
     */
    public BookmapStrategyAddon(Layer1ApiProvider api) {
        this.instanceId = INSTANCE_SEQ.incrementAndGet();
        this.api = api;
        this.config = new AddonConfig();
        this.metricsPanel = new StrategyMetricsPanel();
        this.overlayRenderer = new TradeOverlayRenderer();
        ACTIVE_PANELS.add(this.metricsPanel);
        ensureShared();
        this.metricsPanel.refresh(SHARED_METRICS.snapshot());
    }

    // --------------------------------------------------------- Bookmap hooks

    @Override
    public void onInstrumentAdded(String alias, InstrumentInfo instrumentInfo) {
        if (instrumentInfo != null && instrumentInfo.pips > 0) {
            sharedPricePips = instrumentInfo.pips;
        }
        registerOverlayPainter();
    }

    /** Bookmap unload hook — releases this instance's hold on the shared client. */
    @Override
    public void finish() {
        LOG.info("BookmapStrategyAddon instance #{} finish()", instanceId);
        ACTIVE_PANELS.remove(this.metricsPanel);
        releaseShared();
    }

    /**
     * Returns the per-instance metrics panel as a Bookmap side panel.
     * Each panel is registered in {@link #ACTIVE_PANELS} so the shared
     * event handler can refresh them all on every update.
     */
    @Override
    public StrategyPanel[] getCustomGuiFor(String alias, String indicatorName) {
        StrategyPanel strategyPanel = new StrategyPanel("Strategy Events");
        strategyPanel.add(metricsPanel);
        return new StrategyPanel[]{strategyPanel};
    }

    /** Bookmap chart-paint fallback — delegates to {@link TradeOverlayRenderer}. */
    public void onPaint(Graphics2D g2) {
        overlayRenderer.render(g2, SHARED_OVERLAY);
    }

    /** Per-tick callback — no-op for MVP. */
    public void onTimestamp(long timestampNanos) {
        // No-op for MVP.
    }

    // ----------------------------------------------------------- accessors

    public StrategyMetricsPanel getMetricsPanel() {
        return metricsPanel;
    }

    public OverlayState getOverlayState() {
        return SHARED_OVERLAY;
    }

    public MetricsState getMetricsState() {
        return SHARED_METRICS;
    }

    public TradeOverlayRenderer getOverlayRenderer() {
        return overlayRenderer;
    }

    public boolean isLoaded() {
        synchronized (SHARED_LOCK) {
            return sharedWs != null;
        }
    }

    // ---------------------------------------------------------- internals

    /**
     * Ensure the shared WebSocket client + supervisor are running.
     * Idempotent: called once per instance; only the first call actually
     * starts the supervisor thread.
     */
    private void ensureShared() {
        synchronized (SHARED_LOCK) {
            refCount++;
            ClassLoader cl = getClass().getClassLoader();
            String clTag = cl == null ? "bootstrap"
                : cl.getClass().getSimpleName() + "@"
                + Integer.toHexString(System.identityHashCode(cl));
            if (sharedWs == null) {
                BuildInfo bi = BuildInfo.load();
                LOG.info("================================================================");
                LOG.info("  Bookmap Strategy Add-on  version={}  build#{}  built={}",
                    bi.version, bi.buildNumber, bi.buildTime);
                LOG.info("  instance #{}  classloader={}  jvmGen={}",
                    instanceId, clTag, AddonGeneration.readCurrent());
                LOG.info("  wsUrl={}  symbol={}",
                    config.getWsUrl(), config.getSymbol());
                LOG.info("  transport=java.net.http.WebSocket (shared singleton)");
                LOG.info("  diagnostic log -> {}", DiagnosticLog.path());
                LOG.info("================================================================");
                DiagnosticLog.log("Add-on starting: version=" + bi.version
                    + " build#" + bi.buildNumber + " built=" + bi.buildTime
                    + " instance=#" + instanceId + " classloader=" + clTag);
                sharedWs = new StrategyWebSocketClient(config, SHARED_HANDLER);
                sharedWs.start();
            } else {
                LOG.info(
                    "BookmapStrategyAddon instance #{} (classloader {}) "
                        + "attaching to existing WS in same classloader (refCount={})",
                    instanceId, clTag, refCount);
                DiagnosticLog.log("Instance #" + instanceId + " attached to existing"
                    + " WS (refCount=" + refCount + " classloader=" + clTag + ")");
            }
        }
    }

    /**
     * Decrement the shared reference count. When the last instance
     * releases, the WS supervisor is shut down and the chart painter
     * is cleared.
     */
    private void releaseShared() {
        synchronized (SHARED_LOCK) {
            if (refCount <= 0) {
                return;
            }
            refCount--;
            LOG.info("Shared refCount is now {}", refCount);
            if (refCount == 0 && sharedWs != null) {
                LOG.info("Last add-on instance — shutting down shared WS client");
                try {
                    sharedWs.stop();
                } catch (Exception ex) {
                    LOG.debug("Error stopping shared WS client", ex);
                }
                sharedWs = null;
                try {
                    SHARED_PAINTER.clear();
                } catch (Exception ex) {
                    LOG.debug("Error clearing shared painter", ex);
                }
                painterRegistered = false;
            }
        }
    }

    /**
     * Register the chart overlay painter with Bookmap exactly once
     * across all instances.
     */
    private void registerOverlayPainter() {
        if (api == null) {
            return;
        }
        synchronized (SHARED_LOCK) {
            if (painterRegistered) {
                return;
            }
            painterRegistered = true;
        }
        try {
            Layer1ApiUserMessageModifyScreenSpacePainter msg =
                Layer1ApiUserMessageModifyScreenSpacePainter
                    .builder(BookmapStrategyAddon.class, "Trade Overlays")
                    .setIsAdd(true)
                    .setScreenSpacePainterFactory(SHARED_PAINTER)
                    .build();
            api.sendUserMessage(msg);
            LOG.info("Bookmap addon: trade overlay painter registered (instance #{})",
                instanceId);
        } catch (Exception ex) {
            LOG.warn("Bookmap addon: failed to register overlay painter", ex);
            synchronized (SHARED_LOCK) {
                painterRegistered = false;
            }
        }
    }

    /**
     * Shared event handler — feeds the singleton state then fans out
     * to every active metrics panel.
     */
    private static final EventHandler SHARED_HANDLER = new EventHandler() {
        @Override
        public void onEvent(StrategyEvent event) {
            if (event == null) {
                return;
            }
            try {
                EventType type = event.getType();
                if (type == EventType.ENTRY) {
                    SHARED_OVERLAY.addEntry(event);
                    SHARED_PAINTER.addEvent(event, sharedPricePips);
                } else if (type == EventType.EXIT) {
                    SHARED_OVERLAY.addExit(event);
                    SHARED_PAINTER.addEvent(event, sharedPricePips);
                }
                SHARED_METRICS.update(event);
                MetricsSnapshot snap = SHARED_METRICS.snapshot();
                for (StrategyMetricsPanel p : ACTIVE_PANELS) {
                    p.refresh(snap);
                }
            } catch (Throwable t) {
                // Never let a handler error close the WebSocket — the
                // JDK runtime treats listener exceptions as fatal.
                LOG.warn("Bookmap addon: SHARED_HANDLER.onEvent threw", t);
            }
        }

        @Override
        public void onConnectionStateChanged(String state) {
            try {
                SHARED_METRICS.setConnection(state);
                MetricsSnapshot snap = SHARED_METRICS.snapshot();
                for (StrategyMetricsPanel p : ACTIVE_PANELS) {
                    p.refresh(snap);
                }
            } catch (Throwable t) {
                LOG.warn("Bookmap addon: SHARED_HANDLER.onConnectionStateChanged threw", t);
            }
        }

        @Override
        public void onTransportError(Throwable t) {
            LOG.debug("Bookmap addon: transport error", t);
        }
    };

    /**
     * Stand-alone harness — opens a Swing window and starts the WS
     * client. Useful for end-to-end connectivity testing without
     * launching Bookmap. Run the mock emitter first:
     *
     * <pre>
     *   python -m bookmap_publisher.mock_emitter --symbol BTCUSDT
     * </pre>
     */
    public static void main(String[] args) {
        BookmapStrategyAddon addon = new BookmapStrategyAddon(null);
        SwingUtilities.invokeLater(() -> {
            JFrame frame = new JFrame("Bookmap Strategy Add-on (smoke test)");
            frame.setDefaultCloseOperation(JFrame.EXIT_ON_CLOSE);
            frame.setContentPane(addon.getMetricsPanel());
            frame.pack();
            frame.setLocationRelativeTo(null);
            frame.setVisible(true);
        });
        Runtime.getRuntime().addShutdownHook(new Thread(addon::finish, "bookmap-shutdown"));
    }
}
