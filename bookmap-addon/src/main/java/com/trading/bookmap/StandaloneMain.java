package com.trading.bookmap;

import com.trading.bookmap.config.AddonConfig;
import com.trading.bookmap.ipc.EventHandler;
import com.trading.bookmap.ipc.StrategyWebSocketClient;
import com.trading.bookmap.model.EventType;
import com.trading.bookmap.model.StrategyEvent;
import com.trading.bookmap.state.MetricsSnapshot;
import com.trading.bookmap.state.MetricsState;
import com.trading.bookmap.ui.StrategyMetricsPanel;

import javax.swing.JFrame;
import javax.swing.SwingUtilities;

/**
 * Standalone smoke-test harness — opens a Swing window connected to the
 * Python publisher without launching Bookmap.
 *
 * <p>This class has <b>zero</b> {@code velox.*} imports, so it can be
 * loaded from the fat-jar without the Bookmap app on the classpath.
 * {@link BookmapStrategyAddon} cannot be used as the {@code Main-Class}
 * because its class signature implements Bookmap interfaces that are
 * {@code compileOnly} and therefore absent from the fat-jar at runtime.
 *
 * <pre>
 *   # Start the mock emitter first:
 *   python -m bookmap_publisher.mock_emitter --symbol BTCUSDT --interval 1.0
 *
 *   # Then run the standalone window:
 *   java -jar build/libs/bookmap-addon-0.1.0+build.N-all.jar
 * </pre>
 */
public final class StandaloneMain {

    private StandaloneMain() {}

    public static void main(String[] args) {
        AddonConfig config = new AddonConfig();
        MetricsState metrics = new MetricsState();
        StrategyMetricsPanel panel = new StrategyMetricsPanel();

        EventHandler handler = new EventHandler() {
            @Override
            public void onEvent(StrategyEvent event) {
                if (event == null) return;
                metrics.update(event);
                MetricsSnapshot snap = metrics.snapshot();
                panel.refresh(snap);
            }

            @Override
            public void onConnectionStateChanged(String state) {
                metrics.setConnection(state);
                panel.refresh(metrics.snapshot());
            }

            @Override
            public void onTransportError(Throwable t) {
                System.err.println("[bookmap-standalone] transport error: " + t.getMessage());
            }
        };

        StrategyWebSocketClient ws = new StrategyWebSocketClient(config, handler);

        SwingUtilities.invokeLater(() -> {
            JFrame frame = new JFrame("Strategy Events (standalone)");
            frame.setDefaultCloseOperation(JFrame.EXIT_ON_CLOSE);
            frame.setContentPane(panel);
            frame.pack();
            frame.setLocationRelativeTo(null);
            frame.setVisible(true);
        });

        ws.start();

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            ws.stop();
        }, "bookmap-standalone-shutdown"));
    }
}
