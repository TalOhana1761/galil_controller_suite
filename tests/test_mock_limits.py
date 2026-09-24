"""
Tests for mock controller limit-switch simulation and direction tracking.

Covers:
  - Continuous jog / halt behaviour
  - XY auto-limit trip (both fwd+rev fire together, one physical switch)
  - Z auto-limit trip (fwd and rev fire separately, two physical switches)
  - Stop-code correctness on limit and motion-complete
  - last_direction tracking across move_relative, move_absolute, jog
  - Home-switch simulation

Run:  pytest tests/test_mock_limits.py -v
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from controller.mock_controller import MockGalilController

# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def ctrl():
    """Fresh controller with default trip points; servo off."""
    return MockGalilController()


@pytest.fixture
def ctrl_on():
    """Controller with all axes enabled."""
    c = MockGalilController()
    c.enable_axes("ABC")
    return c


# ── 1. Continuous jog advances position ─────────────────────────────────────

def test_jog_advances_continuously(ctrl_on):
    """After jog(+), polling multiple times should move position forward."""
    ctrl_on.jog("A", 1000)
    positions = []
    for _ in range(5):
        s = ctrl_on.poll_all_axes()
        positions.append(s["A"].pos)
    # Each poll should have moved the axis forward by up to 50 counts
    assert positions[-1] > positions[0], (
        "Position did not advance after 5 polls: did jog set a far target?")


def test_jog_reverse_retreats_position(ctrl_on):
    """After jog(-), position should decrease over multiple polls."""
    ctrl_on.jog("A", -1000)
    first = ctrl_on.poll_all_axes()["A"].pos
    for _ in range(5):
        ctrl_on.poll_all_axes()
    last = ctrl_on.poll_all_axes()["A"].pos
    assert last < first


# ── 2. Halt stops jog motion ─────────────────────────────────────────────────

def test_halt_stops_jog(ctrl_on):
    """halt_axis() decelerates the axis to rest; position is stable afterwards."""
    ctrl_on.jog("A", 5000)
    # Advance a few steps to build up speed
    for _ in range(3):
        ctrl_on.poll_all_axes()

    ctrl_on.halt_axis("A")

    # Allow decel ramp to complete (braking distance ~250 counts at 5000 counts/s)
    for _ in range(5):
        ctrl_on.poll_all_axes()

    # Position must be stable — no ongoing drift
    snapshots = [ctrl_on.poll_all_axes()["A"].pos for _ in range(5)]
    spread = max(snapshots) - min(snapshots)
    assert spread < 5, f"Not stable after halt: spread={spread:.1f} counts"


def test_halt_all_stops_all_axes(ctrl_on):
    """halt_all() should stop motion on all three axes."""
    for ax, spd in [("A", 1000), ("B", -1000), ("C", 500)]:
        ctrl_on.jog(ax, spd)
    for _ in range(3):
        ctrl_on.poll_all_axes()

    ctrl_on.halt_all()
    # Allow decel ramp to complete before measuring stability
    for _ in range(5):
        ctrl_on.poll_all_axes()
    snapshots = [ctrl_on.poll_all_axes() for _ in range(5)]

    for ax in ["A", "B", "C"]:
        spread = max(s[ax].pos for s in snapshots) - min(s[ax].pos for s in snapshots)
        assert spread < 5, f"Axis {ax} still moving after halt_all (spread={spread:.1f})"


# ── 3. XY auto-limit: both inputs trip together ──────────────────────────────

def _drive_to(ctrl, axis, target, max_polls=2000):
    """Poll until position reaches target or max polls exceeded."""
    ctrl.enable_axes(axis)
    ctrl.move_absolute(axis, target)
    for _ in range(max_polls):
        s = ctrl.poll_all_axes()
        if abs(s[axis].pos - target) < 2.0:
            return s
    return ctrl.poll_all_axes()


def test_only_the_fwd_flag_trips_after_a_forward_move(ctrl):
    """One switch per axis. Reaching it going FORWARD means the forward end, so
    only that flag rises - until 2026-09-24 A and B raised both together, which
    reads as "blocked in both directions" and leaves no way off the switch."""
    ctrl.set_trip_points("A", fwd=10_000.0, rev=-10_000.0)
    _drive_to(ctrl, "A", 12_000.0)
    # Snap will hold near 10000; just poll a few times to ensure auto-detect fires
    for _ in range(3):
        ctrl.poll_all_axes()
    s = ctrl.poll_all_axes()["A"]
    assert s.last_direction >= 0, "the drive should have recorded + as last"
    assert s.fwd_limit is True,  "FWD limit should be active"
    assert s.rev_limit is False, "the switch cannot be at both ends at once"


def test_only_the_rev_flag_trips_after_a_reverse_move(ctrl):
    """The same physical switch, the opposite conclusion, because the axis got
    there going the other way."""
    ctrl.set_trip_points("A", fwd=10_000.0, rev=-10_000.0)
    _drive_to(ctrl, "A", -12_000.0)
    for _ in range(3):
        ctrl.poll_all_axes()
    s = ctrl.poll_all_axes()["A"]
    assert s.last_direction < 0, "the drive should have recorded - as last"
    assert s.rev_limit is True
    assert s.fwd_limit is False


def test_xy_limits_clear_when_back_in_bounds(ctrl):
    """After tripping, moving back inside the safe zone clears both limits."""
    ctrl.set_trip_points("A", fwd=5_000.0, rev=-5_000.0)
    # Drive out of bounds then force pos back in bounds directly
    ctrl.enable_axes("A")
    ctrl.move_absolute("A", 8_000.0, wait=True)
    # Snap to verify limit trips
    for _ in range(3):
        ctrl.poll_all_axes()
    assert ctrl.poll_all_axes()["A"].fwd_limit is True

    # Now return inside bounds
    ctrl.move_absolute("A", 0.0, wait=True)
    for _ in range(3):
        ctrl.poll_all_axes()
    s = ctrl.poll_all_axes()["A"]
    assert s.fwd_limit is False, "FWD limit should clear once back in bounds"
    assert s.rev_limit is False, "REV limit should clear once back in bounds"


# ── 4. Z auto-limit: fwd and rev fire independently ──────────────────────────

def test_z_only_fwd_limit_trips_at_top(ctrl):
    """Moving C past its fwd trip should set fwd_limit but NOT rev_limit."""
    ctrl.set_trip_points("C", fwd=5_000.0, rev=-500.0)
    _drive_to(ctrl, "C", 7_000.0)
    for _ in range(3):
        ctrl.poll_all_axes()
    s = ctrl.poll_all_axes()["C"]
    assert s.fwd_limit is True,  "FWD limit must be active for Z at top"
    assert s.rev_limit is False, "REV limit must NOT be active when only FWD trips"


def test_z_only_rev_limit_trips_at_bottom(ctrl):
    """Moving C past its rev trip should set rev_limit but NOT fwd_limit."""
    ctrl.set_trip_points("C", fwd=5_000.0, rev=-500.0)
    _drive_to(ctrl, "C", -2_000.0)
    for _ in range(3):
        ctrl.poll_all_axes()
    s = ctrl.poll_all_axes()["C"]
    assert s.rev_limit is True,  "REV limit must be active for Z at bottom"
    assert s.fwd_limit is False, "FWD limit must NOT be active when only REV trips"


# ── 5. Limit snaps motion (position stops advancing after trip) ───────────────

def test_limit_snaps_motion_xy(ctrl_on):
    """Once XY limit trips, further polls should not advance position."""
    ctrl_on.set_trip_points("A", fwd=500.0, rev=-500.0)
    ctrl_on.jog("A", 10_000)  # heading toward fwd boundary

    # Drive until limit trips
    for _ in range(200):
        s = ctrl_on.poll_all_axes()
        if s["A"].fwd_limit:
            break

    assert s["A"].fwd_limit is True, "Limit should have tripped during jog"
    pos_at_trip = ctrl_on.poll_all_axes()["A"].pos

    # After snapping, position should stay stable
    for _ in range(10):
        ctrl_on.poll_all_axes()
    pos_later = ctrl_on.poll_all_axes()["A"].pos

    assert abs(pos_later - pos_at_trip) < 5, (
        f"Position advanced {abs(pos_later - pos_at_trip):.1f} counts after limit snap")


# ── 6. Stop-code correctness ──────────────────────────────────────────────────

def test_stop_code_6_when_fwd_limit(ctrl_on):
    """stop_code should be 6 (fwd limit) when only fwd_limit is active (Z axis)."""
    ctrl_on.set_trip_points("C", fwd=500.0, rev=-500.0)
    _drive_to(ctrl_on, "C", 1_000.0)
    for _ in range(3):
        ctrl_on.poll_all_axes()
    s = ctrl_on.poll_all_axes()["C"]
    assert s.fwd_limit is True
    assert s.stop_code == 6


def test_stop_code_7_when_rev_limit(ctrl_on):
    """stop_code should be 7 (rev limit) when only rev_limit is active (Z axis)."""
    ctrl_on.set_trip_points("C", fwd=500.0, rev=-500.0)
    _drive_to(ctrl_on, "C", -1_000.0)
    for _ in range(3):
        ctrl_on.poll_all_axes()
    s = ctrl_on.poll_all_axes()["C"]
    assert s.rev_limit is True
    assert s.stop_code == 7


def test_stop_code_10_when_at_rest(ctrl_on):
    """stop_code should be 10 (motion complete) when at target, no limit active."""
    ctrl_on.move_absolute("A", 1_000.0, wait=True)
    s = ctrl_on.poll_all_axes()["A"]
    assert s.stop_code == 10


def test_stop_code_0_when_servo_off(ctrl):
    """stop_code should be 0 when servo is disabled."""
    s = ctrl.poll_all_axes()["A"]
    assert s.servo_on is False
    assert s.stop_code == 0


# ── 7. last_direction tracking ────────────────────────────────────────────────

def test_last_direction_fwd_after_move_relative(ctrl):
    ctrl.move_relative("A", +500)
    s = ctrl.poll_all_axes()["A"]
    assert s.last_direction == +1


def test_last_direction_rev_after_move_relative(ctrl):
    ctrl.move_relative("A", -500)
    s = ctrl.poll_all_axes()["A"]
    assert s.last_direction == -1


def test_last_direction_fwd_after_jog(ctrl):
    ctrl.jog("B", +2000)
    s = ctrl.poll_all_axes()["B"]
    assert s.last_direction == +1


def test_last_direction_rev_after_jog(ctrl):
    ctrl.jog("B", -2000)
    s = ctrl.poll_all_axes()["B"]
    assert s.last_direction == -1


# ── 8. Home switch simulation ─────────────────────────────────────────────────

def test_simulate_home_switch(ctrl):
    ctrl.simulate_home_switch("A", True)
    s = ctrl.poll_all_axes()["A"]
    assert s.home_switch is True

    ctrl.simulate_home_switch("A", False)
    s = ctrl.poll_all_axes()["A"]
    assert s.home_switch is False
