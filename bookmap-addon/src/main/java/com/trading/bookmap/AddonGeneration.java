package com.trading.bookmap;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * JVM-wide generation counter used to coordinate between concurrent
 * {@code BookmapStrategyAddon} class incarnations.
 *
 * <h3>Why it exists</h3>
 * Bookmap reloads add-on JARs into <i>new classloaders</i>. The previous
 * classloader is not unloaded immediately — its daemon threads (including
 * our WebSocket supervisor) keep running. Because static fields are
 * scoped <b>per classloader</b>, a normal {@code static volatile} flag
 * cannot tell the old supervisor that a new instance has taken over.
 *
 * <p>This class works around the limitation by storing the active
 * generation token in {@link System#getProperties()} — a single
 * {@code java.util.Properties} instance loaded by the bootstrap
 * classloader and therefore visible to <i>every</i> classloader in the
 * JVM. New incarnations publish a higher generation; older supervisors
 * poll the property and bail out when they observe a newer value.
 *
 * <p>The token is monotonic across reloads: each call to
 * {@link #claim()} returns a value strictly greater than every previous
 * value observed in this JVM, even if {@link System#nanoTime()} regresses
 * (which it can on some platforms).
 */
public final class AddonGeneration {

    private static final Logger LOG = LoggerFactory.getLogger(AddonGeneration.class);

    /** Key under which the current generation token is published. */
    static final String KEY = "com.trading.bookmap.ws.generation";

    /** Optional human-readable description of who currently holds the lease. */
    static final String OWNER_KEY = "com.trading.bookmap.ws.owner";

    private AddonGeneration() {
        // utility
    }

    /**
     * Atomically publish a new, strictly-monotonic generation token and
     * return it to the caller. Synchronises on {@link System#getProperties()}
     * so concurrent claimants in different classloaders see a consistent
     * counter.
     */
    public static long claim(String ownerDescription) {
        synchronized (System.getProperties()) {
            long now = System.nanoTime();
            long existing = readCurrent();
            // Ensure strict monotonicity even if nanoTime() regresses.
            long next = Math.max(now, existing + 1);
            System.setProperty(KEY, Long.toString(next));
            if (ownerDescription != null) {
                System.setProperty(OWNER_KEY, ownerDescription);
            }
            LOG.info("AddonGeneration: claimed generation {} for owner '{}' (previous={})",
                next, ownerDescription, existing);
            return next;
        }
    }

    /**
     * Return the current generation token, or {@code 0} if none has been
     * published yet (or the value is unparseable).
     */
    public static long readCurrent() {
        String v = System.getProperty(KEY);
        if (v == null) {
            return 0L;
        }
        try {
            return Long.parseLong(v);
        } catch (NumberFormatException ex) {
            return 0L;
        }
    }

    /**
     * Read the human-readable owner string, or {@code "unknown"} when
     * nothing has been published.
     */
    public static String readOwner() {
        String v = System.getProperty(OWNER_KEY);
        return v == null ? "unknown" : v;
    }

    /**
     * {@code true} if the caller's generation is still the latest one
     * published to the JVM-wide registry.
     */
    public static boolean isStillCurrent(long myGeneration) {
        return readCurrent() == myGeneration;
    }
}
