"""
Hardware check of GalilController.move_microns() on one axis.

Runs the closed-loop move at four distances, three times each:

    1 um x3,  10 um x3,  50 um x3,  100 um x3      = 483 um of travel

and for every single move records the encoder before and after, the distance
actually travelled, and the error against what was asked for. The move function
reports its own measurement too; both are logged, because a disagreement between
them is a bug in the function rather than in the stage.

What the numbers mean here:

  - move_microns() targets an ENCODER position (_TP + microns x 12.8) and steps
    towards it until the error is inside tol_counts, so a residual error of one
    or two counts is the loop working correctly, not failing. One motor
    microstep moves the encoder 2 counts (0.156 um) - that is the floor, and no
    result can beat it.
  - Every move is in the same direction, so drivetrain take-up is paid once, at
    the start. It shows up as moves that produce NO encoder motion at all rather
    than as a large error - axis C swallowed two whole 1 um moves this way - so
    the summary counts the dead moves and excludes them before judging accuracy.
  - Repeatability - the spread of the three repeats at one distance - is the
    number that matters for positioning, and it is what this test measures best.
  - The micron errors CANNOT test encoder scale. move_microns() targets
    _TP + microns x 12.8 and this script measures delta_TP / 12.8, so the same
    constant sits on both sides and cancels. If 12.8 were wrong, every error
    column here would still read zero while the stage moved the wrong distance.
    Checking 12.8 itself needs an external length reference. What the summary
    does check independently is encoder counts per MOTOR count, since _TP and
    the PR counts come from different constants and must agree at 2.0.

The return run at the end drives the same total distance back and reports how
far from the starting reading the axis lands. That is hysteresis plus lost
motion, and it is the one measurement here that needs a direction reversal.

Nothing moves until the BiSS link reads clean. LD=3 disables the hardware limit
switches, so the total excursion is checked against travel_mm before starting.

Run directly against real hardware (not part of the pytest suite):
    python test_move_microns_hw.py             # axis A, 1/10/50/100 um x3
    python test_move_microns_hw.py B
    python test_move_microns_hw.py C --speed 4000
    python test_move_microns_hw.py A --reverse        # test the other direction
    python test_move_microns_hw.py A --distances 1,5,25 --repeats 5
    python test_move_microns_hw.py A --no-return      # skip the return run
    python test_move_microns_hw.py C --verify         # with vs without the
                                                      # verify pass, to tune
                                                      # RELAX_UNDERSHOOT_UM
    python test_move_microns_hw.py C --verify --undershoot 0.55

Results go to test_move_microns_{axis}.log. Runs append, and loguru rotates
at 10 MB, so older content may sit in a timestamped sibling file.
"""
import argparse
import statistics
import sys
import time

import gclib

from controller.galil_controller import (
    GalilController, BISS_CONFIG,
    MOTOR_COUNTS_PER_UM, ENCODER_COUNTS_PER_UM, AXIS_TRAVEL_MM,
    RELAX_UNDERSHOOT_UM,
)
from utils.logger_setup import setup_logger

CONTROLLER_ADDRESS = "COM4"      # update if your controller enumerates elsewhere

DISTANCES = (1.0, 10.0, 50.0, 100.0)   # um per move
REPEATS   = 3                          # times each distance is repeated
SPEED     = 2000                       # motor counts/s; deliberately slow
SETTLE_S  = 0.3                        # settle before the confirming encoder read
MOVE_TIMEOUT = 30                      # s, passed through to GMotionComplete

# _SSm0 status bits
SS_TIMEOUT     = 1
SS_CRC_INVALID = 2
SS_WARNING     = 4
SS_ERROR       = 8

# One motor microstep = 2 encoder counts. Nothing this loop does can land closer
# than that, so it is the yardstick every error below is judged against.
MICROSTEP_UM = 2.0 / ENCODER_COUNTS_PER_UM      # 0.156 um

logger = None   # set in main() once the axis is known


# ── Output helpers ──────────────────────────────────────────────────────────

def emit(line: str = "", level: str = "info"):
    """Print to console and write to the log, so both tell the same story."""
    print(line)
    getattr(logger, level)(line if line else " ")


