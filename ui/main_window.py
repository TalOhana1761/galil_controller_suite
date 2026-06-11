"""
GalilMainWindow — QMainWindow assembly.
Sidebar navigation + QStackedWidget panels + always-visible bottom bar.

Bottom bar contains:
  HALT (ST)     — decelerated stop, motors remain enabled
  E-STOP (AB+MO) — immediate hard cut, bypasses queue
"""
import sys
import os
import json

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QGridLayout,
    QPushButton, QLabel, QStackedWidget, QFrame,
    QSizePolicy, QStatusBar,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSlot
from PyQt5.QtGui import QFont
from loguru import logger

from controller.data_models import AppSettings
from utils.position_memory import PositionMemory
from ui.polling_worker import PollingWorker

from ui.panels.connection_panel import ConnectionPanel
from ui.panels.jog_panel import JogPanel
from ui.panels.settings_panel import SettingsPanel
from ui.panels.log_panel import LogPanel

POSITION_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config", "last_positions.json")

NAV_ITEMS = [
    ("🔌  Connection",  0),
    ("🕹  Jog Control", 1),
    ("⚙   Settings",   2),
    ("📋  Log",        3),
]


class GalilMainWindow(QMainWindow):
    def __init__(self, controller, use_mock: bool = False):
        super().__init__()
        self.controller = controller
        self.use_mock = use_mock
        self.settings = _load_settings()
        self.memory = PositionMemory()
        self.worker = PollingWorker(controller)
        self._active_nav = 0

        self._build_ui()
        self._connect_signals()

        # If mock: start worker immediately (no connection needed)
        if use_mock:
            self.worker.start()
            self._on_worker_started()

    # ── Layout ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Sidebar
        sidebar = self._build_sidebar()
        main_layout.addWidget(sidebar)

        # Separator line
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setStyleSheet("background-color: #21262D;")
        sep.setMaximumWidth(1)
        main_layout.addWidget(sep)

        # Content area (stack + bottom bar)
        content = QVBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)

        self._stack = QStackedWidget()
        content.addWidget(self._stack, stretch=1)

        # Bottom bar (always visible)
        content.addWidget(self._build_bottom_bar())

        main_layout.addLayout(content, stretch=1)

        # Build all panels
        self._conn_panel = ConnectionPanel(
            self.controller, self.worker, self.settings)
        self._jog_panel  = JogPanel(
            self.controller, self.worker, self.settings, self.memory)
        self._set_panel  = SettingsPanel(
            self.controller, self.worker, self.settings)
        self._log_panel  = LogPanel(
            self.controller, self.worker)

        for panel in [self._conn_panel, self._jog_panel,
                      self._set_panel, self._log_panel]:
            self._stack.addWidget(panel)

        self._nav_buttons[0].click()

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setFixedWidth(180)
        sidebar.setStyleSheet("background-color: #161B22;")
        sl = QVBoxLayout(sidebar)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)

        logo = QLabel("GALIL\nDMC-4133")
        logo.setFont(QFont("Segoe UI", 11, QFont.Bold))
        logo.setStyleSheet("color: #00C9A7; padding: 16px 12px 8px 12px;")
        logo.setAlignment(Qt.AlignCenter)
        sl.addWidget(logo)

        if self.use_mock:
            mock_lbl = QLabel("MOCK MODE")
            mock_lbl.setStyleSheet(
                "color: #F0B429; font-size: 8pt; font-weight: bold;"
                "background-color: #2A2200; padding: 3px; margin: 0 8px;")
            mock_lbl.setAlignment(Qt.AlignCenter)
            sl.addWidget(mock_lbl)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("background-color: #21262D; margin: 4px 0;")
        sep.setMaximumHeight(1)
        sl.addWidget(sep)

        self._nav_buttons: list[QPushButton] = []
        for label, idx in NAV_ITEMS:
            btn = QPushButton(label)
            btn.setObjectName("btn_nav")
            btn.setProperty("active", "false")
            btn.clicked.connect(lambda checked, i=idx: self._navigate(i))
            sl.addWidget(btn)
            self._nav_buttons.append(btn)

        sl.addStretch()

        # Axis mini-status at bottom of sidebar
        self._sidebar_pos = {}
        pos_frame = QFrame()
        pos_frame.setStyleSheet("background-color: #0D1117; margin: 4px;")
        pf = QGridLayout(pos_frame)
        pf.setContentsMargins(6, 6, 6, 6)
        pf.setSpacing(2)
        for i, (ax, color) in enumerate([("A","#00C9A7"),("B","#9B59B6"),("C","#F0B429")]):
            lbl = QLabel(f"{ax}:")
            lbl.setStyleSheet(f"color: {color}; font-weight: bold; font-size: 9pt;")
            pos_v = QLabel("—")
            pos_v.setFont(QFont("Consolas", 9))
            pos_v.setAlignment(Qt.AlignRight)
            pf.addWidget(lbl,  i, 0)
            pf.addWidget(pos_v, i, 1)
            self._sidebar_pos[ax] = pos_v
        sl.addWidget(pos_frame)
        return sidebar

    def _build_bottom_bar(self) -> QWidget:
        bar = QFrame()
        bar.setFixedHeight(52)
        bar.setStyleSheet("background-color: #161B22; border-top: 1px solid #21262D;")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(12, 4, 12, 4)
        bl.setSpacing(8)

        # Status indicator
        self._conn_indicator = QLabel("● MOCK" if self.use_mock else "● OFF")
        self._conn_indicator.setFont(QFont("Consolas", 9))
        self._conn_indicator.setStyleSheet("color: #F0B429;" if self.use_mock else "color: #8B949E;")
        bl.addWidget(self._conn_indicator)
        bl.addStretch()

        # Zero All
        btn_zero = QPushButton("ZERO ALL")
        btn_zero.clicked.connect(lambda: self.worker.queue_command(
            self.controller.define_zero, "ABC"))
        bl.addWidget(btn_zero)

        # Enable / Disable
        btn_enable = QPushButton("Enable Motors")
        btn_enable.setObjectName("btn_enable")
        btn_enable.setToolTip("Enable stepper motors. Stepper mode does not use Galil SH servo-enable.")
        btn_enable.clicked.connect(lambda: (
            #self.worker.queue_command(self.controller.configure_stepper_axes, "ABC", 2),
            self.worker.queue_command(self.controller.enable_axes, "ABC")
        ))
        bl.addWidget(btn_enable)

        btn_disable = QPushButton("Disable Motors")
        btn_disable.setToolTip("Disable motor outputs using MO.")
        btn_disable.clicked.connect(lambda: self.worker.queue_command(
            self.controller.disable_axes, "ABC"))
        bl.addWidget(btn_disable)

        bl.addSpacing(16)

        # HALT — ST: decelerated stop, motors remain enabled
        self.btn_halt = QPushButton("◼  HALT")
        self.btn_halt.setObjectName("btn_halt")
        self.btn_halt.setMinimumWidth(100)
        self.btn_halt.setToolTip(
            "HALT — ST command.\n"
            "Decelerates all axes to a stop using the programmed DC value.\n"
            "Motors remain enabled after stopping.\n"
            "Safe to call at any time during motion.")
        self.btn_halt.clicked.connect(self._halt_all)
        bl.addWidget(self.btn_halt)

        # E-STOP — AB + MO: immediate cut, bypasses queue
        self.btn_estop = QPushButton("⚠  E-STOP")
        self.btn_estop.setObjectName("btn_estop")
        self.btn_estop.setMinimumWidth(110)
        self.btn_estop.setToolTip(
            "EMERGENCY STOP — AB + MO.\n"
            "Cuts motor current immediately. NO deceleration.\n"
            "Motors disabled. Axis may drift or drop.\n"
            "Use only for true emergencies.\n"
            "Bypasses command queue — executes immediately.")
        self.btn_estop.clicked.connect(self._emergency_stop)
        bl.addWidget(self.btn_estop)

        return bar

    # ── Navigation ───────────────────────────────────────────────────────────

    def _navigate(self, idx: int):
        self._stack.setCurrentIndex(idx)
        for i, btn in enumerate(self._nav_buttons):
            active = (i == idx)
            btn.setProperty("active", "true" if active else "false")
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        self._active_nav = idx

    # ── Signal wiring ────────────────────────────────────────────────────────

    def _connect_signals(self):
        self.worker.status_updated.connect(self._jog_panel.on_status_updated)
        self.worker.status_updated.connect(self._on_status_sidebar)
        self.worker.error_occurred.connect(self._log_panel.on_error)

        self._set_panel.settings_applied.connect(self._on_settings_applied)
        self._conn_panel.connected.connect(self._on_connected)
        self._conn_panel.disconnected.connect(self._on_disconnected)

    @property
    def log_panel(self):
        """Public accessor for the LogPanel used by LogBridge in run.py."""
        return self._log_panel

    @pyqtSlot(dict)
    def _on_status_sidebar(self, status: dict):
        for ax, s in status.items():
            if ax in self._sidebar_pos:
                self._sidebar_pos[ax].setText(f"{s.pos:.0f}")

    @pyqtSlot(object, object)
    def _on_connected(self, controller, worker):
        self._conn_indicator.setText(f"● {controller.info.model}")
        self._conn_indicator.setStyleSheet("color: #3DBA6F;")

    @pyqtSlot()
    def _on_disconnected(self):
        self._conn_indicator.setText("● Disconnected")
        self._conn_indicator.setStyleSheet("color: #8B949E;")

    @pyqtSlot(object)
    def _on_settings_applied(self, settings: AppSettings):
        self.settings = settings

    def _on_worker_started(self):
        self._conn_indicator.setText("● MOCK — live")
        self._conn_indicator.setStyleSheet("color: #F0B429;")

    # ── Stop actions ─────────────────────────────────────────────────────────

    def _halt_all(self):
        """ST — decelerated stop; motors remain enabled. Safe for normal use."""
        self.worker.clear_queue()
        self.worker.queue_command(self.controller.halt_all)

    def _emergency_stop(self):
        """AB + MO — immediate hard cut. Bypasses queue. Emergency only."""
        # Purge queue first so no motion command restarts after AB+MO
        self.worker.clear_queue()
        # Bypass queue — call directly (only safe exception to the rule)
        try:
            self.controller.emergency_stop()
        except Exception:
            pass
        self._log_panel.append("ERROR", "EMERGENCY STOP: AB + MO sent")

    # ── Shutdown ─────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        # Save last positions
        # try:
        #     positions = {
        #         "axis_a": {"last_position": self.controller.poll_all_axes().get("axis_a", 0), "last_direction": self._dash_panel.axis_a_status.last_direction},
        #         "axis_b": {"last_position": self.controller.poll_all_axes().get("axis_b", 0), "last_direction": self._dash_panel.axis_b_status.last_direction},
        #         "axis_c": {"last_position": self.controller.poll_all_axes().get("axis_c", 0), "last_direction": self._dash_panel.axis_c_status.last_direction},
        #     }
        #     with open(POSITION_PATH, "w") as f:
        #         json.dump(positions, f, indent=2)
        # except Exception:
        #     pass

        logger.info("Shutting down application...")

        if self.worker.isRunning():
            self.worker.stop()
        try:
            self.settings.save_to_file()
            self.controller.halt_all()
            self.controller.disable_axes("ABC")
            self.controller.disconnect()
        except Exception:
            pass
        event.accept()


def _load_settings() -> AppSettings:
    path = os.path.join(os.path.dirname(__file__), "..", "config", "settings.json")
    try:
        import json
        with open(path) as f:
            data = json.load(f)
        return AppSettings(**data)
    except Exception:
        return AppSettings()
