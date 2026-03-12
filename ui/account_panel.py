from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QHeaderView,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QColor


class AccountPanel(QWidget):
    """Shows account balance, open positions, and order history."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(400, 150)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        header = QLabel("Account / Execution")
        header.setFont(QFont("Menlo", 9, QFont.Bold))
        header.setStyleSheet("color: #b4b4c8; background: #0f0f19; padding: 4px;")
        layout.addWidget(header)

        balance_row = QHBoxLayout()
        balance_row.setContentsMargins(4, 2, 4, 2)
        self._bal_label = self._make_label("Balance: --")
        self._avail_label = self._make_label("Available: --")
        self._pnl_label = self._make_label("Unrealized PnL: --")
        self._rpnl_label = self._make_label("Realized PnL: 0.00")
        balance_row.addWidget(self._bal_label)
        balance_row.addWidget(self._avail_label)
        balance_row.addWidget(self._pnl_label)
        balance_row.addWidget(self._rpnl_label)
        balance_row.addStretch()
        layout.addLayout(balance_row)

        pos_label = QLabel(" Open Positions")
        pos_label.setFont(QFont("Menlo", 8, QFont.Bold))
        pos_label.setStyleSheet("color: #8888aa; background: #121220; padding: 2px;")
        layout.addWidget(pos_label)

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
        layout.addWidget(self._pos_table)

        order_label = QLabel(" Recent Orders")
        order_label.setFont(QFont("Menlo", 8, QFont.Bold))
        order_label.setStyleSheet("color: #8888aa; background: #121220; padding: 2px;")
        layout.addWidget(order_label)

        self._last_entry_price = 0.0
        self._last_entry_side = None
        self._realized_pnl = 0.0

        self._order_table = QTableWidget()
        self._order_table.setColumnCount(7)
        self._order_table.setHorizontalHeaderLabels(
            ["Time", "Side", "Qty", "Price", "PnL", "Status", "Signal"])
        self._order_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._order_table.setAlternatingRowColors(True)
        self._order_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._order_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._order_table.verticalHeader().setVisible(False)
        self._apply_table_style(self._order_table)
        layout.addWidget(self._order_table)

        self._max_order_rows = 200

    def update_account(self, balance, available, positions):
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

    def add_order(self, order):
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

        order_pnl = 0.0
        pnl_text = ""
        pnl_color = None
        fill_price = order.fill_price
        fill_qty = order.fill_quantity

        if status == "FILLED" and fill_price > 0 and fill_qty > 0:
            if self._last_entry_price > 0 and self._last_entry_side is not None:
                is_closing = (self._last_entry_side == "BUY" and not is_buy) or \
                             (self._last_entry_side == "SELL" and is_buy)
                if is_closing:
                    if self._last_entry_side == "BUY":
                        order_pnl = (fill_price - self._last_entry_price) * fill_qty
                    else:
                        order_pnl = (self._last_entry_price - fill_price) * fill_qty
                    self._realized_pnl += order_pnl
                    pnl_text = f"{order_pnl:+.2f}"
                    pnl_color = QColor(30, 200, 100) if order_pnl >= 0 else QColor(255, 80, 80)
                    self._last_entry_price = 0.0
                    self._last_entry_side = None
                    self._update_rpnl()
                else:
                    self._last_entry_price = fill_price
                    self._last_entry_side = order.side.value
                    pnl_text = "ENTRY"
                    pnl_color = QColor(120, 120, 160)
            else:
                self._last_entry_price = fill_price
                self._last_entry_side = order.side.value
                pnl_text = "ENTRY"
                pnl_color = QColor(120, 120, 160)

        items = [
            (time_str, None),
            (order.side.value, side_color),
            (f"{fill_qty:.6f}", None),
            (f"{fill_price:.2f}", None),
            (pnl_text, pnl_color),
            (status, status_color),
            (order.signal_type, None),
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

    def _update_rpnl(self):
        color = "#1ea05a" if self._realized_pnl >= 0 else "#cc3c3c"
        self._rpnl_label.setText(f"Realized PnL: {self._realized_pnl:.2f}")
        self._rpnl_label.setStyleSheet(
            f"color: {color}; font-family: Menlo; font-size: 11px; padding: 2px 6px;"
        )

    def clear(self):
        self._pos_table.setRowCount(0)
        self._order_table.setRowCount(0)
        self._bal_label.setText("Balance: --")
        self._avail_label.setText("Available: --")
        self._pnl_label.setText("Unrealized PnL: --")
        self._realized_pnl = 0.0
        self._last_entry_price = 0.0
        self._last_entry_side = None
        self._rpnl_label.setText("Realized PnL: 0.00")
        self._rpnl_label.setStyleSheet(
            "color: #b4b4c8; font-family: Menlo; font-size: 11px; padding: 2px 6px;"
        )

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
