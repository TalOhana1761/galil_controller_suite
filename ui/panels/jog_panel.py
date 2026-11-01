"""
Panel 3 — Jog Control.
XY D-pad (axes A & B) + Z column (axis C).
Hold-to-jog: pressed → start jog, released → ST (HALT, decel stop).
Step-click mode for precise increments.
Limit switch status table and position memory.
"""

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QGroupBox, QLabel, QPushButton, QDoubleSpinBox,
    QSlider, QListWidget, QListWidgetItem, QFileDialog,
    QTableWidget, QTableWidgetItem, QHeaderView, QFrame,
    QSizePolicy, QInputDialog, QMessageBox, QApplication,
    QAbstractSpinBox, QAbstractItemView, QLineEdit,
)
import time

from PyQt5.QtCore import Qt, QTimer, QEvent, pyqtSlot
from PyQt5.QtGui import QFont, QColor
from loguru import logger

from controller.data_models import AxisStatus
from utils.zero_offsets import ZeroOffsets
from utils import z_retract_state
from ui.polling_worker import TOUCH_ALARM_INPUT

# Settle window used for interactive (jog) moves. Imported from the controller so
# there is one source of truth, but guarded: run.py only imports
# galil_controller in the non-mock branch, and that module imports gclib at the
# top, so an unguarded import here would break --mock on a machine without it.
try:
    from controller.galil_controller import JOG_SETTLE_WINDOW_MS
except Exception:
    JOG_SETTLE_WINDOW_MS = 60
from utils.position_memory import PositionMemory
from utils.unit_converter import counts_to_um

# Axis names, the stage/controller direction map and its converters now live
# in utils.stage_axes - the soft travel limits and check_limits.py need them
# too, and a second copy of the sign map would be an inversion waiting to
# happen. Imported under the same names, so call sites below are unchanged.
from utils.stage_axes import (        # noqa: E402  (grouped with the constants)
    AXES, AXIS_NAME, AXIS_SIGN, to_controller, to_stage,
)

STEP_PRESETS = [0.1, 1.0, 10.0, 100.0, 1000.0]

# Keyboard equivalent of each jog button, keyed "A+" / "C-" as _jog_btns is.
# Named in the tooltips so the binding is discoverable from the panel itself.
JOG_KEY_NAME = {
    "A+": "Right",   "A-": "Left",
    "B+": "Up",      "B-": "Down",
    "C+": "Page Up", "C-": "Page Down",
}

# If the button is released within this many ms it counts as a "click"
# and fires a fixed step-move.  Held longer → continuous jog.
_JOG_HOLD_MS = 150

# ── Z retract / insert ───────────────────────────────────────────────────────
#
# The Z limit switch at the stage's minus end sits BEYOND the scale tape, so the
# last stretch of the way there is blind: _TP is not a valid reading once the
# head runs off, and no closed-loop move can be aimed at it. Retract therefore
# runs closed loop to 0 um, where the encoder is still good, and OPEN LOOP from
# there - a plain jog, stopped by the limit switch itself.
#
# Nothing stops a jog except the poll loop noticing the switch, and that is one
# cycle (~165 ms) later, so the overshoot is speed x cycle: ~100 um at the 4000
# counts/s jog default, ~77 um here. It is open loop and past the tape, so that
# overshoot cannot be measured or corrected afterwards - only avoided.
#
# Raised from 500 to 1000 on 2026-09-24 (94 s of blind jog was too long to
# watch), then to 3000 on 2026-10-08: the full round trip still took about a
# minute. The user accepted ~77 um past the switch for it, on the grounds that
# the retract is still stopped by the switch itself. 3000 counts/s is 469 um/s,
# so the ~7.4 mm to the switch takes about 16 s.
BLIND_JOG_SPEED = 3000         # counts/s for both blind phases

# How far a blind jog may run before it is given up on. The switch sits about
# 7.4 mm past 0 um (measured on the rig, 2026-09-24 - the 100 s that run
# reported includes the closed-loop approach to 0 um, the blind part was 94 s),
# so this is that with margin: enough for the real move, short enough to bound
# a runaway.
#
# It is a DISTANCE, not a time, although the check itself is a timeout - open
# loop, elapsed time IS distance. Writing the bound as the distance means
# retuning BLIND_JOG_SPEED cannot silently change how far a runaway may travel,
# which a hardcoded number of seconds would.
#
# Until the switch trips this is the ONLY thing bounding the move: a broken wire
# reads ACTIVE under CN=1 and would stop it, but an unplugged one does not.
BLIND_MAX_TRAVEL_UM = 10_500.0

# Insert stops when the encoder says its data is valid again, i.e. bit 3 of the
# BiSS status word is clear. Not bit 2 (warning): B and C sit at warning at rest
# on this rig, so requiring a clean word would never be satisfied.
_ENC_ERROR_BIT = 1 << 3
# ...and it must stay clear this many polls running. The error bit was measured
# INTERMITTENT near the tape end (9 of 75 reads on 2026-09-24), so a single
# clean read is not evidence the head is back on.
_ENC_OK_POLLS = 3

# On connect the retract state is CHECKED against the rig, not just loaded from
# disk: the file is per PC, so a retract done from another laptop is invisible
# to it (2026-10-08: retracted from a laptop, controller moved to this PC, which
# offered Retract Z on a head already parked off the tape).
#
# The encoder is the evidence. Off the tape the BiSS frame is flagged (error or
# timeout bit) or decodes to nonsense, so the head counts as ON the tape only if
# every one of _RZ_CHECK_READS reads is unflagged AND within the tape length of
# the micron origin. Any doubt lands in OFF_TAPE, the state whose move recovers.
# Ten reads rather than the insert's three, because a corrupted frame near the
# tape end passed unflagged on 66 of 75 reads (2026-09-24); the position test
# is what catches that one - it was 2^23 counts (655 mm) out.
_ENC_TIMEOUT_BIT = 1 << 0
_RZ_CHECK_READS = 10

# Retract/insert states. The button is the whole UI for this, so its text says
# which one is next: "Retract Z" while the head is on the tape, "Insert Z" once
# it is parked off the end.
_RZ_ON_TAPE   = "on_tape"      # idle, button offers Retract
_RZ_RETRACTING = "retracting"  # closed loop to 0 um, then blind to the switch
_RZ_OFF_TAPE  = "off_tape"     # parked at the switch, button offers Insert
_RZ_INSERTING = "inserting"    # blind back until the encoder reads valid


