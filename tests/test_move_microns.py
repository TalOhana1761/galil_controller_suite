"""
Tests for move_microns() — closed-loop micron-unit relative motion.

The mock half checks the drop-in twin. The hardware half drives the real
GalilController through a fake gclib device (FakeGalil below), which is the only
way to test the loop itself: that it steps, measures, corrects, converges on an
ENCODER target, and gives up sensibly when the target is finer than one
microstep.

Covers:
  - µm → motor counts with each axis's own counts_per_um
  - convergence inside tolerance, in more than one step
  - recovery from a drivetrain that under-delivers every step
  - one-directional approach (gain < 1 never reverses into backlash)
  - the resolution-floor, overshoot and max_steps exits
  - axis validation, travel guard, limit guards

Run:  pytest tests/test_move_microns.py -v
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from controller.mock_controller import (
    MockGalilController, MOTOR_COUNTS_PER_UM, AXIS_TRAVEL_MM,
)

AXES = ["A", "B", "C"]


@pytest.fixture
def ctrl():
    c = MockGalilController()
    c.enable_axes("ABC")
    return c


# ── Mock: conversion and convergence ────────────────────────────────────────

@pytest.mark.parametrize("axis", AXES)
def test_converges_using_that_axis_counts_per_um(ctrl, axis):
    ctrl.move_microns(axis, 10.0)
    want = 10.0 * MOTOR_COUNTS_PER_UM[axis]
    assert abs(ctrl._pos[axis] - want) <= 1.0


@pytest.mark.parametrize("axis", AXES)
def test_negative_microns_move_backwards(ctrl, axis):
    ctrl.move_microns(axis, -10.0)
    want = -10.0 * MOTOR_COUNTS_PER_UM[axis]
    assert abs(ctrl._pos[axis] - want) <= 1.0
    assert ctrl._last_dir[axis] == -1


def test_returns_measured_microns(ctrl):
    moved = ctrl.move_microns("A", 25.0)
    assert moved == pytest.approx(25.0, abs=1.0 / MOTOR_COUNTS_PER_UM["A"])


def test_zero_is_a_no_op(ctrl):
    assert ctrl.move_microns("A", 0.0) == 0.0
    assert ctrl._pos["A"] == 0.0


def test_move_is_iterative_not_one_shot(ctrl):
    steps = []
    inner = ctrl.move_relative
    ctrl.move_relative = lambda ax, counts, **kw: (steps.append(counts),
                                                   inner(ax, counts, **kw))[1]
    ctrl.move_microns("A", 10.0)
    assert len(steps) > 1, "expected a step/measure/correct loop, not a single move"


def test_max_steps_bounds_the_loop(ctrl):
    ctrl.move_microns("A", 100.0, max_steps=1)
    # One damped step only: 90% of the way, not inside tolerance.
    assert abs(ctrl._pos["A"] - 100.0 * MOTOR_COUNTS_PER_UM["A"]) > 1.0


# ── Mock: guards ────────────────────────────────────────────────────────────

def test_unknown_axis_rejected(ctrl):
    with pytest.raises(ValueError):
        ctrl.move_microns("D", 10.0)


@pytest.mark.parametrize("axis", AXES)
def test_move_longer_than_travel_rejected(ctrl, axis):
    with pytest.raises(ValueError):
        ctrl.move_microns(axis, AXIS_TRAVEL_MM[axis] * 1000.0 + 1.0)
    assert ctrl._pos[axis] == 0.0


def test_move_into_active_limit_rejected(ctrl):
    ctrl.simulate_fwd_limit("A", True)
    with pytest.raises(RuntimeError):
        ctrl.move_microns("A", 10.0)


def test_move_off_active_limit_allowed(ctrl):
    ctrl.simulate_fwd_limit("C", True)   # Z has independent fwd/rev switches
    ctrl.move_microns("C", -10.0)
    assert abs(ctrl._pos["C"] - -10.0 * MOTOR_COUNTS_PER_UM["C"]) <= 1.0


# ── Hardware loop, against a fake gclib device ──────────────────────────────

class FakeGalil:
    """Minimal gclib stand-in: an encoder that responds to PR/BG.

    Mirrors the real scales — the motor is 6.4 counts/µm and the encoder 12.8,
    so every motor count commanded moves _TP by ENC_PER_MOTOR = 2.

    efficiency < 1 models a drivetrain that under-delivers (lost motion, dropped
    microsteps); trip_at makes _LF go active once the axis passes a position.
    """
    ENC_PER_MOTOR = 2.0

    def __init__(self, efficiency: float = 1.0, lag_reads: int = 0):
        self.tp = {ax: 0.0 for ax in AXES}
        self.lf = {ax: 1 for ax in AXES}      # 1 = clear, 0 = ACTIVE
        self.lr = {ax: 1 for ax in AXES}      # reverse input, same convention
        self.pending = {ax: 0 for ax in AXES}
        self.efficiency = efficiency
        # lag_reads > 0 models the real drivetrain: BG returns, GMotionComplete
        # returns, and the stage is STILL moving - the rest of the travel arrives
        # over the next few encoder reads instead of all at once.
        self.lag_reads = lag_reads
        self.inflight = {ax: 0.0 for ax in AXES}
        self.trip_at: dict[str, float] = {}
        self.commands: list[str] = []
        self.poll_steps = 0                       # _BG polls a move lasts
        self.moving = {ax: 0 for ax in AXES}
        self.remaining = {ax: 0.0 for ax in AXES}

    def _release(self, ax: str):
        """Hand over one read's worth of the motion still in flight."""
        if not self.inflight[ax]:
            return
        if abs(self.inflight[ax]) <= 1.0:
            # Less than one encoder count left to give: the stage has stopped.
            chunk, self.inflight[ax] = self.inflight[ax], 0.0
        else:
            chunk = self.inflight[ax] / max(self.lag_reads, 1)
            self.inflight[ax] -= chunk
        self.tp[ax] += chunk

    def GCommand(self, cmd: str) -> str:
        cmd = cmd.strip()
        self.commands.append(cmd)
        if cmd.startswith("MG _TP"):
            self._release(cmd[-1])
            return f"{self.tp[cmd[-1]]:.0f}\r\n"
        if cmd.startswith("MG _LR"):
            return f"{self.lr[cmd[-1]]:.4f}\r\n"
        if cmd.startswith("MG _LF"):
            return f"{self.lf[cmd[-1]]:.4f}\r\n"
        if cmd.startswith("PR"):
            self.pending[cmd[2]] = int(cmd.split("=")[1])
            return ":"
        if cmd.startswith("MG _BG"):
            # Combined form first: the controller asks for "_BGx, _LFx" in
            # one round trip, because during a move a round trip is travel.
            ax = cmd.split("_BG")[1][0]
            self._advance(ax)
            moving = 1.0 if self.moving.get(ax, 0) > 0 else 0.0
            if "_LF" in cmd:
                return f"{moving:.4f} {self.lf[ax]:.4f}"
            return f"{moving:.4f}"
        if cmd.startswith("ST"):
            ax = cmd[2]
            self.moving[ax] = 0                   # halted where it stands
            self.remaining[ax] = 0.0
            return ":"
        if cmd.startswith("BG"):
            ax = cmd[2]
            travel = self.pending[ax] * self.ENC_PER_MOTOR * self.efficiency
            if self.poll_steps:
                self.moving[ax] = self.poll_steps
                self.remaining[ax] = travel
                self.pending[ax] = 0
                return ":"
            if self.lag_reads:
                # Only half of it has landed by the time motion completes.
                self.tp[ax] += travel / 2.0
                self.inflight[ax] += travel / 2.0
            else:
                self.tp[ax] += travel
            self.pending[ax] = 0
            trip = self.trip_at.get(ax)
            if trip is not None and self.tp[ax] >= trip:
                self.lf[ax] = 0
            return ":"
        return ":"

    # ── Motion that takes time, so a limit can trip part way through ────────
    # poll_steps > 0 makes BG start a move that completes over that many
    # _BG polls, advancing the position each poll. Without it BG lands the whole
    # travel instantly and nothing can trip mid-step.

    def _advance(self, ax: str):
        if self.moving.get(ax, 0) <= 0:
            return
        chunk = self.remaining[ax] / self.moving[ax]
        self.remaining[ax] -= chunk
        self.moving[ax] -= 1
        self.tp[ax] += chunk
        trip = self.trip_at.get(ax)
        if trip is not None and self.tp[ax] >= trip:
            self.lf[ax] = 0

    def GMotionComplete(self, axis):
        while self.moving.get(axis, 0) > 0:
            self._advance(axis)

    def GTimeout(self, ms): pass

    @property
    def steps(self) -> list[int]:
        return [int(c.split("=")[1]) for c in self.commands if c.startswith("PR")]


