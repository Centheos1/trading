package com.trading.bookmap.state;

import com.trading.bookmap.model.StrategyEvent;

import java.util.Collections;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;

/**
 * Thread-safe store for trade-overlay events (ENTRY / EXIT).
 *
 * <p>Backed by {@link CopyOnWriteArrayList} so concurrent reads (from
 * the Bookmap render thread) and writes (from the WebSocket client
 * thread) never need explicit locking. {@link #snapshotEntries()} and
 * {@link #snapshotExits()} return unmodifiable views suitable for
 * passing directly to {@code TradeOverlayRenderer}.
 *
 * <p>The store applies a coarse size cap so a long-running session
 * cannot exhaust memory; oldest events are trimmed when the cap is
 * exceeded. The cap is intentionally generous — production sessions
 * fire a handful of intents per minute at most.
 */
public final class OverlayState {

    private static final int DEFAULT_MAX = 5_000;

    private final int maxEvents;
    private final CopyOnWriteArrayList<StrategyEvent> entries = new CopyOnWriteArrayList<>();
    private final CopyOnWriteArrayList<StrategyEvent> exits = new CopyOnWriteArrayList<>();

    public OverlayState() {
        this(DEFAULT_MAX);
    }

    public OverlayState(int maxEvents) {
        this.maxEvents = Math.max(64, maxEvents);
    }

    public void addEntry(StrategyEvent event) {
        if (event == null) {
            return;
        }
        entries.add(event);
        trim(entries);
    }

    public void addExit(StrategyEvent event) {
        if (event == null) {
            return;
        }
        exits.add(event);
        trim(exits);
    }

    public List<StrategyEvent> snapshotEntries() {
        return Collections.unmodifiableList(entries);
    }

    public List<StrategyEvent> snapshotExits() {
        return Collections.unmodifiableList(exits);
    }

    public int entryCount() {
        return entries.size();
    }

    public int exitCount() {
        return exits.size();
    }

    public void clear() {
        entries.clear();
        exits.clear();
    }

    private void trim(CopyOnWriteArrayList<StrategyEvent> bucket) {
        while (bucket.size() > maxEvents) {
            bucket.remove(0);
        }
    }
}