def read_int(controller: GalilController, cmd: str) -> int:
    return int(float(controller.send(cmd).strip()))


def describe_ss(axis: str, bits: int) -> str:
    return (f"_SS{axis}0={bits} timeout={bits & 1} crc={(bits >> 1) & 1} "
            f"warn={(bits >> 2) & 1} err={(bits >> 3) & 1}")


def blocking_bits(bits: int) -> int:
    """The part of _SSm0 that makes the position untrustworthy.

    WARN is excluded on purpose: per section 7.4 of the LAK manual it only means
    signal margin below 12.5%, with the frame still passing CRC. That is a
    maintenance signal, not a reason to distrust the reading.
    """
    return bits & ~SS_WARNING


def counts_to_um(counts: float) -> float:
    return counts / ENCODER_COUNTS_PER_UM


# ── Instrumentation ─────────────────────────────────────────────────────────

class StepTap:
    """Records the PR steps move_microns() issues, by wrapping controller.send.

    The function returns a distance, not its step sequence, and on hardware the
    sequence is the interesting part: how many corrections the loop needed, and
    whether any of them reversed.
    """

    def __init__(self, controller: GalilController, axis: str):
        self._controller = controller
        self._prefix = f"PR{axis}="
        self._inner = controller.send
        self.steps: list[int] = []
        controller.send = self._tap

    def _tap(self, cmd: str) -> str:
        if cmd.startswith(self._prefix):
            self.steps.append(int(float(cmd.split("=")[1])))
        return self._inner(cmd)

    def reset(self):
        self.steps = []

    def restore(self):
        self._controller.send = self._inner


# ── Test phases ─────────────────────────────────────────────────────────────

def check_link(controller: GalilController, axis: str) -> bool:
    """Check the BiSS link. False only if the position cannot be trusted."""
    emit()
    emit("=== 1. BiSS link check ===")
    emit(f"Configured with SS{axis}={BISS_CONFIG}")

    bits = read_int(controller, f"MG _SS{axis}0")
    blocking = blocking_bits(bits)

    if bits == 0:
        emit(f"[OK  ] {describe_ss(axis, bits)}")
        return True
    if not blocking:
        emit(f"[WARN] {describe_ss(axis, bits)}", "warning")
        emit("       Signal margin below 12.5% (LAK manual 7.4). CRC is valid, so "
             "the positions below are still good.", "warning")
        return True

    emit(f"[FAIL] {describe_ss(axis, bits)}", "error")
    emit("       The position cannot be trusted. Not moving the motor.", "error")
    return False


def check_baseline(controller: GalilController, axis: str) -> int:
    """Read the encoder at rest; the jitter here bounds what the errors can mean."""
    emit()
    emit("=== 2. Encoder at rest ===")

    samples = []
    for _ in range(5):
        samples.append(read_int(controller, f"MG _TP{axis}"))
        time.sleep(0.05)

    spread = max(samples) - min(samples)
    emit(f"Samples: {samples}")
    emit(f"Spread : {spread} counts ({counts_to_um(spread):.3f} um)")

    if spread > 2:
        emit(f"[WARN] {spread} counts of jitter at rest is comparable to the "
             f"errors being measured; treat small differences with suspicion.",
             "warning")
    else:
        emit(f"[OK  ] Steady at rest ({spread} counts)")

    return samples[-1]


def settle(controller: GalilController, axis: str, tol: float = 1.0,
           window_ms: int = 200, timeout_s: float = 3.0):
    """Sample _TP until it stops changing. Returns (position, ms, samples).

    Same criterion as GalilController._wait_until_still: the spread across a
    whole window must stay inside one count, because reads are only ~16 ms apart
    and judging consecutive ones lets a slow crawl through.
    """
    t0 = time.perf_counter()
    window_s = window_ms / 1000.0
    samples = []
    while True:
        time.sleep(0.005)
        now = time.perf_counter()
        samples.append((now, float(controller.send(f"MG _TP{axis}").strip())))
        recent = [x for x in samples if now - x[0] <= window_s]
        if len(recent) >= 3 and now - samples[0][0] >= window_s:
            if max(v for _, v in recent) - min(v for _, v in recent) <= tol:
                return recent[-1][1], (now - t0) * 1000.0, samples
        if now - t0 > timeout_s:
            return samples[-1][1], (now - t0) * 1000.0, samples


