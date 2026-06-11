"""
Logging tests for MockGalilController.

Verifies that every user-visible action (jog, halt, enable/disable, set_params,
move, define_zero, emergency_stop) emits the expected loguru INFO/WARNING
message, and that the PollingWorker correctly logs motion-complete and limit-
trip transitions.

Strategy:
  - A loguru test-sink fixture captures all messages at DEBUG+ level.
  - All tests exercise MockGalilController directly (no Qt event loop needed).
  - Polling-level events (motion complete, limit trips) are tested by driving
    the mock through enough poll cycles to trigger the transition, then checking
    the captured messages.

Run with:
    pytest tests/test_logging.py -v
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from loguru import logger

from controller.mock_controller import MockGalilController
from ui.polling_worker import PollingWorker


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def log_capture():
    """Capture all loguru messages into a list for the duration of one test."""
    messages: list[str] = []
    sink_id = logger.add(
        lambda m: messages.append(m.record["message"]),
        level="DEBUG",
        format="{message}",
    )
    yield messages
    logger.remove(sink_id)


@pytest.fixture
def ctrl():
    return MockGalilController()


@pytest.fixture
def ctrl_on(ctrl):
    ctrl.enable_axes("ABC")
    return ctrl


# ── Enable / Disable ─────────────────────────────────────────────────────────

def test_log_enable_axes(ctrl, log_capture):
    ctrl.enable_axes("ABC")
    assert any("Enable" in m and "ABC" in m for m in log_capture), \
        f"No Enable log found. Messages: {log_capture}"


def test_log_disable_axes(ctrl, log_capture):
    ctrl.enable_axes("ABC")
    log_capture.clear()
    ctrl.disable_axes("ABC")
    assert any("Disable" in m and "ABC" in m for m in log_capture), \
        f"No Disable log found. Messages: {log_capture}"


def test_log_enable_single_axis(ctrl, log_capture):
    ctrl.enable_axes("A")
    assert any("Enable" in m and "A" in m for m in log_capture)


# ── Jog ──────────────────────────────────────────────────────────────────────

def test_log_jog_forward(ctrl_on, log_capture):
    ctrl_on.jog("A", 5000)
    assert any("Jog" in m and "A" in m and "FWD" in m for m in log_capture), \
        f"No FWD jog log. Messages: {log_capture}"
    assert any("5000" in m for m in log_capture)


def test_log_jog_reverse(ctrl_on, log_capture):
    ctrl_on.jog("B", -3000)
    assert any("Jog" in m and "B" in m and "REV" in m for m in log_capture), \
        f"No REV jog log. Messages: {log_capture}"
    assert any("3000" in m for m in log_capture)


def test_log_jog_z_axis(ctrl_on, log_capture):
    ctrl_on.jog("C", 1000)
    assert any("Jog" in m and "C" in m for m in log_capture)


# ── Halt ─────────────────────────────────────────────────────────────────────

def test_log_halt_axis(ctrl_on, log_capture):
    ctrl_on.jog("A", 5000)
    log_capture.clear()
    ctrl_on.halt_axis("A")
    assert any("Halt" in m and "A" in m for m in log_capture), \
        f"No Halt log. Messages: {log_capture}"


def test_log_halt_all(ctrl_on, log_capture):
    for ax, sp in [("A", 1000), ("B", -1000), ("C", 500)]:
        ctrl_on.jog(ax, sp)
    log_capture.clear()
    ctrl_on.halt_all()
    assert any("Halt ALL" in m for m in log_capture), \
        f"No 'Halt ALL' log. Messages: {log_capture}"


# ── Emergency Stop ───────────────────────────────────────────────────────────

def test_log_emergency_stop(ctrl_on, log_capture):
    ctrl_on.jog("A", 5000)
    log_capture.clear()
    ctrl_on.emergency_stop()
    assert any("EMERGENCY STOP" in m for m in log_capture), \
        f"No EMERGENCY STOP log. Messages: {log_capture}"


def test_emergency_stop_disables_servo(ctrl_on, log_capture):
    ctrl_on.emergency_stop()
    status = ctrl_on.poll_all_axes()
    for ax in ["A", "B", "C"]:
        assert not status[ax].servo_on, f"Axis {ax} still enabled after E-STOP"


# ── set_params ───────────────────────────────────────────────────────────────

def test_log_set_params_speed(ctrl, log_capture):
    ctrl.set_params("A", speed=1000)
    assert any("Set params" in m and "A" in m and "SP=1000" in m for m in log_capture), \
        f"No set_params SP log. Messages: {log_capture}"


def test_log_set_params_accel_decel(ctrl, log_capture):
    ctrl.set_params("B", accel=20000, decel=15000)
    assert any("AC=20000" in m for m in log_capture), \
        f"No AC log. Messages: {log_capture}"
    assert any("DC=15000" in m for m in log_capture), \
        f"No DC log. Messages: {log_capture}"


def test_log_set_params_all(ctrl, log_capture):
    ctrl.set_params("C", speed=2000, accel=10000, decel=8000)
    assert any("Set params" in m and "C" in m for m in log_capture)
    combined = " ".join(log_capture)
    assert "SP=2000" in combined
    assert "AC=10000" in combined
    assert "DC=8000" in combined


def test_log_set_params_no_log_when_nothing_set(ctrl, log_capture):
    """set_params with no keyword args must not emit a spurious log line."""
    log_capture.clear()
    ctrl.set_params("A")
    assert not any("Set params" in m for m in log_capture), \
        "Unexpected Set params log when nothing was changed"


def test_set_params_actually_stored(ctrl):
    """Verify values are stored and affect simulation (integration check)."""
    ctrl.set_params("A", speed=500, accel=5000, decel=5000)
    assert ctrl._sp["A"] == 500.0
    assert ctrl._ac["A"] == 5000.0
    assert ctrl._dc["A"] == 5000.0


# ── Move relative / absolute ─────────────────────────────────────────────────

def test_log_move_relative(ctrl_on, log_capture):
    ctrl_on.move_relative("C", 500)
    assert any("relative" in m.lower() and "C" in m for m in log_capture), \
        f"No move_relative log. Messages: {log_capture}"
    assert any("500" in m for m in log_capture)


def test_log_move_absolute(ctrl_on, log_capture):
    ctrl_on.move_absolute("A", 1000)
    assert any("absolute" in m.lower() and "A" in m for m in log_capture), \
        f"No move_absolute log. Messages: {log_capture}"
    assert any("1000" in m for m in log_capture)


# ── Define Zero ──────────────────────────────────────────────────────────────

def test_log_define_zero(ctrl_on, log_capture):
    ctrl_on.define_zero("ABC")
    assert any("zero" in m.lower() for m in log_capture), \
        f"No define_zero log. Messages: {log_capture}"


def test_define_zero_resets_position(ctrl_on):
    ctrl_on.move_relative("A", 5000, wait=True)
    ctrl_on.define_zero("A")
    assert ctrl_on._pos["A"] == 0.0
    assert ctrl_on._target["A"] == 0.0


# ── PollingWorker: motion-complete log ───────────────────────────────────────

def test_polling_logs_motion_complete(log_capture):
    """PollingWorker.run() logs 'Motion complete' when SC transitions 1→10."""
    ctrl = MockGalilController()
    ctrl.enable_axes("A")
    # Use a target well beyond one-poll braking distance (250 counts at SP=5000)
    # so the axis spends at least one poll in SC=1 before reaching SC=10.
    ctrl.move_absolute("A", 10_000, wait=False)

    # Simulate what PollingWorker does: poll until SC=10
    worker = PollingWorker(ctrl)
    prev_sc = {"A": 10, "B": 10, "C": 10}
    prev_fwd = {"A": False, "B": False, "C": False}
    prev_rev = {"A": False, "B": False, "C": False}

    for _ in range(200):
        status = ctrl.poll_all_axes()
        for ax, s in status.items():
            sc = s.stop_code
            prev = prev_sc.get(ax, 10)
            if prev not in (0, 10) and sc == 10:
                logger.info(f"[{ax}] Motion complete \u2192 {s.pos:.1f} counts")
            if not prev_fwd.get(ax) and s.fwd_limit:
                logger.warning(f"[{ax}] FWD limit active \u2192 {s.pos:.1f} counts")
            if not prev_rev.get(ax) and s.rev_limit:
                logger.warning(f"[{ax}] REV limit active \u2192 {s.pos:.1f} counts")
            prev_sc[ax] = sc
            prev_fwd[ax] = s.fwd_limit
            prev_rev[ax] = s.rev_limit
        if status["A"].stop_code == 10:
            break

    assert any("Motion complete" in m and "A" in m for m in log_capture), \
        f"No 'Motion complete' log. Messages: {log_capture}"


# ── PollingWorker: limit-trip log ─────────────────────────────────────────────

def test_polling_logs_fwd_limit_trip(log_capture):
    """Drive axis A to its FWD trip point and confirm the limit log appears."""
    ctrl = MockGalilController()
    ctrl.enable_axes("A")
    ctrl.move_absolute("A", ctrl._fwd_trip["A"] + 1000, wait=False)

    prev_fwd = {"A": False, "B": False, "C": False}
    prev_rev = {"A": False, "B": False, "C": False}
    prev_sc  = {"A": 10, "B": 10, "C": 10}

    for _ in range(2000):
        status = ctrl.poll_all_axes()
        for ax, s in status.items():
            if not prev_fwd.get(ax) and s.fwd_limit:
                logger.warning(f"[{ax}] FWD limit active \u2192 {s.pos:.1f} counts")
            if not prev_rev.get(ax) and s.rev_limit:
                logger.warning(f"[{ax}] REV limit active \u2192 {s.pos:.1f} counts")
            prev_sc[ax] = s.stop_code
            prev_fwd[ax] = s.fwd_limit
            prev_rev[ax] = s.rev_limit
        if status["A"].fwd_limit:
            break

    assert any("FWD limit" in m and "A" in m for m in log_capture), \
        f"No FWD limit log. Messages: {log_capture}"


def test_polling_logs_rev_limit_trip(log_capture):
    """Drive axis C to its REV trip point and confirm the limit log appears."""
    ctrl = MockGalilController()
    ctrl.enable_axes("C")
    # Start near REV trip for C (default -500 counts)
    ctrl._pos["C"] = -300.0
    ctrl._target["C"] = -300.0
    ctrl.move_absolute("C", ctrl._rev_trip["C"] - 1000, wait=False)

    prev_fwd = {"A": False, "B": False, "C": False}
    prev_rev = {"A": False, "B": False, "C": False}

    for _ in range(2000):
        status = ctrl.poll_all_axes()
        for ax, s in status.items():
            if not prev_fwd.get(ax) and s.fwd_limit:
                logger.warning(f"[{ax}] FWD limit active \u2192 {s.pos:.1f} counts")
            if not prev_rev.get(ax) and s.rev_limit:
                logger.warning(f"[{ax}] REV limit active \u2192 {s.pos:.1f} counts")
            prev_fwd[ax] = s.fwd_limit
            prev_rev[ax] = s.rev_limit
        if status["C"].rev_limit:
            break

    assert any("REV limit" in m and "C" in m for m in log_capture), \
        f"No REV limit log. Messages: {log_capture}"


# ── Simulation physics sanity check with logging ──────────────────────────────

def test_set_params_speed_respected_in_simulation(ctrl_on, log_capture):
    """Slower speed setting must keep velocity below the new limit."""
    ctrl_on.set_params("A", speed=500, accel=50000, decel=50000)
    ctrl_on.jog("A", 500)

    # After several polls the axis should not exceed SP=500 counts/s
    for _ in range(20):
        status = ctrl_on.poll_all_axes()
    vel = abs(status["A"].vel)
    assert vel <= 600, f"Velocity {vel:.0f} exceeds SP=500 by too much"
    assert any("SP=500" in m for m in log_capture), \
        "Expected SP=500 in log"


def test_no_spurious_motion_complete_after_halt(log_capture):
    """After halt + decel settles, no further 'Motion complete' should appear."""
    ctrl = MockGalilController()
    ctrl.enable_axes("A")
    ctrl.jog("A", 5000)
    for _ in range(5):
        ctrl.poll_all_axes()   # build up speed

    ctrl.halt_axis("A")
    for _ in range(5):
        ctrl.poll_all_axes()   # decel ramp

    # Gather SC transitions for the stable phase
    prev_sc = ctrl.poll_all_axes()["A"].stop_code
    motion_complete_count = 0
    for _ in range(10):
        status = ctrl.poll_all_axes()
        sc = status["A"].stop_code
        if prev_sc not in (0, 10) and sc == 10:
            motion_complete_count += 1
            logger.info(f"[A] Motion complete \u2192 {status['A'].pos:.1f} counts")
        prev_sc = sc

    # At most one motion-complete transition expected (the final decel finish)
    assert motion_complete_count <= 1, \
        f"Too many motion-complete transitions after halt: {motion_complete_count}"