@pytest.fixture
def hw(tmp_path):
    """The REAL controller class, driven through a fake gclib device.

    LAST_DIR_PATH is redirected to a temp file for the duration: move_microns()
    persists the direction whenever it changes, and without this the suite writes
    fabricated directions into the project's config/last_direction.json. That
    happened on 2026-09-23 — the file ended up holding a fake test's values
    rather than the rig's, which is worse than having no file at all, because the
    limit logic would trust it.
    """
    import controller.galil_controller as gc
    original = gc.LAST_DIR_PATH
    gc.LAST_DIR_PATH = str(tmp_path / "last_direction.json")
    # Give the FAKE rig a switch on every axis. These tests exercise the limit
    # GUARD, not which switches happen to be bolted to the real stage this week,
    # and gc.LIMIT_INPUT holds the latter - all three since 2026-10-12, but it
    # has changed before - so without this a limit test could pass vacuously.
    original_wired = gc.LIMIT_INPUT
    gc.LIMIT_INPUT = {ax: "FWD" for ax in ("A", "B", "C")}
    c = gc.GalilController()
    c._g = FakeGalil()
    yield c
    gc.LAST_DIR_PATH = original
    gc.LIMIT_INPUT = original_wired


def test_hw_converges_on_an_encoder_target(hw):
    from controller.galil_controller import ENCODER_COUNTS_PER_UM
    moved = hw.move_microns("A", 10.0)
    assert hw._g.tp["A"] == pytest.approx(10.0 * ENCODER_COUNTS_PER_UM, abs=1.0)
    assert moved == pytest.approx(10.0, abs=1.0 / ENCODER_COUNTS_PER_UM)
    assert len(hw._g.steps) > 1, "expected a step/measure/correct loop"


