import math
from collections import deque

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QCheckBox, QComboBox,
    QLabel, QPushButton, QSpinBox, QDoubleSpinBox, QSlider,
)
from PySide6.QtCore import Qt, QRectF, QSettings
from PySide6.QtGui import QPainter, QColor, QFont, QPen, QPainterPath

# --------------- configuration ---------------
CVD_AXIS_TICKS = 5
CVD_AXIS_PADDING_PCT = 0.10
SHOW_CVD_GRID_LINES = True


class CVDWidget(QWidget):
    """Professional order-flow CVD panel with delta histogram,
    cumulative volume delta line, and rolling mean."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(400, 100)

        self._s = self._load_settings()

            # ---- shared time reference from heatmap ----
        self._ref_now = 0
        self._ref_window_ms = 60_000
        self._ref_right_inset = 0

        # ---- incremental data ----
        self._raw = deque(maxlen=200_000)
        self._bins = deque(maxlen=50_000)
        self._session_cvd = 0.0
        self._cvd_offset = 0.0
        self._cb_ts = -1
        self._cb_d = 0.0
        self._cb_b = 0.0
        self._cb_s = 0.0

        # ---- layout ----
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._ctrl = self._build_controls()
        self._ctrl.setVisible(False)
        lay.addWidget(self._ctrl)

        self._chart = _CVDChart(self)
        lay.addWidget(self._chart, 1)

    # ---------------------------------------------------------------- defaults

    @staticmethod
    def _defaults():
        return {
            "visible_window_ms": 60_000,
            "delta_bin_ms": 500,
            "rolling_window": 50,
            "show_histogram": True,
            "show_cvd_line": True,
            "show_rolling_mean": True,
            "cvd_mode": "rebased",
            "smoothing_source": "cvd",
            "smoothing_type": "ema",
            "histogram_opacity": 0.8,
            "histogram_min_bar_px": 1,
            "cvd_line_width": 2.0,
            "mean_line_width": 1.5,
        }

    def _load_settings(self):
        d = self._defaults()
        qs = QSettings("OrderFlowTrading", "CVDPanel")
        for key, default in d.items():
            v = qs.value(f"cvd/{key}")
            if v is None:
                continue
            try:
                if isinstance(default, bool):
                    d[key] = v if isinstance(v, bool) else str(v).lower() in ("true", "1")
                elif isinstance(default, int):
                    d[key] = int(float(v))
                elif isinstance(default, float):
                    d[key] = float(v)
                else:
                    d[key] = str(v)
            except (ValueError, TypeError):
                pass
        return d

    def _save(self):
        qs = QSettings("OrderFlowTrading", "CVDPanel")
        for k, v in self._s.items():
            qs.setValue(f"cvd/{k}", v)

    # ---------------------------------------------------------------- controls

    def _build_controls(self):
        w = QWidget()
        w.setMaximumHeight(60)
        w.setStyleSheet(
            "QWidget{background:#14162a}"
            "QCheckBox{color:#8890b0;font-size:10px;spacing:2px}"
            "QCheckBox::indicator{width:12px;height:12px}"
            "QLabel{color:#606888;font-size:10px}"
            "QComboBox{background:#0e1020;color:#8890b0;border:1px solid #252840;"
            "font-size:10px;padding:1px 4px;max-width:80px}"
            "QComboBox::drop-down{border:none}"
            "QComboBox QAbstractItemView{background:#14162a;color:#8890b0}"
            "QSpinBox,QDoubleSpinBox{background:#0e1020;color:#8890b0;"
            "border:1px solid #252840;font-size:10px;padding:1px;max-width:50px}"
            "QPushButton{background:#1e2a4a;color:#8890b0;border:1px solid #2a3660;"
            "font-size:10px;padding:2px 8px}"
            "QPushButton:hover{background:#2a3a6a}"
            "QSlider::groove:horizontal{height:4px;background:#252840}"
            "QSlider::handle:horizontal{width:10px;margin:-3px 0;"
            "background:#4a5a90;border-radius:5px}"
        )

        vl = QVBoxLayout(w)
        vl.setContentsMargins(6, 3, 6, 3)
        vl.setSpacing(2)

        # ---- row 1 ----
        r1 = QHBoxLayout()
        r1.setSpacing(8)

        self._chk_hist = QCheckBox("Hist")
        self._chk_hist.setChecked(self._s["show_histogram"])
        self._chk_hist.toggled.connect(lambda v: self._set("show_histogram", v))
        r1.addWidget(self._chk_hist)

        self._chk_cvd = QCheckBox("CVD")
        self._chk_cvd.setChecked(self._s["show_cvd_line"])
        self._chk_cvd.toggled.connect(lambda v: self._set("show_cvd_line", v))
        r1.addWidget(self._chk_cvd)

        self._chk_mean = QCheckBox("Mean")
        self._chk_mean.setChecked(self._s["show_rolling_mean"])
        self._chk_mean.toggled.connect(lambda v: self._set("show_rolling_mean", v))
        r1.addWidget(self._chk_mean)

        r1.addWidget(QLabel("Mode:"))
        self._mode_cb = QComboBox()
        self._mode_cb.addItem("Rebased", "rebased")
        self._mode_cb.addItem("Session", "session")
        self._mode_cb.setCurrentIndex(0 if self._s["cvd_mode"] == "rebased" else 1)
        self._mode_cb.currentIndexChanged.connect(
            lambda _: self._set("cvd_mode", self._mode_cb.currentData())
        )
        r1.addWidget(self._mode_cb)

        r1.addWidget(QLabel("Bin:"))
        self._bin_cb = QComboBox()
        for lbl, v in [("100ms", 100), ("250ms", 250), ("500ms", 500),
                        ("1s", 1000), ("5s", 5000)]:
            self._bin_cb.addItem(lbl, v)
        idx_map = {100: 0, 250: 1, 500: 2, 1000: 3, 5000: 4}
        self._bin_cb.setCurrentIndex(idx_map.get(self._s["delta_bin_ms"], 2))
        self._bin_cb.currentIndexChanged.connect(self._on_bin_changed)
        r1.addWidget(self._bin_cb)

        btn = QPushButton("Reset CVD")
        btn.clicked.connect(self._on_reset)
        r1.addWidget(btn)

        r1.addStretch()
        vl.addLayout(r1)

        # ---- row 2 ----
        r2 = QHBoxLayout()
        r2.setSpacing(8)

        r2.addWidget(QLabel("Rolling:"))
        self._win_spin = QSpinBox()
        self._win_spin.setRange(2, 500)
        self._win_spin.setValue(self._s["rolling_window"])
        self._win_spin.valueChanged.connect(lambda v: self._set("rolling_window", v))
        r2.addWidget(self._win_spin)

        r2.addWidget(QLabel("Src:"))
        self._src_cb = QComboBox()
        self._src_cb.addItem("CVD", "cvd")
        self._src_cb.addItem("Delta", "delta")
        self._src_cb.setCurrentIndex(0 if self._s["smoothing_source"] == "cvd" else 1)
        self._src_cb.currentIndexChanged.connect(
            lambda _: self._set("smoothing_source", self._src_cb.currentData())
        )
        r2.addWidget(self._src_cb)

        r2.addWidget(QLabel("Type:"))
        self._type_cb = QComboBox()
        self._type_cb.addItem("EMA", "ema")
        self._type_cb.addItem("SMA", "sma")
        self._type_cb.setCurrentIndex(0 if self._s["smoothing_type"] == "ema" else 1)
        self._type_cb.currentIndexChanged.connect(
            lambda _: self._set("smoothing_type", self._type_cb.currentData())
        )
        r2.addWidget(self._type_cb)

        r2.addWidget(QLabel("Opacity:"))
        self._opacity_sl = QSlider(Qt.Horizontal)
        self._opacity_sl.setRange(10, 100)
        self._opacity_sl.setValue(int(self._s["histogram_opacity"] * 100))
        self._opacity_sl.setMaximumWidth(60)
        self._opacity_sl.valueChanged.connect(
            lambda v: self._set("histogram_opacity", v / 100.0)
        )
        r2.addWidget(self._opacity_sl)

        r2.addWidget(QLabel("CVD lw:"))
        self._cvd_lw = QDoubleSpinBox()
        self._cvd_lw.setRange(0.5, 5.0)
        self._cvd_lw.setSingleStep(0.5)
        self._cvd_lw.setValue(self._s["cvd_line_width"])
        self._cvd_lw.valueChanged.connect(lambda v: self._set("cvd_line_width", v))
        r2.addWidget(self._cvd_lw)

        r2.addWidget(QLabel("Mean lw:"))
        self._mean_lw = QDoubleSpinBox()
        self._mean_lw.setRange(0.5, 5.0)
        self._mean_lw.setSingleStep(0.5)
        self._mean_lw.setValue(self._s["mean_line_width"])
        self._mean_lw.valueChanged.connect(lambda v: self._set("mean_line_width", v))
        r2.addWidget(self._mean_lw)

        r2.addStretch()
        vl.addLayout(r2)

        return w

    def _set(self, key, val):
        self._s[key] = val
        self._save()
        self._chart.update()

    def _on_bin_changed(self, _idx=None):
        self._s["delta_bin_ms"] = self._bin_cb.currentData()
        self._rebuild_bins()
        self._save()
        self._chart.update()

    def _on_reset(self):
        self._cvd_offset = self._session_cvd
        self._chart.update()

    def toggle_settings(self):
        self._ctrl.setVisible(not self._ctrl.isVisible())

    # ---------------------------------------------------------------- time ref

    def set_time_ref(self, now_ts, visible_window_ms, right_inset_px=0):
        """Set shared time reference from the heatmap for x-axis alignment."""
        self._ref_now = now_ts
        self._ref_window_ms = visible_window_ms
        self._ref_right_inset = right_inset_px

    # ---------------------------------------------------------------- data

    def add_trade(self, ts, _price, qty, is_buy):
        """Ingest a single trade and update bins incrementally."""
        self._raw.append((ts, qty, is_buy))

        delta = qty if is_buy else -qty
        bin_ms = max(self._s["delta_bin_ms"], 1)
        bstart = (ts // bin_ms) * bin_ms

        if bstart != self._cb_ts:
            if self._cb_ts >= 0:
                self._bins.append((
                    self._cb_ts, self._cb_d,
                    self._cb_b, self._cb_s,
                    self._session_cvd,
                ))
            self._cb_ts = bstart
            self._cb_d = 0.0
            self._cb_b = 0.0
            self._cb_s = 0.0

        self._session_cvd += delta
        self._cb_d += delta
        if is_buy:
            self._cb_b += qty
        else:
            self._cb_s += qty

    def refresh(self):
        """Called by the timer to prune old data and repaint."""
        vis_ms = self._ref_window_ms if self._ref_now > 0 else self._s["visible_window_ms"]
        if self._bins:
            now = max(self._cb_ts, self._bins[-1][0]) if self._bins else self._cb_ts
            cutoff = now - vis_ms * 3
            while self._bins and self._bins[0][0] < cutoff:
                self._bins.popleft()
        self._chart.update()

    def _rebuild_bins(self):
        """Re-bin all raw trades after a bin-size change."""
        self._bins.clear()
        self._session_cvd = 0.0
        self._cvd_offset = 0.0
        self._cb_ts = -1
        self._cb_d = self._cb_b = self._cb_s = 0.0

        bin_ms = max(self._s["delta_bin_ms"], 1)
        for ts, qty, is_buy in self._raw:
            delta = qty if is_buy else -qty
            bstart = (ts // bin_ms) * bin_ms

            if bstart != self._cb_ts:
                if self._cb_ts >= 0:
                    self._bins.append((
                        self._cb_ts, self._cb_d,
                        self._cb_b, self._cb_s,
                        self._session_cvd,
                    ))
                self._cb_ts = bstart
                self._cb_d = self._cb_b = self._cb_s = 0.0

            self._session_cvd += delta
            self._cb_d += delta
            if is_buy:
                self._cb_b += qty
            else:
                self._cb_s += qty

    def _all_bins(self):
        """Return finalized bins + current incomplete bin."""
        out = list(self._bins)
        if self._cb_ts >= 0:
            out.append((
                self._cb_ts, self._cb_d,
                self._cb_b, self._cb_s,
                self._session_cvd,
            ))
        return out


# ====================================================================
# Inner chart widget — rendering only
# ====================================================================

class _CVDChart(QWidget):

    def __init__(self, parent_cvd):
        super().__init__(parent_cvd)
        self._w = parent_cvd
        self.setMouseTracking(True)

        self._bg = QColor(12, 14, 28)
        self._text = QColor(100, 105, 125)
        self._grid = QColor(25, 28, 45)
        self._tick_grid = QColor(30, 35, 55)
        self._zero_c = QColor(60, 65, 85)
        self._cvd_c = QColor(80, 200, 240)
        self._mean_c = QColor(225, 195, 55)
        self._pos_c = QColor(40, 170, 85)
        self._neg_c = QColor(195, 60, 60)

        self._ml = 70
        self._mr = 10
        self._mt = 4
        self._mb = 18

        self._font_tick = QFont("Menlo", 7)
        self._font_label = QFont("Menlo", 8)
        self._font_gear = QFont("Menlo", 11)
        self._gear_color = QColor(70, 75, 100)

    # ------------------------------------------------------------- paint

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), self._bg)

        all_bins = self._w._all_bins()
        s = self._w._s

        if len(all_bins) < 2:
            painter.setPen(self._text)
            painter.drawText(self.rect(), Qt.AlignCenter, "CVD \u2014 Waiting for data\u2026")
            self._draw_gear(painter)
            painter.end()
            return

        # ---- time reference (shared from heatmap or fallback) ----
        if self._w._ref_now > 0:
            now = self._w._ref_now
            vis_ms = self._w._ref_window_ms
        else:
            now = all_bins[-1][0]
            vis_ms = s["visible_window_ms"]

        t_start = now - vis_ms
        ts_span = float(vis_ms)
        if ts_span <= 0:
            painter.end()
            return

        px, py = self._ml, self._mt
        pw_full = self.width() - self._ml - self._mr
        ph = self.height() - self._mt - self._mb
        pw = pw_full - self._w._ref_right_inset
        if pw <= 0 or ph <= 0:
            painter.end()
            return

        inv_ts = pw / ts_span

        # ---- visible bins ----
        first_vis = 0
        for i, b in enumerate(all_bins):
            if b[0] >= t_start:
                first_vis = i
                break

        vis = all_bins[first_vis:]
        if len(vis) < 2:
            painter.setPen(self._text)
            painter.drawText(self.rect(), Qt.AlignCenter, "CVD \u2014 Waiting for data\u2026")
            self._draw_gear(painter)
            painter.end()
            return

        # ---- CVD values ----
        if s["cvd_mode"] == "rebased":
            cvd_base = vis[0][4]
        else:
            cvd_base = self._w._cvd_offset

        cvd_pts = [(b[0], b[4] - cvd_base) for b in vis]

        # ---- delta histogram values ----
        deltas = [(b[0], b[1]) for b in vis]

        # ---- rolling mean ----
        mean_pts = []
        if s["show_rolling_mean"]:
            mean_pts = self._compute_mean(all_bins, first_vis, s, cvd_base)

        # ---- unified y-scale from all visible series ----
        all_vals = []
        if s["show_cvd_line"]:
            all_vals.extend(v for _, v in cvd_pts)
        if s["show_rolling_mean"] and mean_pts:
            all_vals.extend(v for _, v in mean_pts)
        if s["show_histogram"]:
            all_vals.extend(d for _, d in deltas)
        if not all_vals:
            all_vals = [0.0]

        val_min = min(all_vals)
        val_max = max(all_vals)

        if s["show_histogram"]:
            val_min = min(val_min, 0.0)
            val_max = max(val_max, 0.0)

        span = val_max - val_min
        if span <= 0:
            span = 1.0
        pad = span * CVD_AXIS_PADDING_PCT
        d_min = val_min - pad
        d_max = val_max + pad
        d_range = d_max - d_min
        if d_range <= 0:
            d_range = 1.0

        inv_dr = ph / d_range

        def yp(value):
            return py + ph - (value - d_min) * inv_dr

        zero_y = yp(0.0)
        bin_ms = s["delta_bin_ms"]
        bar_w = max(1.0, bin_ms * inv_ts - 1)
        hist_alpha = int(s["histogram_opacity"] * 255)
        min_bar_px = s["histogram_min_bar_px"]

        # Clip to plot area so bars don't bleed into the right inset.
        painter.save()
        painter.setClipRect(QRectF(px, py, pw, ph))

        # 1. Y-axis grid lines + tick labels
        if SHOW_CVD_GRID_LINES:
            ticks, step = self._nice_ticks(d_min, d_max, CVD_AXIS_TICKS)
            painter.setFont(self._font_tick)
            for tick in ticks:
                y = yp(tick)
                if y < py - 2 or y > py + ph + 2:
                    continue
                is_zero = abs(tick) < step * 0.01
                if is_zero:
                    continue
                painter.setPen(QPen(self._tick_grid, 0.5, Qt.DotLine))
                painter.drawLine(int(px), int(y), int(px + pw), int(y))
                painter.setPen(self._text)
                painter.drawText(2, int(y + 4), self._fmt(tick, step))

        # 2. Histogram
        if s["show_histogram"]:
            pos_c = QColor(self._pos_c.red(), self._pos_c.green(),
                           self._pos_c.blue(), hist_alpha)
            neg_c = QColor(self._neg_c.red(), self._neg_c.green(),
                           self._neg_c.blue(), hist_alpha)
            painter.setPen(Qt.NoPen)

            for t, d in deltas:
                x = px + (t - t_start) * inv_ts
                if x + bar_w < px or x > px + pw:
                    continue
                d_y = yp(d)
                if d >= 0:
                    raw_h = zero_y - d_y
                    h = max(min_bar_px, raw_h)
                    painter.setBrush(pos_c)
                    painter.drawRect(QRectF(x, zero_y - h, bar_w, h))
                else:
                    raw_h = d_y - zero_y
                    h = max(min_bar_px, raw_h)
                    painter.setBrush(neg_c)
                    painter.drawRect(QRectF(x, zero_y, bar_w, h))

        # 3. Zero line
        if py - 2 <= zero_y <= py + ph + 2:
            painter.setPen(QPen(self._zero_c, 1, Qt.DashLine))
            painter.drawLine(int(px), int(zero_y), int(px + pw), int(zero_y))
            painter.setFont(self._font_tick)
            painter.setPen(self._text)
            painter.drawText(2, int(zero_y + 4), "0")

        # 4. Rolling mean
        if s["show_rolling_mean"] and len(mean_pts) >= 2:
            mean_path = QPainterPath()
            started = False
            for t, v in mean_pts:
                x = px + (t - t_start) * inv_ts
                y = yp(v)
                if not started:
                    mean_path.moveTo(x, y)
                    started = True
                else:
                    mean_path.lineTo(x, y)
            painter.setPen(QPen(self._mean_c, s["mean_line_width"]))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(mean_path)

        # 5. CVD line
        if s["show_cvd_line"] and len(cvd_pts) >= 2:
            cvd_path = QPainterPath()
            started = False
            for t, v in cvd_pts:
                x = px + (t - t_start) * inv_ts
                y = yp(v)
                if not started:
                    cvd_path.moveTo(x, y)
                    started = True
                else:
                    cvd_path.lineTo(x, y)
            painter.setPen(QPen(self._cvd_c, s["cvd_line_width"]))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(cvd_path)

        painter.restore()

        # 6. Axes border
        painter.setPen(QPen(self._grid, 1))
        painter.drawLine(int(px), int(py), int(px), int(py + ph))
        painter.drawLine(int(px), int(py + ph), int(px + pw), int(py + ph))

        # 7. Current value labels
        cur_cvd = cvd_pts[-1][1] if cvd_pts else 0
        cur_d = deltas[-1][1] if deltas else 0
        c = self._pos_c if cur_cvd >= 0 else self._neg_c
        painter.setPen(QPen(c, 1))
        painter.setFont(self._font_label)
        painter.drawText(
            int(px + pw - 155), int(py + 12),
            f"CVD: {cur_cvd:+.1f}  \u0394: {cur_d:+.2f}",
        )

        self._draw_gear(painter)
        painter.end()

    # ------------------------------------------------------------- helpers

    def _draw_gear(self, painter):
        painter.setFont(self._font_gear)
        painter.setPen(self._gear_color)
        painter.drawText(self.width() - 20, 15, "\u2699")

    @staticmethod
    def _nice_ticks(lo, hi, target=5):
        """Compute evenly-spaced nice tick values between lo and hi."""
        span = hi - lo
        if span <= 1e-12:
            return [lo], 1.0
        raw = span / max(target, 1)
        mag = 10 ** math.floor(math.log10(max(abs(raw), 1e-15)))
        r = raw / mag
        if r <= 1.5:
            step = mag
        elif r <= 3.5:
            step = 2 * mag
        elif r <= 7.5:
            step = 5 * mag
        else:
            step = 10 * mag
        first = math.ceil(lo / step) * step
        ticks = []
        v = first
        safety = 0
        while v <= hi + step * 0.001 and safety < 20:
            ticks.append(round(v, 10))
            v += step
            safety += 1
        return ticks, step

    @staticmethod
    def _fmt(v, step):
        """Format a tick value with precision appropriate for the step size."""
        if step >= 10:
            return f"{v:.0f}"
        if step >= 0.1:
            return f"{v:.1f}"
        return f"{v:.2f}"

    @staticmethod
    def _compute_mean(all_bins, first_vis, s, cvd_base):
        window = s["rolling_window"]
        source = s["smoothing_source"]
        stype = s["smoothing_type"]

        if source == "cvd":
            vals = [b[4] - cvd_base for b in all_bins]
        else:
            vals = [b[1] for b in all_bins]

        n = len(vals)
        if n == 0:
            return []

        smoothed = [0.0] * n

        if stype == "ema":
            alpha = 2.0 / (window + 1)
            smoothed[0] = vals[0]
            for i in range(1, n):
                smoothed[i] = alpha * vals[i] + (1.0 - alpha) * smoothed[i - 1]
        else:
            running = 0.0
            for i in range(n):
                running += vals[i]
                if i >= window:
                    running -= vals[i - window]
                count = min(i + 1, window)
                smoothed[i] = running / count

        return [(all_bins[i][0], smoothed[i]) for i in range(first_vis, n)]

    def mousePressEvent(self, event):
        pos = event.position()
        if pos.x() > self.width() - 25 and pos.y() < 20:
            self._w.toggle_settings()
        super().mousePressEvent(event)
