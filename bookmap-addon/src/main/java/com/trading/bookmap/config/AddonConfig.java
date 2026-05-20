package com.trading.bookmap.config;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;
import java.util.Properties;

/**
 * Strongly-typed view over {@code bookmap-addon.properties}.
 *
 * <p>Resolution order:
 * <ol>
 *   <li>JVM system properties (e.g. {@code -Dbookmap.wsUrl=...})</li>
 *   <li>{@code bookmap-addon.properties} on the classpath</li>
 *   <li>Hard-coded defaults declared in this class</li>
 * </ol>
 *
 * <p>Each accessor returns a sensible default when the underlying
 * property is missing or unparseable so a stripped-down jar can run
 * without any external configuration.
 */
public final class AddonConfig {

    private static final Logger LOG = LoggerFactory.getLogger(AddonConfig.class);

    public static final String DEFAULT_WS_URL = "ws://localhost:8765";
    public static final long DEFAULT_RECONNECT_DELAY_MS = 3000L;
    public static final int DEFAULT_MAX_RECONNECTS = -1;
    public static final String DEFAULT_SYMBOL = "BTCUSDT";

    private static final String RESOURCE = "/bookmap-addon.properties";
    private static final String PREFIX = "bookmap.";

    private final Properties props;

    public AddonConfig() {
        this.props = load();
    }

    /** Test-friendly constructor that lets callers inject a {@link Properties}. */
    public AddonConfig(Properties props) {
        this.props = props == null ? new Properties() : props;
    }

    public String getWsUrl() {
        return resolveString("wsUrl", DEFAULT_WS_URL);
    }

    public long getReconnectDelayMs() {
        return resolveLong("reconnectDelayMs", DEFAULT_RECONNECT_DELAY_MS);
    }

    public int getMaxReconnects() {
        return resolveInt("maxReconnects", DEFAULT_MAX_RECONNECTS);
    }

    public String getSymbol() {
        return resolveString("symbol", DEFAULT_SYMBOL);
    }

    private String resolveString(String key, String fallback) {
        String sysProp = System.getProperty(PREFIX + key);
        if (sysProp != null && !sysProp.isEmpty()) {
            return sysProp;
        }
        String raw = props.getProperty(key);
        if (raw == null || raw.isEmpty()) {
            return fallback;
        }
        return raw;
    }

    private long resolveLong(String key, long fallback) {
        try {
            return Long.parseLong(resolveString(key, Long.toString(fallback)));
        } catch (NumberFormatException ex) {
            LOG.warn("Bookmap addon: '{}' is not a long; using {}", key, fallback);
            return fallback;
        }
    }

    private int resolveInt(String key, int fallback) {
        try {
            return Integer.parseInt(resolveString(key, Integer.toString(fallback)));
        } catch (NumberFormatException ex) {
            LOG.warn("Bookmap addon: '{}' is not an int; using {}", key, fallback);
            return fallback;
        }
    }

    private static Properties load() {
        Properties props = new Properties();
        try (InputStream in = AddonConfig.class.getResourceAsStream(RESOURCE)) {
            if (in != null) {
                props.load(in);
            } else {
                LOG.debug("Bookmap addon: no {} on classpath; using defaults", RESOURCE);
            }
        } catch (IOException ex) {
            LOG.warn("Bookmap addon: failed to load {}; using defaults", RESOURCE, ex);
        }
        return props;
    }
}
