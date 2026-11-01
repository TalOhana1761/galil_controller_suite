"""Retract Z / Insert Z: the blind moves past the end of the scale tape.

The Z limit switch at the stage's minus end sits BEYOND the tape, so the last
stretch of the way there is open loop - _TP is not a valid reading once the head
runs off, and no closed-loop move can be aimed at it. Retract runs closed loop to
0 um, where the encoder is still good, then jogs blind until the switch stops it.
Insert jogs back until the encoder calls its own data valid again.

What these tests defend, in rough order of how much it would hurt to get wrong:

  - The blind jog goes the way the operator means. Stage -Z is controller +C, so
    a sign slip drives the head further onto the tape instead of off it, or off
    the far end.
  - Soft limits are suspended while the head is off the tape. They are computed
    from _TP, so once it is garbage a bound would fire on nonsense and halt the
    very move that put the head there.
  - The insert does not stop on ONE clean encoder read. The error bit was
    measured intermittent near the tape end (9 of 75 reads), so a single clean
    sample is not evidence the head is back on.
  - The button is disabled while moving, and its text/colour says what is next.

Run:  pytest tests/test_z_retract.py -v
"""
import sys, os
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, ".."))

import pytest

from controller.mock_controller import MockGalilController
from utils.stage_axes import AXIS_SIGN

ENC = 12.8
ORIGIN = 6_991_849.0        # C's micron origin on the rig, as of 2026-09-24
ENC_ERROR = 1 << 3


@pytest.fixture
def ctrl():
    c = MockGalilController()
    c.enable_axes("ABC")
    for ax in ("A", "B", "C"):
        c.set_trip_points(ax, 10_000_000.0, -10_000_000.0)
    return c


@pytest.fixture
def panel(ctrl, tmp_path):
    """A shown JogPanel whose queued commands are captured, not executed."""
    from PyQt5.QtWidgets import QApplication
    from controller.data_models import AppSettings
    from utils.position_memory import PositionMemory
    from utils.zero_offsets import ZeroOffsets
    from utils.soft_limits import SoftLimits
    from utils import z_retract_state
    from ui.panels.jog_panel import JogPanel
    from ui.polling_worker import PollingWorker

    app = QApplication.instance() or QApplication([])
    # The panel PERSISTS the retract state on every change. Without this the
    # suite would write the rig's real config/z_retract_state.json from a test,
    # and a fabricated OFF_TAPE there is worse than no file - the panel would
    # offer Insert Z on a stage that was never retracted.
    original_state_path = z_retract_state.STATE_PATH
    z_retract_state.STATE_PATH = str(tmp_path / "z_retract_state.json")
    settings = AppSettings()
    settings.encoder_counts_per_um = ENC
    zeros = ZeroOffsets(path=str(tmp_path / "zero_offsets.json"))
    zeros.set_um_origin("C", ORIGIN)

    queued = []
    w = PollingWorker(ctrl, soft_limits=SoftLimits(settings, zeros))
    w.queue_command = lambda fn, *a, **k: queued.append((getattr(fn, "__name__", str(fn)), a))
    w.clear_queue = lambda: None
    p = JogPanel(ctrl, w, settings, PositionMemory(path=str(tmp_path / "pos.json")),
                 zeros=zeros)
    p.show(); app.processEvents()
    # Both buttons prompt before moving, and a real modal blocks an offscreen
    # run outright. Answer Yes by default and record what was asked; the tests
    # that care about the prompt itself override this.
    p._prompts = []
    p._confirm = lambda title, question: (p._prompts.append(question), True)[1]
    p._queued = queued
    p._app = app
    p._ctrl = ctrl
    p._worker = w
    p._state_path = z_retract_state.STATE_PATH
    yield p
    p.hide()
    z_retract_state.STATE_PATH = original_state_path


def status(ctrl, *, fwd=False, enc=None, pos=ORIGIN):
    st = ctrl.poll_all_axes()
    st["C"].pos = pos
    st["C"].fwd_limit = fwd
    st["C"].rev_limit = False
    st["C"].enc_status = enc
    return st


