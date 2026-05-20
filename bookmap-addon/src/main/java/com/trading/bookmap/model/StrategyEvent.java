package com.trading.bookmap.model;

import java.util.Map;

/**
 * Immutable wire-format event from the Python publisher.
 *
 * <p>Mirrors the schema documented in {@code docs/BOOKMAP_INTEGRATION.md}
 * and emitted by {@code bookmap_publisher/event_builder.py}. Unknown
 * JSON fields are tolerated so the publisher can add fields without
 * breaking older add-on builds.
 *
 * <h3>Dependency-free deserialisation</h3>
 * The class is parsed by {@link FlatJsonParser} rather than Jackson.
 * Bookmap pre-loads its own (older) copy of {@code jackson-core} on
 * the parent classloader, and that copy's {@code JsonParser} is
 * missing methods that newer {@code jackson-databind} releases call
 * during deserialisation context construction
 * (e.g. {@code getReadCapabilities()} added in Jackson 2.12). Using
 * a hand-rolled parser eliminates that classloader conflict entirely.
 */
public final class StrategyEvent {

    private final EventType type;
    private final String rawType;
    private final String symbol;
    private final long timestamp;
    private final double price;
    private final String side;
    private final double qty;
    private final String label;
    private final String name;
    private final String value;

    public StrategyEvent(
        String type,
        String symbol,
        Long timestamp,
        Double price,
        String side,
        Double qty,
        String label,
        String name,
        String value
    ) {
        this.rawType = type == null ? "" : type;
        this.type = EventType.fromString(type);
        this.symbol = symbol == null ? "" : symbol;
        this.timestamp = timestamp == null ? 0L : timestamp;
        this.price = price == null ? 0.0 : price;
        this.side = side == null ? "" : side;
        this.qty = qty == null ? 0.0 : qty;
        this.label = label == null ? "" : label;
        this.name = name == null ? "" : name;
        this.value = value == null ? "" : value;
    }

    /**
     * Parse a JSON line emitted by the Python publisher into an event.
     * Returns {@code null} for empty / malformed input rather than
     * throwing — the caller decides whether to drop the message
     * silently or surface a diagnostic.
     */
    public static StrategyEvent fromJson(String json) {
        if (json == null || json.isEmpty()) {
            return null;
        }
        Map<String, Object> m;
        try {
            m = FlatJsonParser.parseObject(json);
        } catch (RuntimeException ex) {
            throw new IllegalArgumentException(
                "Malformed event JSON: " + ex.getMessage(), ex);
        }
        return new StrategyEvent(
            FlatJsonParser.getString(m, "type", ""),
            FlatJsonParser.getString(m, "symbol", ""),
            FlatJsonParser.getLong(m, "timestamp", 0L),
            FlatJsonParser.getDouble(m, "price", 0.0),
            FlatJsonParser.getString(m, "side", ""),
            FlatJsonParser.getDouble(m, "qty", 0.0),
            FlatJsonParser.getString(m, "label", ""),
            FlatJsonParser.getString(m, "name", ""),
            FlatJsonParser.getString(m, "value", "")
        );
    }

    public EventType getType() {
        return type;
    }

    /**
     * Original {@code type} string as it appeared on the wire — useful
     * for diagnostics when {@link #getType()} returns {@link EventType#UNKNOWN}.
     */
    public String getRawType() {
        return rawType;
    }

    public String getSymbol() {
        return symbol;
    }

    public long getTimestamp() {
        return timestamp;
    }

    public double getPrice() {
        return price;
    }

    public String getSide() {
        return side;
    }

    public double getQty() {
        return qty;
    }

    public String getLabel() {
        return label;
    }

    public String getName() {
        return name;
    }

    public String getValue() {
        return value;
    }

    @Override
    public String toString() {
        return "StrategyEvent{type=" + type
            + ", symbol='" + symbol + '\''
            + ", timestamp=" + timestamp
            + ", price=" + price
            + ", side='" + side + '\''
            + ", qty=" + qty
            + ", label='" + label + '\''
            + ", name='" + name + '\''
            + ", value='" + value + '\''
            + '}';
    }
}
