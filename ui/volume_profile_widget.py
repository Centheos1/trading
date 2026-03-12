import math

from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QPainter, QColor, QFont, QPen

# --------------- configuration ---------------
PROFILE_MODE = "split"            # "total" or "split"
PROFILE_SQRT_SCALING = True
PROFILE_MIN_ALPHA = 15            # ~0.06 * 255
PROFILE_BAR_HEIGHT_FRAC = 0.80
SHOW_POC = True
SHOW_VALUE_AREA = False


class VolumeProfileWidget(QWidget):
    """Bookmap-style side volume profile built from executed trade volume."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(140, 120)
        self.setMaximumWidth(260)

        self._profile = []
        self._poc_price = 0.0
        self._price_min = 0.0
        self._price_max = 0.0

        self._buy_color = QColor(40, 180, 90)
        self._sell_color = QColor(190, 65, 65)
        self._total_color = QColor(195, 165, 50)
        self._poc_color = QColor(255, 200, 50)
        self._bg_color = QColor(12, 14, 28)
        self._text_color = QColor(100, 105, 125)
        self._grid_color = QColor(25, 28, 45)

        self._margin_left = 55
        self._margin_right = 4
        self._margin_top = 10
        self._margin_bottom = 22

        self._font_small = QFont("Menlo", 7)
        self._font_axis = QFont("Menlo", 8)

    def set_profile(self, profile, poc_price, price_min, price_max):
        """Set the volume profile data.

        profile: list of (price, total_vol, buy_vol, sell_vol)
        """
        n = len(profile)
        key = (n, poc_price, price_min, price_max)
        if key == getattr(self, '_profile_key', None):
            return
        self._profile_key = key
        self._profile = profile
        self._poc_price = poc_price
        self._price_min = price_min
        self._price_max = price_max
        self.update()

    # ---------------------------------------------------------------- paint
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), self._bg_color)

        if not self._profile or self._price_max <= self._price_min:
            painter.setPen(self._text_color)
            painter.drawText(self.rect(), Qt.AlignCenter, "No data")
            painter.end()
            return

        px = self._margin_left
        py = self._margin_top
        pw = self.width() - self._margin_left - self._margin_right
        ph = self.height() - self._margin_top - self._margin_bottom

        if pw <= 0 or ph <= 0:
            painter.end()
            return

        pr = self._price_max - self._price_min

        max_vol = max((v[1] for v in self._profile), default=0.0)
        if max_vol <= 0:
            painter.end()
            return

        if PROFILE_SQRT_SCALING:
            norm_max = math.sqrt(max_vol)
        else:
            norm_max = max_vol

        n_bars = len(self._profile)
        raw_bar_h = ph / max(n_bars, 1)
        bar_h = max(1.5, raw_bar_h * PROFILE_BAR_HEIGHT_FRAC)

        inv_pr = 1.0 / pr
        inv_norm = 1.0 / norm_max
        inv_vol = 1.0 / max_vol
        is_split = (PROFILE_MODE == "split")
        is_sqrt = PROFILE_SQRT_SCALING
        half_bar = bar_h * 0.5

        painter.setPen(Qt.NoPen)
        for price, total, buy_v, sell_v in self._profile:
            y = py + ph - (price - self._price_min) * inv_pr * ph

            if y < py - bar_h or y > py + ph + bar_h:
                continue

            if is_sqrt:
                total_w = math.sqrt(max(total, 0.0)) * inv_norm * pw
            else:
                total_w = max(total, 0.0) * inv_norm * pw

            if total_w < 0.3:
                continue

            alpha = min(225, max(PROFILE_MIN_ALPHA,
                                 int(total * inv_vol * 180 + 50)))
            yt = y - half_bar

            if is_split:
                if is_sqrt:
                    buy_w = math.sqrt(max(buy_v, 0.0)) * inv_norm * pw
                    sell_w = math.sqrt(max(sell_v, 0.0)) * inv_norm * pw
                else:
                    buy_w = max(buy_v, 0.0) * inv_norm * pw
                    sell_w = max(sell_v, 0.0) * inv_norm * pw

                bc = QColor(self._buy_color)
                bc.setAlpha(alpha)
                sc = QColor(self._sell_color)
                sc.setAlpha(alpha)

                painter.setBrush(bc)
                painter.drawRect(QRectF(px, yt, buy_w, bar_h))
                painter.setBrush(sc)
                painter.drawRect(QRectF(px + buy_w, yt, sell_w, bar_h))
            else:
                tc = QColor(self._total_color)
                tc.setAlpha(alpha)
                painter.setBrush(tc)
                painter.drawRect(QRectF(px, yt, total_w, bar_h))

        if SHOW_POC and self._poc_price > 0:
            poc_y = py + ph - (self._poc_price - self._price_min) / pr * ph
            if py <= poc_y <= py + ph:
                painter.setPen(QPen(self._poc_color, 1.5, Qt.DashLine))
                painter.drawLine(int(px), int(poc_y), int(px + pw), int(poc_y))

                painter.setFont(self._font_small)
                painter.setPen(self._poc_color)
                painter.drawText(int(px + 2), int(poc_y - 3), "POC")

        self._draw_price_axis(painter, px, py, pw, ph, pr)
        painter.end()

    def _draw_price_axis(self, painter, px, py, pw, ph, pr):
        painter.setFont(self._font_axis)
        painter.setPen(self._text_color)

        n_labels = min(10, int(ph / 40))
        for i in range(n_labels + 1):
            frac = i / max(n_labels, 1)
            price = self._price_min + pr * frac
            y = py + ph - frac * ph

            painter.setPen(self._text_color)
            painter.drawText(2, int(y + 4), f"{price:.2f}")

            painter.setPen(QPen(self._grid_color, 0.5, Qt.DotLine))
            painter.drawLine(int(px), int(y), int(px + pw), int(y))
