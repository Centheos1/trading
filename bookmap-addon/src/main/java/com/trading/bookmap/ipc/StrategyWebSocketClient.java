package com.trading.bookmap.ipc;

import com.trading.bookmap.AddonGeneration;
import com.trading.bookmap.DiagnosticLog;
import com.trading.bookmap.config.AddonConfig;
import com.trading.bookmap.model.StrategyEvent;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.net.URI;
import java.net.URISyntaxException;
import java.net.http.HttpClient;
import java.net.http.WebSocket;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Single-connection WebSocket client that pulls {@link StrategyEvent}s
 * from the Python publisher and forwards them to an {@link EventHandler}.
 *
 * <h3>Transport choice</h3>
 * Uses {@link java.net.http.WebSocket} (JDK 11+) rather than the
 * {@code org.java-websocket} library. Bookmap ships its own, older copy
 * of {@code java-websocket} on the parent class-loader; that copy sends
 * {@code Sec-WebSocket-Version: 8} (a pre-RFC draft), which the Python
 * {@code websockets} server correctly rejects. By using the JDK's built-in
 * client (always loaded by the bootstrap class-loader) we avoid the
 * classloader conflict entirely and always negotiate RFC 6455 (version 13).
 *
 * <h3>Classloader supersession</h3>
 * Bookmap reloads add-on JARs into fresh classloaders without unloading
 * the previous one, so stale supervisor threads can keep running for the
 * remainder of the Bookmap session. To prevent multiple supervisors from
 * connecting to the publisher concurrently, this client claims a
 * JVM-wide generation token via {@link AddonGeneration} on
 * {@link #start()}. A small scheduled task polls the token; if a newer
 * client publishes a higher generation, the supervisor gracefully closes
 * its active socket and exits the reconnect loop.
 *
 * <h3>Threading model</h3>
 * <ul>
 *   <li>A supervisor daemon thread drives the connect → wait → reconnect loop.</li>
 *   <li>A second, single-shot scheduled-executor thread polls the JVM-wide
 *       generation token every 250&nbsp;ms.</li>
 *   <li>The JDK {@link HttpClient}'s internal selector thread fires the
 *       {@link WebSocket.Listener} callbacks; they are cheap and non-blocking.</li>
 *   <li>None of these threads ever touch the Bookmap rendering pipeline.</li>
 * </ul>
 */
public final class StrategyWebSocketClient {

    private static final Logger LOG = LoggerFactory.getLogger(StrategyWebSocketClient.class);
    private static final long GEN_POLL_PERIOD_MS = 250L;

    private final AddonConfig config;
    private final EventHandler handler;
    private final AtomicBoolean started = new AtomicBoolean(false);
    private final AtomicBoolean shouldRun = new AtomicBoolean(false);
    private final AtomicInteger reconnectAttempts = new AtomicInteger(0);

    private volatile WebSocket activeWs;
    private volatile Thread supervisor;
    private volatile long myGeneration;
    private volatile ScheduledExecutorService genPoller;
    private volatile ScheduledFuture<?> genPollHandle;

    public StrategyWebSocketClient(AddonConfig config, EventHandler handler) {
        this.config = config;
        this.handler = handler;
    }

    public void start() {
        if (!started.compareAndSet(false, true)) {
            return;
        }
        // Claim a fresh JVM-wide generation; any older supervisor still
        // running in a previous classloader will see this and exit.
        String owner = describeOwner();
        myGeneration = AddonGeneration.claim(owner);
        LOG.info("Bookmap addon: WS client starting (generation={}, owner={})",
            myGeneration, owner);
        DiagnosticLog.log("WS client starting (generation=" + myGeneration
            + ", owner=" + owner + ", url=" + config.getWsUrl() + ")");

        shouldRun.set(true);
        supervisor = new Thread(this::supervisorLoop, "bookmap-ws-supervisor");
        supervisor.setDaemon(true);
        supervisor.start();

        // Cross-classloader supersession watchdog.
        genPoller = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "bookmap-ws-gen-poller");
            t.setDaemon(true);
            return t;
        });
        genPollHandle = genPoller.scheduleAtFixedRate(
            this::pollGenerationAndMaybeShutDown,
            GEN_POLL_PERIOD_MS, GEN_POLL_PERIOD_MS, TimeUnit.MILLISECONDS);
    }

    public void stop() {
        shouldRun.set(false);
        started.set(false);
        cancelGenPoller();
        WebSocket ws = this.activeWs;
        if (ws != null && !ws.isOutputClosed()) {
            try {
                ws.sendClose(WebSocket.NORMAL_CLOSURE, "shutdown");
            } catch (Exception ex) {
                LOG.debug("Bookmap addon: error sending close frame", ex);
            }
        }
        Thread s = this.supervisor;
        if (s != null) {
            s.interrupt();
        }
    }

    public boolean isConnected() {
        WebSocket ws = this.activeWs;
        return ws != null && !ws.isInputClosed() && !ws.isOutputClosed();
    }

    public int getReconnectAttempts() {
        return reconnectAttempts.get();
    }

    public long getGeneration() {
        return myGeneration;
    }

    /**
     * Runs periodically on {@code bookmap-ws-gen-poller}. If a newer
     * generation has been published (i.e. a different classloader's
     * client has taken over), close our active socket and stop the
     * reconnect loop so the old classloader stays passive.
     */
    private void pollGenerationAndMaybeShutDown() {
        try {
            if (!shouldRun.get()) {
                return;
            }
            long current = AddonGeneration.readCurrent();
            if (current != myGeneration) {
                LOG.info(
                    "Bookmap addon: superseded by newer instance (myGen={}, currentGen={}, newOwner='{}'). "
                        + "Shutting down stale supervisor.",
                    myGeneration, current, AddonGeneration.readOwner());
                shouldRun.set(false);
                WebSocket ws = this.activeWs;
                if (ws != null && !ws.isOutputClosed()) {
                    try {
                        ws.sendClose(WebSocket.NORMAL_CLOSURE, "superseded");
                    } catch (Exception ex) {
                        LOG.debug("Bookmap addon: error closing socket on supersession", ex);
                    }
                }
                Thread s = this.supervisor;
                if (s != null) {
                    s.interrupt();
                }
                cancelGenPoller();
            }
        } catch (Throwable t) {
            LOG.debug("Bookmap addon: gen poller threw", t);
        }
    }

    private void cancelGenPoller() {
        ScheduledFuture<?> h = this.genPollHandle;
        if (h != null) {
            h.cancel(false);
        }
        ScheduledExecutorService p = this.genPoller;
        if (p != null) {
            p.shutdown();
        }
    }

    private String describeOwner() {
        ClassLoader cl = getClass().getClassLoader();
        String clName = cl == null ? "bootstrap" : cl.getClass().getSimpleName()
            + "@" + Integer.toHexString(System.identityHashCode(cl));
        return "StrategyWebSocketClient[" + clName + "]";
    }

    private void supervisorLoop() {
        URI uri;
        try {
            uri = new URI(config.getWsUrl());
        } catch (URISyntaxException ex) {
            LOG.error("Bookmap addon: invalid wsUrl '{}'", config.getWsUrl(), ex);
            return;
        }

        HttpClient httpClient = HttpClient.newHttpClient();
        int maxReconnects = config.getMaxReconnects();
        long delayMs = Math.max(250L, config.getReconnectDelayMs());

        while (shouldRun.get()) {
            // Cheap check before each attempt — avoids racing against a
            // supersession that landed while we were sleeping.
            if (!AddonGeneration.isStillCurrent(myGeneration)) {
                LOG.info("Bookmap addon: detected newer generation before reconnect; exiting loop");
                break;
            }

            CountDownLatch closeLatch = new CountDownLatch(1);
            InnerListener listener = new InnerListener(uri, closeLatch);

            try {
                WebSocket ws = httpClient
                    .newWebSocketBuilder()
                    .buildAsync(uri, listener)
                    .get();

                this.activeWs = ws;
                reconnectAttempts.set(0);
                safeReportState("CONNECTED");

                closeLatch.await();

            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
                return;
            } catch (Exception ex) {
                Throwable cause = ex.getCause() != null ? ex.getCause() : ex;
                handler.onTransportError(cause);
                LOG.debug("Bookmap addon: connect attempt failed for {}", uri, cause);
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
        LOG.info("Bookmap addon: supervisor loop exited (generation={})", myGeneration);
        cancelGenPoller();
    }

    private void safeReportState(String state) {
        try {
            handler.onConnectionStateChanged(state);
        } catch (Exception ex) {
            LOG.debug("Bookmap addon: connection state listener threw", ex);
        }
    }

    // ------------------------------------------------------------------ inner

    /**
     * Bulletproof JDK {@link WebSocket.Listener} implementation.
     *
     * <p>Every callback wraps its body in {@code catch (Throwable)} because
     * the JDK runtime treats <i>any</i> listener exception as fatal and
     * abruptly closes the connection without sending a Close frame
     * (server logs see a TCP RST and no clean disconnect). Pre-#7 builds
     * lost connections in 1-2 s because a single unhandled Jackson
     * {@code RuntimeException} or BufferedImage rendering error
     * propagated out of {@link #onText}.
     *
     * <p>Per-connection {@code messagesReceived} / {@code connectedAtMs}
     * counters are surfaced in {@code onClose} so the user can tell
     * "we connected but got nothing" apart from "we got events then
     * lost the connection".
     */
    private final class InnerListener implements WebSocket.Listener {

        private final URI uri;
        private final CountDownLatch closeLatch;
        private final StringBuilder textBuffer = new StringBuilder();
        private volatile long connectedAtMs;
        private volatile long messagesReceived;

        InnerListener(URI uri, CountDownLatch closeLatch) {
            this.uri = uri;
            this.closeLatch = closeLatch;
        }

        @Override
        public void onOpen(WebSocket webSocket) {
            try {
                connectedAtMs = System.currentTimeMillis();
                messagesReceived = 0;
                LOG.info("Bookmap addon: connected to {} (generation={})",
                    uri, myGeneration);
                DiagnosticLog.log("WS open  uri=" + uri + " gen=" + myGeneration);
                webSocket.request(1);
            } catch (Throwable t) {
                LOG.warn("Bookmap addon: onOpen threw", t);
                DiagnosticLog.log("WS onOpen threw", t);
            }
        }

        @Override
        public CompletionStage<?> onText(WebSocket webSocket, CharSequence data, boolean last) {
            try {
                textBuffer.append(data);
                if (last) {
                    String message = textBuffer.toString();
                    textBuffer.setLength(0);
                    messagesReceived++;
                    dispatchMessage(message);
                }
            } catch (Throwable t) {
                // Defensive: even StringBuilder.append could theoretically
                // throw (OOM, very long sequences). Never let it close the WS.
                LOG.warn("Bookmap addon: onText body threw", t);
                DiagnosticLog.log("WS onText threw (msg #" + messagesReceived + ")", t);
            }
            try {
                webSocket.request(1);
            } catch (Throwable t) {
                LOG.warn("Bookmap addon: webSocket.request(1) threw", t);
                DiagnosticLog.log("WS request(1) threw", t);
            }
            return null;
        }

        @Override
        public CompletionStage<?> onClose(WebSocket webSocket, int statusCode, String reason) {
            try {
                long uptime = System.currentTimeMillis() - connectedAtMs;
                LOG.info(
                    "Bookmap addon: socket closed (code={}, reason='{}', "
                        + "messagesReceived={}, uptime={}ms, generation={})",
                    statusCode, reason, messagesReceived, uptime, myGeneration);
                DiagnosticLog.log("WS close code=" + statusCode
                    + " reason='" + reason + "'"
                    + " messages=" + messagesReceived
                    + " uptime=" + uptime + "ms");
            } catch (Throwable t) {
                LOG.warn("Bookmap addon: onClose threw", t);
            } finally {
                closeLatch.countDown();
            }
            return null;
        }

        @Override
        public void onError(WebSocket webSocket, Throwable error) {
            try {
                long uptime = System.currentTimeMillis() - connectedAtMs;
                LOG.warn(
                    "Bookmap addon: WebSocket onError after {}ms, {} messages received: {}: {}",
                    uptime, messagesReceived,
                    error.getClass().getName(), error.getMessage(), error);
                DiagnosticLog.log("WS onError uptime=" + uptime + "ms"
                    + " messages=" + messagesReceived
                    + " type=" + error.getClass().getName()
                    + " msg=" + error.getMessage(), error);
                handler.onTransportError(error);
            } catch (Throwable t) {
                LOG.warn("Bookmap addon: onError handler itself threw", t);
            } finally {
                closeLatch.countDown();
            }
        }

        /**
         * Parse + dispatch a complete text message. Catches
         * {@link Throwable} on BOTH the parse path and the handler path
         * because Jackson can throw a variety of unchecked exceptions
         * (NPE, IAE, DatabindException) for malformed inputs, and any
         * uncaught throw would propagate out of {@link #onText} and
         * silently abort the WebSocket.
         */
        private void dispatchMessage(String message) {
            if (message == null || message.isEmpty()) {
                return;
            }
            StrategyEvent event;
            try {
                event = StrategyEvent.fromJson(message);
            } catch (Throwable ex) {
                LOG.warn("Bookmap addon: failed to parse message ({} chars): {}",
                    message.length(), abbreviate(message), ex);
                DiagnosticLog.log("Parse failure on msg #" + messagesReceived
                    + " payload=" + abbreviate(message), ex);
                return;
            }
            if (event == null) {
                return;
            }
            try {
                handler.onEvent(event);
            } catch (Throwable ex) {
                LOG.warn("Bookmap addon: event handler raised on msg #{}",
                    messagesReceived, ex);
                DiagnosticLog.log("Handler raised on msg #" + messagesReceived, ex);
            }
        }

        private String abbreviate(String s) {
            if (s == null) {
                return "<null>";
            }
            if (s.length() <= 200) {
                return s;
            }
            return s.substring(0, 200) + "...(+" + (s.length() - 200) + " chars)";
        }
    }
}