def test_hw_corrects_a_drivetrain_that_under_delivers(hw):
    """Every step lands 20% short — an open-loop move would end 20% short too."""
    from controller.galil_controller import ENCODER_COUNTS_PER_UM
    hw._g.efficiency = 0.8
    hw.move_microns("A", 10.0)
    assert hw._g.tp["A"] == pytest.approx(10.0 * ENCODER_COUNTS_PER_UM, abs=1.0)


def test_hw_approach_never_reverses(hw):
    """gain < 1 undershoots, so the move never has to back up into lost motion."""
    steps = None
    hw.move_microns("A", 50.0)
    steps = hw._g.steps
    assert all(s > 0 for s in steps), f"direction reversed mid-move: {steps}"


def test_hw_tolerance_below_the_floor_stops_at_the_floor(hw):
    """A tolerance finer than one microstep must stop, not dither forever."""
    hw.move_microns("A", 10.0, tol_counts=0.01, max_steps=40)
    assert len(hw._g.steps) < 40
    assert abs(hw._g.tp["A"] - 128.0) <= 2.0


def test_hw_does_not_reverse_to_fix_an_overshoot(hw):
    """Overshoot costs less than the lost motion a reversal would pay."""
    hw._g.efficiency = 1.5          # every step lands 50% long
    hw.move_microns("A", 10.0)
    assert all(s > 0 for s in hw._g.steps), f"reversed: {hw._g.steps}"
    assert hw._g.tp["A"] > 128.0    # left overshot, deliberately


def test_hw_waits_for_the_stage_to_stop_before_measuring(hw):
    """Motion complete is not the same as stopped.

    On axis C the stage was still travelling when the loop measured - up to 56
    encoder counts on a 100 um move - so the loop corrected against a stale
    reading and overshot when the rest arrived. Here half of every step lands
    after the fact; the loop has to wait it out rather than chase it.
    """
    from controller.galil_controller import ENCODER_COUNTS_PER_UM
    hw._g.lag_reads = 4
    hw.move_microns("A", 100.0)
    assert hw._g.inflight["A"] == 0.0, "measured while the stage was still moving"
    # Without the wait this lands ~177 counts (13.8 um) past target, exactly as
    # axis C did on hardware. With it, one microstep.
    off_by = abs(hw._g.tp["A"] - 100.0 * ENCODER_COUNTS_PER_UM)
    assert off_by <= 2.0 + 1e-9, f"{off_by:.1f} counts past target"


