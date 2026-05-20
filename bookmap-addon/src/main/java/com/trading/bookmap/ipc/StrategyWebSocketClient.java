package com.trading.bookmap.ipc;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.trading.bookmap.config.AddonConfig;
import com.trading.bookmap.model.StrategyEvent;

import org.java_websocket.client.WebSocketClient;
import org.java_websocket.handshake.ServerHandshake;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.net.URI;
import java.net.URISyntaxException;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Single-connection WebSocket client that pulls {@link StrategyEvent}s
 * from the Python publisher and forwards them to an
 * {@link EventHandler}.
 *
 * <p>Threading model:
 * <ul>
 *   <li>The underlying {@link WebSocketClient} owns a dedicated
 *       network IO thread; all {@code onOpen / onMessage / onClose /
 *       onError} callbacks happen on that thread.</li>
 *   <li>A separate reconnect loop runs on a daemon thread spawned
 *       lazily by {@link #start()}. It re-attempts connection with a
 *       capped fixed delay (configurable) whenever the network
 *       thread reports a disconnect.</li>
 *   <li>Neither thread ever blocks the Bookmap rendering pipeline:
 *       the handler is expected to dispatch UI work to the EDT.</li>
 * </ul>
 */
public final class StrategyWebSocketClient {

    private static final Logger LOG = LoggerFactory.getLogger(StrategyWebSocketClient.class);

    private final AddonConfig config;
    private final EventHandler handler;
    private final ObjectMapper mapper;
    private final AtomicBoolean started = new AtomicBoolean(false);
    private final AtomicBoolean shouldRun = new AtomicBoolean(false);
    private final AtomicInteger reconnectAttempts = new AtomicInteger(0);

    private volatile InnerClient client;
    private volatile Thread supervisor;

    public StrategyWebSocketClient(AddonConfig config, EventHandler handler) {
        this(config, handler, new ObjectMapper());
    }

    public StrategyWebSocketClient(AddonConfig config, EventHandler handler, ObjectMapper mapper) {
        this.config = config;
        this.handler = handler;
        this.mapper = mapper;
    }

    public void start() {
        if (!started.compareAndSet(false, true)) {
            return;
        }
        shouldRun.set(true);
        supervisor = new Thread(this::supervisorLoop, "bookmap-ws-supervisor");
        supervisor.setDaemon(true);
        supervisor.start();
    }

    public void stop() {
        shouldRun.set(false);
        started.set(false);
        InnerClient c = this.client;
        if (c != null) {
            try {
                c.closeBlocking();
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
        Thread s = this.supervisor;
        if (s != null) {
            s.interrupt();
        }
    }

    public boolean isConnected() {
        InnerClient c = this.client;
        return c != null && c.isOpen();
    }

    public int getReconnectAttempts() {
        return reconnectAttempts.get();
    }

    private void supervisorLoop() {
        URI uri;
        try {
            uri = new URI(config.getWsUrl());
        } catch (URISyntaxException ex) {
            LOG.error("Bookmap addon: invalid wsUrl '{}'", config.getWsUrl(), ex);
            return;
        }
        int maxReconnects = config.getMaxReconnects();
        long delayMs = Math.max(250L, config.getReconnectDelayMs());
        while (shouldRun.get()) {
            InnerClient c = new InnerClient(uri);
            this.client = c;
            try {
                boolean connected = c.connectBlocking();
                if (connected) {
                    reconnectAttempts.set(0);
                    safeReportState("CONNECTED");
                    // Block until the connection actually closes.
                    c.awaitClose();
                } else {
                    LOG.debug("Bookmap addon: connectBlocking returned false for {}", uri);
                }
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
                return;
            } catch (Exception ex) {
                handler.onTransportError(ex);
                LOG.debug("Bookmap addon: connect attempt failed", ex);
            }

            if (!shouldRun.get()) {
                break;
            }
            int attempts = reconnectAttempts.incrementAndGet();
            if (maxReconnects >= 0 && attempts > maxReconnects) {
                LOG.warn("Bookmap addon: exhausted {} reconnect attempts, giving up",
                    maxReconnects);
                break;
            }
            safeReportState("DISCONNECTED");
            try {
                Thread.sleep(delayMs);
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
                return;
            }
        }
    }

    private void safeReportState(String state) {
        try {
            handler.onConnectionStateChanged(state);
        } catch (Exception ex) {
            LOG.debug("Bookmap addon: connection state listener threw", ex);
        }
    }

    private final class InnerClient extends WebSocketClient {

        private final Object closeLatch = new Object();
        private volatile boolean closed = false;

        InnerClient(URI uri) {
            super(uri);
            setConnectionLostTimeout(0);
        }

        @Override
        public void onOpen(ServerHandshake handshakedata) {
            LOG.info("Bookmap addon: connected to {} (status={})",
                getURI(), handshakedata.getHttpStatusMessage());
        }

        @Override
        public void onMessage(String message) {
            if (message == null || message.isEmpty()) {
                return;
            }
            StrategyEvent event;
            try {
                event = mapper.readValue(message, StrategyEvent.class);
            } catch (JsonProcessingException ex) {
                LOG.debug("Bookmap addon: dropping malformed message: {}", message, ex);
                return;
            }
            try {
                handler.onEvent(event);
            } catch (Exception ex) {
                LOG.warn("Bookmap addon: event handler raised", ex);
            }
        }

        @Override
        public void onClose(int code, String reason, boolean remote) {
            LOG.info(
                "Bookmap addon: socket closed (code={}, remote={}, reason='{}')",
                code, remote, reason
            );
            signalClosed();
        }

        @Override
        public void onError(Exception ex) {
            LOG.debug("Bookmap addon: socket error", ex);
            handler.onTransportError(ex);
            signalClosed();
        }

        void awaitClose() throws InterruptedException {
            synchronized (closeLatch) {
                while (!closed) {
                    closeLatch.wait();
                }
            }
        }

        private void signalClosed() {
            synchronized (closeLatch) {
                closed = true;
                closeLatch.notifyAll();
            }
        }
    }
}
