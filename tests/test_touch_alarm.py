"""Touch alarm: DI1 is pulled up and shorted to ground when the manipulator
hand touches metal it should not. 1 = clear, 0 = alarm.

What these defend:
  - the polarity: 0 means ON. Getting it backwards shows a green "Off" while
    the hand is touching;
  - a failed read shows as unknown, never as "Off";
  - the controller command is @IN[1].

Run:  pytest tests/test_touch_alarm.py -v
"""
import sys, os
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, ".."))

import pytest

from controller.mock_controller import MockGalilController


@pytest.fixture
def worker():
    from ui.polling_worker import PollingWorker
    c = MockGalilController()
    w = PollingWorker(c)
    got = []
    w.touch_alarm.connect(got.append)
    w._got = got
    w._ctrl = c
    return w


def test_reads_di1(worker):
    from ui.polling_worker import TOUCH_ALARM_INPUT
    assert TOUCH_ALARM_INPUT == 1
    assert worker.touch_alarm_input == 1


def test_one_is_clear(worker):
    worker._ctrl.simulate_digital_input(1, 1)
    worker._poll_touch_alarm()
    assert worker._got[-1] is False


def test_zero_is_the_alarm(worker):
    worker._ctrl.simulate_digital_input(1, 0)
    worker._poll_touch_alarm()
    assert worker._got[-1] is True, "a shorted (0) input must read as alarm ON"


def test_a_failed_read_is_unknown_not_off(worker):
    def boom(n):
        raise RuntimeError("timeout")
    worker._ctrl.digital_input = boom
    worker._poll_touch_alarm()
    assert worker._got[-1] is None


def test_the_real_controller_asks_for_at_in_1():
    from controller import galil_controller as gc
    sent = []
    ctrl = gc.GalilController.__new__(gc.GalilController)
    ctrl._read_float = lambda cmd: (sent.append(cmd), 0.0)[1]
    assert ctrl.digital_input(1) == 0
    assert sent == ["MG @IN[1]"]


@pytest.fixture
def panel(tmp_path):
    from PyQt5.QtWidgets import QApplication
    from controller.data_models import AppSettings
    from utils.position_memory import PositionMemory
    from utils.zero_offsets import ZeroOffsets
    from utils import z_retract_state
    from ui.panels.jog_panel import JogPanel
    from ui.polling_worker import PollingWorker

    app = QApplication.instance() or QApplication([])
    original = z_retract_state.STATE_PATH
    z_retract_state.STATE_PATH = str(tmp_path / "z_retract_state.json")
    c = MockGalilController()
    p = JogPanel(c, PollingWorker(c), AppSettings(),
                 PositionMemory(path=str(tmp_path / "pos.json")),
                 zeros=ZeroOffsets(path=str(tmp_path / "zero_offsets.json")))
    yield p
    z_retract_state.STATE_PATH = original


def test_box_starts_unknown(panel):
    assert panel._touch_box.text() == "—"


def test_box_reads_off_in_green_and_on_in_red(panel):
    panel.on_touch_alarm(False)
    assert panel._touch_box.text() == "Off"
    assert "#2E7D32" in panel._touch_box.styleSheet()
    panel.on_touch_alarm(True)
    assert panel._touch_box.text() == "On"
    assert "#C62828" in panel._touch_box.styleSheet()
    panel.on_touch_alarm(None)
    assert panel._touch_box.text() == "—"


# ── Read from inside a blocking move, too ────────────────────────────────────
# A closed-loop move (step click, HOME, GO TO) runs on the polling thread and
# holds off the poll for its whole duration, so without this the box would show
# whatever it read before the move started.

@pytest.fixture
def moving(tmp_path, monkeypatch):
    """The REAL controller on a fake device whose DI1 can be set, driven by a
    real PollingWorker, with every alarm emission recorded."""
    import controller.galil_controller as gc
    from tests.test_move_microns import FakeGalil
    from ui.polling_worker import PollingWorker

    class FakeWithInputs(FakeGalil):
        def __init__(self):
            super().__init__()
            self.di = {1: 1}
        def GCommand(self, cmd):
            if cmd.strip().startswith("MG @IN["):
                self.commands.append(cmd.strip())
                n = int(cmd.split("[")[1].split("]")[0])
                return f"{self.di.get(n, 1):.4f}\r\n"
            return super().GCommand(cmd)

    monkeypatch.setattr(gc, "LAST_DIR_PATH", str(tmp_path / "last_direction.json"))
    monkeypatch.setattr(gc, "PROGRESS_INTERVAL_S", 0.0)    # the fake is instant
    c = gc.GalilController()
    c._g = FakeWithInputs()
    c._g.poll_steps = 8                 # each step takes 8 _BG polls
    w = PollingWorker(c)
    got = []
    w.touch_alarm.connect(got.append)
    return c, w, got


def test_the_worker_hooks_the_touch_read_into_the_move_loop(moving):
    c, w, _ = moving
    assert c.motion_tick == w._poll_touch_alarm


def test_di1_is_read_while_the_axis_is_moving(moving):
    c, _, got = moving
    c.move_microns("A", 100.0)
    cmds = c._g.commands
    first_bg, last_bg = cmds.index("BGA"), len(cmds) - 1 - cmds[::-1].index("BGA")
    # At least one read between a BG and the end of that same step.
    assert any(cmd == "MG @IN[1]" for cmd in cmds[first_bg:last_bg]), \
        "DI1 was never read during motion"
    assert got and got[-1] is False


def test_an_alarm_during_a_move_is_reported_and_the_move_carries_on(moving):
    """Reporting only, until it is decided what a touch should do."""
    import controller.galil_controller as gc
    c, _, got = moving
    c._g.di[1] = 0
    moved = c.move_microns("A", 100.0)
    assert True in got, "the alarm was not reported from inside the move"
    assert not any(cmd.startswith("STA") for cmd in c._g.commands), \
        "a touch stopped the axis - nothing has been decided to do that yet"
    assert moved == pytest.approx(100.0, abs=1.0 / gc.ENCODER_COUNTS_PER_UM)


def test_without_the_hook_the_move_never_reads_di1(moving):
    """The hardware scripts install no hooks; their command stream is unchanged."""
    c, _, _ = moving
    c.motion_tick = None
    c.position_listener = None
    c.move_microns("A", 100.0)
    assert "MG @IN[1]" not in c._g.commands