def rz(panel):
    from ui.panels.jog_panel import (
        _RZ_ON_TAPE, _RZ_RETRACTING, _RZ_OFF_TAPE, _RZ_INSERTING)
    return {"on": _RZ_ON_TAPE, "retracting": _RZ_RETRACTING,
            "off": _RZ_OFF_TAPE, "inserting": _RZ_INSERTING}


# -- The button ---------------------------------------------------------------

def test_starts_offering_retract_in_red(panel):
    assert panel._btn_rz.text() == "Retract Z"
    assert "#FF4C4C" in panel._btn_rz.styleSheet()
    assert panel._btn_rz.isEnabled()


def test_button_is_disabled_while_retracting(panel):
    panel._btn_rz.click(); panel._app.processEvents()
    assert panel._rz_state == rz(panel)["retracting"]
    assert not panel._btn_rz.isEnabled(), "left the button live during the move"


def test_becomes_insert_in_green_once_the_switch_stops_it(panel):
    panel._btn_rz.click(); panel._app.processEvents()
    panel._ctrl._last_dir["C"] = +1                  # the retract direction
    panel.on_status_updated(status(panel._ctrl, fwd=True))
    panel._app.processEvents()

    assert panel._rz_state == rz(panel)["off"]
    assert panel._btn_rz.text() == "Insert Z"
    assert "#3DBA6F" in panel._btn_rz.styleSheet()
    assert panel._btn_rz.isEnabled(), "never handed the button back"


# -- Direction, which is where a sign slip would be worst ---------------------

def test_retract_jogs_toward_the_stage_minus_end(panel):
    """Stage -Z is controller +C, so the blind jog speed must be POSITIVE."""
    from ui.panels.jog_panel import BLIND_JOG_SPEED
    panel._queued.clear()
    panel._btn_rz.click(); panel._app.processEvents()

    # The retract is one queued command that moves then jogs; call it against
    # the mock to see what it asks the controller for.
    fn, args = panel._queued[0]
    assert fn == "_retract_sequence"
    jogged = []
    panel._ctrl.jog = lambda ax, sp: jogged.append((ax, sp))
    panel._ctrl.move_to_counts = lambda *a, **k: 0.0
    panel._retract_sequence(*args)

    assert jogged == [("C", +BLIND_JOG_SPEED)], (
        f"retract jogged {jogged}; stage -Z is controller +C, and "
        f"AXIS_SIGN['C'] is {AXIS_SIGN['C']}")


def test_insert_jogs_back_the_other_way(panel):
    from ui.panels.jog_panel import BLIND_JOG_SPEED
    panel._rz_state = rz(panel)["off"]
    panel._refresh_rz_button()
    panel._queued.clear()
    panel._btn_rz.click(); panel._app.processEvents()

    assert panel._queued[0][0] == "jog"
    assert panel._queued[0][1] == ("C", -BLIND_JOG_SPEED)


def test_the_blind_speed_stays_well_under_the_jog_default(panel):
    """Nothing stops a blind jog but the poll noticing, one cycle later, and past
    the tape that overshoot cannot be measured or corrected - only avoided.

    The ceiling is a deliberate tripwire, not a derived figure: raising it is a
    decision about how far past the switch the stage may coast, so it should
    take a test change too.
    """
    from ui.panels.jog_panel import BLIND_JOG_SPEED
    overshoot_um = BLIND_JOG_SPEED / 6.4 * 0.165
    assert BLIND_JOG_SPEED <= 3000, (
        f"{BLIND_JOG_SPEED} counts/s coasts {overshoot_um:.0f} um past the "
        f"switch at a ~165 ms poll cycle")
    assert overshoot_um < 80, f"{overshoot_um:.0f} um of blind overshoot"


# -- Soft limits must not fire on a reading that is off the tape --------------

def test_soft_limits_are_suspended_for_the_blind_phases(panel):
    soft = panel._worker.soft_limits
    assert not soft.is_suspended("C")

    panel._btn_rz.click(); panel._app.processEvents()
    assert soft.is_suspended("C"), (
        "a bound computed from an off-tape _TP would halt the retract itself")

    panel._ctrl._last_dir["C"] = +1
    panel.on_status_updated(status(panel._ctrl, fwd=True))
    assert soft.is_suspended("C"), "still off the tape while parked at the switch"


