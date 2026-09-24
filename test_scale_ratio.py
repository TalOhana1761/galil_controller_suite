"""
Characterise the encoder-to-motor scale ratio on one axis.

Axis C reports 2.0022x the commanded motion over a 0.4 mm test. That distance is
too short to trust: ~15 um of reversal backlash is 3.7% of it. This test measures
the same ratio over several millimetres, where backlash is a rounding error, and
asks three questions the short test could not:

  1. Is the ratio CONSTANT with distance?
     A fixed ratio (microstepping, gearing, frame width) stays put. Mechanical
     faults - slip, binding, lost steps - grow with distance.
  2. Is the ratio CONSTANT with speed?
     A configuration ratio is speed-independent. A drive losing or gaining steps
     is not.
  3. Is the encoder's LSB alive?
     A one-bit frame shift doubles the reading and pins the LSB to 0. Counting
     odd readings over a long run tests that far better than eyeballing nine
     samples.

WHAT THIS TEST CANNOT DO: it cannot tell you whether the motor or the encoder is
the one that is wrong. Both are internally consistent - _RP tracks the commanded
counts exactly, and the encoder passes CRC on every read. Breaking that tie needs
a measurement from outside both: a dial indicator on the stage, or moving the
stage by hand with the motor off. This test tells you what KIND of fault it is,
which narrows where to look.

SAFETY: if the 2x is real, the stage moves TWICE the commanded distance. Every
distance below is COMMANDED; the script computes the worst-case real excursion
and refuses to run if it exceeds --headroom. LD=3 disables the hardware limit
switches, so --headroom is the only protection. Measure it before trusting it.

Run directly against real hardware (not part of the pytest suite):
    python test_scale_ratio.py                       # axis C, defaults
    python test_scale_ratio.py C --headroom 40
    python test_scale_ratio.py A --headroom 10       # a known-good axis, for comparison
    python test_scale_ratio.py C --reverse-first

Results are written to test_scale_ratio_{axis}.log.
"""
import argparse
import json
import os
import sys
import time

import gclib

from controller.data_models import AppSettings
from controller.galil_controller import GalilController, BISS_CONFIG
from test_axis_encoder import (ENCODER_COUNTS_PER_UM, SS_WARNING,
                               blocking_bits, describe_ss)
from utils.logger_setup import setup_logger

CONTROLLER_ADDRESS = "COM4"

# All distances are COMMANDED mm. Real travel may be 2x these.
TAKEUP_MM      = 0.3             # discarded first move, absorbs backlash
SEGMENTS_MM    = [0.5, 1.0, 2.0, 4.0]   # ratio-vs-distance, one direction
SPEED_TRIAL_MM = 1.0             # ratio-vs-speed, out and back at each speed
SPEEDS         = [500, 2000, 8000]      # motor counts/s

SETTLE_S     = 0.4
MOVE_TIMEOUT = 120               # long moves at 500 counts/s are slow
SUSPECTED    = 2.0               # the ratio we are trying to confirm

CONSTANCY_PCT = 1.0   # segments must agree within this to count as "constant"
EXACTNESS_PCT = 0.5   # mean must be within this of a whole-number ratio

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "config", "settings.json")

logger = None
lsb_samples = []   # every _TP reading taken, for the parity check


def emit(line: str = "", level: str = "info"):
    print(line)
    getattr(logger, level)(line if line else " ")


def read_int(controller: GalilController, cmd: str) -> int:
    return int(float(controller.send(cmd).strip()))


def read_encoder(controller: GalilController, axis: str) -> int:
    """Read _TP and record it for the LSB parity check."""
    value = read_int(controller, f"MG _TP{axis}")
    lsb_samples.append(value)
    return value


# ── Measurement ─────────────────────────────────────────────────────────────

def move_and_measure(controller: GalilController, axis: str, mm: float,
                     speed: int, motor_cpu: float, direction: int) -> dict | None:
    """One move. Returns the measured ratio, or None if the move was refused."""
    motor_counts = int(round(mm * 1000.0 * motor_cpu * direction))
    expected_enc = mm * 1000.0 * ENCODER_COUNTS_PER_UM * direction

    before = read_encoder(controller, axis)
    try:
        controller.move_relative(axis, motor_counts, speed=speed,
                                 wait=True, timeout=MOVE_TIMEOUT)
    except RuntimeError as e:
        emit(f"[FAIL] move refused: {e}", "error")
        return None
    except gclib.GclibError as e:
        emit(f"[FAIL] move rejected: {e}", "error")
        return None

    time.sleep(SETTLE_S)
    after = read_encoder(controller, axis)
    bits = read_int(controller, f"MG _SS{axis}0")
    delta = after - before

    return {
        "mm": mm, "speed": speed, "motor_counts": motor_counts,
        "before": before, "after": after, "delta": delta,
        "expected": expected_enc,
        "ratio": delta / expected_enc if expected_enc else float("nan"),
        "ss": bits,
    }