def test_hw_limit_tripping_mid_move_stops_the_loop(hw):
    hw._g.trip_at["A"] = 40.0        # goes active partway through
    hw.move_microns("A", 100.0)      # would need 1280 encoder counts
    assert hw._g.tp["A"] < 1280.0
    assert hw._g.lf["A"] == 0


def test_hw_refuses_to_drive_further_into_a_limit(hw):
    hw._g.lf["A"] = 0
    hw._last_dir["A"] = +1
    with pytest.raises(RuntimeError):
        hw.move_microns("A", 10.0)


def test_hw_travel_guard(hw):
    with pytest.raises(ValueError):
        hw.move_microns("A", 25_000.0)   # axis A travel is 20 mm
    assert hw._g.steps == []


# ── Microstep snapping ──────────────────────────────────────────────────────

def test_snap_targets_a_reachable_distance(ctrl):
    """1 um is 6.4 microsteps, which does not exist. Nearest is 6 (0.9375 um).

    Note it rounds DOWN here: 6 microsteps is 0.0625 um away from 1 um, 7 is
    0.09375 away. Ask for 1.09375 explicitly if the 7-step point is wanted.
    """
    step = 1.0 / MOTOR_COUNTS_PER_UM["A"]
    moved = ctrl.move_microns("A", 1.0)
    assert moved == pytest.approx(6 * step, abs=step)


def test_snap_leaves_on_grid_distances_alone(ctrl):
    step = 1.0 / MOTOR_COUNTS_PER_UM["A"]
    for dist in (10.0, 50.0, step * 3, step * 7):
        c = MockGalilController()
        c.enable_axes("ABC")
        # Within one step: the loop stops inside tol_counts, it does not chase
        # the last count.
        assert c.move_microns("A", dist) == pytest.approx(dist, abs=step)


def test_snap_off_reports_the_raw_residual(ctrl):
    """Without snapping the loop lands on a neighbour and the error is real."""
    moved = ctrl.move_microns("A", 1.0, snap=False)
    assert moved != pytest.approx(1.0, abs=1e-9)
    assert abs(moved - 1.0) <= 1.0 / MOTOR_COUNTS_PER_UM["A"]


def test_sub_microstep_request_commands_nothing(ctrl):
    assert ctrl.move_microns("A", 0.05) == 0.0
    assert ctrl._pos["A"] == 0.0


# ── Interactive-move plumbing (what the jog panel passes) ───────────────────

def test_button_callback_brackets_the_move(ctrl):
    """The jog panel greys its button out for the duration of the move."""
    calls = []
    ctrl.move_microns("A", 10.0,
                      button_state_callback=lambda ax, d, on: calls.append((ax, d, on)))
    assert calls[0] == ("A", "+", False)
    assert calls[-1] == ("A", "+", True)


def test_button_callback_re_enables_after_a_refusal(ctrl):
    """A refused move must not leave the button disabled forever."""
    calls = []
    ctrl.simulate_fwd_limit("A", True)
    with pytest.raises(RuntimeError):
        ctrl.move_microns("A", 10.0,
                          button_state_callback=lambda ax, d, on: calls.append((ax, d, on)))
    assert calls[-1] == ("A", "+", True)


def test_settle_window_is_accepted_by_both_controllers(ctrl):
    """The jog panel passes settle_window_ms; the mock must not choke on it."""
    import inspect
    from controller.galil_controller import GalilController
    for fn in (GalilController.move_microns, MockGalilController.move_microns):
        params = inspect.signature(fn).parameters
        assert "settle_window_ms" in params
        assert "button_state_callback" in params
    ctrl.move_microns("A", 10.0, settle_window_ms=60)


# ── Limit handling during a move ────────────────────────────────────────────

