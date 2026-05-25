package com.trading.bookmap.render;

import com.trading.bookmap.DiagnosticLog;
import com.trading.bookmap.model.EventType;
import com.trading.bookmap.model.StrategyEvent;

import velox.api.layer1.layers.strategies.interfaces.ScreenSpaceCanvas;
import velox.api.layer1.layers.strategies.interfaces.ScreenSpaceCanvas.CanvasIcon;
import velox.api.layer1.layers.strategies.interfaces.ScreenSpaceCanvas.HorizontalCoordinate;
import velox.api.layer1.layers.strategies.interfaces.ScreenSpaceCanvas.PreparedImage;
import velox.api.layer1.layers.strategies.interfaces.ScreenSpaceCanvas.RelativeDataVerticalCoordinate;
import velox.api.layer1.layers.strategies.interfaces.ScreenSpaceCanvas.RelativeHorizontalCoordinate;
import velox.api.layer1.layers.strategies.interfaces.ScreenSpaceCanvas.RelativePixelVerticalCoordinate;
import velox.api.layer1.layers.strategies.interfaces.ScreenSpaceCanvas.RelativeVerticalCoordinate;
import velox.api.layer1.layers.strategies.interfaces.ScreenSpaceCanvas.VerticalCoordinate;
import velox.api.layer1.layers.strategies.interfaces.ScreenSpaceCanvasFactory;
import velox.api.layer1.layers.strategies.interfaces.ScreenSpaceCanvasFactory.ScreenSpaceCanvasType;
import velox.api.layer1.layers.strategies.interfaces.ScreenSpacePainterAdapter;
import velox.api.layer1.layers.strategies.interfaces.ScreenSpacePainterFactory;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.awt.Color;
import java.awt.Graphics2D;
import java.awt.RenderingHints;
import java.awt.image.BufferedImage;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;

/**
 * Bookmap {@link ScreenSpacePainterFactory} that draws ENTRY / EXIT markers
 * directly on the heatmap chart at the event's timestamp and price.
 *
 * <h3>Per-instrument scaling</h3>
 * Bookmap's {@code RelativeDataVerticalCoordinate} expects price as
 * <em>tick units</em> (price ÷ pips). Each instrument has its own pip
 * size (BTC = 0.01, ES = 0.25, …), so a single shared pips value cannot
 * correctly position markers on multiple charts simultaneously.
 *
 * <p>Instead, the painter resolves pips lazily per-canvas via an
 * {@link InstrumentInfoLookup} supplied at construction time. The
 * caller registers each chart's pips via
 * {@link com.trading.bookmap.BookmapStrategyAddon#onInstrumentAdded}.
 *
 * <h3>Symbol filtering</h3>
 * Each canvas is associated with one alias (e.g. {@code BTCUSDT@BN}).
 * Events from the WebSocket carry their own {@code symbol} (e.g.
 * {@code BTCUSDT}). We only draw a marker on a canvas whose alias
 * resolves to the same symbol — this prevents BTC entries from being
 * placed at nonsense prices on an ES chart and vice-versa.
 *
 * <h3>Marker shapes</h3>
 * <ul>
 *   <li>BUY ENTRY: green upward triangle ▲</li>
 *   <li>SELL ENTRY: red downward triangle ▼</li>
 *   <li>EXIT (any side): purple diamond ◆</li>
 * </ul>
 */
public final class TradeOverlayPainter implements ScreenSpacePainterFactory {

    private static final Logger LOG = LoggerFactory.getLogger(TradeOverlayPainter.class);

    static final int ICON_HALF = 14;

    /** Caller-supplied lookup so the painter can resolve per-alias metadata. */
    public interface InstrumentInfoLookup {
        /** Returns the pip size for the given Bookmap alias, or {@code 0.0} if unknown. */
        double pipsForAlias(String alias);

        /** Returns the symbol associated with the given alias, or {@code ""} if unknown. */
        String symbolForAlias(String alias);
    }

    private final InstrumentInfoLookup lookup;
    private final CopyOnWriteArrayList<MarkerSpec> markers = new CopyOnWriteArrayList<>();
    private final Map<String, InnerPainter> activePainters = new ConcurrentHashMap<>();