def report_move(label: str, r: dict):
    ok = not blocking_bits(r["ss"])
    emit(f"[{'OK  ' if ok else 'FAIL'}] {label:<22} "
         f"cmd {r['motor_counts']:+8d} counts  "
         f"enc {r['delta']:+9d}  expect {r['expected']:+9.0f}  "
         f"ratio {r['ratio']:.4f}"
         + ("" if ok else f"  {describe_ss('', r['ss'])}"),
         "info" if ok else "error")


# ── Phases ──────────────────────────────────────────────────────────────────

def phase_distance(controller, axis, motor_cpu, direction) -> list[dict]:
    emit()
    emit("=== 2. Ratio vs distance ===")
    emit("Segments run in one direction, so only the discarded take-up move")
    emit("crosses a reversal. A constant ratio here means a fixed scale error;")
    emit("a ratio that grows with distance means something mechanical.")
    emit()

    emit(f"Taking up backlash ({TAKEUP_MM} mm, discarded)...")
    if move_and_measure(controller, axis, TAKEUP_MM, SPEEDS[1],
                        motor_cpu, direction) is None:
        return []

    rows = []
    for mm in SEGMENTS_MM:
        r = move_and_measure(controller, axis, mm, SPEEDS[1], motor_cpu, direction)
        if r is None:
            break
        report_move(f"{mm} mm", r)
        rows.append(r)
        if blocking_bits(r["ss"]):
            emit("       BiSS status went bad. Stopping.", "error")
            break
    return rows


def phase_speed(controller, axis, motor_cpu, direction) -> list[dict]:
    emit()
    emit("=== 4. Ratio vs speed ===")
    emit("Same distance at each speed, out and back so the axis stays put.")
    emit("A configuration ratio does not care about speed; a drive dropping or")
    emit("doubling pulses does.")
    emit()

    rows = []
    for speed in SPEEDS:
        out = move_and_measure(controller, axis, SPEED_TRIAL_MM, speed,
                               motor_cpu, direction)
        if out is None:
            break
        report_move(f"{SPEED_TRIAL_MM} mm @ {speed}", out)
        rows.append(out)
        back = move_and_measure(controller, axis, SPEED_TRIAL_MM, speed,
                                motor_cpu, -direction)
        if back is None:
            break
    return rows


# ── Analysis ────────────────────────────────────────────────────────────────