def test_soft_limits_come_back_once_the_head_is_on_the_tape(panel):
    from ui.panels.jog_panel import _ENC_OK_POLLS
    soft = panel._worker.soft_limits
    panel._rz_state = rz(panel)["off"]
    panel._btn_rz.click(); panel._app.processEvents()      # insert
    assert soft.is_suspended("C")

    for _ in range(_ENC_OK_POLLS):
        panel.on_status_updated(status(panel._ctrl, enc=0))
    assert panel._rz_state == rz(panel)["on"]
    assert not soft.is_suspended("C"), "_TP is trustworthy again; bounds must be too"


# -- The insert stop condition ------------------------------------------------

def test_insert_ignores_a_single_clean_read(panel):
    """The error bit is intermittent near the tape end, so one clean sample is
    not evidence the head is back on."""
    panel._rz_state = rz(panel)["off"]
    panel._btn_rz.click(); panel._app.processEvents()

    panel.on_status_updated(status(panel._ctrl, enc=0))          # clean
    assert panel._rz_state == rz(panel)["inserting"], "stopped on one read"
    panel.on_status_updated(status(panel._ctrl, enc=ENC_ERROR))  # error again
    assert panel._rz_enc_ok_count == 0, "did not reset the run of clean reads"


def test_insert_stops_after_a_run_of_clean_reads(panel):
    from ui.panels.jog_panel import _ENC_OK_POLLS
    panel._rz_state = rz(panel)["off"]
    panel._btn_rz.click(); panel._app.processEvents()
    panel._queued.clear()

    for _ in range(_ENC_OK_POLLS):
        panel.on_status_updated(status(panel._ctrl, enc=0))

    assert panel._rz_state == rz(panel)["on"]
    assert ("_insert_return_sequence", (ORIGIN,)) in panel._queued, (
        "left the axis jogging, or did not send it back to 0 um")
    assert panel._btn_rz.text() == "Retract Z"


def test_insert_finishes_closed_loop_at_the_micron_origin(panel):
    """The jog stops wherever the tape is first found; the return makes every
    insert end at 0 um. Halt first - a closed-loop move must not start while the
    blind jog is still running."""
    calls = []
    panel.controller = type("C", (), {
        "halt_axis": lambda self, ax: calls.append(("halt_axis", ax)),
        "move_to_counts": lambda self, ax, tgt, **k: calls.append(("move_to_counts", ax, tgt)),
    })()
    panel._insert_return_sequence(ORIGIN)
    assert calls == [("halt_axis", "C"), ("move_to_counts", "C", ORIGIN)]


def test_insert_without_an_origin_only_halts(panel):
    calls = []
    panel.controller = type("C", (), {
        "halt_axis": lambda self, ax: calls.append("halt"),
        "move_to_counts": lambda self, *a, **k: calls.append("move"),
    })()
    panel._insert_return_sequence(0.0)
    assert calls == ["halt"], "moved to a raw _TP of 0 - hundreds of mm away"


def test_insert_waits_when_nobody_read_the_encoder(panel):
    """enc_status is None when the worker was not asked for it - not a value to
    act on."""
    panel._rz_state = rz(panel)["off"]
    panel._btn_rz.click(); panel._app.processEvents()
    for _ in range(5):
        panel.on_status_updated(status(panel._ctrl, enc=None))
    assert panel._rz_state == rz(panel)["inserting"]


def test_the_encoder_watch_is_only_on_while_inserting(panel):
    assert "C" not in panel._worker.watch_encoder, "an idle panel costs a read"
    panel._rz_state = rz(panel)["off"]
    panel._btn_rz.click(); panel._app.processEvents()
    assert "C" in panel._worker.watch_encoder

    from ui.panels.jog_panel import _ENC_OK_POLLS
    for _ in range(_ENC_OK_POLLS):
        panel.on_status_updated(status(panel._ctrl, enc=0))
    assert "C" not in panel._worker.watch_encoder, "kept paying for the read"


