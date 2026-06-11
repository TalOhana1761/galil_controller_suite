"""
Panel 4 — Motion Parameters & Settings.
Per-axis tabs: SP / AC / DC / ER.
Global tab: poll interval, display units, encoder counts/µm.
Persists to config/settings.json.
"""
import json
import os

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QGroupBox, QLabel, QPushButton, QDoubleSpinBox,
    QSpinBox, QComboBox, QTabWidget, QCheckBox,
    QFileDialog, QMessageBox,
)
from PyQt5.QtCore import pyqtSignal
from PyQt5.QtGui import QFont

from controller.data_models import AppSettings, AxisSettings

AXES = ["A", "B", "C"]
SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config", "settings.json")


class _AxisTab(QWidget):
    """Per-axis settings tab with motion parameters and limit switch types."""

    changed = pyqtSignal()

    def __init__(self, axis: str, axis_settings: AxisSettings, parent=None):
        super().__init__(parent)
        self.axis = axis
        self._build(axis_settings)

    def _build(self, s: AxisSettings):
        root = QVBoxLayout(self)
        root.setSpacing(10)

        # Motion group
        mo_grp = QGroupBox("MOTION PARAMETERS")
        mg = QGridLayout(mo_grp)
        params = [
            ("Speed (SP)",          "speed",       0, 100000, 1,     "counts/sec"),
            ("Acceleration (AC)",   "accel",       0, 1000000, 1000, "counts/sec²"),
            ("Deceleration (DC)",   "decel",       0, 1000000, 1000, "counts/sec²"),
            ("Error Limit (ER)",    "error_limit", 0, 100000, 10,   "counts"),
        ]
        self._spins: dict[str, QDoubleSpinBox] = {}
        for i, (label, attr, lo, hi, step, unit) in enumerate(params):
            mg.addWidget(QLabel(f"{label}:"), i, 0)
            sp = QDoubleSpinBox()
            sp.setRange(lo, hi)
            sp.setDecimals(1)
            sp.setSingleStep(step)
            sp.setValue(getattr(s, attr))
            sp.valueChanged.connect(self.changed)
            mg.addWidget(sp, i, 1)
            mg.addWidget(QLabel(unit), i, 2)
            self._spins[attr] = sp
        root.addWidget(mo_grp)

        # Limit switch types
        lim_grp = QGroupBox("LIMIT SWITCH WIRING")
        lg = QGridLayout(lim_grp)
        self._lim_combos: dict[str, QComboBox] = {}
        for i, (key, label) in enumerate([
            ("fwd_limit_type", "Forward Limit"),
            ("rev_limit_type", "Reverse Limit"),
            ("home_limit_type", "Home Switch"),
        ]):
            lg.addWidget(QLabel(f"{label}:"), i, 0)
            cb = QComboBox()
            cb.addItems(["NO — Normally Open", "NC — Normally Closed"])
            cb.setCurrentIndex(0 if getattr(s, key) == "NO" else 1)
            cb.currentIndexChanged.connect(self.changed)
            lg.addWidget(cb, i, 1)
            self._lim_combos[key] = cb
        root.addWidget(lim_grp)

        root.addStretch()

    def get_values(self) -> AxisSettings:
        lim_type = lambda cb: "NO" if cb.currentIndex() == 0 else "NC"
        return AxisSettings(
            speed       = self._spins["speed"].value(),
            accel       = self._spins["accel"].value(),
            decel       = self._spins["decel"].value(),
            error_limit = self._spins["error_limit"].value(),
            fwd_limit_type  = lim_type(self._lim_combos["fwd_limit_type"]),
            rev_limit_type  = lim_type(self._lim_combos["rev_limit_type"]),
            home_limit_type = lim_type(self._lim_combos["home_limit_type"]),
            counts_per_um   = self._cpm_spin.value(),
        )

    def load_values(self, s: AxisSettings):
        self._spins["speed"].setValue(s.speed)
        self._spins["accel"].setValue(s.accel)
        self._spins["decel"].setValue(s.decel)
        self._spins["error_limit"].setValue(s.error_limit)
        #self._cpm_spin.setValue(s.counts_per_um)
        self._lim_combos["fwd_limit_type"].setCurrentIndex(0 if s.fwd_limit_type == "NO" else 1)
        self._lim_combos["rev_limit_type"].setCurrentIndex(0 if s.rev_limit_type == "NO" else 1)
        self._lim_combos["home_limit_type"].setCurrentIndex(0 if s.home_limit_type == "NO" else 1)