    public TradeOverlayPainter(InstrumentInfoLookup lookup) {
        this.lookup = lookup;
    }

    /**
     * Add an ENTRY or EXIT event so it appears on every active chart whose
     * symbol matches. May be called from any thread. Non-ENTRY/EXIT events
     * are silently ignored.
     */
    public void addEvent(StrategyEvent event) {
        if (event == null) {
            return;
        }
        EventType type = event.getType();
        if (type != EventType.ENTRY && type != EventType.EXIT) {
            return;
        }
        MarkerSpec spec = new MarkerSpec(event);
        markers.add(spec);
        DiagnosticLog.log("TradeOverlay: queued " + type
            + " symbol=" + spec.symbol
            + " price=" + spec.price
            + " ts=" + spec.timestampMs
            + " activeCanvases=" + activePainters.size());
        for (InnerPainter p : activePainters.values()) {
            p.applySpec(spec);
        }
    }

    /** Remove all markers from all active canvases (e.g. on add-on unload). */
    public void clear() {
        markers.clear();
        for (InnerPainter p : activePainters.values()) {
            p.canvas.dispose();
        }
        activePainters.clear();
    }

    @Override
    public ScreenSpacePainterAdapter createScreenSpacePainter(
            String alias,
            String indicatorName,
            ScreenSpaceCanvasFactory canvasFactory) {
        // In Bookmap's screen-space painter API the parameters are:
        //   alias        = a synthetic key like "com.trading.bookmap.BookmapStrategyAddon#Trade Overlays"
        //   indicatorName = the ACTUAL instrument alias, e.g. "BTCUSDT@BN"
        // We must use indicatorName for pips/symbol lookups; alias is only
        // used as the map key to find and dispose this painter later.
        String instrumentAlias = (indicatorName != null && !indicatorName.isEmpty())
            ? indicatorName : alias;
        InnerPainter painter = new InnerPainter(alias, instrumentAlias, canvasFactory, markers);
        activePainters.put(alias, painter);
        DiagnosticLog.log("TradeOverlay: createScreenSpacePainter painterKey=" + alias
            + " instrumentAlias=" + instrumentAlias
            + " backlog=" + markers.size());
        return painter;
    }

    // ------------------------------------------------------------------ inner

    private final class InnerPainter implements ScreenSpacePainterAdapter {

        private final String painterKey;
        /** The actual instrument alias (e.g. "BTCUSDT@BN"), used for pips/symbol lookup. */
        private final String instrumentAlias;
        final ScreenSpaceCanvas canvas;
        private final Set<Long> applied = ConcurrentHashMap.newKeySet();

        InnerPainter(String painterKey, String instrumentAlias,
                     ScreenSpaceCanvasFactory factory, List<MarkerSpec> existing) {
            this.painterKey = painterKey;
            this.instrumentAlias = instrumentAlias;
            this.canvas = factory.createCanvas(ScreenSpaceCanvasType.HEATMAP);
            for (MarkerSpec spec : existing) {
                applySpec(spec);
            }
        }

