"""
Panel 2 — Live Dashboard.
Shows axis positions, velocity, error, torque for all three axes.
Includes a live pyqtgraph position history and limit alert banner.
"""
from collections import deque
import time

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QGroupBox, QLabel, QPushButton, QFrame, QSizePolicy,
)
from PyQt5.QtCore import Qt, pyqtSlot
from PyQt5.QtGui import QFont

try:
    import pyqtgraph as pg
    _HAS_PG = True
except ImportError:
    _HAS_PG = False

from controller.data_models import AxisStatus

AXIS_COLORS = {"A": "#00C9A7", "B": "#9B59B6", "C": "#F0B429"}
AXES = ["A", "B", "C"]

STOP_CODES = {
    0:  ("OFF",      "#8B949E"),
    1:  ("HALT",     "#F0B429"),
    2:  ("ABORT",    "#FF4C4C"),
    3:  ("POS ERR",  "#FF4C4C"),
    6:  ("FWD LIM",  "#FF4C4C"),
    7:  ("REV LIM",  "#FF4C4C"),
    8:  ("HOME SW",  "#F0B429"),
    10: ("DONE",     "#3DBA6F"),
    15: ("MOVING",   "#00C9A7"),
}


class _AxisCard(QGroupBox):
    """One axis status card showing encoder position and motor state."""

    def __init__(self, axis: str, parent=None):
        super().__init__(parent)
        self.axis = axis
        color = AXIS_COLORS[axis]
        self.setStyleSheet(f"QGroupBox {{ border: 1px solid {color}; }}")
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(4)

        # Axis label
        title_row = QHBoxLayout()
        ax_lbl = QLabel(f"Axis {self.axis}")
        ax_lbl.setFont(QFont("Segoe UI", 11, QFont.Bold))
        ax_lbl.setStyleSheet(f"color: {AXIS_COLORS[self.axis]};")
        title_row.addWidget(ax_lbl)
        title_row.addStretch()
        self._servo_badge = QLabel("OFF")
        self._servo_badge.setFont(QFont("Segoe UI", 9, QFont.Bold))
        self._servo_badge.setStyleSheet("color: #8B949E;")
        title_row.addWidget(self._servo_badge)
        layout.addLayout(title_row)

        # Position — large
        self._pos_lbl = QLabel("0.00")
        self._pos_lbl.setFont(QFont("Consolas", 24, QFont.Bold))
        self._pos_lbl.setAlignment(Qt.AlignRight)
        self._pos_lbl.setObjectName("pos_label")
        layout.addWidget(self._pos_lbl)

        units_lbl = QLabel("counts (encoder)")
        units_lbl.setStyleSheet("color: #8B949E; font-size: 8pt;")
        units_lbl.setAlignment(Qt.AlignRight)
        layout.addWidget(units_lbl)
        self._units_lbl = units_lbl

        # Secondary readings
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        labels = [
            ("Velocity",  "vel_lbl",    "counts/s"),
            ("Pos Error", "err_lbl",    "counts"),
            ("Torque",    "torque_lbl", "DAC"),
            ("Ref Pos",   "ref_lbl",    "counts"),
        ]
        for i, (name, attr, unit) in enumerate(labels):
            row, col = divmod(i, 2)
            lbl = QLabel(f"{name}:")
            lbl.setStyleSheet("color: #8B949E; font-size: 9pt;")
            grid.addWidget(lbl, row, col * 3)
            val = QLabel("0.0")
            val.setFont(QFont("Consolas", 10))
            grid.addWidget(val, row, col * 3 + 1)
            setattr(self, f"_{attr}", val)
            u = QLabel(unit)
            u.setStyleSheet("color: #484F58; font-size: 8pt;")
            grid.addWidget(u, row, col * 3 + 2)
        layout.addLayout(grid)

        # Stop code
        sc_row = QHBoxLayout()
        sc_row.addWidget(QLabel("Stop:"))
        self._sc_lbl = QLabel("—")
        self._sc_lbl.setFont(QFont("Consolas", 9))
        sc_row.addWidget(self._sc_lbl)
        sc_row.addStretch()
        layout.addLayout(sc_row)

        # Limit / home LEDs
        led_row = QHBoxLayout()
        self._leds = {}
        for label in ("FWD", "REV", "HOME"):
            col = QVBoxLayout()
            dot = QLabel("●")
            dot.setAlignment(Qt.AlignCenter)
            dot.setObjectName("led_clear")
            txt = QLabel(label)
            txt.setAlignment(Qt.AlignCenter)
            txt.setStyleSheet("font-size: 7pt; color: #8B949E;")
            col.addWidget(dot)
            col.addWidget(txt)
            led_row.addLayout(col)
            self._leds[label] = dot
        layout.addLayout(led_row)

    def update(self, status: AxisStatus, units: str = "counts", cpm: float = 6.4):
        if units == "um":
            pos_val = status.pos / cpm
            ref_val = status.pos_ref / cpm
            err_val = status.err / cpm
            unit_str = "µm (encoder)"
            fmt = ".3f"
        else:
            pos_val = status.pos
            ref_val = status.pos_ref
            err_val = status.err
            unit_str = "counts (encoder)"
            fmt = ".1f"

        self._pos_lbl.setText(f"{pos_val:{fmt}}")
        self._units_lbl.setText(unit_str)
        self._vel_lbl.setText(f"{status.vel:.1f}")
        self._err_lbl.setText(f"{err_val:.2f}")
        self._torque_lbl.setText(f"{status.torque:.3f}")
        self._ref_lbl.setText(f"{ref_val:{fmt}}")

        sc_text, sc_color = STOP_CODES.get(status.stop_code, (str(status.stop_code), "#8B949E"))
        self._sc_lbl.setText(sc_text)
        self._sc_lbl.setStyleSheet(f"color: {sc_color}; font-family: Consolas;")

        if status.servo_on:
            self._servo_badge.setText("ENABLED")
            self._servo_badge.setStyleSheet("color: #3DBA6F; font-weight: bold;")
        else:
            self._servo_badge.setText("OFF")
            self._servo_badge.setStyleSheet("color: #8B949E;")

        def _led(key, active):
            dot = self._leds[key]
            if active:
                dot.setObjectName("led_active")
            else:
                dot.setObjectName("led_clear")
            dot.style().unpolish(dot)
            dot.style().polish(dot)

        _led("FWD",  status.fwd_limit)
        _led("REV",  status.rev_limit)
        _led("HOME", status.home_switch)