class SettingsPanel(QWidget):
    settings_applied = pyqtSignal(object)   # AppSettings

    def __init__(self, controller, worker, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.worker = worker
        self.settings = settings
        self._general_tab: _AxisTab | None = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)

        title = QLabel("MOTION SETTINGS")
        title.setFont(QFont("Segoe UI", 13, QFont.Bold))
        title.setObjectName("axis_A")
        root.addWidget(title)

        self._tabs = QTabWidget()
        self._general_tab = _AxisTab("GENERAL", self.settings.axis_a)
        self._general_tab.changed.connect(self._mark_dirty)
        self._tabs.addTab(self._general_tab, "General")
        self._tabs.addTab(self._build_global_tab(), "Global")
        root.addWidget(self._tabs)

        # Buttons
        btn_row = QHBoxLayout()
        self._btn_apply = QPushButton("APPLY SETTINGS")
        self._btn_apply.setObjectName("btn_enable")
        self._btn_apply.clicked.connect(self._apply)
        self._btn_reset = QPushButton("RESET")
        self._btn_reset.clicked.connect(self._reset)
        self._btn_save = QPushButton("SAVE TO FILE")
        self._btn_save.clicked.connect(self._save_to_file)
        btn_row.addWidget(self._btn_apply)
        btn_row.addWidget(self._btn_reset)
        btn_row.addWidget(self._btn_save)
        root.addLayout(btn_row)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color: #8B949E; font-size: 9pt;")
        root.addWidget(self._status_lbl)
        root.addStretch()

    def _build_global_tab(self) -> QWidget:
        w = QWidget()
        g = QGridLayout(w)
        g.setSpacing(8)

        g.addWidget(QLabel("Poll interval (ms):"), 0, 0)
        self._poll_spin = QSpinBox()
        self._poll_spin.setRange(50, 2000)
        self._poll_spin.setValue(self.settings.poll_interval_ms)
        g.addWidget(self._poll_spin, 0, 1)

        g.addWidget(QLabel("History depth (pts):"), 1, 0)
        self._hist_spin = QSpinBox()
        self._hist_spin.setRange(100, 6000)
        self._hist_spin.setValue(self.settings.position_history_depth)
        g.addWidget(self._hist_spin, 1, 1)

        g.addWidget(QLabel("Display units:"), 2, 0)
        self._units_combo = QComboBox()
        self._units_combo.addItems(["counts", "µm"])
        self._units_combo.setCurrentText(self.settings.display_units)
        g.addWidget(self._units_combo, 2, 1)

        g.addWidget(QLabel("Last address:"), 3, 0)
        from PyQt5.QtWidgets import QLineEdit
        self._addr_edit = QLineEdit(self.settings.last_address)
        g.addWidget(self._addr_edit, 3, 1)

        self._auto_reconnect = QCheckBox("Auto-reconnect on drop")
        self._auto_reconnect.setChecked(self.settings.auto_reconnect)
        g.addWidget(self._auto_reconnect, 4, 0, 1, 2)

        g.setRowStretch(5, 1)
        return w

    def _mark_dirty(self):
        self._btn_apply.setStyleSheet("border-color: #00C9A7; color: #00C9A7;")

    def _apply(self):
        if self._general_tab is None:
            return
        s = self._general_tab.get_values()
        for ax in AXES:
            self.worker.queue_command(
                self.controller.set_params, ax,
                speed=s.speed, accel=s.accel, decel=s.decel, err_limit=s.error_limit
            )
        self._collect_settings()
        self._btn_apply.setStyleSheet("")
        self._status_lbl.setText("Settings applied to controller.")
        self.settings_applied.emit(self.settings)

    def _reset(self):
        if self._general_tab is None:
            return
        self._general_tab.load_values(self.settings.axis_settings("A"))
        self._btn_apply.setStyleSheet("")
        self._status_lbl.setText("Values reset to last saved.")

    def _collect_settings(self):
        if self._general_tab is None:
            return
        values = self._general_tab.get_values()
        self.settings.axis_a = values
        self.settings.axis_b = values
        self.settings.axis_c = values
        self.settings.poll_interval_ms = self._poll_spin.value()
        self.settings.position_history_depth = self._hist_spin.value()
        self.settings.display_units = self._units_combo.currentText()
        self.settings.last_address = self._addr_edit.text()
        self.settings.auto_reconnect = self._auto_reconnect.isChecked()

    def _save_to_file(self):
        self._collect_settings()
        path = os.path.abspath(SETTINGS_PATH)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.settings.model_dump(), f, indent=2)
        self._status_lbl.setText(f"Saved → {path}")