class JogPanel(QWidget):
    def __init__(self, controller, worker, settings, position_memory: PositionMemory,
                 parent=None, zeros: ZeroOffsets | None = None):
        super().__init__(parent)
        self.controller = controller
        self.worker = worker
        self.settings = settings
        self.memory = position_memory
        # Focusable, so widgets that borrow the jog keys can hand them back.
        self.setFocusPolicy(Qt.StrongFocus)
        # Display-only zero per axis, persisted so it survives a restart. The
        # main window passes ITS instance so the soft travel limits measure from
        # the same origins this panel displays from; a second instance would
        # drift the moment either wrote.
        self.zeros = zeros if zeros is not None else ZeroOffsets()
        # Raw counts behind the counts column, per axis, as last displayed.
        self._raw_counts: dict[str, float] = {}
        self._current_status: dict[str, AxisStatus] = {}
        self._step_um = 10.0
        self._speed = int(settings.axis_a.speed)
        self._step_btns: list[QPushButton] = []
        # Jog buttons keyed by "A+" / "A-" / "B+" / "B-" / "C+" / "C-"
        self._jog_btns: dict[str, QPushButton] = {}
        self._pos_lbls: dict[str, QLabel] = {}
        self._pos_um_lbls: dict[str, QLabel] = {}

        # Axes currently jogging because a jog key is held down.
        self._key_jogging: dict[str, int] = {}

        # Z retract/insert, restored from disk: nothing on the rig can be asked
        # whether the head is off the tape, because _TP is not a valid reading
        # while it is. See utils/z_retract_state.py for why OFF_TAPE is written
        # at the START of a retract and ON_TAPE only when an insert completes.
        self._rz_state = (_RZ_OFF_TAPE
                          if z_retract_state.load() == z_retract_state.OFF_TAPE
                          else _RZ_ON_TAPE)
        self._rz_started = 0.0
        self._rz_enc_ok_count = 0
        # The on-connect check (check_z_retract): None when not running, else
        # the per-read verdicts collected so far.
        self._rz_check: list | None = None
        self._rz_check_polls = 0

        self._build_ui()
        # A restored OFF_TAPE has to reach the Z buttons now, not a poll later.
        self._apply_z_lock()
        if self._rz_state != _RZ_ON_TAPE and self.worker.soft_limits is not None:
            self.worker.soft_limits.suspend("C")     # _TP is not a position
        self.worker.jog_button_state.connect(self._on_jog_button_state_changed)
        # A retract/insert is driven by the poll, so a command that RAISES would
        # otherwise leave the button stuck until the blind timeout - three
        # minutes of a dead control with nothing said. Fail it immediately.
        self.worker.error_occurred.connect(self._on_worker_error)
        self._refresh_memory_list()

        # Application-wide, so the keys work without the panel holding focus.
        QApplication.instance().installEventFilter(self)

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
        # Give the arrows back to jogging once the value has been entered,
        # otherwise the box keeps them for as long as it stays focused.
        self._custom_step.editingFinished.connect(self.setFocus)
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

        dpad.addWidget(btn_yp, 0, 1)
        dpad.addWidget(btn_xm, 1, 0)
        dpad.addWidget(btn_xp, 1, 2)
        dpad.addWidget(btn_ym, 2, 1)
        col.addLayout(dpad)

        # Speed slider
        speed_grp = QGroupBox("Jog Speed")
        sg = QVBoxLayout(speed_grp)
        self._speed_slider = QSlider(Qt.Horizontal)
        # No keyboard focus: this is a mouse control, and the arrows belong to
        # jogging. Without this, adjusting the speed left the slider focused and
        # the next arrow press moved the slider instead of the stage.
        self._speed_slider.setFocusPolicy(Qt.NoFocus)
        self._speed_slider.setToolTip(
            "Speed for KEYBOARD jogging only (arrows for X/Y, Page Up/Down for "
            "Z) - the step buttons use the configured axis speed.")
        self._speed_slider.setRange(100, 50000)
        self._speed_slider.setValue(self._speed)
        self._speed_lbl = QLabel(f"{self._speed} counts/s")
        self._speed_slider.valueChanged.connect(self._on_speed_changed)
        sg.addWidget(self._speed_slider)
        sg.addWidget(self._speed_lbl)
        col.addWidget(speed_grp)

        # Live position readout
        pos_grp = QGroupBox("Current Positions")
        pg_l = QGridLayout(pos_grp)
        for i, ax in enumerate(AXES):
            # Stage names, not controller letters. The logs, the hardware test
            # scripts and every _TP/_LF operand still say A/B/C, so the mapping
            # lives in the tooltip rather than being lost.
            ax_lbl = QLabel(f"{AXIS_NAME[ax]}:")
            ax_lbl.setToolTip(f"Stage {AXIS_NAME[ax]} is controller axis {ax} "
                              f"(_TP{ax}, _LF{ax} in the logs)")
            pg_l.addWidget(ax_lbl, i, 0)
            lbl = QLabel("0.00")
            lbl.setFont(QFont("Consolas", 12))
            lbl.setObjectName(f"axis_{ax}")
            pg_l.addWidget(lbl, i, 1)
            # "enc counts", not just "counts": this column is _TP, the ENCODER
            # at 12.8 counts/um. The motor is 6.4 counts/um, so a 10 um move is
            # 64 microsteps commanded but 128 counts shown here. Two scales, one
            # word - which is exactly how they get conflated.
            enc_lbl = QLabel("enc counts")
            enc_lbl.setToolTip("Encoder counts (_TP) at 12.8 counts/um, measured "
                               "from the ZERO point in the STAGE direction - so "
                               "Y and Z read the opposite sign to raw _TP in the "
                               "logs. The motor is 6.4 counts/um, so 10 um = 64 "
                               "microsteps commanded and 128 encoder counts here.")
            pg_l.addWidget(enc_lbl, i, 2)

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
            f"Hold: continuous jog at {self._speed} counts/s\n"
            f"Key: hold {JOG_KEY_NAME[axis + ('+' if direction > 0 else '-')]}"
        )

        # Mutable state shared by the closures
        state = {"jogging": False}
        timer = QTimer()
        timer.setSingleShot(True)

        def _start_jog():
            state["jogging"] = True
            speed_now = int(to_controller(axis, self._speed * direction))
            self.worker.queue_command(self.controller.jog, axis, speed_now)

        def on_press():
            state["jogging"] = False
            timer.start(_JOG_HOLD_MS)

        def on_release():
            if timer.isActive():
                # Released before hold threshold → click → step move
                timer.stop()
                um_to_move = float(to_controller(axis, self._step_um * direction))

                # No special path at a limit any more. The direction that would
                # drive further in has its button disabled by the limit logic in
                # on_status_updated(), and move_microns() stops the axis itself
                # if the switch trips mid-move. Escaping is an ordinary move in
                # the opposite direction.
                self.worker.queue_command(
                    self.controller.move_microns,
                    axis,
                    um_to_move,
                    settle_window_ms=JOG_SETTLE_WINDOW_MS,
                    button_state_callback=self.worker.jog_button_state.emit,
                )
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
                self.worker.queue_command(self.controller.jog, "C",
                                          to_controller("C", self._speed * direction))

            def on_press():
                state["jogging"] = False
                timer.start(_JOG_HOLD_MS)

            def on_release():
                if timer.isActive():
                    timer.stop()
                    axis = "C"
                    self.worker.queue_command(
                        self.controller.move_microns,
                        axis,
                        float(to_controller("C", self._step_um * direction)),
                        settle_window_ms=JOG_SETTLE_WINDOW_MS,
                        button_state_callback=self.worker.jog_button_state.emit,
                    )
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
            f"Hold: continuous jog at {self._speed} counts/s\n"
            f"Key: hold {JOG_KEY_NAME['C+']}"
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

        z_enc_lbl = QLabel("enc counts", alignment=Qt.AlignCenter)  # type: ignore[call-arg]
        z_enc_lbl.setToolTip("Encoder counts (_TP) at 12.8 counts/um, not motor "
                             "microsteps at 6.4. Measured from the ZERO point in "
                             "the STAGE direction, which for Z is the opposite "
                             "sign to raw _TP in the logs.")
        col.addWidget(z_enc_lbl)
        col.addWidget(QLabel("µm", alignment=Qt.AlignCenter))  # type: ignore[call-arg]

        btn_zm = QPushButton("Z−\n▼")
        btn_zm.setObjectName("btn_jog")
        btn_zm.setMinimumHeight(72)
        btn_zm.setToolTip(
            f"Click: step {self._step_um:g} \u00b5m\n"
            f"Hold: continuous jog at {self._speed} counts/s\n"
            f"Key: hold {JOG_KEY_NAME['C-']}"
        )
        self._jog_btns["C-"] = btn_zm
        _wire_z_btn(btn_zm, -1)
        col.addWidget(btn_zm)

        # Retract / Insert Z. One button, because the two are a toggle: whichever
        # it offers is the only one that makes sense from where the head is.
        col.addSpacing(10)
        self._btn_rz = QPushButton()
        self._btn_rz.setMinimumHeight(40)
        self._btn_rz.clicked.connect(self._on_retract_clicked)
        col.addWidget(self._btn_rz)
        self._rz_note = QLabel("", alignment=Qt.AlignCenter)  # type: ignore[call-arg]
        self._rz_note.setWordWrap(True)
        self._rz_note.setStyleSheet("color: #8B949E; font-size: 8pt;")
        col.addWidget(self._rz_note)
        self._refresh_rz_button()

        col.addStretch()
        return col

    # Right column ───────────────────────────────────────────────────────────

    def _build_right_col(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(8)

        # Limit switch table
        lim_grp = QGroupBox("Limit Switch Status")
        lv = QVBoxLayout(lim_grp)
        # No HOME column: _HM is never polled (poll_all_axes reads only _TP and
        # _LF), so it could only ever have shown CLR on real hardware. There is
        # no home switch on this rig at all - homing is a closed-loop move to
        # the counts zero, not a switch seek.
        self._lim_table = QTableWidget(3, 3)
        self._lim_table.setHorizontalHeaderLabels(["Axis", "FWD", "REV"])
        self._lim_table.verticalHeader().setVisible(False)
        self._lim_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._lim_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._lim_table.setSelectionMode(QTableWidget.NoSelection)
        for r, ax in enumerate(AXES):
            self._lim_table.setItem(r, 0, QTableWidgetItem(ax))
            for c in range(1, 3):
                self._lim_table.setItem(r, c, QTableWidgetItem("CLR ●"))
        # Exactly as tall as its three rows. The old 120 px cap clipped the C
        # row behind a scrollbar, and anything looser lets the layout squeeze it.
        self._lim_table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._lim_table.setFixedHeight(
            self._lim_table.horizontalHeader().sizeHint().height()
            + sum(self._lim_table.rowHeight(r) for r in range(len(AXES)))
            + 2 * self._lim_table.frameWidth())
        lv.addWidget(self._lim_table)

        # Limit alert
        self._lim_alert = QLabel("")
        self._lim_alert.setStyleSheet("color: #FF4C4C; font-weight: bold;")
        self._lim_alert.setWordWrap(True)
        self._lim_alert.setVisible(False)
        lv.addWidget(self._lim_alert)
        col.addWidget(lim_grp)

        # Touch alarm. The manipulator hand shorts DI1 to ground when it touches
        # metal it should not, so the input reads 1 normally and 0 in alarm.
        # Display only - nothing here stops motion.
        touch_grp = QGroupBox("Touch Alarm")
        touch_row = QHBoxLayout(touch_grp)
        touch_lbl = QLabel("Touch Alarm")
        touch_lbl.setStyleSheet("font-weight: bold; font-size: 11pt;")
        touch_lbl.setToolTip(f"Digital input DI{TOUCH_ALARM_INPUT}: reads 1 normally "
                             f"(pulled up), 0 when the manipulator hand touches "
                             f"metal. Display only - it does not stop motion.")
        self._touch_box = QLabel("—", alignment=Qt.AlignCenter)  # type: ignore[call-arg]
        self._touch_box.setMinimumSize(110, 34)
        self._set_touch_box(None)
        touch_row.addWidget(touch_lbl)
        touch_row.addStretch()
        touch_row.addWidget(self._touch_box)
        col.addWidget(touch_grp)

        # Homing
        home_grp = QGroupBox("Homing")
        hg = QGridLayout(home_grp)
        for i, ax in enumerate(AXES):
            lbl = AXIS_NAME[ax]
            btn = QPushButton(f"HOME {lbl}")
            btn.setToolTip(f"Drive {lbl} (controller axis {ax}) back to where "
                           f"its counts display reads 0, closed loop. Requires "
                           f"the axis to have been zeroed first.")
            btn.clicked.connect(lambda checked, a=ax: self._home_axis(a))
            hg.addWidget(btn, 0, i)

        # Per-axis display zero. These do NOT move the stage and do not talk to
        # the controller: they record where the axis is now and subtract it from
        # the counts column. See utils/zero_offsets.py for why not DP.
        for i, ax in enumerate(AXES):
            lbl = AXIS_NAME[ax]
            btn = QPushButton(f"ZERO {lbl}")
            btn.setToolTip(f"Set the {lbl} ENCODER COUNTS display to 0 here. "
                           f"Microns are measured from the rig's true zero and "
                           f"are not affected. Display only, nothing moves.")
            btn.clicked.connect(lambda checked, a=ax: self._zero_axis(a))
            hg.addWidget(btn, 1, i)
        col.addWidget(home_grp)

        # Position memory
        mem_grp = QGroupBox("Position Memory")
        mv = QVBoxLayout(mem_grp)
        self._mem_list = QListWidget()
        # The window opens at its content's size, so give the list its height
        # explicitly - otherwise it shrinks to its hint and scrolls at 3 slots.
        self._mem_list.setMinimumHeight(120)
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

    # ── Keyboard jogging ─────────────────────────────────────────────────────
    #
    # Hold a key, the axis runs; let go, it stops. Right/Left drive X (A),
    # Up/Down drive Y (B), Page Up/Page Down drive Z (C) - the third axis is on
    # the page keys because the arrows are a two-axis pad and Z is the one that
    # moves out of that plane. All of them run at the Jog Speed slider's
    # setting - the slider governs ONLY this, never the step buttons, which keep
    # the configured axis speed.
    #
    # Implemented as an application-wide event filter rather than keyPressEvent,
    # because the panel rarely holds focus - the user has just clicked a button
    # or a list. The filter is deliberately narrow: it does nothing unless this
    # panel is visible and its window is active, and it stands aside for widgets
    # that legitimately use these keys themselves (the step spin box, the
    # saved-position list), otherwise it would hijack them. The spin box and the
    # list take Page Up/Page Down as well as the arrows, so one stand-aside rule
    # covers both sets.

    JOG_KEYS = {
        Qt.Key_Right:    ("A", +1),
        Qt.Key_Left:     ("A", -1),
        Qt.Key_Up:       ("B", +1),
        Qt.Key_Down:     ("B", -1),
        Qt.Key_PageUp:   ("C", +1),
        Qt.Key_PageDown: ("C", -1),
    }

    def eventFilter(self, obj, event):
        et = event.type()
        if et not in (QEvent.KeyPress, QEvent.KeyRelease):
            # Releasing the window while a key is held would otherwise leave the
            # axis running with no key to lift.
            if et in (QEvent.WindowDeactivate, QEvent.ApplicationDeactivate):
                self._stop_all_key_jogs()
            return False

        mapping = self.JOG_KEYS.get(event.key())
        if mapping is None or not self._keys_are_mine():
            return False

        # Auto-repeat fires press/release continuously while a key is held. Only
        # the first press and the true release matter.
        if event.isAutoRepeat():
            return True

        axis, direction = mapping
        if et == QEvent.KeyPress:
            self._start_key_jog(axis, direction)
        else:
            self._stop_key_jog(axis)
        return True      # consume it, so the key does not also move a widget

    def _keys_are_mine(self) -> bool:
        """Only claim the jog keys when nothing else needs them."""
        if not self.isVisible() or not self.isActiveWindow():
            return False
        focus = QApplication.focusWidget()
        # These genuinely use the arrows and the page keys for their own editing
        # or navigation, so the jog filter stands aside while they hold focus.
        #
        # QSlider is deliberately NOT in this list: the speed slider is set to
        # NoFocus precisely so these keys stay with jogging. Any slider that is
        # given focus here will swallow them - and a slider takes Page Up/Page
        # Down too - which is the bug reported on 2026-09-23: adjust the speed,
        # press an arrow, and the slider moved instead of the stage.
        return not isinstance(focus, (QAbstractSpinBox, QAbstractItemView, QLineEdit))

    def _start_key_jog(self, axis: str, direction: int):
        if self._key_jogging.get(axis):
            return                      # already running in some direction
        if not self._direction_allowed(axis, direction):
            why = ("Z is retracted" if self._z_manual_locked(axis)
                   else "that direction is at a limit")
            logger.info(f"Key jog {axis}{'+' if direction > 0 else '-'} refused: "
                        f"{why}")
            return
        self._key_jogging[axis] = direction
        self.worker.queue_command(self.controller.jog, axis,
                                  float(to_controller(axis,
                                                      self._speed * direction)))

    def _stop_key_jog(self, axis: str):
        if not self._key_jogging.pop(axis, 0):
            return
        # Ahead of anything still queued: a stop must not wait behind moves.
        self.worker.clear_queue()
        self.worker.queue_command(self.controller.halt_axis, axis)

    def _stop_all_key_jogs(self):
        for axis in list(self._key_jogging):
            self._stop_key_jog(axis)

    # ---- Z retract / insert ----
    #
    # Driven by the POLL, not by a blocking worker command. A blind jog is
    # stopped by the poll loop noticing the limit switch, so a blocking
    # implementation would freeze the very loop that has to stop it - and would
    # swallow HALT for the whole retraction, on the one move that is open loop
    # and past the end of the scale.
    #
    # So each press queues one short command, and the state advances in
    # on_status_updated() as the flags come back.

    def _set_rz_state(self, state: str):
        """The ONLY place _rz_state changes: button, Z lock and disk together.

        Persisting the transient states as their conservative stable equivalent
        is what makes a crash mid-retract safe - anything that is not ON_TAPE is
        stored as OFF_TAPE, so a kill at any point leaves the state that offers
        the recovery move.
        """
        self._rz_state = state
        self._refresh_rz_button()
        self._apply_z_lock()
        # Soft limits follow the same line: off the tape _TP is garbage and a
        # bound evaluated against it fires on nonsense - which is how a parked
        # head came to show BOTH Z limits at once.
        if self.worker.soft_limits is not None:
            if state == _RZ_ON_TAPE:
                self.worker.soft_limits.resume("C")
            else:
                self.worker.soft_limits.suspend("C")
        z_retract_state.save(z_retract_state.ON_TAPE if state == _RZ_ON_TAPE
                             else z_retract_state.OFF_TAPE)

    def _refresh_rz_button(self):
        """Text, colour and enabled state all follow _rz_state."""
        busy = self._rz_state in (_RZ_RETRACTING, _RZ_INSERTING)
        if self._rz_state in (_RZ_ON_TAPE, _RZ_RETRACTING):
            self._btn_rz.setText("Retract Z")
            colour = "#FF4C4C"
            tip = (f"Drive Z to 0 um, then jog blind at {BLIND_JOG_SPEED} "
                   f"counts/s until the limit switch stops it. The last stretch "
                   f"is past the end of the scale tape, so it is open loop.")
        else:
            self._btn_rz.setText("Insert Z")
            colour = "#3DBA6F"
            tip = (f"Jog Z back at {BLIND_JOG_SPEED} counts/s until the encoder "
                   f"reads valid again - back on the tape, at about 0 um.")
        self._btn_rz.setStyleSheet(f"color: {colour}; font-weight: bold;")
        self._btn_rz.setEnabled(not busy and self._rz_check is None
                                and self._z_motor_enabled())
        if not busy and not self._z_motor_enabled():
            tip += chr(10) * 2 + "Disabled: press Enable Motors first."
        self._btn_rz.setToolTip(tip)

    def _confirm(self, title: str, question: str) -> bool:
        """A Yes/No prompt, defaulting to NO.

        Defaulting to No matters: these guard a move that drives the head off
        the scale tape, so Enter or Escape on a dialog nobody read has to mean
        "don't". It is a method rather than an inline QMessageBox so tests can
        replace it - a modal blocks an offscreen test run outright.
        """
        return QMessageBox.question(
            self, title, question,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No) == QMessageBox.Yes

    def _z_motor_enabled(self) -> bool:
        """Has Enable Motors armed C in this session?

        A retract or insert queued with C off moves nothing, but the button
        used to flip regardless and record a state that was not true. Read from
        the controller's own record of what enable_axes() did, not polled, so it
        costs no round trip.
        """
        return "C" in getattr(self.controller, "enabled_axes", ())

    def _on_retract_clicked(self):
        if not self._z_motor_enabled():
            logger.warning("Retract/Insert Z refused: Z motor is not enabled")
            self._refresh_rz_button()
            return
        # Both prompts are idiot-proofing on a move that cannot be undone by
        # looking at the encoder afterwards: once the head is off the tape there
        # is no reading to tell you where it went.
        if self._rz_state == _RZ_ON_TAPE:
            if not self._confirm("Retract Z", "Are you sure to retract Z?"):
                logger.info("Retract Z: cancelled at the prompt")
                return
            self._start_retract()
        elif self._rz_state == _RZ_OFF_TAPE:
            if not self._confirm("Insert Z", "Is the Vacuum valve opened?"):
                logger.info("Insert Z: cancelled at the prompt")
                return
            self._start_insert()

    def _start_retract(self):
        target = self.zeros.get_um_origin("C")
        if target == 0.0:
            # An absolute encoder never reads 0 on this rig, so 0 means the
            # origin has never been set, not that it sits at zero.
            QMessageBox.warning(
                self, "No micron origin",
                "Z has no micron origin set, so there is no 0 um to retract "
                "from." + chr(10) + chr(10) +
                "Run set_micron_origin.py C with the stage parked at zero.")
            logger.warning("Retract Z: no micron origin for C, refusing")
            return

        # _TP is meaningless once the head leaves the tape, so a bound evaluated
        # against it would fire on garbage and halt this very move.
        if self.worker.soft_limits is not None:
            self.worker.soft_limits.suspend("C")

        # Before anything is queued: clear_queue() inside _stop_key_jog would
        # otherwise throw away the retract command queued below.
        self._stop_key_jog("C")

        self._rz_started = time.monotonic()
        self._set_rz_state(_RZ_RETRACTING)      # persists OFF_TAPE straight away
        self._rz_note.setText("moving to 0 um...")
        logger.info(f"Retract Z: to 0 um ({target:.0f} counts), then blind at "
                    f"{BLIND_JOG_SPEED} counts/s to the limit switch")
        self.worker.queue_command(self._retract_sequence, target)

    def _retract_sequence(self, target_counts: float):
        """Runs ON THE WORKER THREAD: closed loop to 0 um, then start the jog.

        One queued command for both, so the blind jog cannot begin before the
        axis has arrived. It RETURNS with the jog running - the poll loop is
        what watches for the switch and halts it.

        The blind clock is restarted HERE, once the jog is running. It used to
        run from the button press, so the closed-loop approach counted as blind
        travel: from +40 mm the approach took 51.6 s, longer than the whole
        ~22 s blind allowance, and the retract aborted 0.1 s into the jog
        (2026-10-08). Safe to write from this thread - the GUI only reads it in
        _rz_tick(), on a poll, and no poll runs until this returns.
        """
        self.controller.move_to_counts("C", target_counts,
                                       settle_window_ms=JOG_SETTLE_WINDOW_MS)
        self.controller.jog("C", to_controller("C", -1) * BLIND_JOG_SPEED)
        self._rz_started = time.monotonic()

    def _insert_return_sequence(self, target_counts: float):
        """Runs ON THE WORKER THREAD: stop the blind jog, then close the loop on 0 um.

        The jog stops wherever the encoder first reads valid again, which is
        not a fixed place - it depends on where the tape's good region starts
        and on ~3 poll cycles of further travel. Finishing at the micron origin
        makes every insert end at the same spot, the spot the retract left from.

        One queued command for both, so the closed-loop move cannot start while
        the jog is still running. move_to_counts() waits for the axis to stand
        still before it reads _TP. An origin of 0 has never been set (an
        absolute encoder never reads 0 on this rig), so it only halts.
        """
        self.controller.halt_axis("C")
        if target_counts == 0.0:
            logger.warning("Insert Z: no micron origin for C, stopping where "
                           "the tape was found")
            return
        self.controller.move_to_counts("C", target_counts,
                                       settle_window_ms=JOG_SETTLE_WINDOW_MS)

    def _start_insert(self):
        if self.worker.soft_limits is not None:
            self.worker.soft_limits.suspend("C")     # still off the tape
        self.worker.watch_encoder.add("C")           # one extra read per poll
        self._rz_enc_ok_count = 0
        self._stop_key_jog("C")
        self._rz_started = time.monotonic()
        self._set_rz_state(_RZ_INSERTING)       # still OFF_TAPE on disk
        self._rz_note.setText("jogging back to the tape...")
        logger.info(f"Insert Z: blind at {BLIND_JOG_SPEED} counts/s until the "
                    f"encoder reads valid")
        self.worker.queue_command(self.controller.jog, "C",
                                  to_controller("C", +1) * BLIND_JOG_SPEED)

    def _blind_speed_um_s(self) -> float:
        return BLIND_JOG_SPEED / self.settings.axis_c.counts_per_um

    def _blind_timeout_s(self) -> float:
        """BLIND_MAX_TRAVEL_UM expressed in seconds at the blind jog speed.

        Derived rather than stored, so the bound stays a distance however the
        speed is tuned. About 134 s for 10.5 mm at 500 counts/s.
        """
        return BLIND_MAX_TRAVEL_UM / self._blind_speed_um_s()

    def _rz_finish(self, state: str, note: str, level=None):
        """Land in `state` and stop watching what that state does not need."""
        level = level or logger.info
        elapsed = time.monotonic() - self._rz_started
        travel_um = elapsed * self._blind_speed_um_s()
        self.worker.watch_encoder.discard("C")
        self._set_rz_state(state)     # hands Z back only when state is ON_TAPE
        self._rz_note.setText(note)
        level(f"Retract/insert: {note} after {elapsed:.1f} s of blind jog "
              f"(about {travel_um:.0f} um at {BLIND_JOG_SPEED} counts/s)")
        if state == _RZ_ON_TAPE and self.worker.soft_limits is not None:
            self.worker.soft_limits.resume("C")      # _TP is trustworthy again

    def _rz_abort(self, why: str):
        self.worker.clear_queue()
        self.worker.queue_command(self.controller.halt_axis, "C")
        logger.error(f"Z retract/insert aborted: {why}")
        # An aborted retract OR insert leaves the head somewhere unknown, so it
        # lands in OFF_TAPE - the state whose button offers the recovering move -
        # and then asks the encoder, which corrects it if the head is on the
        # tape after all. Guessing from which move was running is how the state
        # came to say inserted while the head was retracted, or the reverse.
        self._rz_finish(_RZ_OFF_TAPE, f"stopped: {why}", level=logger.error)
        self.check_z_retract()

    def _on_worker_error(self, message: str):
        if self._rz_state in (_RZ_RETRACTING, _RZ_INSERTING):
            self._rz_abort(message)

    def _rz_tick(self, status: dict):
        """One poll's worth of progress. Called from on_status_updated()."""
        if self._rz_state not in (_RZ_RETRACTING, _RZ_INSERTING):
            return
        s = status.get("C")
        if s is None:
            return

        if not self._z_motor_enabled():
            # E-STOP or Disable Motors mid-sequence: nothing is moving any more,
            # so waiting for the switch or the tape would only end in a timeout.
            self._rz_abort("Z motor disabled")
            return

        if time.monotonic() - self._rz_started > self._blind_timeout_s():
            self._rz_abort(f"no end within {BLIND_MAX_TRAVEL_UM / 1000:.1f} mm")
            return

        if self._rz_state == _RZ_RETRACTING:
            # The limit in the direction being driven. _guard_jog_limits has
            # already issued the halt by the time the flag reaches us.
            if self._direction_blocked("C", -1):
                self._rz_finish(_RZ_OFF_TAPE, "Z Retracted")
            return

        # Inserting: wait for the encoder to call its own data valid, and to
        # KEEP saying so - the error bit was measured intermittent near the end
        # of the tape (9 of 75 reads on 2026-09-24), so one clean read is not
        # evidence that the head is back on.
        if s.enc_status is None:
            return
        if s.enc_status & (_ENC_ERROR_BIT | _ENC_TIMEOUT_BIT):
            self._rz_enc_ok_count = 0
            return
        self._rz_enc_ok_count += 1
        if self._rz_enc_ok_count >= _ENC_OK_POLLS:
            self.worker.clear_queue()
            self.worker.queue_command(self._insert_return_sequence,
                                      self.zeros.get_um_origin("C"))
            self._rz_finish(_RZ_ON_TAPE, "Z Inserted")

    # ---- Retract state, checked against the rig on connect ----

    def check_z_retract(self):
        """Start asking the encoder whether the Z head is on the tape.

        Called on every connect. The stored state is only this PC's memory of
        what IT did, so it is wrong whenever the rig was driven from elsewhere.
        Z's controls and the retract button stay locked until the answer is in,
        ~_RZ_CHECK_READS poll cycles (a couple of seconds).
        """
        if self._rz_state in (_RZ_RETRACTING, _RZ_INSERTING):
            return                      # mid-sequence: it is already watching
        self._rz_check = []
        self._rz_check_polls = 0
        self.worker.watch_encoder.add("C")
        if self.worker.soft_limits is not None:
            self.worker.soft_limits.suspend("C")     # until _TP is known good
        self._rz_note.setText("checking whether Z is retracted...")
        self._refresh_rz_button()
        self._apply_z_lock()

    def _enc_read_valid(self, s) -> bool:
        """One poll's verdict: is this a real on-tape position?"""
        if s.enc_status & (_ENC_ERROR_BIT | _ENC_TIMEOUT_BIT):
            return False
        origin = self.zeros.get_um_origin("C")
        if origin == 0.0:
            return True                 # no origin: the flag bits are all we have
        span = (self.settings.axis_c.encoder_length_mm * 1000.0
                * self.settings.encoder_counts_per_um)
        return abs(s.pos - origin) <= span

    def _rz_check_tick(self, status: dict):
        if self._rz_check is None:
            return
        s = status.get("C")
        if s is None:
            return
        self._rz_check_polls += 1
        if s.enc_status is not None:
            self._rz_check.append(self._enc_read_valid(s))
        elif self._rz_check_polls < 3 * _RZ_CHECK_READS:
            return                      # not read this cycle; wait for one that is
        else:
            self._rz_check.append(False)   # unreadable: assume the worst
        if len(self._rz_check) < _RZ_CHECK_READS:
            return

        bad = self._rz_check.count(False)
        self._rz_check = None
        self.worker.watch_encoder.discard("C")
        stored = self._rz_state
        switch = bool(s.fwd_limit or s.rev_limit)   # soft limits are suspended
        if bad:
            state, note = _RZ_OFF_TAPE, "Z is retracted (encoder off the tape)"
            # The switch is the retract end, whatever this PC last commanded -
            # otherwise a stale last_direction labels it the wrong end and
            # refuses the very jog that inserts.
            self.worker.queue_command(self.controller.declare_limit_end, "C",
                                      to_controller("C", -1))
        else:
            state, note = _RZ_ON_TAPE, ""
        detail = (f"{bad}/{_RZ_CHECK_READS} encoder reads invalid, limit switch "
                  f"{'pressed' if switch else 'clear'}, _TPC={s.pos:.0f}")
        if state != stored:
            logger.warning(f"Z retract check: stored state was {stored} but the "
                           f"rig says {state} ({detail}) - going with the rig")
        else:
            logger.info(f"Z retract check: {state} ({detail})")
        self._set_rz_state(state)
        self._rz_note.setText(note)

    # ── Touch alarm ──────────────────────────────────────────────────────────

    def _set_touch_box(self, active):
        """active: True = alarm (DI1 reads 0), False = clear, None = unknown."""
        if active is None:
            text, bg = "—", "#30363D"
        elif active:
            text, bg = "On", "#C62828"
        else:
            text, bg = "Off", "#2E7D32"
        self._touch_box.setText(text)
        self._touch_box.setStyleSheet(
            f"background-color: {bg}; color: white; font-weight: bold; "
            f"border-radius: 5px; padding: 6px 14px; font-size: 12pt;")

    @pyqtSlot(object)
    def on_touch_alarm(self, active):
        self._set_touch_box(active)

    # ── Limit blocking ───────────────────────────────────────────────────────

    def _direction_blocked(self, axis: str, sign: int) -> bool:
        """True if moving `sign` on `axis` would drive further into a limit.

        `sign` is a STAGE direction - the one written on the button - while the
        limit flags are in controller terms. The conversion happens once, here,
        because this is the single rule behind the button enable state, the
        limit table and the escape hint; converting at each caller instead is
        how they would come to disagree.

        Every axis has ONE switch, tripped at either end of travel, so the flags
        cannot say which end on their own. Resolving that against the last
        commanded direction is the CONTROLLER's job now (2026-09-24) - it hands
        over at most one of fwd_limit/rev_limit already decided, so all three
        axes read the same way here. This used to carry a separate A/B branch
        doing the last_direction arithmetic itself, which meant the rule lived
        in two places.
        """
        s = (self._current_status or {}).get(axis)
        if s is None:
            return False
        sign = to_controller(axis, sign)
        return (s.fwd_limit and sign > 0) or (s.rev_limit and sign < 0)

    def _z_manual_locked(self, axis: str) -> bool:
        """Are Z's own controls dead right now?

        From the moment Retract Z is pressed until Insert Z has completed, the
        head is off the scale tape or on its way there or back. _TP is not a
        position over that stretch, so a step move has nothing to close its loop
        on and a jog would run blind with the soft limits suspended. The only Z
        motion that makes sense in between is the retract/insert button itself.
        """
        return axis == "C" and (self._rz_state != _RZ_ON_TAPE
                                or self._rz_check is not None)

    def _direction_allowed(self, axis: str, sign: int) -> bool:
        """May the operator command `sign` on `axis` right now?

        Two independent reasons to say no, deliberately kept apart: a limit in
        that direction, or Z being locked out mid-retract. Folding the lock into
        _direction_blocked() would make the limit table read HIT for an axis
        that is merely busy, and the escape hint name a button for a limit that
        was never reached.
        """
        if self._z_manual_locked(axis):
            return False
        return not self._direction_blocked(axis, sign)

    def _apply_z_lock(self):
        """Push the lock to the Z buttons now, rather than a poll cycle later."""
        for sign in (+1, -1):
            self._set_direction_enabled("C", sign,
                                        self._direction_allowed("C", sign))

    def _set_direction_enabled(self, axis: str, sign: int, enabled: bool):
        """Enable/disable the control that moves `axis` in `sign`.

        `sign` is a STAGE direction: _jog_btns is keyed by what the button says,
        so no conversion happens here.

        Only the D-pad now. The step-move grid used to need blocking too, but it
        queued exactly what a D-pad click queues and has been removed.
        """
        btn = self._jog_btns.get(f"{axis}{'+' if sign > 0 else '-'}")
        if btn is not None:
            btn.setEnabled(enabled)

    # ── Homing ───────────────────────────────────────────────────────────────

    def _home_axis(self, axis: str):
        """Drive the axis to where its counts display reads 0.

        That position is the counts zero, an absolute _TP reading, so this is an
        ordinary closed-loop absolute move - move_to_counts() - and lands within
        a microstep like any other.

        NOT Galil's HM homing routine, which the button used to call: HM seeks a
        home switch, and this rig runs LD=3 with no reverse switch at all. And
        NOT raw _TP = 0 either: _TP is an absolute PRC coordinate, so axis A's
        zero sits around 3,226,664 counts and driving to a literal 0 would ask
        for 252 mm of travel on a 20 mm axis.
        """
        target = self.zeros.get(axis)
        if target == 0.0:
            # An absolute encoder never reads 0 on this rig - it reads millions -
            # so 0 means "no zero has been set", not "the zero is at 0".
            QMessageBox.warning(
                self, "Not zeroed",
                f"Axis {AXIS_NAME[axis]} has no zero set, so there is nowhere "
                f"to home to." + chr(10) + chr(10) +
                f"Park it where you want zero and press "
                f"ZERO {AXIS_NAME[axis]} first.")
            logger.warning(f"Home {axis}: no counts zero set, refusing")
            return

        here = self._raw_counts.get(axis)
        if here is not None:
            distance_um = counts_to_um(to_stage(axis, target - here),
                                       self.settings.encoder_counts_per_um)
            logger.info(f"Home {axis}: {target:.0f} counts, "
                        f"{distance_um:+.1f} um from here")

        # verify=True: aim short, wait out the post-move relaxation, then close
        # the remainder. Costs ~1 s, which is fine for a deliberate HOME and is
        # why jogging does not use it.
        self.worker.queue_command(
            self.controller.move_to_counts, axis, target,
            settle_window_ms=JOG_SETTLE_WINDOW_MS,
            verify=True,
        )

    # ── Display zero ─────────────────────────────────────────────────────────

    def _zero_axis(self, axis: str):
        """Make the COUNTS column read 0 at the present position.

        The micron column is measured from the true zero and is left alone; see
        utils/zero_offsets.py.

        Nothing is sent to the controller and nothing moves: the offset is
        recorded and subtracted at the display. See utils/zero_offsets.py for
        why this is not the controller's DP command.
        """
        raw = self._raw_counts.get(axis)
        if raw is None:
            logger.warning(f"Zero {axis}: no position shown yet, ignoring")
            return
        self.zeros.set_zero(axis, raw)
        # Repaint immediately rather than waiting for the next poll.
        if self._current_status:
            self.on_status_updated(self._current_status)

    # ── Slot connected to polling worker ─────────────────────────────────────

    @pyqtSlot(str, str, bool)
    def _on_jog_button_state_changed(self, axis: str, direction: str, enabled: bool):
        # move_microns() derives this from the sign of the microns it was given,
        # so it arrives in CONTROLLER terms and has to be converted back before
        # it names a button. Without this a +Y click would grey out the Y− one.
        sign = to_stage(axis, +1 if direction == "+" else -1)
        # Re-enabling at the end of a move must not undo a limit block. A move
        # that ended BECAUSE the switch tripped would otherwise leave the button
        # live until the next poll, which is exactly when it must not be.
        if enabled and not self._direction_allowed(axis, sign):
            return
        self._set_direction_enabled(axis, sign, enabled)

    def _show_position(self, ax: str, counts: float):
        """Paint one axis's position readouts from a raw _TP reading."""
        # Remember what the counts column was built from, so ZERO zeroes THAT
        # and not some other reading of the position.
        self._raw_counts[ax] = counts

        # Two independent origins, on purpose:
        #   counts  - the ZERO buttons, a working zero re-set at will
        #   microns - the rig's true zero, set once and left alone
        # so that re-zeroing the counts mid-job cannot silently move what
        # the micron reading means.
        # Both columns are signed distances FROM an origin, so they carry a
        # direction and are shown in stage terms. The origins themselves stay
        # raw _TP; only the difference is flipped. Without this the number
        # would count DOWN while the Y+ button drove the stage up.
        zeroed = to_stage(ax, self.zeros.apply(ax, counts))
        um = counts_to_um(to_stage(ax, self.zeros.apply_um(ax, counts)),
                          self.settings.encoder_counts_per_um)
        if ax in self._pos_lbls:
            self._pos_lbls[ax].setText(f"{zeroed:.1f}")
            self._pos_um_lbls[ax].setText(f"{um:.3f}")
        if ax == "C":
            self._z_pos_lbl.setText(f"{zeroed:.1f}")
            self._z_pos_um_lbl.setText(f"{um:.3f}")

    @pyqtSlot(str, float)
    def on_move_progress(self, axis: str, counts: float):
        """A position read mid-move, while the poll is blocked behind it.

        Position ONLY. The limit table, the button enable state and the Z
        retract state machine all wait for the next real poll: this reading
        carries no limit flags, and treating it as a poll would re-enable
        buttons on flags that were never read.
        """
        self._show_position(axis, counts)

    @pyqtSlot(dict)
    def on_status_updated(self, status: dict):
        self._current_status = status
        # Before the display: a retract/insert in progress decides here whether
        # it has finished, and _direction_blocked() below reads the same flags.
        self._rz_check_tick(status)
        self._rz_tick(status)
        self._refresh_rz_button()       # follows Enable/Disable Motors

        # Positions come from the poll signal, not from config/last_positions.json.
        # The file is rewritten by poll_all_axes() every cycle, so reading it here
        # raced the writer and could hand back a half-written file; it was also a
        # cycle behind, and in mock mode it showed whatever the last hardware
        # session left behind instead of the simulated stage.
        for ax, s in status.items():
            self._show_position(ax, s.pos)

        def _cell(active: bool) -> QTableWidgetItem:
            item = QTableWidgetItem("HIT ●" if active else "CLR ●")
            item.setForeground(QColor("#FF4C4C" if active else "#3DBA6F"))
            return item

        for r, ax in enumerate(AXES):
            if ax not in status:
                continue
            self._lim_table.setItem(r, 1, _cell(self._direction_blocked(ax, +1)))
            self._lim_table.setItem(r, 2, _cell(self._direction_blocked(ax, -1)))

        # Update jog button enable state and build escape-direction hints
        alert_lines: list[str] = []
        for ax, s in status.items():
            # One rule for every axis and both controls: a direction is live
            # unless it would drive further into a switch.
            for sign in (+1, -1):
                self._set_direction_enabled(ax, sign,
                                            self._direction_allowed(ax, sign))

            if not (s.fwd_limit or s.rev_limit):
                continue

            # Name the button that gets you off the switch, in the terms it is
            # labelled with. Driving this off _direction_blocked() - the same
            # rule that greys the buttons out - is what stops the hint pointing
            # at a disabled button, which naming the controller FWD/REV flag
            # directly would now do on the reversed axes.
            lbl = AXIS_NAME[ax]
            free = [sg for sg in (+1, -1) if not self._direction_blocked(ax, sg)]
            if free:
                way = "+" if free[0] > 0 else chr(0x2212)
                alert_lines.append(
                    f"Axis {ax} ({lbl}) LIMIT — jog {lbl}{way} to escape")
            else:
                alert_lines.append(
                    f"Axis {ax} ({lbl}) LIMIT — both directions blocked")

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
        
        # Saved slots hold absolute _TP readings, so they go straight to
        # move_all_absolute(), which closes the loop on each axis in turn. The
        # panel used to subtract the current position here and the controller
        # subtracted it AGAIN, so the stage moved (target - current) - current.
        self.worker.queue_command(
            self.controller.move_all_absolute,
            {"A": slot.pos_a, "B": slot.pos_b, "C": slot.pos_c},
            settle_window_ms=JOG_SETTLE_WINDOW_MS,
            verify=True,
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
        """Saved slots, shown in microns from the true zero.

        Slots store absolute _TP so they identify a physical place, but _TP is a
        PRC coordinate in the millions and means nothing to read. The micron
        origin is what makes it legible, so the list shows the same numbers the
        position display does.
        """
        self._mem_list.clear()
        cpu = self.settings.encoder_counts_per_um
        for slot in self.memory.all_slots():
            um = {ax: counts_to_um(to_stage(ax, self.zeros.apply_um(ax, raw)), cpu)
                  for ax, raw in (("A", slot.pos_a), ("B", slot.pos_b),
                                  ("C", slot.pos_c))}
            text = (f"{slot.name}   X:{um['A']:+.2f}  "
                    f"Y:{um['B']:+.2f}  Z:{um['C']:+.2f} µm")
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, slot.name)
            item.setToolTip(
                f"Encoder counts (_TP): A={slot.pos_a:.0f}  "
                f"B={slot.pos_b:.0f}  C={slot.pos_c:.0f}" + chr(10) +
                f"Saved {slot.timestamp}")
            self._mem_list.addItem(item)