# -- Refusals and bounds ------------------------------------------------------

def test_retract_refuses_without_a_micron_origin(panel, monkeypatch=None):
    """0 um has to mean something before the closed-loop half can aim at it."""
    from PyQt5.QtWidgets import QMessageBox
    panel.zeros.set_um_origin("C", 0.0)
    warned = []
    QMessageBox.warning = staticmethod(lambda *a, **k: warned.append(a))
    panel._queued.clear()
    panel._btn_rz.click(); panel._app.processEvents()

    assert warned, "started a blind move with no 0 um to start it from"
    assert panel._queued == []
    assert panel._rz_state == rz(panel)["on"]


def test_the_bound_is_a_distance_not_a_number_of_seconds(panel):
    """Retuning the blind speed must not silently change how far a runaway may
    travel, which a hardcoded timeout would."""
    from ui.panels.jog_panel import BLIND_MAX_TRAVEL_UM
    slow = panel._blind_timeout_s()
    panel.settings.axis_c.counts_per_um *= 2      # same counts/s, half the um/s
    fast = panel._blind_timeout_s()
    assert fast == pytest.approx(slow * 2), (
        "the timeout did not follow the speed, so the distance bound moved")
    panel.settings.axis_c.counts_per_um /= 2

    # The switch sits ~10 mm past 0 um, so the bound has to clear that with room
    # to spare while still being a useful bound.
    assert 10_000 < BLIND_MAX_TRAVEL_UM < 20_000, (
        f"{BLIND_MAX_TRAVEL_UM} um either cannot reach the switch or is no "
        f"longer a useful bound")


def test_a_retract_that_never_finds_the_switch_is_bounded(panel):
    """Open loop: elapsed time IS distance, so the timeout bounds the travel."""
    import time as _t
    panel._btn_rz.click(); panel._app.processEvents()
    panel._queued.clear()

    panel._rz_started = _t.monotonic() - panel._blind_timeout_s() - 1
    panel.on_status_updated(status(panel._ctrl))

    assert ("halt_axis", ("C",)) in panel._queued, "left it running blind"
    assert panel._rz_state == rz(panel)["off"], (
        "an aborted retract may be anywhere, possibly off the tape - the button "
        "must offer the move that brings it back")


def test_a_failing_command_does_not_strand_the_button(panel):
    """The sequence is poll-driven, so a command that RAISES would otherwise
    leave the button dead until the blind timeout - minutes, silently."""
    panel._btn_rz.click(); panel._app.processEvents()
    assert not panel._btn_rz.isEnabled()

    panel._worker.error_occurred.emit("C: limit active - jogging + drives into it")
    panel._app.processEvents()

    # An abort no longer guesses where the head is: it lands off_tape and asks
    # the encoder, holding the button only until that answers.
    assert panel._rz_state == rz(panel)["off"]
    assert panel._rz_check is not None, "did not ask the encoder after the abort"
    from ui.panels.jog_panel import _RZ_CHECK_READS
    for _ in range(_RZ_CHECK_READS):
        panel.on_status_updated(status(panel._ctrl, enc=ENC_ERROR))
    assert panel._btn_rz.isEnabled(), "button stayed dead after the move failed"
    assert panel._rz_state == rz(panel)["off"]


# -- The confirmation prompts -------------------------------------------------

def test_retract_asks_before_moving(panel):
    panel._btn_rz.click(); panel._app.processEvents()
    assert panel._prompts == ["Are you sure to retract Z?"]


def test_retract_does_nothing_if_the_answer_is_no(panel):
    panel._confirm = lambda *a: False
    panel._queued.clear()
    panel._btn_rz.click(); panel._app.processEvents()

    assert panel._queued == [], "started the retract after the user said no"
    assert panel._rz_state == rz(panel)["on"]
    assert panel._btn_rz.isEnabled()
    assert not panel._worker.soft_limits.is_suspended("C"), (
        "suspended the bounds for a move that never happened")


def test_insert_asks_about_the_vacuum_valve(panel):
    panel._rz_state = rz(panel)["off"]
    panel._refresh_rz_button()
    panel._btn_rz.click(); panel._app.processEvents()
    assert panel._prompts == ["Is the Vacuum valve opened?"]


