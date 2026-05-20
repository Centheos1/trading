package com.trading.bookmap.state;

import com.trading.bookmap.model.EventType;
import com.trading.bookmap.model.StrategyEvent;

import java.util.concurrent.atomic.AtomicReference;

/**
 * Thread-safe latest-value cache for metrics-panel fields.
 *
 * <p>Backed by a single {@link AtomicReference} to the immutable
 * {@link MetricsSnapshot}. Writers compose a new snapshot from the
 * current one and {@code compareAndSet} it in, so readers always see a
 * consistent picture and no field is ever half-updated.
 *
 * <p>Field mapping from the wire schema:
 * <ul>
 *   <li>{@code ENTRY}    → {@code lastEntry}, {@code symbol}, position is updated by POSITION</li>
 *   <li>{@code EXIT}     → {@code lastExit}, {@code symbol}</li>
 *   <li>{@code METRIC}   → routed by {@code name}: PnL → {@code pnl}; Regime → {@code regime}; otherwise {@code lastSignal}</li>
 *   <li>{@code POSITION} → {@code position} (side + qty)</li>
 *   <li>{@code HEALTH}   → {@code health}, {@code connection}</li>
 * </ul>
 */
public final class MetricsState {

    private final AtomicReference<MetricsSnapshot> ref = new AtomicReference<>(empty());

    public MetricsSnapshot snapshot() {
        return ref.get();
    }

    public void setConnection(String state) {
        MetricsSnapshot s;
        MetricsSnapshot n;
        do {
            s = ref.get();
            n = new MetricsSnapshot(
                state,
                s.symbol, s.regime, s.position, s.pnl,
                s.lastSignal, s.lastEntry, s.lastExit, s.health
            );
        } while (!ref.compareAndSet(s, n));
    }

    public void update(StrategyEvent event) {
        if (event == null) {
            return;
        }
        MetricsSnapshot s;
        MetricsSnapshot n;
        do {
            s = ref.get();
            n = applyEvent(s, event);
        } while (!ref.compareAndSet(s, n));
    }

    private static MetricsSnapshot applyEvent(MetricsSnapshot s, StrategyEvent ev) {
        String symbol = nonEmpty(ev.getSymbol(), s.symbol);
        String connection = s.connection;
        String regime = s.regime;
        String position = s.position;
        String pnl = s.pnl;
        String lastSignal = s.lastSignal;
        String lastEntry = s.lastEntry;
        String lastExit = s.lastExit;
        String health = s.health;

        EventType type = ev.getType();
        if (type == EventType.ENTRY) {
            lastEntry = formatLastTrade(ev);
            lastSignal = nonEmpty(ev.getLabel(), lastSignal);
        } else if (type == EventType.EXIT) {
            lastExit = formatLastTrade(ev);
            lastSignal = nonEmpty(ev.getLabel(), lastSignal);
        } else if (type == EventType.METRIC) {
            String name = ev.getName();
            String value = ev.getValue();
            if ("PnL".equalsIgnoreCase(name)) {
                pnl = value;
            } else if ("Regime".equalsIgnoreCase(name)) {
                regime = value;
            } else if (!name.isEmpty()) {
                lastSignal = name + "=" + value;
            } else if (!ev.getLabel().isEmpty()) {
                lastSignal = ev.getLabel();
            }
        } else if (type == EventType.POSITION) {
            String label = ev.getLabel();
            if (!label.isEmpty()) {
                position = label;
            } else if (ev.getQty() > 0 && !ev.getSide().isEmpty()) {
                position = ev.getSide() + " " + ev.getQty();
            } else {
                position = "Flat";
            }
        } else if (type == EventType.HEALTH) {
            String value = ev.getValue();
            health = nonEmpty(value, ev.getLabel());
            if ("CONNECTED".equalsIgnoreCase(value)
                    || "DISCONNECTED".equalsIgnoreCase(value)
                    || "DEGRADED".equalsIgnoreCase(value)) {
                connection = value;
            }
        }

        return new MetricsSnapshot(
            connection, symbol, regime, position, pnl,
            lastSignal, lastEntry, lastExit, health
        );
    }

    private static String formatLastTrade(StrategyEvent ev) {
        StringBuilder sb = new StringBuilder();
        if (!ev.getSide().isEmpty()) {
            sb.append(ev.getSide()).append(' ');
        }
        if (ev.getQty() > 0) {
            sb.append(String.format("%.4f", ev.getQty())).append(' ');
        }
        if (ev.getPrice() > 0) {
            sb.append('@').append(String.format("%.2f", ev.getPrice()));
        }
        if (sb.length() == 0) {
            return ev.getLabel();
        }
        return sb.toString().trim();
    }

    private static String nonEmpty(String candidate, String fallback) {
        return (candidate == null || candidate.isEmpty()) ? fallback : candidate;
    }

    private static MetricsSnapshot empty() {
        return new MetricsSnapshot(
            "DISCONNECTED", "", "", "Flat", "0.00",
            "", "", "", ""
        );
    }
}