def run_verify_comparison(controller: GalilController, axis: str, tap: StepTap,
                          distances, repeats: int, direction: int, speed: float,
                          undershoot_um: float, settle_s: float) -> list:
    """Same move, with and without the verify pass, measured the same way.

    The point is to tune RELAX_UNDERSHOOT_UM from data. Both variants are
    measured after the SAME dwell, which is the only fair comparison: a plain
    move measured immediately looks better than it is, because the post-move
    relaxation has not arrived yet. Waiting `settle_s` for both removes that.

      plain    move_microns(distance)                  - ends where the loop last
                                                         measured, then creeps on
      verified move_to_counts(target, verify=True)     - aims short by
                                                         undershoot_um, waits, then
                                                         closes what is left

    Blocks of `repeats` each, same direction throughout, so neither variant pays
    a reversal the other does not.
    """
    emit()
    emit("=== Verify comparison ===")
    emit(f"undershoot {undershoot_um:.3f} um, both variants measured "
         f"{settle_s:.1f} s after the move ends")
    emit()
    emit(f"{'step':>7} | {'mode':<9} | {'mean err':>9} | {'spread':>8} | "
         f"{'worst':>8} | {'secs':>6}")
    emit("-" * 62)

    rows = []
    for dist in distances:
        signed = dist * direction
        for mode in ("plain", "verified"):
            errors, times = [], []
            for _ in range(repeats):
                start, _, _ = settle(controller, axis)
                target = start + signed * ENCODER_COUNTS_PER_UM
                t0 = time.perf_counter()
                try:
                    if mode == "plain":
                        controller.move_microns(axis, signed, speed=speed,
                                                timeout=MOVE_TIMEOUT)
                    else:
                        controller.move_to_counts(axis, target, speed=speed,
                                                  timeout=MOVE_TIMEOUT,
                                                  verify=True,
                                                  undershoot_um=undershoot_um)
                except (RuntimeError, gclib.GclibError) as e:
                    emit(f"[FAIL] {mode} {signed:+.1f} um: {e}", "error")
                    return rows
                times.append(time.perf_counter() - t0)

                # Let the relaxation arrive, then measure - same for both.
                time.sleep(settle_s)
                final, _, _ = settle(controller, axis)
                errors.append(counts_to_um(final - target))

            mean = statistics.fmean(errors)
            rows.append({"dist": signed, "mode": mode, "mean": mean,
                         "spread": max(errors) - min(errors),
                         "worst": max(errors, key=abs),
                         "secs": statistics.fmean(times)})
            emit(f"{signed:>+7.1f} | {mode:<9} | {mean:>+9.4f} | "
                 f"{rows[-1]['spread']:>8.4f} | {rows[-1]['worst']:>+8.4f} | "
                 f"{rows[-1]['secs']:>6.2f}")

    # ── What it says about the undershoot ───────────────────────────────────
    emit()
    emit("-- Reading --")
    emit(f"{'step':>7} | {'plain':>10} | {'verified':>10} | {'change':>18}")
    emit("-" * 54)
    improved = 0
    for dist in distances:
        signed = dist * direction
        pl = next((r for r in rows if r["dist"] == signed and r["mode"] == "plain"), None)
        vf = next((r for r in rows if r["dist"] == signed and r["mode"] == "verified"), None)
        if not pl or not vf:
            continue
        delta = abs(pl["mean"]) - abs(vf["mean"])
        better = delta > 1e-9
        improved += better
        verdict = "better" if better else ("same" if abs(delta) <= 1e-9 else "WORSE")
        emit(f"{signed:>+7.1f} | {pl['mean']:>+10.4f} | {vf['mean']:>+10.4f} | "
             f"{verdict:>10} {delta:+.4f}")

    emit()
    emit("Repeatability (spread across the repeats) matters as much as the mean:")
    emit(f"{'step':>7} | {'plain':>10} | {'verified':>10}")
    emit("-" * 32)
    for dist in distances:
        signed = dist * direction
        pl = next((r for r in rows if r["dist"] == signed and r["mode"] == "plain"), None)
        vf = next((r for r in rows if r["dist"] == signed and r["mode"] == "verified"), None)
        if pl and vf:
            emit(f"{signed:>+7.1f} | {pl['spread']:>10.4f} | {vf['spread']:>10.4f}")

    verified = [r for r in rows if r["mode"] == "verified"]
    if verified:
        mean_v = statistics.fmean(r["mean"] for r in verified)
        emit()
        emit(f"Verified moves average {mean_v:+.4f} um from target.")
        # error = final - target, so POSITIVE is past the target and NEGATIVE is
        # short of it. Both branches were the wrong way round until 2026-09-23,
        # and told the user to enlarge an undershoot that was already too big.
        if mean_v > MICROSTEP_UM / 2:
            emit(f"       Landing PAST the target - the undershoot is too small. "
                 f"Try --undershoot {undershoot_um + abs(mean_v):.2f}")
        elif mean_v < -MICROSTEP_UM / 2:
            emit(f"       Landing SHORT - the undershoot is too large. Try "
                 f"--undershoot {max(0.0, undershoot_um - abs(mean_v)):.2f}")
        else:
            emit(f"       Inside half a microstep ({MICROSTEP_UM / 2:.4f} um). "
                 f"RELAX_UNDERSHOOT_UM = {undershoot_um:.2f} is about right for "
                 f"this axis.")
        emit(f"       ({improved} of {len(distances)} distances improved.)")

    return rows


