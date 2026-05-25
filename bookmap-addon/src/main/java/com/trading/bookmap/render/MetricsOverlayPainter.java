package com.trading.bookmap.render;

import com.trading.bookmap.BuildInfo;
import com.trading.bookmap.state.MetricsSnapshot;

import velox.api.layer1.layers.strategies.interfaces.ScreenSpaceCanvas;
import velox.api.layer1.layers.strategies.interfaces.ScreenSpaceCanvas.CanvasIcon;
import velox.api.layer1.layers.strategies.interfaces.ScreenSpaceCanvas.HorizontalCoordinate;
import velox.api.layer1.layers.strategies.interfaces.ScreenSpaceCanvas.PreparedImage;
import velox.api.layer1.layers.strategies.interfaces.ScreenSpaceCanvas.RelativeHorizontalCoordinate;
import velox.api.layer1.layers.strategies.interfaces.ScreenSpaceCanvas.RelativePixelHorizontalCoordinate;
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
import java.awt.Font;
import java.awt.Graphics2D;
import java.awt.RenderingHints;
import java.awt.image.BufferedImage;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Bookmap {@link ScreenSpacePainterFactory} that draws a semi-transparent
 * metrics HUD box in the top-right corner of the heatmap canvas.
 *
 * <p>The box is always visible while the chart is open — no dialog, no
 * side panel, no click required. It updates live whenever
 * {@link #updateSnapshot(MetricsSnapshot)} is called from the WebSocket
 * event handler.
 *
 * <h3>Rendering strategy</h3>
 * Each metrics update renders a new {@link PreparedImage} (230 × 160 px)
 * and calls {@code canvas.removeShape(old)} + {@code canvas.addShape(new)}
 * to swap it in. The {@link ScreenSpacePainterAdapter} viewport callbacks
 * ({@code onHeatmapActivePixelsWidth}, {@code onHeatmapPixelsHeight}) keep
 * the icon coordinates pinned to the top-right corner as the chart resizes.
 *
 * <h3>Coordinate system</h3>
 * Uses {@code HEATMAP} canvas so coordinates are local to the heatmap area
 * (excluding the price ladder). Y-axis is up: {@code VERTICAL_PIXEL_ZERO}
 * is the bottom edge, and larger values move toward the top. The box is
 * anchored at {@code (chartW - BOX_W - MARGIN)} from the left and
 * {@code (chartH - MARGIN)} from the bottom (= near the top).
 */
public final class MetricsOverlayPainter implements ScreenSpacePainterFactory {

    private static final Logger LOG = LoggerFactory.getLogger(MetricsOverlayPainter.class);

    static final int BOX_W  = 240;
    static final int BOX_H  = 158;
    static final int MARGIN = 12;

    /** Cached build label — read once, never changes during a session. */
    private static final String BUILD_LABEL = BuildInfo.load().shortLabel();

    private final AtomicReference<PreparedImage> currentImage =
        new AtomicReference<>(renderImage(null));

    private final CopyOnWriteArrayList<InnerPainter> painters =
        new CopyOnWriteArrayList<>();

    // -------------------------------------------------------- public API

    /**
     * Push a fresh metrics snapshot to all active chart painters.
     * Safe to call from any thread. Silently no-ops if no charts are
     * currently attached.
     */
    public void updateSnapshot(MetricsSnapshot snap) {
        PreparedImage img = renderImage(snap);
        currentImage.set(img);
        for (InnerPainter p : painters) {
            try {
                p.updateImage(img);
            } catch (Exception ex) {
                LOG.debug("MetricsOverlayPainter: updateSnapshot painter error", ex);
            }
        }
    }

    @Override
    public ScreenSpacePainterAdapter createScreenSpacePainter(
            String alias,
            String indicatorName,
            ScreenSpaceCanvasFactory canvasFactory) {
        InnerPainter p = new InnerPainter(canvasFactory, currentImage.get());
        painters.add(p);
        return p;
    }

    // --------------------------------------------------------- rendering

    /**
     * Render the full metrics box as a {@link PreparedImage}.
     * Called on every metrics update; kept fast (no I/O, no allocations
     * beyond the one {@link BufferedImage}).
     */
    static PreparedImage renderImage(MetricsSnapshot snap) {
        BufferedImage img = new BufferedImage(BOX_W, BOX_H, BufferedImage.TYPE_INT_ARGB);
        Graphics2D g = img.createGraphics();
        try {
            g.setRenderingHint(RenderingHints.KEY_ANTIALIASING,
                RenderingHints.VALUE_ANTIALIAS_ON);
            g.setRenderingHint(RenderingHints.KEY_TEXT_ANTIALIASING,
                RenderingHints.VALUE_TEXT_ANTIALIAS_LCD_HRGB);

            // Background
            g.setColor(new Color(15, 15, 25, 215));
            g.fillRoundRect(0, 0, BOX_W, BOX_H, 10, 10);

            // Border
            g.setColor(new Color(55, 55, 80, 255));
            g.drawRoundRect(0, 0, BOX_W - 1, BOX_H - 1, 10, 10);

            Font headerFont = new Font(Font.MONOSPACED, Font.BOLD,  11);
            Font labelFont  = new Font(Font.MONOSPACED, Font.PLAIN, 10);
            Font valueFont  = new Font(Font.MONOSPACED, Font.BOLD,  10);

            int lx     = 8;   // left margin
            int labelW = 80;  // width of the label column
            int y      = 16;
            int lineH  = 14;

            // Header row
            g.setFont(headerFont);
            g.setColor(new Color(200, 200, 230));
            g.drawString("Strategy Events", lx, y);

            g.setFont(labelFont);
            g.setColor(new Color(110, 110, 135));
            g.drawString(BUILD_LABEL, BOX_W - 88, y);

            // Separator
            y += 5;
            g.setColor(new Color(55, 55, 80));
            g.drawLine(lx, y, BOX_W - lx, y);
            y += lineH - 1;

            if (snap == null) {
                g.setFont(valueFont);
                g.setColor(COLOR_DISCONNECTED);
                g.drawString("DISCONNECTED", lx + labelW, y);
                return new PreparedImage(img);
            }

            row(g, labelFont, valueFont, lx, y, labelW, "Connection",
                snap.connection, statusColor(snap.connection));
            y += lineH;
            row(g, labelFont, valueFont, lx, y, labelW, "Symbol",    snap.symbol,    null);
            y += lineH;
            row(g, labelFont, valueFont, lx, y, labelW, "Regime",    snap.regime,    null);
            y += lineH;
            row(g, labelFont, valueFont, lx, y, labelW, "Position",  snap.position,  null);
            y += lineH;
            row(g, labelFont, valueFont, lx, y, labelW, "PnL",       snap.pnl,       pnlColor(snap.pnl));
            y += lineH;
            row(g, labelFont, valueFont, lx, y, labelW, "Last Entry",snap.lastEntry, null);
            y += lineH;
            row(g, labelFont, valueFont, lx, y, labelW, "Last Exit", snap.lastExit,  null);

        } finally {
            g.dispose();
        }
        return new PreparedImage(img);
    }

    // --------------------------------------------------------- helpers

    private static final Color COLOR_CONNECTED    = new Color(0x1f8b4c);
    private static final Color COLOR_DISCONNECTED = new Color(0xc62828);
    private static final Color COLOR_DEGRADED     = new Color(0xe67e22);
    private static final Color COLOR_PNL_POS      = new Color(0x2ecc71);
    private static final Color COLOR_PNL_NEG      = new Color(0xe74c3c);
    private static final Color COLOR_VALUE        = new Color(220, 220, 235);
    private static final Color COLOR_LABEL        = new Color(145, 145, 170);

    private static void row(Graphics2D g, Font labelFont, Font valueFont,
            int x, int y, int labelW,
            String label, String value, Color valueColor) {
        g.setFont(labelFont);
        g.setColor(COLOR_LABEL);
        g.drawString(label, x, y);

        g.setFont(valueFont);
        g.setColor(valueColor != null ? valueColor : COLOR_VALUE);
        String v = (value == null || value.isEmpty()) ? "--" : value;
        if (v.length() > 20) {
            v = v.substring(0, 19) + "\u2026";
        }
        g.drawString(v, x + labelW, y);
    }

    private static Color statusColor(String v) {
        if ("CONNECTED".equalsIgnoreCase(v))    return COLOR_CONNECTED;
        if ("DISCONNECTED".equalsIgnoreCase(v)) return COLOR_DISCONNECTED;
        if ("DEGRADED".equalsIgnoreCase(v))     return COLOR_DEGRADED;
        return null;
    }

    private static Color pnlColor(String pnl) {
        if (pnl == null || pnl.isEmpty() || "--".equals(pnl)) return null;
        if (pnl.startsWith("+")) return COLOR_PNL_POS;
        if (pnl.startsWith("-")) return COLOR_PNL_NEG;
        return null;
    }

    // ---------------------------------------------------------- inner

    private final class InnerPainter implements ScreenSpacePainterAdapter {

        private ScreenSpaceCanvas canvas;
        private CanvasIcon icon;

        /** Heatmap pixel dimensions — updated via viewport callbacks. */
        private volatile int chartW = 800;
        private volatile int chartH = 400;

        InnerPainter(ScreenSpaceCanvasFactory factory, PreparedImage initial) {
            this.canvas  = factory.createCanvas(ScreenSpaceCanvasType.HEATMAP);
            this.icon    = buildIcon(initial, chartW, chartH);
            canvas.addShape(this.icon);
        }

        /** Swap image without recreating the canvas. */
        synchronized void updateImage(PreparedImage img) {
            try {
                canvas.removeShape(icon);
                icon = buildIcon(img, chartW, chartH);
                canvas.addShape(icon);
            } catch (Exception ex) {
                LOG.debug("MetricsOverlayPainter: updateImage failed", ex);
            }
        }

        // ---- ScreenSpacePainterAdapter viewport callbacks

        @Override
        public void onHeatmapActivePixelsWidth(int w) {
            if (w > 0 && w != chartW) {
                chartW = w;
                reposition();
            }
        }

        @Override
        public void onHeatmapPixelsHeight(int h) {
            if (h > 0 && h != chartH) {
                chartH = h;
                reposition();
            }
        }

        /** Rebuild the icon at updated coordinates after a resize. */
        private synchronized void reposition() {
            try {
                PreparedImage img = icon.getImage();
                canvas.removeShape(icon);
                icon = buildIcon(img, chartW, chartH);
                canvas.addShape(icon);
            } catch (Exception ex) {
                LOG.debug("MetricsOverlayPainter: reposition failed", ex);
            }
        }

        @Override
        public void dispose() {
            painters.remove(this);
            try {
                canvas.dispose();
            } catch (Exception ex) {
                LOG.debug("MetricsOverlayPainter: dispose failed", ex);
            }
        }

        /**
         * Build a {@link CanvasIcon} pinned to the top-right corner.
         *
         * <p>Coordinate system (HEATMAP canvas):
         * <ul>
         *   <li>X: {@code HORIZONTAL_PIXEL_ZERO} = left edge of heatmap, px increases right.</li>
         *   <li>Y: {@code VERTICAL_PIXEL_ZERO} = bottom edge of heatmap, px increases upward.</li>
         *   <li>{@code CanvasIcon(img, x1, y1, x2, y2)} — (x1,y1) is bottom-left,
         *       (x2,y2) is top-right in this Y-up space.</li>
         * </ul>
         */
        private CanvasIcon buildIcon(PreparedImage img, int w, int h) {
            HorizontalCoordinate x1 = new RelativePixelHorizontalCoordinate(
                RelativeHorizontalCoordinate.HORIZONTAL_PIXEL_ZERO,
                w - BOX_W - MARGIN);
            HorizontalCoordinate x2 = new RelativePixelHorizontalCoordinate(
                RelativeHorizontalCoordinate.HORIZONTAL_PIXEL_ZERO,
                w - MARGIN);
            // y2 = top of box (near top of chart = large Y in Y-up space)
            VerticalCoordinate y2 = new RelativePixelVerticalCoordinate(
                RelativeVerticalCoordinate.VERTICAL_PIXEL_ZERO,
                h - MARGIN);
            // y1 = bottom of box = y2 - BOX_H
            VerticalCoordinate y1 = new RelativePixelVerticalCoordinate(
                RelativeVerticalCoordinate.VERTICAL_PIXEL_ZERO,
                h - MARGIN - BOX_H);
            return new CanvasIcon(img, x1, y1, x2, y2);
        }
    }
}
