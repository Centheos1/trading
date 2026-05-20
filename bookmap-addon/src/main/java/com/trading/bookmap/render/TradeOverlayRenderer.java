package com.trading.bookmap.render;

import com.trading.bookmap.model.StrategyEvent;
import com.trading.bookmap.state.OverlayState;

import java.awt.BasicStroke;
import java.awt.Color;
import java.awt.Font;
import java.awt.Graphics2D;
import java.awt.RenderingHints;
import java.awt.Stroke;
import java.util.List;

/**
 * Placeholder overlay renderer.
 *
 * <p>Provides a Bookmap-shaped interface for drawing ENTRY / EXIT
 * markers on top of the chart. The actual Bookmap canvas API
 * (pixel-perfect time/price → screen coordinates) is not wired up
 * yet; both {@link #mapTimestampToX(long)} and
 * {@link #mapPriceToY(double)} are pure stubs that callers can swap
 * out via {@link #setCoordinateMapper(CoordinateMapper)} once the
 * real Bookmap viewport is available.
 *
 * <p>Once the real Bookmap {@code Layer} / canvas API is integrated
 * the {@code CoordinateMapper} stub becomes the single point where
 * the add-on touches Bookmap-specific pixel maths — the rest of the
 * code is plain Java2D and can be unit-tested headlessly.
 */
public final class TradeOverlayRenderer {

    /**
     * Pluggable mapping from event time / price to chart pixels.
     * Implementations bind to the real Bookmap viewport. The default
     * returns ``-1`` so missing wiring is visible in tests.
     */
    public interface CoordinateMapper {
        int mapTimestampToX(long timestamp);
        int mapPriceToY(double price);
    }

    private CoordinateMapper coordinateMapper = new CoordinateMapper() {
        @Override
        public int mapTimestampToX(long timestamp) {
            // TODO: Bookmap canvas API
            // Replace with the real Bookmap viewport's
            // ``timeToScreenCoordinate(long ts)`` once the addon is
            // attached to a chart layer.
            return -1;
        }

        @Override
        public int mapPriceToY(double price) {
            // TODO: Bookmap canvas API
            // Replace with the real Bookmap viewport's
            // ``priceToScreenCoordinate(double price)``.
            return -1;
        }
    };

    public void setCoordinateMapper(CoordinateMapper mapper) {
        if (mapper != null) {
            this.coordinateMapper = mapper;
        }
    }

    /**
     * Render entry + exit markers from the supplied overlay state.
     *
     * <p>Callers should invoke this from the Bookmap render thread
     * (the chart paint callback), supplying the {@link Graphics2D}
     * that Bookmap hands them for the active layer.
     */
    public void render(Graphics2D g2, OverlayState state) {
        if (g2 == null || state == null) {
            return;
        }
        drawEntries(g2, state.snapshotEntries());
        drawExits(g2, state.snapshotExits());
    }

    public void drawEntries(Graphics2D g2, List<StrategyEvent> entries) {
        if (g2 == null || entries == null || entries.isEmpty()) {
            return;
        }
        Stroke previous = g2.getStroke();
        Object previousAA = g2.getRenderingHint(RenderingHints.KEY_ANTIALIASING);
        try {
            g2.setRenderingHint(RenderingHints.KEY_ANTIALIASING,
                RenderingHints.VALUE_ANTIALIAS_ON);
            g2.setStroke(new BasicStroke(1.5f));
            for (StrategyEvent ev : entries) {
                drawMarker(g2, ev, /*entry=*/true);
            }
        } finally {
            g2.setStroke(previous);
            g2.setRenderingHint(RenderingHints.KEY_ANTIALIASING, previousAA);
        }
    }

    public void drawExits(Graphics2D g2, List<StrategyEvent> exits) {
        if (g2 == null || exits == null || exits.isEmpty()) {
            return;
        }
        Stroke previous = g2.getStroke();
        Object previousAA = g2.getRenderingHint(RenderingHints.KEY_ANTIALIASING);
        try {
            g2.setRenderingHint(RenderingHints.KEY_ANTIALIASING,
                RenderingHints.VALUE_ANTIALIAS_ON);
            g2.setStroke(new BasicStroke(1.5f));
            for (StrategyEvent ev : exits) {
                drawMarker(g2, ev, /*entry=*/false);
            }
        } finally {
            g2.setStroke(previous);
            g2.setRenderingHint(RenderingHints.KEY_ANTIALIASING, previousAA);
        }
    }

    public int mapTimestampToX(long timestamp) {
        return coordinateMapper.mapTimestampToX(timestamp);
    }

    public int mapPriceToY(double price) {
        return coordinateMapper.mapPriceToY(price);
    }

    private void drawMarker(Graphics2D g2, StrategyEvent ev, boolean entry) {
        int x = mapTimestampToX(ev.getTimestamp());
        int y = mapPriceToY(ev.getPrice());
        if (x < 0 || y < 0) {
            // TODO: Bookmap canvas API — coordinate mapper not wired
            // yet; silently skip the marker so the placeholder does
            // not splatter the chart with bogus draws.
            return;
        }
        Color marker = colorFor(ev, entry);
        g2.setColor(marker);
        int size = 8;
        if (entry) {
            int[] xs = {x, x - size, x + size};
            int[] ys = {y - size, y + size, y + size};
            g2.fillPolygon(xs, ys, 3);
        } else {
            int[] xs = {x, x - size, x + size};
            int[] ys = {y + size, y - size, y - size};
            g2.fillPolygon(xs, ys, 3);
        }
        g2.setColor(Color.DARK_GRAY);
        Font previousFont = g2.getFont();
        g2.setFont(previousFont.deriveFont(Font.PLAIN, 10f));
        String side = ev.getSide();
        if (side != null && !side.isEmpty()) {
            g2.drawString(side, x + size + 2, y - size);
        }
        String priceText = String.format("%.2f", ev.getPrice());
        g2.drawString(priceText, x + size + 2, y + size);
        g2.setFont(previousFont);
    }

    private Color colorFor(StrategyEvent ev, boolean entry) {
        String side = ev.getSide();
        if (entry) {
            if ("BUY".equalsIgnoreCase(side)) {
                return new Color(0x1f8b4c);
            }
            if ("SELL".equalsIgnoreCase(side)) {
                return new Color(0xc62828);
            }
            return new Color(0x4f8be0);
        }
        return new Color(0x8e44ad);
    }
}