def summarize(axis, dist_rows, ret, speed_rows, motor_cpu):
    emit()
    emit("=== 5. Summary ===")

    if not dist_rows:
        emit("No usable measurements.", "error")
        return

    # ── ratio vs distance ───────────────────────────────────────────────────
    emit()
    emit("-- Ratio vs distance --")
    emit(f"{'cmd mm':>8} | {'motor counts':>13} | {'enc counts':>11} | "
         f"{'expected':>10} | {'ratio':>8}")
    emit("-" * 62)
    for r in dist_rows:
        emit(f"{r['mm']:>8.2f} | {r['motor_counts']:>+13d} | {r['delta']:>+11d} | "
             f"{r['expected']:>+10.0f} | {r['ratio']:>8.4f}")

    ratios = [r["ratio"] for r in dist_rows]
    mean = sum(ratios) / len(ratios)
    spread = max(ratios) - min(ratios)
    spread_pct = 100.0 * spread / abs(mean) if mean else float("inf")

    # The long segments dominate: a cumulative ratio over the whole run is the
    # single most precise number this test produces.
    total_enc = dist_rows[-1]["after"] - dist_rows[0]["before"]
    total_exp = sum(r["expected"] for r in dist_rows)
    cumulative = total_enc / total_exp if total_exp else float("nan")

    emit()
    emit(f"Mean of segments : {mean:.4f}   (spread {spread:.4f}, {spread_pct:.2f}%)")
    emit(f"Cumulative       : {cumulative:.4f}   "
         f"over {sum(r['mm'] for r in dist_rows):.1f} mm commanded")
    if ret:
        emit(f"Return direction : {ret['ratio']:.4f}")

    constant_distance = spread_pct <= CONSTANCY_PCT

    # ── ratio vs speed ──────────────────────────────────────────────────────
    constant_speed = None
    if speed_rows:
        emit()
        emit("-- Ratio vs speed --")
        for r in speed_rows:
            emit(f"{r['speed']:>8} counts/s | ratio {r['ratio']:.4f}")
        s_ratios = [r["ratio"] for r in speed_rows]
        s_spread = max(s_ratios) - min(s_ratios)
        s_pct = 100.0 * s_spread / abs(sum(s_ratios) / len(s_ratios))
        emit(f"Spread across speeds: {s_spread:.4f} ({s_pct:.2f}%)")
        constant_speed = s_pct <= CONSTANCY_PCT

    # ── LSB parity ──────────────────────────────────────────────────────────
    emit()
    emit("-- Encoder LSB --")
    odd = sum(1 for v in lsb_samples if v % 2)
    emit(f"{odd} of {len(lsb_samples)} _TP{axis} readings were odd")
    lsb_alive = odd > 0
    if lsb_alive:
        emit("[OK  ] LSB is alive, so the reading is not shifted left by a bit.")
        emit("       The encoder is reporting a genuine position, not a doubled one.")
    else:
        emit("[WARN] Every reading was even. That is what a one-bit frame shift "
             "looks like - the encoder value would be doubled.", "warning")
        emit("       Sweep n3 (zero padding, 0-7) in BISS_CONFIG on this axis.",
             "warning")

    # ── verdict ─────────────────────────────────────────────────────────────
    emit()
    emit("-- Verdict --")
    off_pct = 100.0 * abs(abs(cumulative) - SUSPECTED) / SUSPECTED
    near_suspected = off_pct <= EXACTNESS_PCT
    near_unity = abs(abs(cumulative) - 1.0) <= EXACTNESS_PCT / 100.0

    if near_unity:
        emit(f"Ratio is {cumulative:.4f} - this axis tracks correctly. No 2x error "
             f"over {sum(r['mm'] for r in dist_rows):.1f} mm.")
        return

    emit(f"Ratio is {cumulative:.4f}, which is {off_pct:.2f}% from exactly "
         f"{SUSPECTED:.1f}.")
    emit(f"Constant with distance: {'YES' if constant_distance else 'NO'}"
         f"  ({spread_pct:.2f}% spread across segments)")
    if constant_speed is not None:
        emit(f"Constant with speed   : {'YES' if constant_speed else 'NO'}")

    emit()
    if near_suspected and constant_distance and constant_speed is not False:
        emit(f"CONFIRMED: a clean, fixed {SUSPECTED:.1f}x. It does not vary with")
        emit("distance or speed, so this is a CONFIGURATION fault, not mechanics.")
        emit("Slip, binding and lost steps all vary with one or the other.")
        emit()
        emit("Candidates, in the order worth checking:")
        emit(f"  1. Stepper drive microstepping on {axis}. counts_per_um must equal")
        emit("     (steps_per_rev x microsteps) / leadscrew_pitch_um. Half the")
        emit("     microstepping doubles the distance per pulse.")
        emit("  2. Drive clocking on both step-pulse edges (doubles the pulses).")
        emit("  3. Gearing or leadscrew pitch differing from what counts_per_um")
        emit(f"     ({motor_cpu} counts/um) assumes.")
        emit("  4. If the LSB check above failed, the BiSS frame instead.")
        emit()
        emit("This test CANNOT say whether the motor or the encoder is the wrong")
        emit("one. Settle that with a dial indicator on the stage, or by moving")
        emit("the stage by hand with the motor off and reading the _TP delta.")
    elif not constant_distance:
        emit("NOT a clean scale error: the ratio changes with distance.")
        emit("That points at mechanics - slip, binding, or lost steps - rather")
        emit("than a configuration number. Check the coupling and the drivetrain.")
    elif constant_speed is False:
        emit("NOT a clean scale error: the ratio changes with speed.")
        emit("That points at the drive dropping or doubling pulses under load.")
        emit("Check drive current, acceleration limits, and the step pulse width.")
    else:
        emit(f"Ratio is fixed but is not {SUSPECTED:.1f}x. Whatever the cause, it")
        emit("is a constant scale factor - compare counts_per_um against the")
        emit("actual leadscrew pitch and drive microstepping.")