        void applySpec(MarkerSpec spec) {
            if (!applied.add(spec.id)) {
                return;
            }
            // Use instrumentAlias (e.g. "BTCUSDT@BN") for pips/symbol lookup —
            // NOT painterKey ("com.trading.bookmap.BookmapStrategyAddon#Trade Overlays").
            String chartSymbol = lookup.symbolForAlias(instrumentAlias);
            String eventSymbol = spec.symbol == null ? "" : spec.symbol;
            if (!chartSymbol.isEmpty() && !eventSymbol.isEmpty()
                    && !symbolsMatch(chartSymbol, eventSymbol)) {
                return;
            }

            double pips = lookup.pipsForAlias(instrumentAlias);
            if (pips <= 0.0) {
                pips = 1.0;
            }
            try {
                long tsNanos = spec.timestampMs * 1_000_000L;
                double ticks = spec.price / pips;

                HorizontalCoordinate xBase = RelativeHorizontalCoordinate.HORIZONTAL_DATA_ZERO;
                VerticalCoordinate priceCoord = new RelativeDataVerticalCoordinate(
                    RelativeVerticalCoordinate.VERTICAL_DATA_ZERO, ticks);

                CanvasIcon icon = new CanvasIcon(
                    spec.image,
                    new RelativeHorizontalCoordinate(xBase, -ICON_HALF, tsNanos),
                    new RelativePixelVerticalCoordinate(priceCoord, -ICON_HALF),
                    new RelativeHorizontalCoordinate(xBase,  ICON_HALF, tsNanos),
                    new RelativePixelVerticalCoordinate(priceCoord,  ICON_HALF)
                );
                canvas.addShape(icon);
                DiagnosticLog.log("TradeOverlay: drew "
                    + (spec.isEntry ? "ENTRY" : "EXIT")
                    + " instrument=" + instrumentAlias
                    + " symbol=" + eventSymbol
                    + " price=" + spec.price
                    + " pips=" + pips
                    + " ticks=" + ticks);
            } catch (Exception ex) {
                LOG.debug("TradeOverlayPainter: failed to place marker for instrument={}",
                    instrumentAlias, ex);
                DiagnosticLog.log("TradeOverlay: place failed instrument=" + instrumentAlias
                    + " price=" + spec.price, ex);
            }
        }

        @Override
        public void dispose() {
            activePainters.remove(painterKey);
            try {
                canvas.dispose();
            } catch (Exception ex) {
                LOG.debug("TradeOverlayPainter: canvas dispose failed for instrument={}",
                    instrumentAlias, ex);
            }
        }
    }

    /**
     * Loose symbol match — accepts exact matches and the common case where
     * an alias prefix encodes the symbol (e.g. {@code BTCUSDT@BN} carries
     * symbol {@code BTCUSDT}). Comparison is case-insensitive.
     */
    static boolean symbolsMatch(String chartSymbol, String eventSymbol) {
        if (chartSymbol.equalsIgnoreCase(eventSymbol)) {
            return true;
        }
        return chartSymbol.toUpperCase().contains(eventSymbol.toUpperCase());
    }

    // ------------------------------------------------------------------ spec

    static final class MarkerSpec {

        final long id;
        final long timestampMs;
        final double price;
        final String symbol;
        final boolean isEntry;
        final PreparedImage image;

        MarkerSpec(StrategyEvent ev) {
            this.timestampMs = ev.getTimestamp();
            this.price = ev.getPrice();
            this.symbol = ev.getSymbol();
            this.isEntry = ev.getType() == EventType.ENTRY;
            this.id = timestampMs
                ^ Double.doubleToLongBits(price)
                ^ (long) ev.getType().ordinal() * 31L;
            this.image = renderMarker(ev);
        }

        private static PreparedImage renderMarker(StrategyEvent ev) {
            int size = ICON_HALF * 2;
            BufferedImage img = new BufferedImage(size, size, BufferedImage.TYPE_INT_ARGB);
            Graphics2D g = img.createGraphics();
            try {
                g.setRenderingHint(RenderingHints.KEY_ANTIALIASING,
                    RenderingHints.VALUE_ANTIALIAS_ON);
                g.setColor(new Color(0, 0, 0, 0));
                g.fillRect(0, 0, size, size);

                boolean isEntry = ev.getType() == EventType.ENTRY;
                boolean isBuy   = "BUY".equalsIgnoreCase(ev.getSide());

                Color fill = isEntry
                    ? (isBuy ? new Color(0x1f8b4c) : new Color(0xc62828))
                    : new Color(0x8e44ad);
                g.setColor(fill);

                int m = 1;
                if (isEntry && isBuy) {
                    int[] xs = {size / 2, m, size - m};
                    int[] ys = {m, size - m, size - m};
                    g.fillPolygon(xs, ys, 3);
                } else if (isEntry) {
                    int[] xs = {size / 2, m, size - m};
                    int[] ys = {size - m, m, m};
                    g.fillPolygon(xs, ys, 3);
                } else {
                    int cx = size / 2, cy = size / 2;
                    int[] xs = {cx, m, cx, size - m};
                    int[] ys = {m, cy, size - m, cy};
                    g.fillPolygon(xs, ys, 4);
                }
            } finally {
                g.dispose();
            }
            return new PreparedImage(img);
        }
    }
}
