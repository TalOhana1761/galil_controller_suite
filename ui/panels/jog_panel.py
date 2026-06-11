"""
Panel 3 — Jog Control.
XY D-pad (axes A & B) + Z column (axis C).
Hold-to-jog: pressed → start jog, released → ST (HALT, decel stop).
Step-click mode for precise increments.
Limit switch status table and position memory.
"""
import os
import json

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QGroupBox, QLabel, QPushButton, QDoubleSpinBox,
    QSlider, QListWidget, QListWidgetItem, QFileDialog,
    QTableWidget, QTableWidgetItem, QHeaderView, QFrame,
    QSizePolicy, QInputDialog,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSlot
from PyQt5.QtGui import QFont, QColor
from loguru import logger

from controller.data_models import AxisStatus
from utils.position_memory import PositionMemory
from utils.unit_converter import counts_to_um, um_to_counts

AXES = ["A", "B", "C"]
AXIS_LABEL = {"A": "X (A)", "B": "Y (B)", "C": "Z (C)"}
STEP_PRESETS = [0.1, 1.0, 10.0, 100.0, 1000.0]

# If the button is released within this many ms it counts as a "click"
# and fires a fixed step-move.  Held longer → continuous jog.
_JOG_HOLD_MS = 150

POSITION_PATH = "config/last_positions.json"
# POSITION_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "last_positions.json")
limit_flags = {"A": False, "B": False, "C": False}

