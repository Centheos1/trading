package com.trading.bookmap.ui;

import com.trading.bookmap.BuildInfo;
import com.trading.bookmap.state.MetricsSnapshot;

import javax.swing.BorderFactory;
import javax.swing.JLabel;
import javax.swing.JPanel;
import javax.swing.SwingUtilities;
import javax.swing.UIManager;
import java.awt.BorderLayout;
import java.awt.Color;
import java.awt.Component;
import java.awt.Dimension;
import java.awt.Font;
import java.awt.GridLayout;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Compact Swing widget that displays the latest strategy metrics.
 *
 * <pre>
 *   ┌──────────────────────────────┐
 *   │ Strategy Metrics             │
 *   ├──────────────────────────────┤
 *   │ Connection :  CONNECTED  🟢  │
 *   │ Symbol     :  BTCUSDT        │
 *   │ Regime     :  Absorption     │
 *   │ Position   :  BUY 0.25       │
 *   │ PnL        :  +12.34    🟢   │
 *   │ Last Signal:  ENTRY_BOUNCE   │
 *   │ Last Entry :  BUY 0.25 @67k  │
 *   │ Last Exit  :  SELL 0.25 @67k │
 *   │ Health     :  CONNECTED  🟢  │
 *   └──────────────────────────────┘
 * </pre>
 *
 * <p>Value labels for Connection, Health and PnL are colour-coded:
 * <ul>
 *   <li>CONNECTED → green</li>
 *   <li>DISCONNECTED → red</li>
 *   <li>DEGRADED → orange</li>
 *   <li>Positive PnL (+…) → green</li>
 *   <li>Negative PnL (−…) → red</li>
 * </ul>
 *
 * <p>Thread-safety: {@link #refresh(MetricsSnapshot)} is safe to call
 * from any thread; mutations are dispatched onto the EDT via
 * {@link SwingUtilities#invokeLater(Runnable)}.
 */
public final class StrategyMetricsPanel extends JPanel {

    private static final Color COLOR_CONNECTED    = new Color(0x1f8b4c);
    private static final Color COLOR_DISCONNECTED = new Color(0xc62828);
    private static final Color COLOR_DEGRADED     = new Color(0xe67e22);
    private static final Color COLOR_PNL_POS      = new Color(0x1f8b4c);
    private static final Color COLOR_PNL_NEG      = new Color(0xc62828);

    private static final String FIELD_CONNECTION  = "Connection";
    private static final String FIELD_SYMBOL      = "Symbol";
    private static final String FIELD_REGIME      = "Regime";
    private static final String FIELD_POSITION    = "Position";
    private static final String FIELD_PNL         = "PnL";
    private static final String FIELD_LAST_SIGNAL = "Last Signal";
    private static final String FIELD_LAST_ENTRY  = "Last Entry";
    private static final String FIELD_LAST_EXIT   = "Last Exit";
    private static final String FIELD_HEALTH      = "Health";

    private final Map<String, JLabel> valueLabels = new LinkedHashMap<>();

    public StrategyMetricsPanel() {
        super(new BorderLayout(4, 4));
        setBorder(BorderFactory.createEmptyBorder(6, 8, 6, 8));
        setPreferredSize(new Dimension(320, 270));

        BuildInfo bi = BuildInfo.load();
        JPanel header = new JPanel(new BorderLayout());
        header.setBorder(BorderFactory.createMatteBorder(0, 0, 1, 0, new Color(180, 180, 180)));
        JLabel title = new JLabel("Strategy Metrics");
        title.setFont(title.getFont().deriveFont(Font.BOLD, title.getFont().getSize2D() + 1f));
        JLabel build = new JLabel(bi.shortLabel());
        build.setFont(build.getFont().deriveFont(Font.PLAIN, build.getFont().getSize2D() - 1f));
        build.setForeground(new Color(120, 120, 120));
        header.add(title, BorderLayout.WEST);
        header.add(build, BorderLayout.EAST);
        add(header, BorderLayout.NORTH);

        JPanel grid = new JPanel(new GridLayout(0, 2, 6, 3));
        addRow(grid, FIELD_CONNECTION);
        addRow(grid, FIELD_SYMBOL);
        addRow(grid, FIELD_REGIME);
        addRow(grid, FIELD_POSITION);
        addRow(grid, FIELD_PNL);
        addRow(grid, FIELD_LAST_SIGNAL);
        addRow(grid, FIELD_LAST_ENTRY);
        addRow(grid, FIELD_LAST_EXIT);
        addRow(grid, FIELD_HEALTH);
        add(grid, BorderLayout.CENTER);
    }

    private void addRow(JPanel grid, String label) {
        JLabel key = new JLabel(label + ":");
        key.setFont(key.getFont().deriveFont(Font.PLAIN));
        JLabel val = new JLabel("--");
        val.setFont(val.getFont().deriveFont(Font.PLAIN));
        val.setHorizontalAlignment(JLabel.RIGHT);
        valueLabels.put(label, val);
        grid.add(key);
        grid.add(val);
    }

    /**
     * Apply a metrics snapshot to the panel. Safe to call from any
     * thread — mutation is dispatched onto the EDT.
     */
    public void refresh(MetricsSnapshot snapshot) {
        if (snapshot == null) {
            return;
        }
        Runnable r = () -> applySnapshotOnEdt(snapshot);
        if (SwingUtilities.isEventDispatchThread()) {
            r.run();
        } else {
            SwingUtilities.invokeLater(r);
        }
    }

    private void applySnapshotOnEdt(MetricsSnapshot s) {
        setColored(FIELD_CONNECTION, s.connection, statusColor(s.connection));
        set(FIELD_SYMBOL,      s.symbol);
        set(FIELD_REGIME,      s.regime);
        set(FIELD_POSITION,    s.position);
        setColored(FIELD_PNL,  s.pnl,        pnlColor(s.pnl));
        set(FIELD_LAST_SIGNAL, s.lastSignal);
        set(FIELD_LAST_ENTRY,  s.lastEntry);
        set(FIELD_LAST_EXIT,   s.lastExit);
        setColored(FIELD_HEALTH, s.health,   statusColor(s.health));
    }

    private void set(String field, String value) {
        setColored(field, value, null);
    }

    private void setColored(String field, String value, Color color) {
        JLabel label = valueLabels.get(field);
        if (label == null) {
            return;
        }
        label.setText(value == null || value.isEmpty() ? "--" : value);
        if (color != null) {
            label.setForeground(color);
        } else {
            label.setForeground(UIManager.getColor("Label.foreground"));
        }
    }

    private static Color statusColor(String value) {
        if (value == null) {
            return null;
        }
        if ("CONNECTED".equalsIgnoreCase(value)) {
            return COLOR_CONNECTED;
        }
        if ("DISCONNECTED".equalsIgnoreCase(value)) {
            return COLOR_DISCONNECTED;
        }
        if ("DEGRADED".equalsIgnoreCase(value)) {
            return COLOR_DEGRADED;
        }
        return null;
    }

    private static Color pnlColor(String pnl) {
        if (pnl == null || pnl.isEmpty() || pnl.equals("--")) {
            return null;
        }
        if (pnl.startsWith("+")) {
            return COLOR_PNL_POS;
        }
        if (pnl.startsWith("-")) {
            return COLOR_PNL_NEG;
        }
        return null;
    }

    /**
     * Helper for embedding in containers that need a stable AWT
     * {@link Component} reference.
     */
    public Component asComponent() {
        return this;
    }
}