def test_insert_does_nothing_if_the_answer_is_no(panel):
    panel._rz_state = rz(panel)["off"]
    panel._refresh_rz_button()
    panel._confirm = lambda *a: False
    panel._queued.clear()
    panel._btn_rz.click(); panel._app.processEvents()

    assert panel._queued == [], "started the insert after the user said no"
    assert panel._rz_state == rz(panel)["off"], "left the retracted state"
    assert "C" not in panel._worker.watch_encoder


def test_the_prompt_offers_yes_no_and_defaults_to_no(panel):
    """These guard a move that cannot be undone by looking at the encoder
    afterwards, so Enter or Escape on a dialog nobody read has to mean don't.

    Drives the REAL _confirm, not the fixture's stub.
    """
    from PyQt5.QtWidgets import QMessageBox
    from ui.panels.jog_panel import JogPanel

    seen = {}
    original = QMessageBox.question

    def fake(parent, title, text, buttons, default):
        seen.update(title=title, text=text, buttons=buttons, default=default)
        return default                      # whatever Enter would pick
    QMessageBox.question = staticmethod(fake)
    try:
        answered = JogPanel._confirm(panel, "Retract Z", "Are you sure?")
    finally:
        QMessageBox.question = original

    assert seen["buttons"] == QMessageBox.Yes | QMessageBox.No
    assert seen["default"] == QMessageBox.No, "Enter would have started the move"
    assert answered is False, "the default answer must not proceed"


# -- Z's own controls are locked out for the whole round trip -----------------

def _z_controls(panel):
    return (panel._jog_btns["C+"].isEnabled(), panel._jog_btns["C-"].isEnabled())


def test_z_buttons_die_the_moment_retract_is_pressed(panel):
    assert _z_controls(panel) == (True, True)
    panel._btn_rz.click(); panel._app.processEvents()
    assert _z_controls(panel) == (False, False), (
        "Z+/Z- stayed live while the head was on its way off the tape")


def test_z_buttons_stay_dead_while_retracted(panel):
    panel._btn_rz.click(); panel._app.processEvents()
    panel._ctrl._last_dir["C"] = +1
    panel.on_status_updated(status(panel._ctrl, fwd=True))
    panel._app.processEvents()

    assert panel._rz_state == rz(panel)["off"]
    assert _z_controls(panel) == (False, False), (
        "a poll re-enabled Z+/Z- while the head was parked off the tape")
    assert panel._btn_rz.isEnabled(), "the Insert button must stay live"


def test_z_buttons_come_back_only_when_the_insert_completes(panel):
    from ui.panels.jog_panel import _ENC_OK_POLLS
    panel._rz_state = rz(panel)["off"]
    panel._btn_rz.click(); panel._app.processEvents()      # insert
    assert _z_controls(panel) == (False, False)

    for _ in range(_ENC_OK_POLLS - 1):
        panel.on_status_updated(status(panel._ctrl, enc=0))
    assert _z_controls(panel) == (False, False), "handed Z back early"

    panel.on_status_updated(status(panel._ctrl, enc=0))
    assert panel._rz_state == rz(panel)["on"]
    assert _z_controls(panel) == (True, True), "never handed Z back"


def test_x_and_y_are_untouched_by_the_z_lock(panel):
    panel._btn_rz.click(); panel._app.processEvents()
    panel.on_status_updated(status(panel._ctrl))
    for key in ("A+", "A-", "B+", "B-"):
        assert panel._jog_btns[key].isEnabled(), f"{key} was caught by the Z lock"


def test_page_keys_are_refused_while_z_is_retracted(panel):
    from PyQt5.QtCore import Qt, QEvent
    from PyQt5.QtGui import QKeyEvent
    panel.activateWindow(); panel._app.processEvents()
    panel._rz_state = rz(panel)["off"]

    for key in (Qt.Key_PageUp, Qt.Key_PageDown):
        panel._queued.clear()
        panel.eventFilter(panel, QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier))
        assert "C" not in panel._key_jogging, f"{key} jogged a retracted Z"
        assert not any(fn == "jog" for fn, _ in panel._queued)
        panel.eventFilter(panel, QKeyEvent(QEvent.KeyRelease, key, Qt.NoModifier))