def run_moves(controller: GalilController, axis: str, tap: StepTap,
              distances, repeats: int, direction: int, speed: float) -> list[dict]:
    """The measurement: every move, encoder before and after, error."""
    emit()
    emit("=== 3. Moves ===")
    emit(f"{len(distances)} distances x {repeats} repeats, "
         f"{'reverse' if direction < 0 else 'forward'}, at {speed:.0f} counts/s")
    emit(f"Resolution floor: 1 microstep = 2 encoder counts = {MICROSTEP_UM:.3f} um")
    emit()

    rows = []
    move_no = 0

    for dist in distances:
        signed = dist * direction
        for rep in range(1, repeats + 1):
            move_no += 1
            tap.reset()

            before = read_int(controller, f"MG _TP{axis}")
            t0 = time.perf_counter()
            try:
                reported = controller.move_microns(
                    axis, signed, speed=speed, timeout=MOVE_TIMEOUT)
            except RuntimeError as e:
                emit(f"[FAIL] move {move_no} ({signed:+.0f} um): refused: {e}", "error")
                return rows
            except gclib.GclibError as e:
                emit(f"[FAIL] move {move_no} ({signed:+.0f} um): rejected: {e}", "error")
                return rows
            elapsed = time.perf_counter() - t0

            time.sleep(SETTLE_S)
            after = read_int(controller, f"MG _TP{axis}")
            bits = read_int(controller, f"MG _SS{axis}0")

            moved_counts = after - before
            moved_um = counts_to_um(moved_counts)
            err_um = moved_um - signed
            # Difference between what the function measured at motion-complete and
            # what the encoder reads after settling: post-move creep, if any.
            creep_um = moved_um - reported

            rows.append({
                "n": move_no, "dist": signed, "rep": rep,
                "before": before, "after": after,
                "moved_counts": moved_counts, "moved_um": moved_um,
                "err_um": err_um, "err_counts": moved_counts - signed * ENCODER_COUNTS_PER_UM,
                "reported": reported, "creep_um": creep_um,
                "steps": list(tap.steps), "secs": elapsed, "ss": bits,
            })

            blocking = blocking_bits(bits)
            at_floor = abs(err_um) <= MICROSTEP_UM
            if blocking:
                tag, level = "FAIL", "error"
            elif not at_floor:
                tag, level = "OVER", "warning"
            elif bits & SS_WARNING:
                tag, level = "WARN", "warning"
            else:
                tag, level = "OK  ", "info"

            emit(f"[{tag}] move {move_no:>2} {signed:>+7.1f} um (rep {rep}): "
                 f"_TP {before} -> {after}  moved {moved_um:+8.4f} um  "
                 f"err {err_um:+7.4f} um  steps {tap.steps}  {elapsed:.2f} s",
                 level)

            if abs(creep_um) > MICROSTEP_UM:
                emit(f"       creep after motion complete: {creep_um:+.4f} um "
                     f"(function saw {reported:+.4f}, encoder settled at "
                     f"{moved_um:+.4f})", "warning")
            if blocking:
                emit(f"       {describe_ss(axis, bits)} - stopping.", "error")
                return rows

    return rows