def test_hw_stops_mid_step_when_the_limit_trips(hw):
    """LD=3 means nothing in hardware stops the axis, so the loop must.

    A bulk step can be hundreds of microns; waiting for it to finish before
    looking at the limit would drive that far past the switch.
    """
    hw._g.poll_steps = 8               # the move takes 8 polls to complete
    hw._g.trip_at["A"] = 200.0         # switch trips a quarter of the way in
    hw.move_microns("A", 100.0)        # would need 1280 encoder counts

    assert any(c.startswith("STA") for c in hw._g.commands), "axis was not halted"
    assert hw._g.lf["A"] == 0
    assert hw._g.tp["A"] < 1280.0, "ran past the limit to the target"


def test_hw_escape_move_is_not_blocked_by_an_active_limit(hw):
    """Starting ON the switch is the escape case: the watch must be disarmed.

    The limit reads active for the whole move until the axis drives off it, so
    aborting on the level rather than the transition would make escape
    impossible - the axis would be stuck on the switch forever.
    """
    from controller.galil_controller import ENCODER_COUNTS_PER_UM
    hw._g.poll_steps = 4
    hw._g.lf["A"] = 0                  # already sitting on the switch
    hw._last_dir["A"] = +1             # got there going forward
    moved = hw.move_microns("A", -10.0)   # drive back off it

    assert not any(c.startswith("STA") for c in hw._g.commands), \
        "escape move was halted by the limit it is escaping"
    assert moved == pytest.approx(-10.0, abs=1.0 / ENCODER_COUNTS_PER_UM)


def test_hw_refuses_to_start_further_into_a_limit(hw):
    hw._g.lf["A"] = 0
    hw._last_dir["A"] = +1
    with pytest.raises(RuntimeError):
        hw.move_microns("A", 10.0)
    assert hw._g.steps == [], "commanded a move into an active limit"


def test_the_old_closed_loop_cluster_is_gone():
    """Removed 2026-09-22. All of it carried the stale *1.25 encoder factor.

    move_relative_closed_loop() targeted _TP + counts*1.25, where 1.25 is
    12.8/10.24 from an older motor scale; the measured encoder-per-motor-count
    ratio is 2.0, so it under-targeted by 1.6x. move_exact() had no callers at
    all, and the rest existed only to serve it.
    """
    import controller.galil_controller as gc
    for name in ("move_relative_closed_loop", "determine_motion_step",
                 "determine_settle_speed", "move_exact",
                 "move_opposite_direction", "move_if_at_limit"):
        assert not hasattr(gc.GalilController, name), f"{name} came back"
        assert not hasattr(MockGalilController, name), f"mock {name} came back"

    # The constants that existed only for them, including the XY/Z pair that
    # read axis_a and axis_c and ignored axis_b.
    for const in ("MOTOR_BACKLASH", "MOTOR_COUNTS_TO_MICRONS_XY",
                  "MOTOR_COUNTS_TO_MICRONS_Z", "ENCODER_COUNTS_TO_MICRONS_XY"):
        assert not hasattr(gc, const), f"{const} came back"

    # And what replaced them.
    assert gc.MOTOR_COUNTS_PER_UM.keys() == {"A", "B", "C"}
    assert gc.ENCODER_COUNTS_PER_UM == 12.8


# ── Absolute moves, for saved positions ─────────────────────────────────────

def test_hw_move_to_counts_lands_on_the_target(hw):
    """A saved slot is an absolute _TP reading, so the move aims at a place."""
    hw._g.tp["A"] = 3_226_664.0
    hw.move_to_counts("A", 3_227_944.0)          # +100 um away
    assert abs(hw._g.tp["A"] - 3_227_944.0) <= 2.0


def test_hw_move_to_counts_works_in_both_directions(hw):
    hw._g.tp["A"] = 3_227_944.0
    hw.move_to_counts("A", 3_226_664.0)          # back again
    assert abs(hw._g.tp["A"] - 3_226_664.0) <= 2.0


def test_hw_move_to_counts_is_a_no_op_when_already_there(hw):
    """Recalling the position you are already at must not nudge the stage."""
    hw._g.tp["A"] = 3_226_664.0
    assert hw.move_to_counts("A", 3_226_664.0) == 0.0
    assert hw._g.steps == []


