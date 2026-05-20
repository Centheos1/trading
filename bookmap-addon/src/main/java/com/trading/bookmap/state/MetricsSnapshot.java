package com.trading.bookmap.state;

/**
 * Immutable value snapshot of {@link MetricsState}.
 *
 * <p>Produced by {@link MetricsState#snapshot()} and passed to the
 * Swing panel for read-only rendering. Keeping the snapshot immutable
 * means the EDT can hold onto the reference for as long as it needs
 * without racing with the WebSocket thread.
 */
public final class MetricsSnapshot {

    public final String connection;
    public final String symbol;
    public final String regime;
    public final String position;
    public final String pnl;
    public final String lastSignal;
    public final String lastEntry;
    public final String lastExit;
    public final String health;

    public MetricsSnapshot(
        String connection,
        String symbol,
        String regime,
        String position,
        String pnl,
        String lastSignal,
        String lastEntry,
        String lastExit,
        String health
    ) {
        this.connection = connection == null ? "" : connection;
        this.symbol = symbol == null ? "" : symbol;
        this.regime = regime == null ? "" : regime;
        this.position = position == null ? "" : position;
        this.pnl = pnl == null ? "" : pnl;
        this.lastSignal = lastSignal == null ? "" : lastSignal;
        this.lastEntry = lastEntry == null ? "" : lastEntry;
        this.lastExit = lastExit == null ? "" : lastExit;
        this.health = health == null ? "" : health;
    }
}
