package com.trading.bookmap.render;

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
 * <h3>Lifecycle</h3>
 * <ol>
 *   <li>Create one instance per add-on.</li>
 *   <li>Register it once via
 *       {@code api.sendUserMessage(Layer1ApiUserMessageModifyScreenSpacePainter...)}
 *       when an instrument is first added.</li>
 *   <li>Push incoming WS events via {@link #addEvent(StrategyEvent, double)}
 *       from any thread — the painter applies them to all active canvases
 *       thread-safely.</li>
 * </ol>
 *
 * <h3>Coordinate mapping</h3>
 * <ul>
 *   <li>Time: {@code HORIZONTAL_DATA_ZERO + eventTimestampNanos} — places the
 *       icon at the exact point on the time axis where the event occurred.</li>
 *   <li>Price: {@code VERTICAL_DATA_ZERO + (price / pips)} — places the icon
 *       at the event price level on the vertical axis.</li>
 *   <li>Both coordinates have a ±{@value #ICON_HALF}px pixel offset to centre
 *       the icon over the point.</li>
 * </ul>
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

    static final int ICON_HALF = 7;

    private final CopyOnWriteArrayList<MarkerSpec> markers = new CopyOnWriteArrayList<>();
    private final Map<String, InnerPainter> activePainters = new ConcurrentHashMap<>();

    /**
     * Add an ENTRY or EXIT event so it appears on all active instrument charts.
     * May be called from any thread. Non-ENTRY/EXIT events are silently ignored.
     *
     * @param event     the strategy event
     * @param pricePips the instrument's minimum price increment (from
     *                  {@code InstrumentInfo.pips}); used to convert price to ticks
     */
    public void addEvent(StrategyEvent event, double pricePips) {
        if (event == null) {
            return;
        }
        EventType type = event.getType();
        if (type != EventType.ENTRY && type != EventType.EXIT) {
            return;
        }
        MarkerSpec spec = new MarkerSpec(event, pricePips);
        markers.add(spec);
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
        InnerPainter painter = new InnerPainter(alias, canvasFactory, markers);
        activePainters.put(alias, painter);
        return painter;
    }

    // ------------------------------------------------------------------ inner

    private final class InnerPainter implements ScreenSpacePainterAdapter {

        private final String alias;
        final ScreenSpaceCanvas canvas;
        private final Set<Long> applied = ConcurrentHashMap.newKeySet();

        InnerPainter(String alias, ScreenSpaceCanvasFactory factory,
                     List<MarkerSpec> existing) {
            this.alias = alias;
            this.canvas = factory.createCanvas(ScreenSpaceCanvasType.HEATMAP);
            for (MarkerSpec spec : existing) {
                applySpec(spec);
            }
        }

        void applySpec(MarkerSpec spec) {
            if (!applied.add(spec.id)) {
                return;
            }
            try {
                long tsNanos = spec.timestampMs * 1_000_000L;
                double ticks = (spec.pricePips > 0) ? (spec.price / spec.pricePips) : spec.price;

                HorizontalCoordinate xBase = RelativeHorizontalCoordinate.HORIZONTAL_DATA_ZERO;
                // Compose vertical: data coordinate at the price, then pixel offsets above/below.
                // RelativeVerticalCoordinate(base, data, pixel) is protected; nesting two
                // public subclasses achieves the same result.
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
            } catch (Exception ex) {
                LOG.debug("TradeOverlayPainter: failed to place marker for alias={}", alias, ex);
            }
        }

        @Override
        public void dispose() {
            activePainters.remove(alias);
            try {
                canvas.dispose();
            } catch (Exception ex) {
                LOG.debug("TradeOverlayPainter: canvas dispose failed for alias={}", alias, ex);
            }
        }
    }

    // ------------------------------------------------------------------ spec

    static final class MarkerSpec {

        final long id;
        final long timestampMs;
        final double price;
        final double pricePips;
        final PreparedImage image;

        MarkerSpec(StrategyEvent ev, double pricePips) {
            this.timestampMs = ev.getTimestamp();
            this.price = ev.getPrice();
            this.pricePips = pricePips;
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
                    // upward-pointing triangle ▲
                    int[] xs = {size / 2, m, size - m};
                    int[] ys = {m, size - m, size - m};
                    g.fillPolygon(xs, ys, 3);
                } else if (isEntry) {
                    // downward-pointing triangle ▼
                    int[] xs = {size / 2, m, size - m};
                    int[] ys = {size - m, m, m};
                    g.fillPolygon(xs, ys, 3);
                } else {
                    // exit: diamond ◆
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