def test_hw_move_to_counts_does_not_drift_on_repeat(hw):
    """Absolute means absolute: going there twice ends in the same place."""
    hw._g.tp["A"] = 3_226_664.0
    hw.move_to_counts("A", 3_227_944.0)
    first = hw._g.tp["A"]
    hw.move_to_counts("A", 3_227_944.0)
    assert hw._g.tp["A"] == first


def test_hw_move_all_absolute_drives_every_axis_to_its_target(hw):
    """The old version subtracted the current position from values the caller
    had already made relative, so it moved (target - current) - current."""
    hw._g.tp = {"A": 3_226_664.0, "B": 3_834_226.0, "C": 6_329_144.0}
    targets = {"A": 3_227_944.0, "B": 3_834_866.0, "C": 6_328_504.0}
    hw.move_all_absolute(targets)
    for ax, want in targets.items():
        assert abs(hw._g.tp[ax] - want) <= 2.0, f"{ax} missed its target"


def test_mock_move_to_counts_matches(ctrl):
    ctrl.move_relative("A", 1000.0, wait=True)
    ctrl.move_to_counts("A", 1640.0)
    assert abs(ctrl._pos["A"] - 1640.0) <= 1.0


# ── Post-move verify ────────────────────────────────────────────────────────

def test_verify_refuses_to_correct_an_overshoot(hw):
    """Relaxation always runs WITH the travel, so the residual is an overshoot.

    Correcting it means reversing, and a reversal costs 7-11 um of take-up on
    this axis to recover ~0.2 um. It must report and leave it.
    """
    hw._g.tp["A"] = 1000.0 + 6.0          # sitting 6 counts PAST the target
    hw._last_dir["A"] = +1                # got there going forward
    residual = hw.post_move_verify("A", 1000.0, relax_s=0.01)
    assert hw._g.steps == [], "reversed into backlash to fix an overshoot"
    assert residual < 0                    # negative = past the target


def test_verify_leaves_a_shortfall_smaller_than_its_own_creep(hw):
    """Correcting costs a fresh ~0.45 um of creep, so a small shortfall is
    better left alone. Simulation had this turn +0.22 um into +0.32."""
    from controller.galil_controller import RELAX_UNDERSHOOT_UM, ENCODER_COUNTS_PER_UM
    small = (RELAX_UNDERSHOOT_UM / 2) * ENCODER_COUNTS_PER_UM
    hw._g.tp["A"] = 1000.0 - small        # short of the target
    hw._last_dir["A"] = +1
    hw.post_move_verify("A", 1000.0, relax_s=0.01)
    assert hw._g.steps == [], "corrected a shortfall smaller than the creep"


def test_verify_closes_a_shortfall_worth_closing(hw):
    """A shortfall bigger than the creep IS worth correcting — same direction,
    so no backlash is paid."""
    from controller.galil_controller import ENCODER_COUNTS_PER_UM
    hw._g.tp["A"] = 1000.0 - 5.0 * ENCODER_COUNTS_PER_UM   # 5 um short
    hw._last_dir["A"] = +1
    hw.post_move_verify("A", 1000.0, relax_s=0.01)
    assert hw._g.steps, "did not correct a 5 um shortfall"
    assert all(s > 0 for s in hw._g.steps), "correction reversed"
    assert abs(hw._g.tp["A"] - 1000.0) <= 2.0


def test_move_to_counts_verify_aims_short(hw):
    """verify=True must AIM short, so the relaxation carries the stage in.

    Asserted on the aim, not the landing: this fake has no creep, so after
    aiming short the shortfall survives, exceeds the do-no-harm threshold and
    gets legitimately closed — the stage ends ON target. On the real stage the
    creep closes most of it first.
    """
    from loguru import logger
    from controller.galil_controller import RELAX_UNDERSHOOT_UM, ENCODER_COUNTS_PER_UM

    messages = []
    sink = logger.add(lambda m: messages.append(m.record["message"]), level="INFO")
    try:
        hw._g.tp["A"] = 0.0
        target = 100.0 * ENCODER_COUNTS_PER_UM
        hw.move_to_counts("A", target, verify=True, relax_s=0.01,
                          settle_window_ms=60)
    finally:
        logger.remove(sink)

    aimed = [m for m in messages if "aiming" in m and "short" in m]
    assert aimed, f"never aimed short; log was {messages}"
    assert f"{RELAX_UNDERSHOOT_UM:.3f} um short" in aimed[0]
    # ...and having aimed short, it still finishes on target.
    assert abs(hw._g.tp["A"] - target) <= 2.0