class JogPanel(QWidget):
    def __init__(self, controller, worker, settings, position_memory: PositionMemory,
                 parent=None):
        super().__init__(parent)
        self.controller = controller
        self.worker = worker
        self.settings = settings
        self.memory = position_memory
        self._current_status: dict[str, AxisStatus] = {}
        self._step_um = 10.0
        self._speed = int(settings.axis_a.speed)
        self._step_btns: list[QPushButton] = []
        # Jog buttons keyed by "A+" / "A-" / "B+" / "B-" / "C+" / "C-"
        self._jog_btns: dict[str, QPushButton] = {}
        self._pos_lbls: dict[str, QLabel] = {}
        self._pos_um_lbls: dict[str, QLabel] = {}

        self._build_ui()
        self.worker.jog_button_state.connect(self._on_jog_button_state_changed)
        self._refresh_memory_list()

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setSpacing(10)

        root.addLayout(self._build_xy_col(), stretch=3)
        root.addLayout(self._build_z_col(), stretch=1)
        root.addLayout(self._build_right_col(), stretch=2)

    # XY column ──────────────────────────────────────────────────────────────

    def _build_xy_col(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(8)

        title = QLabel("XY JOG CONTROL")
        title.setFont(QFont("Segoe UI", 11, QFont.Bold))
        title.setObjectName("axis_A")
        col.addWidget(title)

        # Step presets
        step_row = QHBoxLayout()
        step_row.addWidget(QLabel("Step:"))
        for v in STEP_PRESETS:
            lbl = f"{v:g} µm"
            btn = QPushButton(lbl)
            btn.setObjectName("btn_step")
            btn.setCheckable(True)
            btn.setProperty("active", "false")
            btn.clicked.connect(lambda checked, val=v, b=btn: self._select_step(val, b))
            step_row.addWidget(btn)
            self._step_btns.append(btn)
        self._custom_step = QDoubleSpinBox()
        self._custom_step.setRange(0.001, 100000.0)
        self._custom_step.setValue(self._step_um)
        self._custom_step.setSuffix(" µm")
        self._custom_step.valueChanged.connect(self._on_custom_step)
        step_row.addWidget(self._custom_step)
        col.addLayout(step_row)
        self._step_btns[2].setProperty("active", "true")   # default: 10 µm

        # D-pad
        dpad = QGridLayout()
        dpad.setSpacing(4)

        btn_yp = self._jog_btn("Y+  ▲", "B", +1)
        btn_ym = self._jog_btn("▼  Y−", "B", -1)
        btn_xm = self._jog_btn("◄  X−", "A", -1)
        btn_xp = self._jog_btn("X+  ►", "A", +1)
        self._jog_btns["B+"] = btn_yp
        self._jog_btns["B-"] = btn_ym
        self._jog_btns["A-"] = btn_xm
        self._jog_btns["A+"] = btn_xp

        btn_home_xy = QPushButton("⌂")
        btn_home_xy.setObjectName("btn_jog")
        btn_home_xy.setToolTip("Move X and Y to absolute zero")
        btn_home_xy.clicked.connect(self._go_home_xy)

        dpad.addWidget(btn_yp,      0, 1)
        dpad.addWidget(btn_xm,      1, 0)
        dpad.addWidget(btn_home_xy, 1, 1)
        dpad.addWidget(btn_xp,      1, 2)
        dpad.addWidget(btn_ym,      2, 1)
        col.addLayout(dpad)

        # Step-click XY (uses _step_um converted to counts via per-axis calibration)
        step_move_grp = QGroupBox("Step Move (µm → counts)")
        smg = QGridLayout(step_move_grp)
        for row, (ax, attr) in enumerate([("A", "axis_a"), ("B", "axis_b")]):
            lbl = "X" if ax == "A" else "Y"
            smg.addWidget(QLabel(f"{lbl}:"), row, 0)
            btn_neg = QPushButton("−")
            btn_neg.setObjectName("btn_step")
            btn_neg.clicked.connect(
                lambda checked, a=ax, sa=attr: self.worker.queue_command(
                    self.controller.move_relative, a,
                    -um_to_counts(self._step_um, getattr(self.settings, sa).counts_per_um)))
            btn_pos = QPushButton("+")
            btn_pos.setObjectName("btn_step")
            btn_pos.clicked.connect(
                lambda checked, a=ax, sa=attr: self.worker.queue_command(
                    self.controller.move_relative, a,
                    +um_to_counts(self._step_um, getattr(self.settings, sa).counts_per_um)))
            smg.addWidget(btn_neg, row, 1)
            smg.addWidget(btn_pos, row, 2)
        col.addWidget(step_move_grp)

        # HALT XY
        halt_xy = QPushButton("◼  HALT XY")
        halt_xy.setObjectName("btn_halt")
        halt_xy.clicked.connect(self._halt_xy)
        col.addWidget(halt_xy)

        # Speed slider
        speed_grp = QGroupBox("Jog Speed")
        sg = QVBoxLayout(speed_grp)
        self._speed_slider = QSlider(Qt.Horizontal)
        self._speed_slider.setRange(100, 50000)
        self._speed_slider.setValue(self._speed)
        self._speed_lbl = QLabel(f"{self._speed} counts/s")
        self._speed_slider.valueChanged.connect(self._on_speed_changed)
        sg.addWidget(self._speed_slider)
        sg.addWidget(self._speed_lbl)
        col.addWidget(speed_grp)

        # Absolute target move
        target_grp = QGroupBox("Move to Absolute Position")
        tg = QGridLayout(target_grp)
        self._target_spins: dict[str, QDoubleSpinBox] = {}
        for i, ax in enumerate(["A", "B"]):
            tg.addWidget(QLabel(f"{AXIS_LABEL[ax]}:"), i, 0)
            sp = QDoubleSpinBox()
            sp.setRange(-1e7, 1e7)
            sp.setDecimals(1)
            sp.setSuffix(" counts")
            tg.addWidget(sp, i, 1)
            self._target_spins[ax] = sp
        btn_go_xy = QPushButton("MOVE TO TARGET XY")
        btn_go_xy.setObjectName("btn_enable")
        btn_go_xy.clicked.connect(self._go_to_target_xy)
        tg.addWidget(btn_go_xy, 2, 0, 1, 2)
        col.addWidget(target_grp)

        # Live position readout
        pos_grp = QGroupBox("Current Positions")
        pg_l = QGridLayout(pos_grp)
        for i, ax in enumerate(AXES):
            pg_l.addWidget(QLabel(f"Axis {ax}:"), i, 0)
            lbl = QLabel("0.00")
            lbl.setFont(QFont("Consolas", 12))
            lbl.setObjectName(f"axis_{ax}")
            pg_l.addWidget(lbl, i, 1)
            pg_l.addWidget(QLabel("counts"), i, 2)

            um_lbl = QLabel("0.00")
            um_lbl.setFont(QFont("Consolas", 12))
            um_lbl.setObjectName(f"axis_{ax}_um")
            pg_l.addWidget(um_lbl, i, 3)
            pg_l.addWidget(QLabel("µm"), i, 4)

            self._pos_lbls[ax] = lbl
            self._pos_um_lbls[ax] = um_lbl
        col.addWidget(pos_grp)
        col.addStretch()
        return col

    def _jog_btn(self, label: str, axis: str, direction: int) -> QPushButton:
        """Create a D-pad button with click-vs-hold dual behaviour.

        Click  (< _JOG_HOLD_MS ms)  → move_relative by current step size.
        Hold   (≥ _JOG_HOLD_MS ms)  → start continuous jog; release → halt.
        """
        btn = QPushButton(label)
        btn.setObjectName("btn_jog")
        btn.setToolTip(
            f"Click: step {self._step_um:g} µm\n"
            f"Hold: continuous jog at {self._speed} counts/s"
        )

        # Mutable state shared by the closures
        state = {"jogging": False}
        timer = QTimer()
        timer.setSingleShot(True)

        def _start_jog():
            state["jogging"] = True
            speed_now = int(self._speed * direction)
            self.worker.queue_command(self.controller.jog, axis, speed_now)

        def on_press():
            state["jogging"] = False
            timer.start(_JOG_HOLD_MS)

        def on_release():
            if timer.isActive():
                # Released before hold threshold → click → step move
                timer.stop()
                ax_attr = "axis_a" if axis == "A" else ("axis_b" if axis == "B" else "axis_c")
                cpm = getattr(self.settings, ax_attr).counts_per_um
                counts_to_move = float(self._step_um * cpm * direction)

                if limit_flags[axis] == False:
                    self.worker.queue_command(
                        self.controller.move_relative_closed_loop,
                        axis,
                        counts_to_move,
                        button_state_callback=self.worker.jog_button_state.emit,
                    )
                else:
                    self.worker.queue_command(self.controller.move_if_at_limit, axis, counts_to_move, speed=self._speed)
            elif state["jogging"]:
                # Held long enough → stop continuous jog
                self.worker.queue_command(self.controller.halt_axis, axis)
                state["jogging"] = False

        timer.timeout.connect(_start_jog)
        btn.pressed.connect(on_press)
        btn.released.connect(on_release)
        return btn

    # Z column ───────────────────────────────────────────────────────────────

    def _build_z_col(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(6)

        title = QLabel("Z AXIS (C)")
        title.setFont(QFont("Segoe UI", 11, QFont.Bold))
        title.setObjectName("axis_C")
        col.addWidget(title)

        # Shared helper: same click-vs-hold dual behaviour as _jog_btn
        def _wire_z_btn(btn: QPushButton, direction: int):
            state = {"jogging": False}
            timer = QTimer()
            timer.setSingleShot(True)

            def _start_jog():
                state["jogging"] = True
                self.worker.queue_command(self.controller.jog, "C", self._speed * direction)

            def on_press():
                state["jogging"] = False
                timer.start(_JOG_HOLD_MS)

            def on_release():
                if timer.isActive():
                    timer.stop()
                    axis = "C"
                    cpm = getattr(self.settings, "axis_c").counts_per_um
                    counts_to_move = float(self._step_um * cpm * direction)
                    if limit_flags[axis] == False:
                        self.worker.queue_command(
                            self.controller.move_relative_closed_loop,
                            axis,
                            counts_to_move,
                            button_state_callback=self.worker.jog_button_state.emit,
                        )
                    else:
                        self.worker.queue_command(self.controller.move_if_at_limit, axis, counts_to_move, speed=self._speed)
                elif state["jogging"]:
                    self.worker.queue_command(self.controller.halt_axis, "C")
                    state["jogging"] = False

            timer.timeout.connect(_start_jog)
            btn.pressed.connect(on_press)
            btn.released.connect(on_release)

        btn_zp = QPushButton("▲\nZ+")
        btn_zp.setObjectName("btn_jog")
        btn_zp.setMinimumHeight(72)
        btn_zp.setToolTip(
            f"Click: step {self._step_um:g} \u00b5m\n"
            f"Hold: continuous jog at {self._speed} counts/s"
        )
        self._jog_btns["C+"] = btn_zp
        _wire_z_btn(btn_zp, +1)
        col.addWidget(btn_zp)

        self._z_pos_lbl = QLabel("0.00")
        self._z_pos_lbl.setFont(QFont("Consolas", 16, QFont.Bold))
        self._z_pos_lbl.setAlignment(Qt.AlignCenter)
        self._z_pos_lbl.setObjectName("axis_C")
        col.addWidget(self._z_pos_lbl)

        self._z_pos_um_lbl = QLabel("0.00")
        self._z_pos_um_lbl.setFont(QFont("Consolas", 12))
        self._z_pos_um_lbl.setAlignment(Qt.AlignCenter)
        self._z_pos_um_lbl.setObjectName("axis_C")
        col.addWidget(self._z_pos_um_lbl)

        col.addWidget(QLabel("counts", alignment=Qt.AlignCenter))  # type: ignore[call-arg]
        col.addWidget(QLabel("µm", alignment=Qt.AlignCenter))  # type: ignore[call-arg]

        btn_zm = QPushButton("Z−\n▼")
        btn_zm.setObjectName("btn_jog")
        btn_zm.setMinimumHeight(72)
        btn_zm.setToolTip(
            f"Click: step {self._step_um:g} \u00b5m\n"
            f"Hold: continuous jog at {self._speed} counts/s"
        )
        self._jog_btns["C-"] = btn_zm
        _wire_z_btn(btn_zm, -1)
        col.addWidget(btn_zm)

        halt_z = QPushButton("◼  HALT Z")
        halt_z.setObjectName("btn_halt")
        halt_z.clicked.connect(lambda: (
            self.worker.clear_queue(),
            self.worker.queue_command(self.controller.halt_axis, "C"),
        ))
        col.addWidget(halt_z)

        # Z step + target
        self._z_step = QDoubleSpinBox()
        self._z_step.setRange(0.001, 100000.0)
        self._z_step.setValue(10.0)
        self._z_step.setSuffix(" counts")
        col.addWidget(QLabel("Step:"))
        col.addWidget(self._z_step)

        btn_z_step_p = QPushButton("+1 Step")
        btn_z_step_p.clicked.connect(lambda: self.worker.queue_command(
            self.controller.move_relative, "C", +self._z_step.value()))
        btn_z_step_m = QPushButton("−1 Step")
        btn_z_step_m.clicked.connect(lambda: self.worker.queue_command(
            self.controller.move_relative, "C", -self._z_step.value()))
        col.addWidget(btn_z_step_p)
        col.addWidget(btn_z_step_m)

        col.addWidget(QLabel("Target Z:"))
        self._z_target = QDoubleSpinBox()
        self._z_target.setRange(-1e7, 1e7)
        self._z_target.setDecimals(1)
        self._z_target.setSuffix(" counts")
        col.addWidget(self._z_target)
        btn_go_z = QPushButton("GO TO Z")
        btn_go_z.setObjectName("btn_enable")
        btn_go_z.clicked.connect(lambda: self.worker.queue_command(
            self.controller.move_absolute, "C", self._z_target.value()))
        col.addWidget(btn_go_z)

        btn_zero_z = QPushButton("ZERO Z")
        btn_zero_z.clicked.connect(lambda: self.worker.queue_command(
            self.controller.define_zero, "C"))
        col.addWidget(btn_zero_z)

        col.addStretch()
        return col

    # Right column ───────────────────────────────────────────────────────────

    def _build_right_col(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(8)

        # Limit switch table
        lim_grp = QGroupBox("Limit Switch Status")
        lv = QVBoxLayout(lim_grp)
        self._lim_table = QTableWidget(3, 4)
        self._lim_table.setHorizontalHeaderLabels(["Axis", "FWD", "REV", "HOME"])
        self._lim_table.verticalHeader().setVisible(False)
        self._lim_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._lim_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._lim_table.setSelectionMode(QTableWidget.NoSelection)
        self._lim_table.setMaximumHeight(120)
        for r, ax in enumerate(AXES):
            self._lim_table.setItem(r, 0, QTableWidgetItem(ax))
            for c in range(1, 4):
                self._lim_table.setItem(r, c, QTableWidgetItem("CLR ●"))
        lv.addWidget(self._lim_table)

        # Limit alert
        self._lim_alert = QLabel("")
        self._lim_alert.setStyleSheet("color: #FF4C4C; font-weight: bold;")
        self._lim_alert.setWordWrap(True)
        self._lim_alert.setVisible(False)
        lv.addWidget(self._lim_alert)
        col.addWidget(lim_grp)

        # Homing
        home_grp = QGroupBox("Homing")
        hg = QGridLayout(home_grp)
        for i, ax in enumerate(AXES):
            btn = QPushButton(f"HOME {ax}")
            btn.clicked.connect(lambda checked, a=ax: self.worker.queue_command(
                self.controller.home_axis, a))
            hg.addWidget(btn, 0, i)
        btn_home_all = QPushButton("HOME ALL AXES")
        btn_home_all.clicked.connect(self._home_all)
        hg.addWidget(btn_home_all, 1, 0, 1, 3)
        btn_zero_all = QPushButton("ZERO ALL (DP=0)")
        btn_zero_all.clicked.connect(lambda: self.worker.queue_command(
            self.controller.define_zero, "ABC"))
        hg.addWidget(btn_zero_all, 2, 0, 1, 3)
        col.addWidget(home_grp)

        # Position memory
        mem_grp = QGroupBox("Position Memory")
        mv = QVBoxLayout(mem_grp)
        self._mem_list = QListWidget()
        self._mem_list.setMaximumHeight(140)
        mv.addWidget(self._mem_list)
        btn_row = QHBoxLayout()
        btn_save = QPushButton("SAVE")
        btn_save.clicked.connect(self._save_position)
        btn_go = QPushButton("GO TO")
        btn_go.clicked.connect(self._go_to_saved)
        btn_del = QPushButton("DELETE")
        btn_del.clicked.connect(self._delete_saved)
        btn_open = QPushButton("OPEN FILE")
        btn_open.clicked.connect(self._open_positions_file)
        btn_row.addWidget(btn_save)
        btn_row.addWidget(btn_go)
        btn_row.addWidget(btn_del)
        btn_row.addWidget(btn_open)
        mv.addLayout(btn_row)
        col.addWidget(mem_grp)
        col.addStretch()
        return col

    # ── Slot connected to polling worker ─────────────────────────────────────

    @pyqtSlot(str, str, bool)
    def _on_jog_button_state_changed(self, axis: str, direction: str, enabled: bool):
        btn = self._jog_btns.get(f"{axis}{direction}")
        if btn is not None:
            btn.setEnabled(enabled)

    @pyqtSlot(dict)
    def on_status_updated(self, status: dict):
        self._current_status = status

        with open(POSITION_PATH, "r") as f:
            data = json.load(f)

        for ax, s in status.items():
            if ax in self._pos_lbls:
                counts = data[ax]["last_position"]
                self._pos_lbls[ax].setText(f"{counts:.1f}")
                counts_per_um = getattr(self.settings, f"axis_{ax.lower()}").counts_per_um
                self._pos_um_lbls[ax].setText(f"{counts_to_um(counts, counts_per_um):.3f}")
            if ax == "C":
                counts = data[ax]["last_position"]
                self._z_pos_lbl.setText(f"{counts:.1f}")
                counts_per_um = getattr(self.settings, "axis_c").counts_per_um
                self._z_pos_um_lbl.setText(f"{counts_to_um(counts, counts_per_um):.3f}")

        def _cell(active: bool) -> QTableWidgetItem:
            item = QTableWidgetItem("HIT ●" if active else "CLR ●")
            item.setForeground(QColor("#FF4C4C" if active else "#3DBA6F"))
            return item

        for r, ax in enumerate(AXES):
            if ax not in status:
                continue
            s = status[ax]
            self._lim_table.setItem(r, 1, _cell(s.fwd_limit and s.last_direction >= 1))
            self._lim_table.setItem(r, 2, _cell(s.fwd_limit and s.last_direction <= -1))
            self._lim_table.setItem(r, 3, _cell(s.home_switch))

        # Update jog button enable state and build escape-direction hints
        alert_lines: list[str] = []
        for ax, s in status.items():
            fwd_btn = self._jog_btns.get(f"{ax}+")
            rev_btn = self._jog_btns.get(f"{ax}-")
            at_limit = s.fwd_limit or s.rev_limit
            last_direction = data[ax]["last_direction"]

            if not at_limit:
                limit_flags[ax] = False
                if fwd_btn: fwd_btn.setEnabled(True)
                if rev_btn: rev_btn.setEnabled(True)
            elif ax in ("A", "B"):
                # XY: one switch → both trip simultaneously; use last_direction
                limit_flags[ax] = True
                if last_direction >= 0:   # was moving forward → hit fwd end
                    if fwd_btn: fwd_btn.setEnabled(False)
                    if rev_btn: rev_btn.setEnabled(True)
                    # logger.info(f"last direction for axis {ax} is {s.last_direction}, disabling FWD button")
                    hint = "jog REV (−) to escape"
                else:                        # was moving reverse → hit rev end
                    if fwd_btn: fwd_btn.setEnabled(True)
                    if rev_btn: rev_btn.setEnabled(False)
                    # logger.info(f"last direction for axis {ax} is {s.last_direction}, disabling REV button")
                    hint = "jog FWD (+) to escape"
                axis_lbl = "X" if ax == "A" else "Y"
                alert_lines.append(f"Axis {ax} ({axis_lbl}) LIMIT — {hint}")
            else:
                # Z: fwd and rev switch independently
                limit_flags[ax] = True
                if fwd_btn: fwd_btn.setEnabled(not s.fwd_limit)
                if rev_btn: rev_btn.setEnabled(not s.rev_limit)
                if s.fwd_limit:
                    alert_lines.append("Axis C (Z) FWD limit — jog REV (↓) to escape")
                if s.rev_limit:
                    alert_lines.append("Axis C (Z) REV limit — jog FWD (↑) to escape")

        if alert_lines:
            self._lim_alert.setText("⚠  " + "\n".join(alert_lines))
            self._lim_alert.setVisible(True)
        else:
            self._lim_alert.setVisible(False)

    # ── Step / speed ─────────────────────────────────────────────────────────

    def _select_step(self, val: float, clicked_btn: QPushButton):
        self._step_um = val
        for b in self._step_btns:
            b.setProperty("active", "false")
            b.style().unpolish(b)
            b.style().polish(b)
        clicked_btn.setProperty("active", "true")
        clicked_btn.style().unpolish(clicked_btn)
        clicked_btn.style().polish(clicked_btn)
        self._custom_step.blockSignals(True)
        self._custom_step.setValue(val)
        self._custom_step.blockSignals(False)

    def _on_custom_step(self, val: float):
        self._step_um = val
        for b in self._step_btns:
            b.setProperty("active", "false")
            b.style().unpolish(b)
            b.style().polish(b)

    def _on_speed_changed(self, val: int):
        self._speed = val
        self._speed_lbl.setText(f"{val} counts/s")

    # ── Motion helpers ────────────────────────────────────────────────────────

    def _halt_xy(self):
        self.worker.clear_queue()
        self.worker.queue_command(self.controller.halt_axis, "A")
        self.worker.queue_command(self.controller.halt_axis, "B")

    def _go_home_xy(self):
        with open(POSITION_PATH, "r") as f:
            data = json.load(f)
        self.worker.queue_command(self.controller.move_relative_closed_loop, "A", float(-1 * data["A"]["last_position"]))
        self.worker.queue_command(self.controller.move_relative_closed_loop, "B", float(-1 * data["B"]["last_position"]))

    def _go_to_target_xy(self):
        with open(POSITION_PATH, "r") as f:
            data = json.load(f)
        target_a = float(self._target_spins["A"].value()) - data["A"]["last_position"]
        target_b = float(self._target_spins["B"].value()) - data["B"]["last_position"]
        self.worker.queue_command(self.controller.move_relative_closed_loop, "A", target_a)
        self.worker.queue_command(self.controller.move_relative_closed_loop, "B", target_b)

    def _home_all(self):
        for ax in AXES:
            self.worker.queue_command(self.controller.home_axis, ax)

    # ── Position memory ────────────────────────────────────────────────────────

    def _save_position(self):
        if not self._current_status:
            return
        name, ok = QInputDialog.getText(self, "Save Position", "Position name:")
        if not ok or not name.strip():
            return
        pos_a = self._current_status.get("A", AxisStatus()).pos
        pos_b = self._current_status.get("B", AxisStatus()).pos
        pos_c = self._current_status.get("C", AxisStatus()).pos
        self.memory.save(name.strip(), pos_a, pos_b, pos_c)
        self._refresh_memory_list()

    def _go_to_saved(self):
        item = self._mem_list.currentItem()
        if item is None:
            return
        name = item.data(Qt.UserRole)
        slot = self.memory.get(name)
        if slot is None:
            return
        
        with open(POSITION_PATH, "r") as f:
            data = json.load(f)

        pos_a = slot.pos_a - data["A"]["last_position"]
        pos_b = slot.pos_b - data["B"]["last_position"]
        pos_c = slot.pos_c - data["C"]["last_position"]

        self.worker.queue_command(
            self.controller.move_all_absolute,
            {"A": pos_a, "B": pos_b, "C": pos_c},
        )

    def _delete_saved(self):
        item = self._mem_list.currentItem()
        if item is None:
            return
        name = item.data(Qt.UserRole)
        self.memory.delete(name)
        self._refresh_memory_list()

    def _open_positions_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Positions File", "", "JSON Files (*.json)")
        if path:
            self.memory.load_file(path)
            self._refresh_memory_list()

    def _refresh_memory_list(self):
        self._mem_list.clear()
        for slot in self.memory.all_slots():
            text = (f"{slot.name}   A:{slot.pos_a:.0f}  "
                    f"B:{slot.pos_b:.0f}  C:{slot.pos_c:.0f}")
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, slot.name)
            self._mem_list.addItem(item)