# ── Entry point ─────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Characterise the encoder-to-motor scale ratio on one axis.")
    p.add_argument("axis", nargs="?", default="C",
                   choices=["A", "B", "C", "a", "b", "c"], help="axis (default: C)")
    p.add_argument("--headroom", type=float, default=20.0,
                   help="REAL mm the stage can safely move in the test direction "
                        "(default: 20). The script assumes the worst case, that "
                        "every move travels 2x its command.")
    p.add_argument("--reverse-first", action="store_true",
                   help="run negative first, for an axis parked near its far end")
    p.add_argument("--address", default=CONTROLLER_ADDRESS)
    return p.parse_args()


def main():
    global logger
    args = parse_args()
    axis = args.axis.upper()
    logger = setup_logger(f"test_scale_ratio_{axis.lower()}.log")

    with open(SETTINGS_PATH, "r") as f:
        settings = AppSettings.model_validate(json.load(f))
    motor_cpu = settings.axis_settings(axis).counts_per_um
    direction = -1 if args.reverse_first else +1

    commanded_peak = TAKEUP_MM + sum(SEGMENTS_MM)
    worst_case = commanded_peak * SUSPECTED

    emit(f"=== Axis {axis} scale ratio test ===")
    emit(f"Motor scale   : {motor_cpu} counts/um   "
         f"Encoder scale : {ENCODER_COUNTS_PER_UM} counts/um")
    emit(f"Segments      : {SEGMENTS_MM} mm commanded, "
         f"after a {TAKEUP_MM} mm take-up")
    emit(f"Speeds        : {SPEEDS} counts/s at {SPEED_TRIAL_MM} mm")
    emit(f"Direction     : {'reverse' if direction < 0 else 'forward'} first")
    emit()
    emit(f"Commanded peak excursion : {commanded_peak:.1f} mm")
    emit(f"Worst case if {SUSPECTED:.1f}x is real: {worst_case:.1f} mm of REAL travel")
    emit(f"Headroom you declared    : {args.headroom:.1f} mm")

    if worst_case > args.headroom:
        emit()
        emit(f"[FAIL] Worst case {worst_case:.1f} mm exceeds the {args.headroom:.1f} mm "
             f"headroom. Refusing to run.", "error")
        emit("       Raise --headroom only if you have measured that clearance. "
             "LD=3 means nothing in the controller will stop an overrun.", "error")
        sys.exit(1)
    emit(f"[OK  ] Worst case fits within headroom.")

    controller = GalilController()
    try:
        emit()
        emit(f"Connecting to {args.address}...")
        info = controller.connect(args.address)
        emit(f"Connected: {info.model} Rev {info.revision}, Serial: {info.serial}")
        controller.enable_axes(axis)

        emit()
        emit("=== 1. BiSS link check ===")
        bits = read_int(controller, f"MG _SS{axis}0")
        if blocking_bits(bits):
            emit(f"[FAIL] {describe_ss(axis, bits)}", "error")
            emit("       Position cannot be trusted. Not moving.", "error")
            sys.exit(1)
        emit(f"[{'WARN' if bits else 'OK  '}] {describe_ss(axis, bits)}",
             "warning" if bits else "info")

        dist_rows = phase_distance(controller, axis, motor_cpu, direction)

        ret = None
        if dist_rows:
            emit()
            emit("=== 3. Return ===")
            travelled = TAKEUP_MM + sum(r["mm"] for r in dist_rows)
            ret = move_and_measure(controller, axis, travelled, SPEEDS[1],
                                   motor_cpu, -direction)
            if ret:
                report_move(f"return {travelled} mm", ret)

        speed_rows = phase_speed(controller, axis, motor_cpu, direction) \
            if dist_rows else []

        summarize(axis, dist_rows, ret, speed_rows, motor_cpu)

    except Exception as e:
        emit(f"Fatal error: {e}", "error")
        sys.exit(1)

    finally:
        try:
            controller.send_safe(f"ST{axis}")
            controller.send_safe(f"MO{axis}")
            controller.disconnect()
            emit()
            emit("Disconnected.")
        except Exception as e:
            logger.warning(f"Error during cleanup: {e}")


if __name__ == "__main__":
    main()