def run_return(controller: GalilController, axis: str, baseline: int,
               total_um: float, speed: float) -> None:
    """Drive the whole excursion back and report where the axis lands."""
    emit()
    emit("=== 4. Return run ===")
    emit(f"Driving {-total_um:+.1f} um back to the starting reading.")
    emit("This is the only direction reversal in the test, so what it measures is")
    emit("hysteresis and lost motion, not the loop's accuracy.")

    before = read_int(controller, f"MG _TP{axis}")
    try:
        reported = controller.move_microns(axis, -total_um, speed=speed,
                                           timeout=MOVE_TIMEOUT)
    except (RuntimeError, gclib.GclibError) as e:
        emit(f"[FAIL] return move refused: {e}", "error")
        return

    time.sleep(SETTLE_S)
    after = read_int(controller, f"MG _TP{axis}")

    moved_um = counts_to_um(after - before)
    return_err_counts = after - baseline
    return_err_um = counts_to_um(return_err_counts)

    emit(f"Return move : commanded {-total_um:+.1f} um, moved {moved_um:+.4f} um "
         f"(function reported {reported:+.4f})")
    emit(f"Started at  : {baseline}")
    emit(f"Ended at    : {after}")
    emit(f"Return error: {return_err_counts:+d} counts ({return_err_um:+.4f} um)")

    if abs(return_err_um) <= MICROSTEP_UM:
        emit("[OK  ] Back within one microstep - no measurable lost motion.")
    else:
        emit(f"[INFO] {abs(return_err_um):.2f} um of lost motion on the reversal. "
             f"Expected on this rig (~3 um on B, 8.9-16.9 um on C); it is why "
             f"move_microns() refuses to reverse mid-move.")


# ── Analysis ────────────────────────────────────────────────────────────────

def print_table(rows: list[dict]):
    emit()
    emit("All moves:")
    emit(f"{'#':>3} | {'cmd um':>8} | {'rep':>3} | {'moved um':>10} | {'err um':>9} | "
         f"{'err cts':>8} | {'steps':>4} | {'secs':>5} | {'_SS':>3}")
    emit("-" * 78)
    for r in rows:
        emit(f"{r['n']:>3} | {r['dist']:>8.1f} | {r['rep']:>3} | "
             f"{r['moved_um']:>+10.4f} | {r['err_um']:>+9.4f} | "
             f"{r['err_counts']:>+8.1f} | {len(r['steps']):>4} | "
             f"{r['secs']:>5.2f} | {r['ss']:>3}")


