"""
GalilMainWindow — QMainWindow assembly.
Sidebar navigation + QStackedWidget panels + always-visible bottom bar.

Bottom bar contains:
  HALT (ST)     — decelerated stop, motors remain enabled
  E-STOP (AB+MO) — immediate hard cut, bypasses queue
"""
import os

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QGridLayout,
    QPushButton, QLabel, QStackedWidget, QFrame,
    QSizePolicy, QStatusBar, QScrollArea, QStyle,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSlot, QSize
from PyQt5.QtGui import QFont
from loguru import logger

from controller.data_models import AppSettings
from utils.position_memory import PositionMemory
from ui.polling_worker import PollingWorker

from ui.panels.connection_panel import ConnectionPanel
from ui.panels.jog_panel import JogPanel
from ui.panels.settings_panel import SettingsPanel
from utils.soft_limits import SoftLimits
from utils.zero_offsets import ZeroOffsets
from utils.app_paths import config_path
from ui.panels.log_panel import LogPanel

# The Settings panel was removed on 2026-09-23. Everything it could edit is
# either hardcoded in galil_controller.py (KS, the fine-approach speed, the
# settle windows, the relaxation constants) or lives in config/settings.json,
# where SP/AC/DC have been tuned and measured and should not be nudged from a
# spin box mid-session. enable_axes() is what applies them to the hardware.
NAV_ITEMS = [
    ("🔌  Connection",  0),
    ("🕹  Jog Control", 1),
    ("📋  Log",        2),
    ("⚙  Settings",       3),
]


class _PanelScroll(QScrollArea):
    """A scroll area that asks for its panel's FULL size.

    A stock QScrollArea caps its size hint at roughly 36x24 lines of text, which
    would shrink the window that run.py sizes from sizeHint(). This one passes
    the panel's own hint through, so on a big enough screen the window opens at
    exactly the size it did before and no scroll bar appears; only when the
    window is smaller than the panel do the bars show up.
    """

    def __init__(self, panel: QWidget):
        super().__init__()
        self.setWidget(panel)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        # Never take focus: a focused scroll area scrolls on the arrow and page
        # keys, which belong to keyboard jogging (JogPanel.JOG_KEYS).
        self.setFocusPolicy(Qt.NoFocus)

    def sizeHint(self) -> QSize:
        hint = self.widget().sizeHint()
        bar = self.style().pixelMetric(QStyle.PM_ScrollBarExtent)
        return QSize(hint.width() + bar, hint.height())

    def minimumSizeHint(self) -> QSize:
        return QSize(200, 150)


class GalilMainWindow(QMainWindow):
    def __init__(self, controller, use_mock: bool = False):
        super().__init__()
        self.controller = controller
        self.use_mock = use_mock
        self.settings = _load_settings()
        self.memory = PositionMemory()
        # ONE ZeroOffsets for the whole app. The jog panel displays from it and
        # the soft limits measure from it; two instances would drift the moment
        # either wrote.
        self.zeros = ZeroOffsets()
        # Soft travel limits are applied to every poll result before it is
        # emitted or guarded, so they are indistinguishable from a switch to
        # everything downstream. Holds references to settings and zeros, so an
        # edit in the Settings panel takes effect on the next poll.
        self.worker = PollingWorker(
            controller, soft_limits=SoftLimits(self.settings, self.zeros))
        self._active_nav = 0

        self._build_ui()
        self._connect_signals()

        # If mock: start worker immediately (no connection needed)
        if use_mock:
            self.worker.start()
            self._on_worker_started()
            # No retract check here: the mock's positions are nowhere near the
            # rig's micron origin, so it would always conclude "retracted" - and
            # write that into the SAME state file the real app reads.

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
            self.controller, self.worker, self.settings, self.memory,
            zeros=self.zeros)
        self._log_panel  = LogPanel(
            self.controller, self.worker)
        self._settings_panel = SettingsPanel(self.settings)

        # Each panel in its own scroll area, so a screen smaller than the jog
        # panel (a laptop at 125-150 % scaling) scrolls instead of clipping.
        # Stack indices are unchanged: one wrapper per panel, in order.
        for panel in [self._conn_panel, self._jog_panel, self._log_panel,
                      self._settings_panel]:
            self._stack.addWidget(_PanelScroll(panel))

        self._nav_buttons[0].click()

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setFixedWidth(180)
        sidebar.setStyleSheet("background-color: #161B22;")
        sl = QVBoxLayout(sidebar)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)

        logo = QLabel("STORK\nCONTROLLER")
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

        # Enable / Disable
        btn_enable = QPushButton("Enable Motors")
        btn_enable.setObjectName("btn_enable")
        btn_enable.setToolTip("Enable stepper motors. Stepper mode does not use Galil SH servo-enable.")
        btn_enable.clicked.connect(lambda: (
            #self.worker.queue_command(self.controller.configure_stepper_axes, "ABC", 2),
            self.worker.queue_command(self.controller.enable_biss, "ABC")
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
        self.worker.move_progress.connect(self._jog_panel.on_move_progress)
        self.worker.move_progress.connect(self._on_move_progress_sidebar)
        self.worker.touch_alarm.connect(self._jog_panel.on_touch_alarm)
        self.worker.error_occurred.connect(self._log_panel.on_error)

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

    @pyqtSlot(str, float)
    def _on_move_progress_sidebar(self, axis: str, counts: float):
        if axis in self._sidebar_pos:
            self._sidebar_pos[axis].setText(f"{counts:.0f}")

    @pyqtSlot(object, object)
    def _on_connected(self, controller, worker):
        self._conn_indicator.setText(f"● {controller.info.model}")
        self._conn_indicator.setStyleSheet("color: #3DBA6F;")
        # The stored retract state is this PC's memory only; ask the encoder.
        self._jog_panel.check_z_retract()

    @pyqtSlot()
    def _on_disconnected(self):
        self._conn_indicator.setText("● Disconnected")
        self._conn_indicator.setStyleSheet("color: #8B949E;")

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
        logger.info("Shutting down application...")

        if self.worker.isRunning():
            self.worker.stop()

        # Two separate guards on purpose: AppSettings.save_to_file() did not
        # exist until now, so an AttributeError on the first line was silently
        # skipping the controller teardown below it — the app exited without
        # halting, disabling or closing the link.
        try:
            self.settings.save_to_file()
        except Exception as e:
            logger.warning(f"Could not save settings on exit: {e}")
        try:
            self.controller.halt_all()
            self.controller.disable_axes("ABC")
            self.controller.disconnect()
        except Exception as e:
            logger.warning(f"Controller shutdown: {e}")
        event.accept()


def _load_settings() -> AppSettings:
    path = config_path("settings.json")
    try:
        import json
        with open(path) as f:
            data = json.load(f)
        return AppSettings(**data)
    except Exception:
        return AppSettings()