def test_page_keys_work_again_once_z_is_back(panel):
    from PyQt5.QtCore import Qt, QEvent
    from PyQt5.QtGui import QKeyEvent
    panel.activateWindow(); panel._app.processEvents()
    panel._queued.clear()
    panel.eventFilter(panel, QKeyEvent(QEvent.KeyPress, Qt.Key_PageUp, Qt.NoModifier))
    assert panel._key_jogging.get("C") == +1
    panel.eventFilter(panel, QKeyEvent(QEvent.KeyRelease, Qt.Key_PageUp, Qt.NoModifier))


# -- Remembered across restarts -----------------------------------------------

def _stored(panel):
    """What is on disk right now, or None if nothing has been written."""
    import json, os
    if not os.path.exists(panel._state_path):
        return None
    with open(panel._state_path) as f:
        return json.load(f)["state"]


def _relaunch(panel, tmp_path):
    """A second JogPanel over the same state file - i.e. a restart."""
    from controller.data_models import AppSettings
    from utils.position_memory import PositionMemory
    from utils.zero_offsets import ZeroOffsets
    from ui.panels.jog_panel import JogPanel
    from ui.polling_worker import PollingWorker

    settings = AppSettings()
    settings.encoder_counts_per_um = ENC
    zeros = ZeroOffsets(path=str(tmp_path / "zero_offsets2.json"))
    zeros.set_um_origin("C", ORIGIN)
    w = PollingWorker(panel._ctrl)
    w.queue_command = lambda fn, *a, **k: None
    w.clear_queue = lambda: None
    p2 = JogPanel(panel._ctrl, w, settings,
                  PositionMemory(path=str(tmp_path / "pos2.json")), zeros=zeros)
    p2.show(); panel._app.processEvents()
    return p2


def test_nothing_is_written_until_something_happens(panel):
    assert _stored(panel) is None, "wrote a state file just for opening the panel"


def test_a_completed_retract_is_remembered(panel, tmp_path):
    panel._btn_rz.click(); panel._app.processEvents()
    panel._ctrl._last_dir["C"] = +1
    panel.on_status_updated(status(panel._ctrl, fwd=True))
    assert _stored(panel) == "off_tape"

    p2 = _relaunch(panel, tmp_path)
    try:
        assert p2._btn_rz.text() == "Insert Z", (
            "reopened offering Retract Z on a stage that is already retracted")
        assert "#3DBA6F" in p2._btn_rz.styleSheet()
        assert (p2._jog_btns["C+"].isEnabled(),
                p2._jog_btns["C-"].isEnabled()) == (False, False), (
            "Z controls came back on restart while the head is off the tape")
    finally:
        p2.hide()


def test_a_completed_insert_is_remembered(panel, tmp_path):
    from ui.panels.jog_panel import _ENC_OK_POLLS
    panel._rz_state = rz(panel)["off"]
    panel._btn_rz.click(); panel._app.processEvents()
    for _ in range(_ENC_OK_POLLS):
        panel.on_status_updated(status(panel._ctrl, enc=0))
    assert _stored(panel) == "on_tape"

    p2 = _relaunch(panel, tmp_path)
    try:
        assert p2._btn_rz.text() == "Retract Z"
        assert (p2._jog_btns["C+"].isEnabled(),
                p2._jog_btns["C-"].isEnabled()) == (True, True)
    finally:
        p2.hide()


def test_off_tape_is_stored_the_moment_the_retract_starts(panel):
    """Not when it finishes. A crash between the two would otherwise leave
    on_tape on disk, and the next launch would offer Retract Z - whose first act
    is a closed-loop move against an encoder that is off the tape."""
    panel._btn_rz.click(); panel._app.processEvents()
    assert panel._rz_state == rz(panel)["retracting"]
    assert _stored(panel) == "off_tape", (
        "a kill mid-retract would reopen believing the head is on the tape")