def summarize(axis: str, rows: list[dict], distances, direction: int):
    emit()
    emit("=== Summary ===")

    if not rows:
        emit("No moves completed, nothing to summarize.", "error")
        return

    print_table(rows)

    # Moves at the start that produced no encoder motion: pure drivetrain
    # take-up. Everything that judges the loop's accuracy skips them, and skips
    # at least the first move, which pays whatever take-up was left over.
    dead = 0
    for r in rows:
        if abs(r["moved_counts"]) > 1:
            break
        dead += 1
    exclude = max(dead, 1)

    # ── Per distance ────────────────────────────────────────────────────────
    emit()
    emit("-- Per distance --")
    emit(f"{'cmd um':>8} | {'n':>2} | {'mean moved':>11} | {'mean err':>9} | "
         f"{'max |err|':>9} | {'spread':>8} | {'steps':>5}")
    emit("-" * 70)

    groups = {}
    for dist in distances:
        signed = dist * direction
        grp = [r for r in rows if r["dist"] == signed]
        if not grp:
            continue
        moved = [r["moved_um"] for r in grp]
        errs = [r["err_um"] for r in grp]
        spread = max(moved) - min(moved)
        groups[dist] = {
            "n": len(grp),
            "mean_moved": sum(moved) / len(moved),
            "mean_err": sum(errs) / len(errs),
            "max_err": max(abs(e) for e in errs),
            "spread": spread,
            "mean_steps": sum(len(r["steps"]) for r in grp) / len(grp),
        }
        g = groups[dist]
        emit(f"{signed:>8.1f} | {g['n']:>2} | {g['mean_moved']:>+11.4f} | "
             f"{g['mean_err']:>+9.4f} | {g['max_err']:>9.4f} | "
             f"{g['spread']:>8.4f} | {g['mean_steps']:>5.1f}")

    emit()
    emit("'spread' is repeatability: the range of the repeats at one distance.")
    emit("It is the number that matters for positioning, and it is unaffected by")
    emit("any scale error.")

    # ── Take-up at the start ────────────────────────────────────────────────
    # Take-up shows as moves that produce NO encoder motion at all, not as a
    # large error, so count the dead ones before comparing magnitudes.
    emit()
    emit("-- Take-up at the start --")
    if dead:
        commanded = sum(abs(r["dist"]) for r in rows[:dead])
        motor = sum(abs(c) for r in rows[:dead] for c in r["steps"])
        emit(f"[INFO] The first {dead} move(s) produced no encoder motion at all: "
             f"{commanded:.1f} um ({motor} motor counts) went into taking up the "
             f"drivetrain before the stage moved.")
        emit("       That is lost motion being paid once, in whichever direction "
             "the axis was last driven. It is not a fault of the loop - but it "
             "does set the smallest move this axis can honour from rest.")
    else:
        emit("[OK  ] The first move produced encoder motion - the axis was already "
             "loaded in this direction.")

    live = rows[dead:]
    if live:
        first = live[0]
        rest = [abs(r["err_um"]) for r in live[1:]]
        mean_rest = sum(rest) / len(rest) if rest else 0.0
        emit(f"First move that moved: error {first['err_um']:+.4f} um; "
             f"mean |error| of the ones after it {mean_rest:.4f} um")

    # ── Drivetrain scale ────────────────────────────────────────────────────
    # The micron columns above CANNOT check encoder scale. The loop targets
    # _TP + microns x 12.8 and this script measures delta_TP / 12.8, so the same
    # constant appears on both sides and cancels: if 12.8 were wrong, every error
    # above would still read zero while the stage moved the wrong distance.
    # Validating 12.8 itself needs an external length reference.
    #
    # What IS independent here is the ratio of what the encoder gave to what the
    # motor was told, since the two come from different places - _TP from the
    # BiSS frame, the PR counts from counts_per_um. Expect exactly
    # ENCODER_COUNTS_PER_UM / counts_per_um.
    emit()
    emit("-- Drivetrain scale (independent check) --")
    emit("The micron errors above cannot test encoder scale: target and")
    emit("measurement both use ENCODER_COUNTS_PER_UM, so it cancels out. This")
    emit("ratio can, because _TP and the PR counts come from different constants.")

    expected_ratio = ENCODER_COUNTS_PER_UM / MOTOR_COUNTS_PER_UM[axis]
    scored = [r for r in rows[exclude:] if abs(r["dist"]) >= 10.0 and sum(r["steps"])]
    if len(scored) >= 2:
        enc_delta = sum(r["moved_counts"] for r in scored)
        motor_counts = sum(sum(r["steps"]) for r in scored)
        ratio = enc_delta / motor_counts
        emit(f"Over {len(scored)} moves of 10 um and above (excluding the first "
             f"{exclude}): "
             f"{enc_delta:+d} encoder counts from {motor_counts:+d} motor counts")
        emit(f"Ratio: {ratio:.4f} encoder counts per motor count "
             f"(expected {expected_ratio:.4f} = {ENCODER_COUNTS_PER_UM} / "
             f"{MOTOR_COUNTS_PER_UM[axis]})")

        if abs(ratio - expected_ratio) <= 0.02 * expected_ratio:
            emit("[OK  ] Motor and encoder scales agree - counts_per_um and the "
                 "BiSS frame are consistent with each other.")
        elif abs(ratio - expected_ratio / 2) <= 0.02 * expected_ratio:
            emit("[FAIL] Half the expected ratio. The encoder is reporting half as "
                 "many counts as it should: suspect n3 in BISS_CONFIG shifting a "
                 "real bit off the LSB end.", "error")
        elif abs(ratio - 2 * expected_ratio) <= 0.04 * expected_ratio:
            emit("[FAIL] Twice the expected ratio - either the BiSS frame is "
                 "doubled or this axis's counts_per_um is half what it should be.",
                 "error")
        else:
            emit(f"[WARN] {100 * (ratio / expected_ratio - 1):+.1f}% off the "
                 f"expected ratio. Not a clean power of two, so more likely "
                 f"mechanical: lost steps or a slipping coupling.", "warning")
    else:
        emit("Not enough moves of 10 um or more to measure the ratio.")

    # ── Verdict ─────────────────────────────────────────────────────────────
    emit()
    worst = max(rows, key=lambda r: abs(r["err_um"]))
    judged = rows[exclude:] or rows
    worst_rest = max(judged, key=lambda r: abs(r["err_um"]))
    emit(f"Worst error overall      : {worst['err_um']:+.4f} um on move {worst['n']} "
         f"({worst['dist']:+.0f} um)")
    emit(f"Worst after take-up      : {worst_rest['err_um']:+.4f} um on move "
         f"{worst_rest['n']} ({worst_rest['dist']:+.0f} um)")
    emit(f"Resolution floor         : {MICROSTEP_UM:.4f} um (one microstep)")

    emit()
    if abs(worst_rest["err_um"]) <= MICROSTEP_UM:
        emit("VERDICT: PASS - every move after the take-up landed within one "
             "microstep of its target.")
        emit("         That is the best the drivetrain can do; the loop is closing "
             "correctly.")
    elif abs(worst_rest["err_um"]) <= 2 * MICROSTEP_UM:
        emit("VERDICT: PASS (marginal) - errors are within two microsteps.")
        emit("         Check the at-rest jitter above before reading anything into "
             "the residual.")
    else:
        emit(f"VERDICT: OUT OF TOLERANCE - {abs(worst_rest['err_um']):.3f} um is "
             f"{abs(worst_rest['err_um']) / MICROSTEP_UM:.1f} microsteps.", "warning")
        emit("         Read the per-distance table: an error that grows with "
             "distance is scale, one that is constant is take-up or settling, one "
             "that is random is the stage.", "warning")


