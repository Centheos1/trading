package com.trading.bookmap;

import com.trading.bookmap.config.AddonConfig;
import com.trading.bookmap.ipc.EventHandler;
import com.trading.bookmap.ipc.StrategyWebSocketClient;
import com.trading.bookmap.model.EventType;
import com.trading.bookmap.model.StrategyEvent;
import com.trading.bookmap.render.TradeOverlayRenderer;
import com.trading.bookmap.state.MetricsState;
import com.trading.bookmap.state.OverlayState;
import com.trading.bookmap.ui.StrategyMetricsPanel;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import javax.swing.JFrame;
import javax.swing.SwingUtilities;
import java.awt.Graphics2D;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Entry point for the Bookmap visualisation add-on.
 *
 * <p>The class is intentionally written so it can be loaded by the
 * Bookmap add-on framework (which discovers classes by manifest /
 * descriptor) and also run as a stand-alone Java program for local
 * smoke-testing:
 *
 * <pre>
 *   java -jar build/libs/bookmap-addon-0.1.0-all.jar
 * </pre>
 *
 * <p>Bookmap-specific {@code Layer} / {@code BookmapAddon} registration
 * is deliberately deferred — the real Bookmap SDK interface lives in
 * a proprietary jar that is not part of the public Gradle dependency
 * tree. The lifecycle methods {@link #onLoad()} and {@link #onUnload()}
 * encapsulate everything the SDK adapter will need to call once it is
 * wired up, while {@link #onPaint(Graphics2D)} is the hook the chart
 * layer will invoke to render overlays.
 */
public final class BookmapStrategyAddon {

    private static final Logger LOG = LoggerFactory.getLogger(BookmapStrategyAddon.class);

    private final AddonConfig config;
    private final OverlayState overlayState;
    private final MetricsState metricsState;
    private final StrategyMetricsPanel metricsPanel;
    private final TradeOverlayRenderer overlayRenderer;
    private final StrategyWebSocketClient wsClient;
    private final AtomicBoolean loaded = new AtomicBoolean(false);

    public BookmapStrategyAddon() {
        this(new AddonConfig());
    }

    public BookmapStrategyAddon(AddonConfig config) {
        this.config = config;
        this.overlayState = new OverlayState();
        this.metricsState = new MetricsState();
        this.metricsPanel = new StrategyMetricsPanel();
        this.overlayRenderer = new TradeOverlayRenderer();
        this.wsClient = new StrategyWebSocketClient(config, new InternalHandler());
    }

    /** Bookmap-framework lifecycle entry point. */
    public void onLoad() {
        if (!loaded.compareAndSet(false, true)) {
            return;
        }
        LOG.info("Bookmap strategy add-on loading (wsUrl={}, symbol={})",
            config.getWsUrl(), config.getSymbol());
        wsClient.start();
        metricsPanel.refresh(metricsState.snapshot());
    }

    /** Bookmap-framework lifecycle teardown. */
    public void onUnload() {
        if (!loaded.compareAndSet(true, false)) {
            return;
        }
        LOG.info("Bookmap strategy add-on unloading");
        wsClient.stop();
    }

    /**
     * Per-tick callback. Wired by the Bookmap chart layer once the SDK
     * adapter is in place — currently unused but kept on the public
     * surface so downstream wiring is mechanical.
     */
    public void onTimestamp(long timestampNanos) {
        // No-op for the MVP; data is event-driven via the WS client.
    }

    /**
     * Bookmap chart-paint hook. The SDK adapter will forward the
     * canvas {@link Graphics2D} here on every layer repaint.
     */
    public void onPaint(Graphics2D g2) {
        overlayRenderer.render(g2, overlayState);
    }

    public StrategyMetricsPanel getMetricsPanel() {
        return metricsPanel;
    }

    public OverlayState getOverlayState() {
        return overlayState;
    }

    public MetricsState getMetricsState() {
        return metricsState;
    }

    public TradeOverlayRenderer getOverlayRenderer() {
        return overlayRenderer;
    }

    public boolean isLoaded() {
        return loaded.get();
    }

    private final class InternalHandler implements EventHandler {

        @Override
        public void onEvent(StrategyEvent event) {
            if (event == null) {
                return;
            }
            EventType type = event.getType();
            if (type == EventType.ENTRY) {
                overlayState.addEntry(event);
            } else if (type == EventType.EXIT) {
                overlayState.addExit(event);
            }
            metricsState.update(event);
            metricsPanel.refresh(metricsState.snapshot());
        }

        @Override
        public void onConnectionStateChanged(String state) {
            metricsState.setConnection(state);
            metricsPanel.refresh(metricsState.snapshot());
        }

        @Override
        public void onTransportError(Throwable t) {
            LOG.debug("Bookmap addon: transport error", t);
        }
    }

    /**
     * Stand-alone harness — opens a Swing window and starts the WS
     * client. Useful for verifying end-to-end connectivity against
     * the Python mock emitter without launching Bookmap proper.
     */
    public static void main(String[] args) {
        BookmapStrategyAddon addon = new BookmapStrategyAddon();
        SwingUtilities.invokeLater(() -> {
            JFrame frame = new JFrame("Bookmap Strategy Add-on (smoke test)");
            frame.setDefaultCloseOperation(JFrame.EXIT_ON_CLOSE);
            frame.setContentPane(addon.getMetricsPanel());
            frame.pack();
            frame.setLocationRelativeTo(null);
            frame.setVisible(true);
            addon.onLoad();
        });
        Runtime.getRuntime().addShutdownHook(new Thread(addon::onUnload, "bookmap-shutdown"));
    }
}