class DashboardPanel(QWidget):
    """Panel 2: live dashboard with 3 axis cards + position history chart."""

    halt_requested  = pyqtSlot()
    estop_requested = pyqtSlot()

    def __init__(self, controller, worker, settings, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.worker = worker
        self.settings = settings
        self._limit_dismissed = False
        self._history_depth = settings.position_history_depth
        self._history = {ax: deque([0.0] * self._history_depth,
                                   maxlen=self._history_depth) for ax in AXES}
        self._t_history = deque(range(-self._history_depth, 0),
                                maxlen=self._history_depth)
        self._t0 = time.time()

        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        title = QLabel("LIVE DASHBOARD")
        title.setFont(QFont("Segoe UI", 13, QFont.Bold))
        title.setObjectName("axis_A")
        root.addWidget(title)

        # Limit alert banner (hidden by default)
        self._alert_frame = QFrame()
        self._alert_frame.setObjectName("limit_alert")
        af = QHBoxLayout(self._alert_frame)
        self._alert_lbl = QLabel("LIMIT ACTIVE")
        self._alert_lbl.setStyleSheet("color: #FF4C4C; font-weight: bold;")
        af.addWidget(self._alert_lbl)
        af.addStretch()
        dismiss_btn = QPushButton("Dismiss")
        dismiss_btn.clicked.connect(self._dismiss_alert)
        af.addWidget(dismiss_btn)
        self._alert_frame.setVisible(False)
        root.addWidget(self._alert_frame)

        # Axis cards
        cards_row = QHBoxLayout()
        self._cards: dict[str, _AxisCard] = {}
        for ax in AXES:
            card = _AxisCard(ax)
            self._cards[ax] = card
            cards_row.addWidget(card)
        root.addLayout(cards_row)

        # Live plot
        if _HAS_PG:
            self._plot_widget = pg.PlotWidget(background="#010409")
            self._plot_widget.setLabel("left", "Position (counts)")
            self._plot_widget.setLabel("bottom", "Time (s)")
            self._plot_widget.showGrid(x=True, y=True, alpha=0.15)
            self._plot_widget.setMinimumHeight(160)
            self._curves = {}
            for ax in AXES:
                pen = pg.mkPen(color=AXIS_COLORS[ax], width=2)
                curve = self._plot_widget.plot(
                    list(self._t_history), list(self._history[ax]),
                    pen=pen, name=f"Axis {ax}"
                )
                self._curves[ax] = curve
            legend = self._plot_widget.addLegend(offset=(-10, 10))
            root.addWidget(self._plot_widget)
        else:
            no_pg = QLabel("Install pyqtgraph for live position chart  (pip install pyqtgraph)")
            no_pg.setStyleSheet("color: #8B949E; font-style: italic;")
            root.addWidget(no_pg)
            self._curves = {}

        # Quick actions
        qa_grp = QGroupBox("Quick Actions")
        qa = QHBoxLayout(qa_grp)
        for label, obj_name, fn in [
            ("Enable All",          "btn_enable",  self._enable_all),
            ("Disable All",         "",             self._disable_all),
            ("ST — Stop All",       "btn_halt",     self._halt_all),
            ("AB — Emergency Abort","btn_estop",    self._estop),
        ]:
            btn = QPushButton(label)
            if obj_name:
                btn.setObjectName(obj_name)
            btn.clicked.connect(fn)
            qa.addWidget(btn)
        root.addWidget(qa_grp)

    # ── Slot connected to PollingWorker.status_updated ──────────────────────

    @pyqtSlot(dict)
    def on_status_updated(self, status: dict):
        cpm = self.settings.axis_a.counts_per_um
        units = self.settings.display_units
        now = time.time() - self._t0

        for ax, s in status.items():
            if ax in self._cards:
                self._cards[ax].update(s, units, cpm)
            if ax in self._history:
                self._history[ax].append(s.pos)

        self._t_history.append(now)

        if _HAS_PG and self._curves:
            x = list(self._t_history)
            for ax in AXES:
                self._curves[ax].setData(x, list(self._history[ax]))

        self._check_limits(status)

    def _check_limits(self, status: dict):
        any_active = any(s.fwd_limit or s.rev_limit for s in status.values())
        if any_active and not self._limit_dismissed:
            msgs = []
            for ax, s in status.items():
                if s.fwd_limit:
                    msgs.append(f"Axis {ax} — Forward Limit")
                if s.rev_limit:
                    msgs.append(f"Axis {ax} — Reverse Limit")
            self._alert_lbl.setText("  ⚠  " + "  |  ".join(msgs) + "  — Reverse direction to clear")
            self._alert_frame.setVisible(True)
        elif not any_active:
            self._limit_dismissed = False
            self._alert_frame.setVisible(False)

    def _dismiss_alert(self):
        self._limit_dismissed = True
        self._alert_frame.setVisible(False)

    # ── Quick actions ────────────────────────────────────────────────────────

    def _enable_all(self):
        #self.worker.queue_command(self.controller.configure_stepper_axes, "ABC", 2)
        self.worker.queue_command(self.controller.enable_axes, "ABC")

    def _disable_all(self):
        self.worker.queue_command(self.controller.disable_axes, "ABC")

    def _halt_all(self):
        self.worker.clear_queue()
        self.worker.queue_command(self.controller.halt_all)

    def _estop(self):
        self.worker.clear_queue()
        try:
            self.controller.emergency_stop()
        except Exception:
            pass