# ── Entry point ─────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("axis", nargs="?", default="A",
                   choices=["A", "B", "C", "a", "b", "c"],
                   help="axis to test (default: A)")
    p.add_argument("--distances", default=",".join(str(d) for d in DISTANCES),
                   help=f"comma-separated distances in um (default: "
                        f"{','.join(str(d) for d in DISTANCES)})")
    p.add_argument("--repeats", type=int, default=REPEATS,
                   help=f"repeats per distance (default: {REPEATS})")
    p.add_argument("--reverse", action="store_true",
                   help="run the whole test in the negative direction")
    p.add_argument("--no-return", action="store_true",
                   help="skip the return run (leaves the axis at the far end)")
    p.add_argument("--verify", action="store_true",
                   help="compare each move with and without the verify pass, to "
                        "tune RELAX_UNDERSHOOT_UM")
    p.add_argument("--undershoot", type=float, default=RELAX_UNDERSHOOT_UM,
                   help=f"microns to aim short when verifying "
                        f"(default {RELAX_UNDERSHOOT_UM})")
    p.add_argument("--verify-settle", type=float, default=2.0,
                   help="seconds to wait before the confirming measurement, for "
                        "BOTH variants (default 2.0)")
    p.add_argument("--speed", type=float, default=SPEED,
                   help=f"motor counts/s (default: {SPEED})")
    p.add_argument("--address", default=CONTROLLER_ADDRESS,
                   help=f"controller address (default: {CONTROLLER_ADDRESS})")
    return p.parse_args()


