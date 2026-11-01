"""
Panel 4 — Settings.

Feature 1: soft travel limits. A negative and a positive bound per axis, in
STAGE MICRONS from the micron origin - the same number the position display
shows, so what is typed here is what is read there. Reaching one raises the
matching limit flag exactly as a physical switch would: the button for that
direction greys out, the limit table lights up, and a running jog is halted.

The evaluation is not here - it is utils/soft_limits.py, applied by
PollingWorker to every poll result. This panel only edits the numbers.

A NOTE ON THE PANEL THIS REPLACES. The previous settings panel was deleted on
2026-09-23 because it was broken AND carried a live hazard: its _collect_settings
built ONE AxisSettings from six widgets and assigned it to all three axes, so a
working SAVE would have silently reset axis_c.travel_mm from 200 to 20 and every
encoder_length_mm to its default. Only an unrelated AttributeError stopped it.

This panel therefore mutates the two soft-limit fields on the existing
AxisSettings objects and touches nothing else. It never constructs an
AxisSettings, and it never assigns one axis's settings to another.
"""
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QDoubleSpinBox, QMessageBox,
    QAbstractItemView,
)
from PyQt5.QtCore import Qt, pyqtSignal
from loguru import logger

from utils.stage_axes import AXES, AXIS_NAME

# Wide enough for the 200 mm Z travel with room to spare, and the resolution is
# a tenth of a micron - finer than the 0.156 um microstep, so the bound is never
# the coarser of the two.
_LIMIT_RANGE_UM = 1_000_000.0
_LIMIT_STEP_UM = 10.0

# A spin box cannot hold None, so one value in the range has to mean "unset".
# It is the MINIMUM for BOTH columns, because setSpecialValueText only ever
# applies to a spin box's minimum - putting "no limit" at the top of the
# positive column would print the words at the bottom instead.
#
# The cost is that a positive limit of exactly -1 m cannot be expressed. That is
# three orders of magnitude outside any travel on this rig.
_DISABLED = -_LIMIT_RANGE_UM


