package com.trading.bookmap.model;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;

/**
 * Immutable wire-format event from the Python publisher.
 *
 * <p>Mirrors the schema documented in
 * {@code docs/BOOKMAP_INTEGRATION.md} and emitted by
 * {@code bookmap_publisher/event_builder.py}. Unknown JSON fields are
 * tolerated so the publisher can add fields without breaking older
 * add-on builds.
 *
 * <p>This class is intentionally simple — Jackson populates fields via
 * the all-args constructor when {@link JsonProperty} bindings match.
 * No mutation API is provided; downstream state handlers should treat
 * instances as value objects.
 */
@JsonIgnoreProperties(ignoreUnknown = true)
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
        @JsonProperty("type") String type,
        @JsonProperty("symbol") String symbol,
        @JsonProperty("timestamp") Long timestamp,
        @JsonProperty("price") Double price,
        @JsonProperty("side") String side,
        @JsonProperty("qty") Double qty,
        @JsonProperty("label") String label,
        @JsonProperty("name") String name,
        @JsonProperty("value") String value
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