def test_verify_undershoot_is_capped_on_short_moves(hw):
    """Aiming 0.45 um short of a 0.5 um move would leave it under the 0.31 um
    stiction threshold, i.e. it would not move at all."""
    from controller.galil_controller import ENCODER_COUNTS_PER_UM
    hw._g.tp["A"] = 0.0
    target = 0.5 * ENCODER_COUNTS_PER_UM
    hw.move_to_counts("A", target, verify=True, relax_s=0.01, settle_window_ms=60)
    assert hw._g.tp["A"] > 0, "short move was cancelled by the undershoot"


def test_mock_has_the_same_entry_points():
    import inspect
    from controller.galil_controller import GalilController
    for fn in ("post_move_verify", "move_to_counts"):
        assert hasattr(MockGalilController, fn)
    for name in ("verify", "undershoot_um", "relax_s"):
        assert name in inspect.signature(MockGalilController.move_to_counts).parameters
        assert name in inspect.signature(GalilController.move_to_counts).parameters


# ── Live position during a blocking move ────────────────────────────────────

def test_hw_reports_position_while_the_axis_is_still_moving(hw, monkeypatch):
    """A move blocks the polling thread, so the display used to freeze until it
    ended. position_listener must see readings from INSIDE a step, not only the
    settled ones after it."""
    import controller.galil_controller as gc
    monkeypatch.setattr(gc, "PROGRESS_INTERVAL_S", 0.0)   # the fake is instant
    seen = []
    hw.position_listener = lambda ax, counts: seen.append((ax, counts))
    hw._g.poll_steps = 8
    hw.move_microns("A", 100.0)                           # 1280 encoder counts

    assert seen and all(ax == "A" for ax, _ in seen)
    # A reading strictly between start and the end of the first bulk step
    # (0.9 x 1280) can only have been taken mid-step.
    assert any(0.0 < c < 0.9 * 1280 - 1 for _, c in seen), seen


def test_hw_without_a_listener_issues_no_extra_reads(hw, monkeypatch):
    """Hardware scripts install no listener; their command stream - and so their
    timing - must be exactly what it was."""
    import controller.galil_controller as gc
    monkeypatch.setattr(gc, "PROGRESS_INTERVAL_S", 0.0)
    hw._g.poll_steps = 8
    hw.move_microns("A", 100.0)
    without = list(hw._g.commands)

    hw2 = gc.GalilController()
    hw2._g = FakeGalil()
    hw2._g.poll_steps = 8
    hw2.position_listener = lambda ax, counts: None
    hw2.move_microns("A", 100.0)
    # Count only _TP reads taken WHILE MOVING, i.e. followed by another _BG
    # poll. Total _TP counts are timing-dependent - _wait_until_still() reads for
    # as long as its window lasts - which made a plain total flaky. One such pair
    # exists either way: the start-of-move limit check reads _BG after _TP.
    def mid_motion_tp_reads(cmds):
        return sum(1 for a, b in zip(cmds, cmds[1:])
                   if a == "MG _TPA" and b == "MG _BGA")
    assert mid_motion_tp_reads(without) == 1
    assert mid_motion_tp_reads(hw2._g.commands) > 1


def test_a_listener_that_raises_does_not_break_the_move(hw, monkeypatch):
    import controller.galil_controller as gc
    monkeypatch.setattr(gc, "PROGRESS_INTERVAL_S", 0.0)

    def broken(ax, counts):
        raise RuntimeError("display gone")
    hw.position_listener = broken
    moved = hw.move_microns("A", 10.0)
    assert moved == pytest.approx(10.0, abs=1.0 / gc.ENCODER_COUNTS_PER_UM)


# -- enabled_axes: what the Z retract button keys off ------------------------

def test_hw_enabled_axes_follow_enable_disable_and_estop(hw):
    hw.enabled_axes = set()
    hw.enable_axes("ABC")
    assert hw.enabled_axes == {"A", "B", "C"}
    hw.disable_axes("C")
    assert hw.enabled_axes == {"A", "B"}
    hw.enable_axes("C")
    hw.emergency_stop()
    assert hw.enabled_axes == set()