class SettingsPanel(QWidget):
    """Edits settings in place. Emits settings_changed when SAVE succeeds."""

    settings_changed = pyqtSignal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._spins: dict[tuple[str, str], QDoubleSpinBox] = {}
        self._build_ui()
        self._load_from_settings()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.addWidget(self._build_soft_limits_group())
        root.addStretch()

    def _build_soft_limits_group(self) -> QGroupBox:
        grp = QGroupBox("Soft Travel Limits")
        v = QVBoxLayout(grp)

        blurb = QLabel(
            "Positions at which the software raises the limit flag itself, in "
            "microns from the zero the position display measures from.\n"
            "Reaching one blocks that direction until the axis moves back "
            "inside — the same behaviour as a physical end stop."
        )
        blurb.setWordWrap(True)
        v.addWidget(blurb)

        self._table = QTableWidget(len(AXES), 3)
        self._table.setHorizontalHeaderLabels(
            ["Axis", "Negative limit (µm)", "Positive limit (µm)"])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

        for row, ax in enumerate(AXES):
            name = QTableWidgetItem(f"{AXIS_NAME[ax]}  ({ax})")
            name.setFlags(Qt.ItemIsEnabled)
            name.setToolTip(f"Stage {AXIS_NAME[ax]} is controller axis {ax}")
            self._table.setItem(row, 0, name)

            for col, side in ((1, "neg"), (2, "pos")):
                spin = QDoubleSpinBox()
                spin.setRange(-_LIMIT_RANGE_UM, _LIMIT_RANGE_UM)
                spin.setDecimals(1)
                spin.setSingleStep(_LIMIT_STEP_UM)
                spin.setSuffix(" µm")
                # At the extreme the bound is off; say so instead of showing a
                # number nobody set.
                spin.setSpecialValueText("no limit")
                spin.setToolTip(
                    f"{'Lowest' if side == 'neg' else 'Highest'} "
                    f"{AXIS_NAME[ax]} position allowed, in microns from the "
                    f"display zero. Wind it all the way DOWN for no limit "
                    f"(the box then reads 'no limit').")
                self._spins[(ax, side)] = spin
                self._table.setCellWidget(row, col, spin)

        # Exactly as tall as its rows: three axes never scroll, and a fixed
        # minimum left an empty band under them.
        self._table.resizeRowsToContents()
        self._table.setFixedHeight(
            self._table.horizontalHeader().sizeHint().height()
            + sum(self._table.rowHeight(r) for r in range(len(AXES)))
            + 2 * self._table.frameWidth())
        v.addWidget(self._table)

        row = QHBoxLayout()
        row.addStretch()
        self._btn_revert = QPushButton("REVERT")
        self._btn_revert.clicked.connect(self._load_from_settings)
        row.addWidget(self._btn_revert)
        self._btn_save = QPushButton("SAVE")
        self._btn_save.setObjectName("btn_primary")
        self._btn_save.clicked.connect(self._save)
        row.addWidget(self._btn_save)
        v.addLayout(row)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        v.addWidget(self._status)
        return grp

    # ── Settings <-> widgets ────────────────────────────────────────────────

    def _axis_settings(self, axis: str):
        return getattr(self.settings, f"axis_{axis.lower()}")

    def _load_from_settings(self):
        """Fill the table from the live AppSettings. Also the REVERT action."""
        for ax in AXES:
            a = self._axis_settings(ax)
            neg = a.soft_limit_neg_um
            pos = a.soft_limit_pos_um
            self._spins[(ax, "neg")].setValue(
                _DISABLED if neg is None else float(neg))
            self._spins[(ax, "pos")].setValue(
                _DISABLED if pos is None else float(pos))
        self._status.setText("")

    def _values(self) -> dict:
        """{axis: (neg, pos)} as the settings would store them, None for unset."""
        out = {}
        for ax in AXES:
            neg = self._spins[(ax, "neg")].value()
            pos = self._spins[(ax, "pos")].value()
            out[ax] = (None if neg <= _DISABLED else neg,
                       None if pos <= _DISABLED else pos)
        return out

    def _problems(self, values: dict) -> list[str]:
        """Reject a range that cannot be satisfied, rather than storing it.

        A negative bound at or above the positive one leaves no legal position,
        so BOTH directions would be blocked at once and the axis could not be
        moved at all - including back out.
        """
        bad = []
        for ax, (neg, pos) in values.items():
            if neg is not None and pos is not None and neg >= pos:
                bad.append(
                    f"{AXIS_NAME[ax]}: negative limit ({neg:+.1f}) is not below "
                    f"the positive one ({pos:+.1f}) — that would block both "
                    f"directions with no way out")
        return bad

    def _save(self):
        values = self._values()
        problems = self._problems(values)
        if problems:
            QMessageBox.warning(self, "Limits not saved", "\n\n".join(problems))
            self._status.setText("Not saved — " + problems[0])
            return

        # Mutate the two fields in place on each axis's OWN settings object.
        # Never build an AxisSettings here: the panel this replaced did, and it
        # would have reset travel_mm and encoder_length_mm to their defaults.
        for ax, (neg, pos) in values.items():
            a = self._axis_settings(ax)
            a.soft_limit_neg_um = neg
            a.soft_limit_pos_um = pos

        try:
            path = self.settings.save_to_file()
        except Exception as e:
            logger.error(f"Could not save settings: {e}")
            QMessageBox.critical(self, "Save failed", str(e))
            self._status.setText(f"Save failed: {e}")
            return

        for ax, (neg, pos) in values.items():
            logger.info(f"Soft limits {AXIS_NAME[ax]} ({ax}): "
                        f"{'none' if neg is None else f'{neg:+.1f}'} .. "
                        f"{'none' if pos is None else f'{pos:+.1f}'} um")
        self._status.setText(f"Saved to {path}. In force from the next poll.")
        self.settings_changed.emit()
