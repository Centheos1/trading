package com.trading.bookmap;

import com.trading.bookmap.config.AddonConfig;
import com.trading.bookmap.ipc.EventHandler;
import com.trading.bookmap.ipc.StrategyWebSocketClient;
import com.trading.bookmap.model.EventType;
import com.trading.bookmap.model.StrategyEvent;
import com.trading.bookmap.render.MetricsOverlayPainter;
import com.trading.bookmap.render.TradeOverlayPainter;
import com.trading.bookmap.render.TradeOverlayRenderer;
import com.trading.bookmap.state.MetricsSnapshot;
import com.trading.bookmap.state.MetricsState;
import com.trading.bookmap.state.OverlayState;
import com.trading.bookmap.ui.StrategyMetricsPanel;

import velox.api.layer1.Layer1ApiAdminAdapter;
import velox.api.layer1.Layer1ApiFinishable;
import velox.api.layer1.Layer1ApiInstrumentAdapter;
import velox.api.layer1.Layer1ApiInstrumentListenable;
import velox.api.layer1.Layer1ApiInstrumentListener;
import velox.api.layer1.Layer1ApiProvider;
import velox.api.layer1.Layer1CustomPanelsGetter;
import velox.api.layer1.annotations.Layer1ApiVersion;
import velox.api.layer1.annotations.Layer1ApiVersionValue;
import velox.api.layer1.annotations.Layer1Attachable;
import velox.api.layer1.annotations.Layer1StrategyName;
import velox.api.layer1.data.InstrumentInfo;
import velox.api.layer1.messages.UserMessageLayersChainCreatedTargeted;
import velox.api.layer1.messages.indicators.Layer1ApiUserMessageModifyScreenSpacePainter;
import velox.gui.StrategyPanel;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import javax.swing.JFrame;
import javax.swing.SwingUtilities;
import java.awt.Graphics2D;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentMap;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Bookmap add-on entry point — visualises strategy events streamed
 * from the local Python publisher over ws://localhost:8765.
 *
 * <p>Bookmap discovers this class via the {@link Layer1Attachable}
 * annotation, constructs it by injecting a {@link Layer1ApiProvider},
 * and eventually calls {@link #finish()} when the add-on is disabled.
 *
 * <h3>Lifecycle hook order (CRITICAL — see commit history of build #15)</h3>
 * <ol>
 *   <li>Constructor — start WS client, build per-instance UI.</li>
 *   <li>{@link #onUserMessage(Object)} with
 *       {@link UserMessageLayersChainCreatedTargeted} (targeting this
 *       class) — Bookmap signals that the layers chain is wired up and
 *       ready to accept screen-space painters.
 *       <b>This is when we register the chart overlay painters.</b></li>
 *   <li>{@link #onInstrumentAdded(String, InstrumentInfo)} — captures
 *       per-alias pips/symbol metadata for the trade-marker painter.
 *       Painter registration also runs here defensively in case
 *       Bookmap fires this hook first.</li>
 *   <li>{@link #getCustomGuiFor(String, String)} — Bookmap requests
 *       Configure-add-ons dialog content.</li>
 *   <li>{@link #finish()} — release shared resources.</li>
 * </ol>
 *
 * <p>Earlier builds (7–14) registered painters only from
 * {@code onInstrumentAdded}, which was never invoked in practice
 * (every diagnostic log shows {@code activeCanvases=0}). Build #15
 * adds the {@code onUserMessage} hook that Bookmap actually drives.
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
 *   <li><b>Configure-add-ons panel</b> — a "Strategy Events" metrics
 *       widget rendered inside Bookmap's per-chart Configure dialog,
 *       driven by the shared {@link MetricsState}. Visibility of this
 *       surface is version/license-dependent in Bookmap; the HUD below
 *       is the always-visible alternative.</li>
 *   <li><b>Trade-marker overlay</b> — ENTRY (▲▼) and EXIT (◆) markers
 *       drawn on the heatmap via {@link TradeOverlayPainter}. Uses
 *       per-alias pips ({@link #ALIAS_TO_PIPS}) so multiple instruments
 *       with different tick sizes (e.g. BTC 0.01, ES 0.25) render
 *       correctly when attached simultaneously.</li>
 *   <li><b>Metrics HUD overlay</b> — semi-transparent metrics box
 *       pinned to the top-right of the heatmap via
 *       {@link MetricsOverlayPainter}. Always visible while trading,
 *       independent of dialog state.</li>
 * </ol>
 *
 * <p>For local smoke-testing without launching Bookmap, run:
 * <pre>
 *   java -jar build/libs/bookmap-addon-0.1.0+build.N-all.jar
 * </pre>
 * The fat-jar's {@code Main-Class} is {@link StandaloneMain} (a
 * velox-free entry point); this class cannot be {@code Main-Class}
 * because it implements {@code velox.api.layer1.*} interfaces that
 * are {@code compileOnly} and therefore absent from the fat-jar at
 * runtime.
 *
 * <p>See {@code docs/BOOKMAP_ADDON_AGENT_GUIDELINES.md} for the
 * operational rules (classloader hot-reload, jackson conflicts,
 * coordinate systems, per-alias pip scaling) that constrain how this
 * class can be safely modified.
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

    /** Per-alias pip size (e.g. BTCUSDT@BN → 0.01, ESM6.CME → 0.25). */
    private static final ConcurrentMap<String, Double> ALIAS_TO_PIPS = new ConcurrentHashMap<>();
    /** Per-alias symbol tag (e.g. BTCUSDT@BN → BTCUSDT). */
    private static final ConcurrentMap<String, String> ALIAS_TO_SYMBOL = new ConcurrentHashMap<>();

    private static final TradeOverlayPainter SHARED_PAINTER = new TradeOverlayPainter(
        new TradeOverlayPainter.InstrumentInfoLookup() {
            @Override
            public double pipsForAlias(String alias) {
                Double p = ALIAS_TO_PIPS.get(alias);
                return p != null ? p : 0.0;
            }

            @Override
            public String symbolForAlias(String alias) {
                String s = ALIAS_TO_SYMBOL.get(alias);
                return s != null ? s : "";
            }
        }
    );
    private static final MetricsOverlayPainter SHARED_METRICS_OVERLAY = new MetricsOverlayPainter();
    private static final Set<StrategyMetricsPanel> ACTIVE_PANELS =
        ConcurrentHashMap.newKeySet();
    private static StrategyWebSocketClient sharedWs;
    private static int refCount = 0;

    // -------------------------------------------------- per-instance
    private final int instanceId;
    private final Layer1ApiProvider api;
    private boolean painterRegistered = false;
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
        // Register as instrument listener so onInstrumentAdded fires, populating
        // ALIAS_TO_PIPS with the instrument's tick size (required for correct Y
        // coordinate placement in TradeOverlayPainter).
        if (api != null) {
            try {
                ((Layer1ApiInstrumentListenable) api)
                    .addListener((Layer1ApiInstrumentListener) this);
            } catch (Exception ex) {
                DiagnosticLog.log("instrument addListener failed instance=#" + instanceId, ex);
            }
        }
        // Attempt painter registration immediately from constructor.
        // onUserMessage(UserMessageLayersChainCreatedTargeted) is the documented
        // hook but has never fired in practice — this covers that gap.
        registerOverlayPainter();
    }

    // --------------------------------------------------------- Bookmap hooks

    /**
     * Canonical Bookmap entry point for registering screen-space painters.
     *
     * <p>Bookmap dispatches a {@link UserMessageLayersChainCreatedTargeted}
     * to each {@link Layer1Attachable} once its layers chain is wired up
     * and ready to accept painter modifications. This — NOT
     * {@link #onInstrumentAdded(String, InstrumentInfo)} — is the
     * documented hook for sending
     * {@link Layer1ApiUserMessageModifyScreenSpacePainter}.
     *
     * <p>Historically (builds 7–14) we registered painters only from
     * {@code onInstrumentAdded}, which Bookmap never calls during the
     * normal addon lifecycle in this version. Result: 321 events were
     * queued with {@code activeCanvases=0} and no markers ever rendered.
     * See {@code docs/BOOKMAP_ADDON_AGENT_GUIDELINES.md} §3.2 for the
     * full lifecycle table.
     */
    @Override
    public void onUserMessage(Object data) {
        if (data == null) {
            return;
        }
        // Log ALL message types so we can see what Bookmap is actually sending.
        DiagnosticLog.log("onUserMessage type=" + data.getClass().getSimpleName()
            + " instance=#" + instanceId);
        if (data instanceof UserMessageLayersChainCreatedTargeted) {
            UserMessageLayersChainCreatedTargeted msg =
                (UserMessageLayersChainCreatedTargeted) data;
            boolean forUs = msg.targetClass == null || msg.targetClass == BookmapStrategyAddon.class;
            DiagnosticLog.log("onUserMessage LayersChainCreatedTargeted"
                + " targetClass=" + (msg.targetClass != null ? msg.targetClass.getName() : "null")
                + " isNew=" + msg.isNew
                + " forUs=" + forUs);
            if (forUs) {
                registerOverlayPainter();
            }
        }
    }

    @Override
    public void onInstrumentAdded(String alias, InstrumentInfo instrumentInfo) {
        if (alias != null && instrumentInfo != null) {
            if (instrumentInfo.pips > 0) {
                ALIAS_TO_PIPS.put(alias, instrumentInfo.pips);
            }
            String symbol = symbolFromAlias(alias, instrumentInfo);
            if (!symbol.isEmpty()) {
                ALIAS_TO_SYMBOL.put(alias, symbol);
            }
            DiagnosticLog.log("onInstrumentAdded alias=" + alias
                + " pips=" + instrumentInfo.pips
                + " symbol=" + symbol);
        } else {
            DiagnosticLog.log("onInstrumentAdded alias=" + alias
                + " info=" + (instrumentInfo == null ? "null" : "present"));
        }
        // Defensive: register painters here too in case the
        // LayersChainCreatedTargeted message arrived before we were
        // ready (shouldn't happen, but the registration is idempotent).
        registerOverlayPainter();
    }

    /**
     * Best-effort mapping from a Bookmap alias to a publisher-side symbol.
     * Aliases typically look like {@code BTCUSDT@BN} or
     * {@code ESM6.CME@TM.Lite} — we strip everything from {@code '@'}
     * onward and use {@link InstrumentInfo} symbol fields where available.
     */
    private static String symbolFromAlias(String alias, InstrumentInfo info) {
        if (info != null) {
            String requested = info.requestedSymbol;
            if (requested != null && !requested.isEmpty()) {
                return requested.toUpperCase();
            }
            String full = info.fullName;
            if (full != null && !full.isEmpty()) {
                int at = full.indexOf('@');
                return (at > 0 ? full.substring(0, at) : full).toUpperCase();
            }
        }
        if (alias == null) {
            return "";
        }
        int at = alias.indexOf('@');
        return (at > 0 ? alias.substring(0, at) : alias).toUpperCase();
    }

    /** Bookmap unload hook — releases this instance's hold on the shared client. */
    @Override
    public void finish() {
        LOG.info("BookmapStrategyAddon instance #{} finish()", instanceId);
        ACTIVE_PANELS.remove(this.metricsPanel);
        if (api != null) {
            try {
                ((Layer1ApiInstrumentListenable) api)
                    .removeListener((Layer1ApiInstrumentListener) this);
            } catch (Exception ex) {
                DiagnosticLog.log("instrument removeListener failed instance=#" + instanceId, ex);
            }
        }
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
        strategyPanel.setLayout(new java.awt.BorderLayout());
        strategyPanel.add(metricsPanel, java.awt.BorderLayout.NORTH);
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
     * Register the chart overlay painter with Bookmap for this instance.
     * Each Bookmap instance (addToLayers:false and addToLayers:true) gets
     * its own independent registration — only the addToLayers:true instance
     * has a real canvas, so only that registration results in
     * createScreenSpacePainter being called.
     */
    private void registerOverlayPainter() {
        if (api == null) {
            DiagnosticLog.log("registerOverlayPainter skipped: api==null"
                + " (instance #" + instanceId + ")");
            return;
        }
        if (painterRegistered) {
            DiagnosticLog.log("registerOverlayPainter skipped: already registered"
                + " (instance #" + instanceId + ")");
            return;
        }
        painterRegistered = true;
        try {
            Layer1ApiUserMessageModifyScreenSpacePainter tradeMsg =
                Layer1ApiUserMessageModifyScreenSpacePainter
                    .builder(BookmapStrategyAddon.class, "Trade Overlays")
                    .setIsAdd(true)
                    .setScreenSpacePainterFactory(SHARED_PAINTER)
                    .build();
            api.sendUserMessage(tradeMsg);

            Layer1ApiUserMessageModifyScreenSpacePainter hudMsg =
                Layer1ApiUserMessageModifyScreenSpacePainter
                    .builder(BookmapStrategyAddon.class, "Strategy HUD")
                    .setIsAdd(true)
                    .setScreenSpacePainterFactory(SHARED_METRICS_OVERLAY)
                    .build();
            api.sendUserMessage(hudMsg);

            LOG.info("Bookmap addon: trade overlay + metrics HUD painters registered (instance #{})",
                instanceId);
            DiagnosticLog.log("registerOverlayPainter: sent ModifyScreenSpacePainter messages"
                + " (Trade Overlays + Strategy HUD) instance=#" + instanceId);
        } catch (Exception ex) {
            LOG.warn("Bookmap addon: failed to register overlay painters", ex);
            DiagnosticLog.log("registerOverlayPainter: send failed instance=#" + instanceId, ex);
            painterRegistered = false;
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
                    SHARED_PAINTER.addEvent(event);
                } else if (type == EventType.EXIT) {
                    SHARED_OVERLAY.addExit(event);
                    SHARED_PAINTER.addEvent(event);
                }
                SHARED_METRICS.update(event);
                MetricsSnapshot snap = SHARED_METRICS.snapshot();
                for (StrategyMetricsPanel p : ACTIVE_PANELS) {
                    p.refresh(snap);
                }
                SHARED_METRICS_OVERLAY.updateSnapshot(snap);
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
                SHARED_METRICS_OVERLAY.updateSnapshot(snap);
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