def test_an_unfinished_insert_still_reads_off_tape(panel):
    """The head is only known to be back when the encoder says so."""
    panel._rz_state = rz(panel)["off"]
    panel._btn_rz.click(); panel._app.processEvents()
    assert panel._rz_state == rz(panel)["inserting"]
    assert _stored(panel) == "off_tape"


def test_an_aborted_retract_reads_off_tape(panel):
    import time as _t
    panel._btn_rz.click(); panel._app.processEvents()
    panel._rz_started = _t.monotonic() - panel._blind_timeout_s() - 1
    panel.on_status_updated(status(panel._ctrl))
    assert _stored(panel) == "off_tape", (
        "an aborted retract may have left the head anywhere; off_tape is the "
        "state whose button recovers")


def test_a_cancelled_prompt_writes_nothing(panel):
    panel._confirm = lambda *a: False
    panel._btn_rz.click(); panel._app.processEvents()
    assert _stored(panel) is None, "recorded a retract the user declined"


# -- The state is CHECKED against the encoder on connect ----------------------
#
# 2026-10-08: Z was retracted from a laptop, the controller moved to this PC, and
# this PC - whose stored state had never heard of the retract - offered Retract Z
# on a head already parked off the tape. The file is per PC; the encoder is not.

def _check(panel, n, **kw):
    from ui.panels.jog_panel import _RZ_CHECK_READS
    panel.check_z_retract()
    for _ in range(n if n is not None else _RZ_CHECK_READS):
        panel.on_status_updated(status(panel._ctrl, **kw))
    panel._app.processEvents()


def test_a_retract_done_elsewhere_is_found_on_connect(panel):
    """Stored on_tape, encoder flagged and the switch pressed: retracted."""
    _check(panel, None, fwd=True, enc=ENC_ERROR, pos=ORIGIN + 8_388_608)
    assert panel._rz_state == rz(panel)["off"]
    assert panel._btn_rz.text() == "Insert Z"
    assert panel._z_manual_locked("C")
    assert panel._worker.soft_limits.is_suspended("C")
    with open(panel._state_path) as f:
        assert "off_tape" in f.read()


def test_the_switch_is_declared_the_retract_end(panel):
    """A stale last_direction must not label the switch as the insert's end."""
    _check(panel, None, fwd=True, enc=ENC_ERROR)
    names = [n for n, a in panel._queued]
    assert "declare_limit_end" in names
    args = dict(panel._queued)["declare_limit_end"]
    assert args == ("C", AXIS_SIGN["C"] * -1), "declared the wrong end"


def test_an_insert_done_elsewhere_is_found_on_connect(panel):
    panel._set_rz_state(rz(panel)["off"])
    _check(panel, None, enc=4)                       # warning only, on the tape
    assert panel._rz_state == rz(panel)["on"]
    assert panel._btn_rz.text() == "Retract Z"
    assert not panel._worker.soft_limits.is_suspended("C")
    assert "C" not in panel._worker.watch_encoder


def test_one_bad_read_is_enough_to_call_it_retracted(panel):
    from ui.panels.jog_panel import _RZ_CHECK_READS
    panel.check_z_retract()
    for i in range(_RZ_CHECK_READS):
        panel.on_status_updated(status(panel._ctrl, enc=ENC_ERROR if i == 4 else 0))
    assert panel._rz_state == rz(panel)["off"]


def test_an_unflagged_but_impossible_position_is_off_the_tape(panel):
    """The bit-23 corruption passed the flags on most reads; position catches it."""
    _check(panel, None, enc=0, pos=ORIGIN + 8_388_608)
    assert panel._rz_state == rz(panel)["off"]


def test_a_timeout_bit_is_not_valid_data(panel):
    _check(panel, None, enc=1)
    assert panel._rz_state == rz(panel)["off"]


def test_z_is_locked_until_the_check_answers(panel):
    panel.check_z_retract()
    panel.on_status_updated(status(panel._ctrl, enc=0))
    assert not panel._btn_rz.isEnabled()
    assert panel._z_manual_locked("C")
    assert "C" in panel._worker.watch_encoder


