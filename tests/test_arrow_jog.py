"""
Keyboard jogging, and the jog safety that has to come with it.

Hold a key, the axis runs; let go, it stops. Right/Left drive X (A), Up/Down
drive Y (B), Page Up/Page Down drive Z (C), at the Jog Speed slider's setting
— which governs ONLY this, never the step buttons.

The risky part is not the keys, it is that a continuous jog has no target and
LD=3 disables the controller's own limit action. PollingWorker._guard_jog_limits
is the only thing that stops a held key at an end stop.

Run:  pytest tests/test_arrow_jog.py -v
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from controller.mock_controller import MockGalilController


@pytest.fixture
def ctrl():
    c = MockGalilController()
    c.enable_axes("ABC")
    for ax in ("A", "B", "C"):          # PRC-scale room, not the ±50k default
        c.set_trip_points(ax, 10_000_000.0, -10_000_000.0)
    return c


# ── The jog itself ──────────────────────────────────────────────────────────

def test_jog_takes_a_speed_not_a_distance(ctrl):
    """Both controllers agree that the argument is counts/s.

    They did not until 2026-09-23: the real one took MICRONS and did a PR move,
    so the panel passing the speed slider's value asked for a 4 mm move at the
    default and up to 50 mm at the slider's maximum, on a 20 mm axis.
    """
    import inspect
    from controller.galil_controller import GalilController
    for fn in (GalilController.jog, MockGalilController.jog):
        params = list(inspect.signature(fn).parameters)
        assert params[2] == "speed", f"{fn} still takes {params[2]!r}"


@pytest.mark.parametrize("speed,expected", [(4000, +1), (-4000, -1)])
def test_jog_records_its_direction(ctrl, speed, expected):
    ctrl.jog("A", speed)
    assert ctrl.is_jogging("A") == expected


def test_halt_clears_the_jog_flag(ctrl):
    ctrl.jog("A", 4000)
    ctrl.halt_axis("A")
    assert ctrl.is_jogging("A") == 0


def test_halt_all_clears_every_axis(ctrl):
    for ax in ("A", "B", "C"):
        ctrl.jog(ax, 4000)
    ctrl.halt_all()
    assert all(ctrl.is_jogging(ax) == 0 for ax in ("A", "B", "C"))


def test_an_idle_axis_is_not_jogging(ctrl):
    assert ctrl.is_jogging("A") == 0


# ── The guard that stops a held key at a limit ──────────────────────────────

def _worker(ctrl):
    from ui.polling_worker import PollingWorker
    return PollingWorker(ctrl)


def test_guard_halts_a_jog_that_reaches_a_limit(ctrl):
    w = _worker(ctrl)
    halted = []
    inner = ctrl.halt_axis
    ctrl.halt_axis = lambda a: (halted.append(a), inner(a))[1]

    ctrl.jog("A", 4000)
    w._guard_jog_limits(ctrl.poll_all_axes())
    assert halted == [], "halted with no limit active"

    ctrl.simulate_fwd_limit("A", True)
    w._guard_jog_limits(ctrl.poll_all_axes())
    assert halted == ["A"], "did not halt a jog at a limit"
    assert ctrl.is_jogging("A") == 0


def test_guard_leaves_other_axes_alone(ctrl):
    w = _worker(ctrl)
    halted = []
    inner = ctrl.halt_axis
    ctrl.halt_axis = lambda a: (halted.append(a), inner(a))[1]

    ctrl.jog("C", 4000)
    ctrl.simulate_fwd_limit("A", True)      # a different axis is at a limit
    w._guard_jog_limits(ctrl.poll_all_axes())
    assert halted == [], "halted an axis that was nowhere near a limit"


def test_guard_does_not_halt_a_jog_escaping_a_limit(ctrl):
    """The Insert Z bug, 2026-09-24.

    The axis is REACHED here by moving to the trip point, not by forcing the
    flag with simulate_*(): an override sets fwd/rev directly and so never
    exercises the rule that decides WHICH end a switch is, which is exactly
    where the bug lived. This test passed against the broken code while it was
    written that way.

    Starting the escape jog rewrites last_direction. If the switch is resolved
    against that rather than against the direction latched when it closed, the
    still-pressed switch is re-labelled as the end being driven towards and the
    guard halts the escape one poll after it starts.
    """
    w = _worker(ctrl)
    halted = []
    inner = ctrl.halt_axis
    ctrl.halt_axis = lambda a: (halted.append(a), inner(a))[1]

    ctrl.set_trip_points("A", 10_000.0, -10_000.0)
    ctrl._pos["A"] = -12_000.0          # driven to the reverse end...
    ctrl._last_dir["A"] = -1            # ...going reverse
    st = ctrl.poll_all_axes()["A"]
    assert st.rev_limit and not st.fwd_limit, "the setup did not reach the end"

    ctrl.jog("A", 4000)                 # escape: forward, away from it
    w._guard_jog_limits(ctrl.poll_all_axes())
    assert halted == [], "halted a jog that was escaping the limit"
    assert ctrl.is_jogging("A") == +1

    # ...and driving back INTO it still halts.
    halted.clear()
    ctrl.jog("A", -4000)
    w._guard_jog_limits(ctrl.poll_all_axes())
    assert halted == ["A"], "let a jog drive further into the limit"


def test_guard_ignores_axes_that_are_not_jogging(ctrl):
    w = _worker(ctrl)
    halted = []
    inner = ctrl.halt_axis
    ctrl.halt_axis = lambda a: (halted.append(a), inner(a))[1]

    ctrl.simulate_fwd_limit("A", True)      # at a limit, but parked
    w._guard_jog_limits(ctrl.poll_all_axes())
    assert halted == [], "halted an axis that was not moving"


# ── Who owns the arrow keys ─────────────────────────────────────────────────

def _panel(ctrl):
    """A shown, active JogPanel with its queued commands captured."""
    from PyQt5.QtWidgets import QApplication
    from controller.data_models import AppSettings
    from utils.position_memory import PositionMemory
    from ui.panels.jog_panel import JogPanel
    from ui.polling_worker import PollingWorker

    app = QApplication.instance() or QApplication([])
    queued = []
    w = PollingWorker(ctrl)
    w.queue_command = lambda fn, *a, **k: queued.append(fn.__name__)
    w.clear_queue = lambda: None
    panel = JogPanel(ctrl, w, AppSettings(), PositionMemory())
    panel.show(); app.processEvents()
    panel.activateWindow(); app.processEvents()
    panel.on_status_updated(ctrl.poll_all_axes())
    return app, panel, queued


def _press_key(panel, key=None):
    """Press and release one jog key; True if the panel claimed it."""
    from PyQt5.QtCore import Qt, QEvent
    from PyQt5.QtGui import QKeyEvent
    key = Qt.Key_Right if key is None else key
    handled = panel.eventFilter(
        panel, QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier))
    panel.eventFilter(
        panel, QKeyEvent(QEvent.KeyRelease, key, Qt.NoModifier))
    return handled


def test_speed_slider_does_not_steal_the_arrows(ctrl):
    """Reported 2026-09-23: set the speed, press an arrow, the SLIDER moved.

    The slider had keyboard focus after being dragged, and the filter was
    standing aside for sliders. It is now NoFocus, and QSlider is out of the
    stand-aside list, so the arrows reach the stage either way.
    """
    app, panel, queued = _panel(ctrl)
    panel._speed_slider.setValue(7000); app.processEvents()
    panel._speed_slider.setFocus(); app.processEvents()

    queued.clear()
    assert _press_key(panel), "arrow was not handled after using the slider"
    assert "jog" in queued, "the slider swallowed the arrow instead of jogging"
    assert panel._speed == 7000, "the slider still sets the jog speed"


def test_step_spinbox_keeps_the_arrows_while_editing(ctrl):
    """It needs up/down to increment; jogging must not hijack that."""
    app, panel, queued = _panel(ctrl)
    panel._custom_step.setFocus(); app.processEvents()
    queued.clear()
    assert not _press_key(panel), "jog filter hijacked the step spin box"
    assert queued == []


def test_step_spinbox_gives_the_arrows_back_when_done(ctrl):
    app, panel, queued = _panel(ctrl)
    panel._custom_step.setFocus(); app.processEvents()
    panel._custom_step.editingFinished.emit(); app.processEvents()
    queued.clear()
    assert _press_key(panel), "arrows were not returned after editing"
    assert "jog" in queued


# -- Page Up / Page Down drive Z ---------------------------------------------

def test_every_axis_and_direction_has_a_key():
    """All six of them, and no key claimed twice."""
    from PyQt5.QtCore import Qt
    from ui.panels.jog_panel import JogPanel, JOG_KEY_NAME
    assert set(JogPanel.JOG_KEYS.values()) == {
        ("A", +1), ("A", -1), ("B", +1), ("B", -1), ("C", +1), ("C", -1)}
    assert JogPanel.JOG_KEYS[Qt.Key_PageUp]   == ("C", +1)
    assert JogPanel.JOG_KEYS[Qt.Key_PageDown] == ("C", -1)
    # The tooltips name the same six, so the panel cannot advertise a key that
    # drives nothing.
    assert set(JOG_KEY_NAME) == {"A+", "A-", "B+", "B-", "C+", "C-"}


@pytest.mark.parametrize("key_name,sign", [("Key_PageUp", +1), ("Key_PageDown", -1)])
def test_page_keys_jog_z_in_the_right_direction(ctrl, key_name, sign):
    from PyQt5.QtCore import Qt, QEvent
    from PyQt5.QtGui import QKeyEvent
    app, panel, queued = _panel(ctrl)
    key = getattr(Qt, key_name)

    queued.clear()
    handled = panel.eventFilter(
        panel, QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier))
    assert handled, f"{key_name} was not claimed for Z jogging"
    assert queued == ["jog"], f"{key_name} queued {queued} instead of a jog"
    assert panel._key_jogging.get("C") == sign

    panel.eventFilter(panel, QKeyEvent(QEvent.KeyRelease, key, Qt.NoModifier))
    assert "C" not in panel._key_jogging, "release did not stop the Z jog"
    assert queued[-1] == "halt_axis", f"release queued {queued[-1]!r}, not a halt"


def test_page_keys_are_refused_into_an_active_z_limit(ctrl):
    """C has independent switches, so its own flag is the whole answer.

    Z is reversed in software, so the CONTROLLER's forward switch is the stage's
    MINUS end: an active _LF blocks Page Down and leaves Page Up to escape.
    """
    from PyQt5.QtCore import Qt
    app, panel, queued = _panel(ctrl)
    ctrl.simulate_fwd_limit("C", True)
    panel.on_status_updated(ctrl.poll_all_axes()); app.processEvents()

    queued.clear()
    _press_key(panel, Qt.Key_PageDown)
    assert "jog" not in queued, "jogged further into an active Z limit"
    assert "C" not in panel._key_jogging

    queued.clear()
    _press_key(panel, Qt.Key_PageUp)
    assert "jog" in queued, "blocked the escape direction as well"


def test_z_keys_leave_the_step_spinbox_alone(ctrl):
    """Page Up/Down step a spin box by 10x, so it keeps them while editing."""
    from PyQt5.QtCore import Qt
    app, panel, queued = _panel(ctrl)
    panel._custom_step.setFocus(); app.processEvents()
    queued.clear()
    assert not _press_key(panel, Qt.Key_PageUp), "jog filter hijacked the spin box"
    assert queued == []


def test_window_deactivation_stops_a_held_z_key(ctrl):
    """Alt-tab with Page Up held would otherwise leave Z running."""
    from PyQt5.QtCore import Qt, QEvent
    from PyQt5.QtGui import QKeyEvent
    app, panel, queued = _panel(ctrl)
    panel.eventFilter(panel, QKeyEvent(QEvent.KeyPress, Qt.Key_PageUp, Qt.NoModifier))
    assert panel._key_jogging.get("C") == +1

    queued.clear()
    panel.eventFilter(panel, QEvent(QEvent.WindowDeactivate))
    assert "C" not in panel._key_jogging, "Z kept running after the window lost focus"
    assert "halt_axis" in queued