def main():
    global logger
    args = parse_args()
    axis = args.axis.upper()

    logger = setup_logger(f"test_move_microns_{axis.lower()}.log")

    try:
        distances = [float(d) for d in args.distances.split(",") if d.strip()]
    except ValueError:
        print(f"Bad --distances value: {args.distances!r}")
        sys.exit(1)
    if not distances or args.repeats < 1:
        print("Nothing to test.")
        sys.exit(1)

    direction = -1 if args.reverse else +1
    total_um = sum(distances) * args.repeats
    travel_um = AXIS_TRAVEL_MM[axis] * 1000.0

    emit(f"=== move_microns() hardware test, axis {axis} ===")
    emit(f"Motor scale   : {MOTOR_COUNTS_PER_UM[axis]} counts/um")
    emit(f"Encoder scale : {ENCODER_COUNTS_PER_UM} counts/um "
         f"({ENCODER_COUNTS_PER_UM / MOTOR_COUNTS_PER_UM[axis]:.1f} encoder counts "
         f"per microstep)")
    emit(f"Travel limit  : {AXIS_TRAVEL_MM[axis]} mm")
    emit(f"Plan          : " + ", ".join(f"{d:g} um x{args.repeats}" for d in distances))
    emit(f"Total travel  : {total_um:.1f} um "
         f"{'reverse' if direction < 0 else 'forward'}"
         + ("" if args.no_return else ", then the same back"))

    # LD=3 disables the hardware limit switches, so this check is the only thing
    # standing between the test and an end stop.
    if total_um >= travel_um:
        emit(f"[FAIL] {total_um:.0f} um of travel exceeds the {travel_um:.0f} um "
             f"allowed on axis {axis}. Refusing to run.", "error")
        sys.exit(1)
    if total_um > travel_um * 0.25:
        emit(f"[WARN] Test uses {100 * total_um / travel_um:.0f}% of permitted "
             f"travel. Make sure the stage has room in this direction.", "warning")

    controller = GalilController()
    tap = None
    try:
        emit(f"Connecting to {args.address}...")
        info = controller.connect(args.address)
        emit(f"Connected: {info.model} Rev {info.revision}, "
             f"Serial: {info.serial}, Firmware: {info.firmware}")

        emit(f"Enabling axis {axis} with BiSS C feedback...")
        controller.enable_axes(axis)

        if not check_link(controller, axis):
            sys.exit(1)

        baseline = check_baseline(controller, axis)

        tap = StepTap(controller, axis)

        if args.verify:
            run_verify_comparison(controller, axis, tap, distances, args.repeats,
                                  direction, args.speed, args.undershoot,
                                  args.verify_settle)
            emit()
            emit("--verify: comparison only, the standard run was not performed.")
            return

        rows = run_moves(controller, axis, tap, distances, args.repeats,
                         direction, args.speed)

        if rows and not args.no_return:
            travelled = counts_to_um(rows[-1]["after"] - baseline)
            run_return(controller, axis, baseline, travelled, args.speed)

        summarize(axis, rows, distances, direction)

    except KeyboardInterrupt:
        emit("Interrupted - halting axis.", "warning")
        controller.send_safe(f"ST{axis}")
    except Exception as e:
        emit(f"Fatal error: {e}", "error")
        sys.exit(1)
    finally:
        try:
            if tap is not None:
                tap.restore()
            controller.send_safe(f"ST{axis}")
            controller.send_safe(f"MO{axis}")
            controller.disconnect()
            emit()
            emit("Disconnected.")
        except Exception as e:
            logger.warning(f"Error during cleanup: {e}")


if __name__ == "__main__":
    main()
