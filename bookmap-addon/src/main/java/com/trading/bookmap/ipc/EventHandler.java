package com.trading.bookmap.ipc;

import com.trading.bookmap.model.StrategyEvent;

/**
 * Sink invoked by {@link StrategyWebSocketClient} for every event
 * received from the Python publisher.
 *
 * <p>Implementations are expected to be cheap and non-blocking — the
 * WebSocket client invokes the handler from its own networking thread.
 * Heavier work (UI repaint, rendering) should be deferred via
 * {@code SwingUtilities.invokeLater} or by snapshotting state and
 * letting the Bookmap render thread pick it up later.
 *
 * <p>Connection-state transitions are reported via
 * {@link #onConnectionStateChanged(String)} so the metrics panel and
 * logs can track liveness without re-implementing the reconnect
 * bookkeeping.
 */
public interface EventHandler {

    void onEvent(StrategyEvent event);

    default void onConnectionStateChanged(String state) {
        // No-op by default. Override to surface connection liveness.
    }

    default void onTransportError(Throwable t) {
        // No-op by default. Override to log / surface transport errors.
    }
}