def test_an_unreadable_encoder_does_not_lock_z_forever(panel):
    from ui.panels.jog_panel import _RZ_CHECK_READS
    panel.check_z_retract()
    for _ in range(3 * _RZ_CHECK_READS + _RZ_CHECK_READS):
        panel.on_status_updated(status(panel._ctrl, enc=None))
    assert panel._rz_check is None
    assert panel._rz_state == rz(panel)["off"], "unknown must land on the safe side"


def test_declare_limit_end_relabels_a_pressed_switch(ctrl):
    ctrl._last_dir["C"] = -1
    ctrl.declare_limit_end("C", +1)
    ctrl.set_trip_points("C", 0.0, -10_000_000.0)    # pressed at the fwd boundary
    st = ctrl.poll_all_axes()["C"]
    assert st.fwd_limit and not st.rev_limit


# -- Only with the motors enabled ---------------------------------------------
#
# A retract or insert queued with C off moves nothing, but the button used to
# flip anyway, leaving the stored state saying retracted when the head was in,
# or the reverse.

def test_button_is_dead_until_enable_motors(panel):
    panel._ctrl.disable_axes("ABC")
    panel.on_status_updated(status(panel._ctrl))
    assert not panel._btn_rz.isEnabled()
    assert "Enable Motors" in panel._btn_rz.toolTip()
    panel._ctrl.enable_axes("ABC")
    panel.on_status_updated(status(panel._ctrl))
    assert panel._btn_rz.isEnabled()


def test_a_click_with_motors_off_changes_nothing(panel):
    panel._ctrl.disable_axes("ABC")
    panel._queued.clear()
    panel._on_retract_clicked()          # bypasses the disabled button
    assert panel._rz_state == rz(panel)["on"]
    assert panel._queued == [] and panel._prompts == []
    assert not os.path.exists(panel._state_path), "recorded a retract that never ran"


def test_disabling_mid_insert_aborts_and_rechecks(panel):
    panel._set_rz_state(rz(panel)["off"])
    panel._btn_rz.click(); panel._app.processEvents()
    assert panel._rz_state == rz(panel)["inserting"]
    panel._ctrl.emergency_stop()
    panel.on_status_updated(status(panel._ctrl, enc=ENC_ERROR))
    assert panel._rz_state == rz(panel)["off"], "claimed inserted after an E-STOP"
    assert panel._rz_check is not None


def test_only_c_matters(panel):
    panel._ctrl.disable_axes("AB")
    panel.on_status_updated(status(panel._ctrl))
    assert panel._btn_rz.isEnabled()


# -- The blind clock starts with the blind jog, not the button ----------------
#
# 2026-10-08: retract from +40 mm. The closed-loop approach to 0 um took 51.6 s,
# the timeout counted it as blind travel, and the retract aborted 0.1 s into the
# jog - stopping at ~0 um, where the -100 um soft limit then lit up and looked
# like the cause.

def _park_at_plus_40mm(ctrl):
    # Stage +Z is controller -C, so +40 mm is BELOW the origin in counts.
    at = ORIGIN - 40_000 * ENC
    ctrl._pos["C"] = ctrl._target["C"] = at


def test_a_long_approach_does_not_use_up_the_blind_allowance(panel):
    import time as _t
    panel._btn_rz.click(); panel._app.processEvents()
    # The approach took longer than the whole blind allowance...
    panel._rz_started = _t.monotonic() - panel._blind_timeout_s() - 30
    # ...then the queued sequence finishes it and starts the jog.
    _park_at_plus_40mm(panel._ctrl)
    panel._retract_sequence(ORIGIN)
    panel.on_status_updated(status(panel._ctrl))
    assert panel._rz_state == rz(panel)["retracting"], (
        "aborted on time spent in the closed-loop approach")


def test_the_blind_phase_is_still_bounded(panel):
    import time as _t
    panel._btn_rz.click(); panel._app.processEvents()
    _park_at_plus_40mm(panel._ctrl)
    panel._retract_sequence(ORIGIN)
    panel._rz_started = _t.monotonic() - panel._blind_timeout_s() - 1
    panel.on_status_updated(status(panel._ctrl))
    assert panel._rz_state != rz(panel)["retracting"]
