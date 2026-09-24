"""Software direction reversal for Y (B) and Z (C).

The manipulator's physical +Y and +Z are the controller's -B and -C, so the jog
panel converts at its own boundary: buttons, keys and the two position columns
are in STAGE terms, everything handed to the controller is in CONTROLLER terms.

It is done here and not in the wiring or with MT=2.5 because the loop closes on
an ABSOLUTE encoder whose counting direction cannot be flipped to match - CE
reverses quadrature encoders only and SS (BiSS) has no direction argument - so
reversing the motor alone would make every correction in move_microns() drive
away from the target.

X (A) is NOT reversed, and it is in most of the tests below as the control: a
bug that flipped all three axes would pass a Y/Z-only check.

Run:  pytest tests/test_axis_direction.py -v
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from controller.mock_controller import MockGalilController


@pytest.fixture
def ctrl():
    c = MockGalilController()
    c.enable_axes("ABC")
    for ax in ("A", "B", "C"):          # PRC-scale room, not the +/-50k default
        c.set_trip_points(ax, 10_000_000.0, -10_000_000.0)
    return c


@pytest.fixture
def panel(ctrl, tmp_path):
    """A shown, active JogPanel with what it queues captured as (fn, args).

    Both stores it writes to are redirected into tmp_path. ZeroOffsets.set_zero()
    and PositionMemory.save() go straight to disk, so a test that zeroes an axis
    would otherwise rewrite the rig's real zeros in config/.
    """
    from PyQt5.QtWidgets import QApplication
    from controller.data_models import AppSettings
    from utils.position_memory import PositionMemory
    from utils.zero_offsets import ZeroOffsets
    from ui.panels.jog_panel import JogPanel
    from ui.polling_worker import PollingWorker

    app = QApplication.instance() or QApplication([])
    queued = []
    w = PollingWorker(ctrl)
    w.queue_command = lambda fn, *a, **k: queued.append((fn.__name__, a))
    w.clear_queue = lambda: None
    mem = PositionMemory(path=str(tmp_path / "positions.json"))
    p = JogPanel(ctrl, w, AppSettings(), mem)
    # The panel builds its own ZeroOffsets on the real config path in __init__.
    # Reading it is harmless; writing is not, so swap in a temp-backed one.
    p.zeros = ZeroOffsets(path=str(tmp_path / "zero_offsets.json"))
    p.show(); app.processEvents()
    p.activateWindow(); app.processEvents()
    p.on_status_updated(ctrl.poll_all_axes())
    p._queued = queued
    p._app = app
    p._ctrl = ctrl
    yield p
    p.hide()


# -- The constant itself -----------------------------------------------------

def test_only_y_and_z_are_reversed():
    from ui.panels.jog_panel import AXIS_SIGN
    assert AXIS_SIGN == {"A": +1, "B": -1, "C": -1}


def test_the_two_converters_are_inverses():
    from ui.panels.jog_panel import to_controller, to_stage
    for ax in ("A", "B", "C"):
        for v in (1.0, -7.5, 4000.0):
            assert to_stage(ax, to_controller(ax, v)) == v


# -- Jogging: a signed speed -------------------------------------------------

@pytest.mark.parametrize("key_name,axis,ctrl_sign", [
    ("Key_Right",    "A", +1),      # X is not reversed
    ("Key_Left",     "A", -1),
    ("Key_Up",       "B", -1),      # stage +Y is controller -B
    ("Key_Down",     "B", +1),
    ("Key_PageUp",   "C", -1),      # stage +Z is controller -C
    ("Key_PageDown", "C", +1),
])
def test_key_jog_speed_reaches_the_controller_reversed(panel, key_name, axis, ctrl_sign):
    from PyQt5.QtCore import Qt, QEvent
    from PyQt5.QtGui import QKeyEvent
    key = getattr(Qt, key_name)

    panel._queued.clear()
    panel.eventFilter(panel, QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier))
    assert panel._queued, f"{key_name} queued nothing"
    fn, args = panel._queued[0]
    assert fn == "jog"
    assert args[0] == axis, f"{key_name} drove {args[0]}, not {axis}"
    assert args[1] * ctrl_sign > 0, (
        f"{key_name} sent {args[1]:+g} counts/s to {axis}; "
        f"the controller should see sign {ctrl_sign:+d}")

    panel.eventFilter(panel, QKeyEvent(QEvent.KeyRelease, key, Qt.NoModifier))


def test_the_stage_direction_is_what_is_remembered(panel):
    """_key_jogging keys off the BUTTON, so it stays in stage terms."""
    from PyQt5.QtCore import Qt, QEvent
    from PyQt5.QtGui import QKeyEvent
    panel.eventFilter(panel, QKeyEvent(QEvent.KeyPress, Qt.Key_PageUp, Qt.NoModifier))
    assert panel._key_jogging["C"] == +1, "Page Up is stage +Z however it is sent"
    panel.eventFilter(panel, QKeyEvent(QEvent.KeyRelease, Qt.Key_PageUp, Qt.NoModifier))


# -- Step clicks: a signed distance ------------------------------------------

@pytest.mark.parametrize("btn_key,axis,ctrl_sign", [
    ("A+", "A", +1), ("A-", "A", -1),
    ("B+", "B", -1), ("B-", "B", +1),
    ("C+", "C", -1), ("C-", "C", +1),
])
def test_step_click_microns_reach_the_controller_reversed(panel, btn_key, axis, ctrl_sign):
    btn = panel._jog_btns[btn_key]
    panel._queued.clear()
    btn.pressed.emit()
    btn.released.emit()          # inside the hold threshold, so it is a click
    panel._app.processEvents()

    moves = [(fn, a) for fn, a in panel._queued if fn == "move_microns"]
    assert moves, f"{btn_key} queued no move, got {panel._queued}"
    fn, args = moves[0]
    assert args[0] == axis
    assert args[1] * ctrl_sign > 0, (
        f"{btn_key} asked the controller for {args[1]:+g} um on {axis}; "
        f"expected sign {ctrl_sign:+d}")


# -- Display: a signed distance from an origin -------------------------------

@pytest.mark.parametrize("axis,stage_sign", [("A", +1), ("B", -1), ("C", -1)])
def test_the_columns_read_in_stage_terms(panel, axis, stage_sign):
    """Drive _TP up by a known amount and read the labels back."""
    from ui.panels.jog_panel import AXIS_NAME
    st = panel._ctrl.poll_all_axes()
    here = st[axis].pos
    panel.zeros.set_zero(axis, here)            # counts zero at the present spot
    panel.zeros.set_um_origin(axis, here)
    panel.on_status_updated(st); panel._app.processEvents()

    lbl = panel._z_pos_lbl if axis == "C" else panel._pos_lbls[axis]
    assert float(lbl.text()) == 0.0, f"{AXIS_NAME[axis]} did not read 0 at its zero"

    # +128 encoder counts of CONTROLLER travel = 10 um, which the stage calls
    # +10 um on A and -10 um on B and C.
    st[axis].pos = here + 128.0
    panel.on_status_updated(st); panel._app.processEvents()

    um_lbl = panel._z_pos_um_lbl if axis == "C" else panel._pos_um_lbls[axis]
    assert float(lbl.text()) == pytest.approx(128.0 * stage_sign)
    assert float(um_lbl.text()) == pytest.approx(10.0 * stage_sign)


def test_saved_positions_are_listed_in_stage_terms(panel):
    st = panel._ctrl.poll_all_axes()
    for ax in ("A", "B", "C"):
        panel.zeros.set_um_origin(ax, st[ax].pos)
    panel.memory.save("probe", st["A"].pos + 128.0,
                      st["B"].pos + 128.0, st["C"].pos + 128.0)
    panel._refresh_memory_list(); panel._app.processEvents()

    text = panel._mem_list.item(panel._mem_list.count() - 1).text()
    assert "X:+10.00" in text, text
    assert "Y:-10.00" in text, text
    assert "Z:-10.00" in text, text


# -- Limits, which speak controller ------------------------------------------

@pytest.mark.parametrize("axis,blocked_stage_sign", [("A", +1), ("C", -1)])
def test_a_controller_forward_limit_blocks_the_stage_side_it_is_on(
        panel, axis, blocked_stage_sign):
    """A is not reversed so its FWD switch blocks stage +; C's blocks stage -."""
    panel._ctrl.simulate_fwd_limit(axis, True)
    panel.on_status_updated(panel._ctrl.poll_all_axes()); panel._app.processEvents()

    assert panel._direction_blocked(axis, blocked_stage_sign)
    assert not panel._direction_blocked(axis, -blocked_stage_sign), (
        "the escape direction was blocked too")

    key = f"{axis}{'+' if blocked_stage_sign > 0 else '-'}"
    esc = f"{axis}{'-' if blocked_stage_sign > 0 else '+'}"
    assert not panel._jog_btns[key].isEnabled(), f"{key} stayed live at its limit"
    assert panel._jog_btns[esc].isEnabled(), f"{esc} was disabled, so nothing escapes"


def test_the_escape_hint_names_a_button_that_is_actually_live(panel):
    """The hint used to name the controller's FWD/REV, which on Z is now the
    opposite button to the one that works."""
    panel._ctrl.simulate_fwd_limit("C", True)
    panel.on_status_updated(panel._ctrl.poll_all_axes()); panel._app.processEvents()

    text = panel._lim_alert.text()
    assert "jog Z+ to escape" in text, text
    assert panel._jog_btns["C+"].isEnabled(), "the hint names a disabled button"


def test_the_move_callback_greys_out_the_button_that_was_pressed(panel):
    """move_microns reports the CONTROLLER direction it moved. A stage +Y click
    sends -B, so an unconverted report would grey out Y- instead of Y+."""
    panel._on_jog_button_state_changed("B", "-", False)   # what -B reports
    assert not panel._jog_btns["B+"].isEnabled(), "greyed out the wrong Y button"
    assert panel._jog_btns["B-"].isEnabled()

    panel._on_jog_button_state_changed("B", "-", True)
    assert panel._jog_btns["B+"].isEnabled(), "never re-enabled it"
