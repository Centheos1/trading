from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QHeaderView,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QColor

# ---------------------------------------------------------------------------
# Phase 8B — named constants (§20 no magic constants)
# ---------------------------------------------------------------------------

_MODE_LABEL_PAPER = "Paper"
_MODE_LABEL_LIVE = "Live"
_MODE_LABEL_OBSERVE = "Observe"

_COL_TIME = "Time"
_COL_SIDE = "Side"
_COL_QTY = "Qty"
_COL_PRICE = "Price"
_COL_PNL = "PnL"
_COL_STATUS = "Status"
_COL_SIGNAL = "Signal"
_COL_REASON = "Reason"

_ORDER_COLUMNS = [
    _COL_TIME, _COL_SIDE, _COL_QTY, _COL_PRICE,
    _COL_PNL, _COL_STATUS, _COL_SIGNAL, _COL_REASON,
]

_STRATEGY_NOT_ARMED = "Strategy not armed"


class AccountPanel(QWidget):
    """Shows account balance, open positions, and order history.

    Phase 8B: strategy PnL is driven by ``update_strategy_stats()``
    rather than the legacy FIFO order-matching accumulator.  The FIFO
    attributes (``_last_entry_price``, ``_last_entry_side``,
    ``_realized_pnl``) are intentionally absent.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(400, 150)
        self._connected = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        header = QLabel("Account / Execution")
        header.setFont(QFont("Menlo", 9, QFont.Bold))
        header.setStyleSheet("color: #b4b4c8; background: #0f0f19; padding: 4px;")
        layout.addWidget(header)

        self._placeholder = QLabel("Not connected")
        self._placeholder.setFont(QFont("Menlo", 11))
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet(
            "color: #555570; padding: 20px; background: transparent;")
        layout.addWidget(self._placeholder)

        self._content = QWidget()
        content_layout = QVBoxLayout(self._content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(2)

        # ---- Balance / PnL row ----
        balance_row = QHBoxLayout()
        balance_row.setContentsMargins(4, 2, 4, 2)
        self._bal_label = self._make_label("Balance: --")
        self._avail_label = self._make_label("Available: --")
        self._pnl_label = self._make_label("Unrealized PnL: --")
        self._rpnl_label = self._make_label("Session PnL (strategy): 0.00")
        balance_row.addWidget(self._bal_label)
        balance_row.addWidget(self._avail_label)
        balance_row.addWidget(self._pnl_label)
        balance_row.addWidget(self._rpnl_label)
        balance_row.addStretch()
        content_layout.addLayout(balance_row)

        # ---- Strategy stats row (Phase 8B) ----
        stats_row = QHBoxLayout()
        stats_row.setContentsMargins(4, 0, 4, 2)
        self._mode_label = self._make_label(f"Mode: {_MODE_LABEL_OBSERVE}")
        self._trades_label = self._make_label("Strategy Trades: —")
        stats_row.addWidget(self._mode_label)
        stats_row.addWidget(self._trades_label)
        stats_row.addStretch()
        content_layout.addLayout(stats_row)

        # ---- Open positions table ----
        pos_label = QLabel(" Open Positions")
        pos_label.setFont(QFont("Menlo", 8, QFont.Bold))
        pos_label.setStyleSheet("color: #8888aa; background: #121220; padding: 2px;")
        content_layout.addWidget(pos_label)

        self._pos_table = QTableWidget()
        self._pos_table.setColumnCount(5)
        self._pos_table.setHorizontalHeaderLabels(
            ["Symbol", "Side", "Qty", "Entry Price", "Unrealized PnL"])
        self._pos_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._pos_table.setAlternatingRowColors(True)
        self._pos_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._pos_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._pos_table.verticalHeader().setVisible(False)
        self._pos_table.setMaximumHeight(120)
        self._apply_table_style(self._pos_table)
        content_layout.addWidget(self._pos_table)

        # ---- Recent orders table (Phase 8B: 8 columns, Reason added) ----
        order_label = QLabel(" Recent Orders")
        order_label.setFont(QFont("Menlo", 8, QFont.Bold))
        order_label.setStyleSheet("color: #8888aa; background: #121220; padding: 2px;")
        content_layout.addWidget(order_label)

        self._order_table = QTableWidget()
        self._order_table.setColumnCount(len(_ORDER_COLUMNS))
        self._order_table.setHorizontalHeaderLabels(_ORDER_COLUMNS)
        self._order_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._order_table.setAlternatingRowColors(True)
        self._order_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._order_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._order_table.verticalHeader().setVisible(False)
        self._apply_table_style(self._order_table)
        content_layout.addWidget(self._order_table)

        self._content.setVisible(False)
        layout.addWidget(self._content)

        self._max_order_rows = 200

    def set_connected(self, connected: bool):
        if connected == self._connected:
            return
        self._connected = connected
        self._placeholder.setVisible(not connected)
        self._content.setVisible(connected)

    def update_account(self, balance, available, positions):
        if not self._connected:
            self.set_connected(True)
        self._bal_label.setText(f"Balance: {balance:.2f}")
        self._avail_label.setText(f"Available: {available:.2f}")

        total_upnl = sum(p.unrealized_pnl for p in positions)
        color = "#1ea05a" if total_upnl >= 0 else "#cc3c3c"
        self._pnl_label.setText(f"Unrealized PnL: {total_upnl:.2f}")
        self._pnl_label.setStyleSheet(
            f"color: {color}; font-family: Menlo; font-size: 11px; padding: 2px 6px;"
        )

        self._pos_table.setRowCount(0)
        for pos in positions:
            row = self._pos_table.rowCount()
            self._pos_table.insertRow(row)
            is_long = pos.side.value == "BUY"
            side_color = QColor(30, 200, 100) if is_long else QColor(255, 80, 80)
            pnl_color = QColor(30, 200, 100) if pos.unrealized_pnl >= 0 else QColor(255, 80, 80)

            items = [
                (pos.symbol, None),
                (pos.side.value, side_color),
                (f"{pos.quantity:.6f}", None),
                (f"{pos.entry_price:.2f}", None),
                (f"{pos.unrealized_pnl:.2f}", pnl_color),
            ]
            for col, (text, clr) in enumerate(items):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignCenter)
                if clr:
                    item.setForeground(clr)
                self._pos_table.setItem(row, col, item)

    def update_strategy_stats(
        self,
        side,
        qty: float,
        entry_price: float,
        upnl: float,
        realized_pnl: float,
        trade_count: int,
        mode_label: str,
    ) -> None:
        """Update strategy-attributed stats (Phase 8B).

        Called on each timer tick by ``MainWindow._on_timer_tick``.
        ``mode_label`` must be one of ``_MODE_LABEL_PAPER``,
        ``_MODE_LABEL_LIVE``, or ``_MODE_LABEL_OBSERVE``.

        When ``mode_label == _MODE_LABEL_OBSERVE`` the panel shows a
        clean zero-state ("Strategy not armed") instead of stale data.
        """
        self._mode_label.setText(f"Mode: {mode_label}")

        if mode_label == _MODE_LABEL_OBSERVE:
            self._rpnl_label.setText(
                f"Session PnL (strategy): {_STRATEGY_NOT_ARMED}")
            self._rpnl_label.setStyleSheet(
                "color: #555570; font-family: Menlo; font-size: 11px; padding: 2px 6px;"
            )
            self._trades_label.setText("Strategy Trades: —")
        else:
            color = "#1ea05a" if realized_pnl >= 0 else "#cc3c3c"
            self._rpnl_label.setText(
                f"Session PnL (strategy): {realized_pnl:+.2f}")
            self._rpnl_label.setStyleSheet(
                f"color: {color}; font-family: Menlo; font-size: 11px; padding: 2px 6px;"
            )
            self._trades_label.setText(f"Strategy Trades: {trade_count}")

    def add_order(self, order):
        """Add an order row to the recent-orders table.

        Phase 8B: per-order PnL is no longer computed here via FIFO
        matching.  The session PnL is driven exclusively by
        ``update_strategy_stats()``.  The ``Reason`` column is
        populated from ``order.ripple_reason``.
        """
        from datetime import datetime, timezone
        row = self._order_table.rowCount()
        self._order_table.insertRow(row)

        dt = datetime.fromtimestamp(order.timestamp, tz=timezone.utc)
        time_str = dt.strftime("%H:%M:%S")

        is_buy = order.side.value == "BUY"
        side_color = QColor(30, 200, 100) if is_buy else QColor(255, 80, 80)

        status = order.status.value
        if status == "FILLED":
            status_color = QColor(30, 200, 100)
        elif status == "REJECTED":
            status_color = QColor(255, 80, 80)
        else:
            status_color = QColor(200, 200, 50)

        fill_price = order.fill_price
        fill_qty = order.fill_quantity
        reason = getattr(order, "ripple_reason", "") or ""

        items = [
            (time_str, None),
            (order.side.value, side_color),
            (f"{fill_qty:.6f}", None),
            (f"{fill_price:.2f}", None),
            ("--", None),
            (status, status_color),
            (order.signal_type, None),
            (reason, QColor(160, 160, 200) if reason else None),
        ]
        for col, (text, clr) in enumerate(items):
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignCenter)
            if clr:
                item.setForeground(clr)
            self._order_table.setItem(row, col, item)

        self._order_table.scrollToBottom()
        if self._order_table.rowCount() > self._max_order_rows:
            self._order_table.removeRow(0)

    def clear(self):
        self._pos_table.setRowCount(0)
        self._order_table.setRowCount(0)
        self._bal_label.setText("Balance: --")
        self._avail_label.setText("Available: --")
        self._pnl_label.setText("Unrealized PnL: --")
        self._rpnl_label.setText("Session PnL (strategy): 0.00")
        self._rpnl_label.setStyleSheet(
            "color: #b4b4c8; font-family: Menlo; font-size: 11px; padding: 2px 6px;"
        )
        self._mode_label.setText(f"Mode: {_MODE_LABEL_OBSERVE}")
        self._trades_label.setText("Strategy Trades: —")
        self.set_connected(False)

    @staticmethod
    def _make_label(text):
        lbl = QLabel(text)
        lbl.setFont(QFont("Menlo", 11))
        lbl.setStyleSheet("color: #b4b4c8; padding: 2px 6px;")
        return lbl

    @staticmethod
    def _apply_table_style(table):
        table.setStyleSheet("""
            QTableWidget {
                background-color: #0f0f19;
                color: #b4b4c8;
                gridline-color: #282838;
                font-family: Menlo;
                font-size: 11px;
            }
            QTableWidget::item:selected {
                background-color: #2a2a4a;
            }
            QHeaderView::section {
                background-color: #1a1a2e;
                color: #b4b4c8;
                padding: 3px;
                border: 1px solid #282838;
                font-weight: bold;
                font-size: 10px;
            }
        """)
