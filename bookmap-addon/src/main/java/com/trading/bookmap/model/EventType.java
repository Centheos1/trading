package com.trading.bookmap.model;

/**
 * MVP strategy event types streamed from the Python publisher.
 *
 * <p>Order matches the canonical event schema documented in
 * {@code docs/BOOKMAP_INTEGRATION.md}. {@link #UNKNOWN} is a fallback
 * used when the server emits a type the add-on does not yet handle,
 * so unknown values never throw at deserialisation time.
 */
public enum EventType {
    ENTRY,
    EXIT,
    METRIC,
    POSITION,
    HEALTH,
    UNKNOWN;

    /**
     * Lenient parser that returns {@link #UNKNOWN} for unrecognised
     * values, including {@code null}. Used by {@link StrategyEvent}
     * to keep the JSON path total.
     */
    public static EventType fromString(String raw) {
        if (raw == null) {
            return UNKNOWN;
        }
        try {
            return EventType.valueOf(raw.trim().toUpperCase());
        } catch (IllegalArgumentException ex) {
            return UNKNOWN;
        }
    }
}
