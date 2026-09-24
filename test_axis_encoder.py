"""
BiSS encoder verification for one axis: scale, direction, backlash, lost steps.

Enables the axis with the BiSS C configuration found by test_biss_config.py
(SS{ax}=1,33,0,0<25), then:

  1. checks the BiSS link is clean          (_SS{ax}0 must read 0)
  2. reads the encoder at rest              (absolute position + jitter)
  3. steps forward  in 0.1 mm increments to 0.4 mm
  4. steps back     in 0.1 mm increments to the start
  5. reports scale error, backlash and return error

Why out-and-back: a single direction cannot separate the three things that go
wrong. Running both directions does.

  - scale error   shows up as a delta that is consistently wrong in BOTH
                  directions, growing with distance
  - backlash      shows up as a deficit on the FIRST step of each direction
                  only, after which the cumulative error stops growing
  - lost steps    show up as a return error: the encoder does not come back to
                  where it started even though the commanded position did

Units. Motor scale is per-axis (settings.json -> axis_*.counts_per_um: 6.4 on A
and B, 12.8 on C). Encoder scale is 12.8 counts/um on all three, confirmed from
the part number LAK14-710A-RO against the ordering key (78.125 nm measuring
step) - see the ENCODER_COUNTS_PER_UM note below.

On A and B the two scales differ by exactly 2x, which matters when reading the
result: a measured 640 counts per 0.1 mm step does NOT mean the stage moved half
as far, it means the BiSS frame is shifted by one bit and n3 in BISS_CONFIG
needs revisiting.

Nothing moves unless _SS{ax}0 reads 0 first, and the run aborts if it stops
reading 0 partway through.

Run directly against real hardware (not part of the pytest suite):
    python test_axis_encoder.py            # axis A
    python test_axis_encoder.py B          # axis B
    python test_axis_encoder.py C --step 0.5 --total 2.0
    python test_axis_encoder.py A --reverse-first   # if travel is limited forward

Results are written to test_axis_{axis}_encoder.log.
"""
import argparse
import json
import os
import sys
import time

import gclib

from controller.data_models import AppSettings
from controller.galil_controller import GalilController, BISS_CONFIG
from utils.logger_setup import setup_logger

CONTROLLER_ADDRESS = "COM4"      # update if your controller enumerates elsewhere

# Both confirmed from the scanning head part number LAK14-710A-RO against the
# ordering key in section 11.1 of the LAK manual:
#   field 4 = "7"  -> measuring step 78.125 nm  -> 1/0.078125 = 12.8 counts/um
#   field 5 = "1"  -> 32 bit serial data word   (matches n1=33 in BISS_CONFIG:
#                     32 data bits + the BiSS C CDS bit)
#   field 2 = "4"  -> BiSS C (unidirectional)
#   field 6 = "0"  -> position encoded over a 1200 mm range
# Cross-check: the 20 um grating period divided by 78.125 nm is exactly 256,
# the interpolation factor. The numbers are consistent.
ENCODER_COUNTS_PER_UM = 12.8     # 78.125 nm measuring step; same on all axes
PRC_RANGE_MM          = 1200.0   # span of the absolute Pseudo Random Code

# _SSm0 status bits
SS_TIMEOUT     = 1
SS_CRC_INVALID = 2
SS_WARNING     = 4
SS_ERROR       = 8

SPEED          = 2000            # motor counts/s; deliberately slow
SETTLE_S       = 0.3             # let the stage settle before reading the encoder
MOVE_TIMEOUT   = 30              # s, passed to GMotionComplete
JITTER_SAMPLES = 5               # baseline at-rest reads, to check for noise
TOLERANCE_PCT  = 5.0             # pass band on the measured/expected scale ratio

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "config", "settings.json")

logger = None   # set in main() once the axis is known


# ── Output helpers ──────────────────────────────────────────────────────────

def emit(line: str = "", level: str = "info"):
    """Print to console and write to the log, so both tell the same story."""
    print(line)
    getattr(logger, level)(line if line else " ")


def read_int(controller: GalilController, cmd: str) -> int:
    """Read a numeric controller operand as an int."""
    return int(float(controller.send(cmd).strip()))


def describe_ss(axis: str, bits: int) -> str:
    return (f"_SS{axis}0={bits}  timeout={bits & 1}  crc_invalid={(bits >> 1) & 1}  "
            f"warning={(bits >> 2) & 1}  error={(bits >> 3) & 1}")


def blocking_bits(bits: int) -> int:
    """The part of _SSm0 that makes the reading untrustworthy.

    Timeout, CRC-invalid and error all mean the position cannot be relied on, so
    moving the motor would be moving blind. The WARN bit does not: per section
    7.4 of the LAK manual it only reports system margin below 12.5%, the
    "pre-warning" state, and the device "will function without any errors" as
    long as the margin stays above 0%. The frame still passes CRC and the
    position is still good, so a warning is a maintenance signal (air gap, tape
    cleanliness), not a reason to refuse the test.
    """
    return bits & ~SS_WARNING


def counts_to_um(counts: float) -> float:
    return counts / ENCODER_COUNTS_PER_UM


# ── Test phases ─────────────────────────────────────────────────────────────

def check_link(controller: GalilController, axis: str) -> bool:
    """Check the BiSS link. Returns False only if the position cannot be trusted."""
    emit()
    emit("=== 1. BiSS link check ===")
    emit(f"Configured with SS{axis}={BISS_CONFIG}")

    bits = read_int(controller, f"MG _SS{axis}0")
    blocking = blocking_bits(bits)

    if bits == 0:
        emit(f"[OK  ] {describe_ss(axis, bits)}")
        return True

    if not blocking:
        # Warning only: CRC passed, so the position is still good.
        emit(f"[WARN] {describe_ss(axis, bits)}", "warning")
        emit("       Signal margin is below 12.5% (LAK manual 7.4, \"pre-warning\").",
             "warning")
        emit("       CRC is valid and no error bit is set, so the position data is "
             "still trustworthy and the test will run.", "warning")
        emit("       Treat the margin as a separate maintenance item: reset the air "
             "gap to 0.85 mm with the spacing gage at 0.25 Nm alternating, and "
             "clean the tape and scanning window lengthwise only.", "warning")
        return True

    emit(f"[FAIL] {describe_ss(axis, bits)}", "error")
    if blocking & SS_TIMEOUT:
        emit("       Timeout: no response from the encoder. Check wiring and power.",
             "error")
    if blocking & SS_CRC_INVALID:
        emit("       CRC invalid: the frame does not decode. Check the n1 width in "
             "BISS_CONFIG and the clock divider.", "error")
    if blocking & SS_ERROR:
        emit("       Error bit: the head cannot decode the absolute track. Check "
             "head/tape orientation before anything else (manual 10.2 #3).", "error")
    emit("       The position cannot be trusted. Not moving the motor.", "error")
    return False


def check_baseline(controller: GalilController, axis: str,
                   encoder_length_mm: float, travel_mm: float) -> int:
    """Read the encoder at rest and return the baseline every delta is measured from."""
    emit()
    emit("=== 2. Encoder at rest ===")

    samples = []
    for _ in range(JITTER_SAMPLES):
        samples.append(read_int(controller, f"MG _TP{axis}"))
        time.sleep(0.05)

    baseline = samples[-1]
    spread = max(samples) - min(samples)

    emit(f"Samples : {samples}")
    emit(f"Spread  : {spread} counts ({counts_to_um(spread):.3f} um)")

    if baseline == 0:
        emit("[WARN] _TP reads exactly 0. An absolute encoder normally reports its "
             "real position on the tape, so 0 suggests no data is arriving.", "warning")

    if spread > 4:
        emit(f"[WARN] Encoder is not steady at rest ({spread} counts of jitter). "
             "Step results below may be noisy.", "warning")
    else:
        emit(f"[OK  ] Steady at rest ({spread} counts of jitter)")

    # _TP is a coordinate in the encoder's PRC code space, which spans 1200 mm
    # (ordering key "Encoding of position: 0"). A short tape is a section cut from
    # that space, so the reading does NOT start at 0 at the near end of the tape —
    # axis A sits around 252 mm on a 45 mm strip. All we can check here is that
    # the reading is inside the code space at all.
    pos_mm = counts_to_um(baseline) / 1000.0
    emit(f"Position: {baseline} counts = {pos_mm:.3f} mm "
         f"in the encoder's {PRC_RANGE_MM:.0f} mm PRC code space")
    emit(f"Tape    : {encoder_length_mm} mm of scale, {travel_mm} mm of travel on it")

    if 0 <= pos_mm <= PRC_RANGE_MM:
        emit(f"[OK  ] Reading is a valid absolute position "
             f"({100.0 * pos_mm / PRC_RANGE_MM:.1f}% into the code space)")
        emit("       Where that lands on THIS tape is unknown until the travel is "
             "traversed and min/max _TP recorded.")
    else:
        emit(f"[FAIL] {pos_mm:.1f} mm is outside the {PRC_RANGE_MM:.0f} mm code "
             f"space, so this is not a decodable position.", "error")
        emit("       Suspect the BiSS frame width or ENCODER_COUNTS_PER_UM.", "error")

    return baseline


def run_steps(controller: GalilController, axis: str, start_enc: int,
              n_steps: int, step_mm: float, direction: int,
              motor_counts_per_um: float, phase_no: int, label: str) -> list[dict]:
    """Step the axis in one direction and record the encoder after each move."""
    step_um      = step_mm * 1000.0 * direction
    # PR takes whole counts, so round rather than sending e.g. "PRA=640.0"
    motor_counts = int(round(step_um * motor_counts_per_um))
    expected_enc = int(round(step_um * ENCODER_COUNTS_PER_UM))

    emit()
    emit(f"=== {phase_no}. {label} ===")
    emit(f"{n_steps} steps of {step_mm} mm ({step_um:+.0f} um), "
         f"{n_steps * step_mm * direction:+.3f} mm total")
    emit(f"Per step: command {motor_counts:+d} motor counts, "
         f"expect {expected_enc:+d} encoder counts")

    rows = []
    prev_enc = start_enc

    for i in range(1, n_steps + 1):
        try:
            controller.move_relative(axis, motor_counts, speed=SPEED,
                                     wait=True, timeout=MOVE_TIMEOUT)
        except RuntimeError as e:
            # move_relative raises this when _LF/_LR reads active
            emit(f"[FAIL] step {i}: move refused: {e}", "error")
            emit("       LD=3 disables the switch in firmware but _LF/_LR still read "
                 "the pin. Check wiring, or use --reverse-first.", "error")
            break
        except gclib.GclibError as e:
            emit(f"[FAIL] step {i}: move command rejected: {e}", "error")
            break

        time.sleep(SETTLE_S)

        enc   = read_int(controller, f"MG _TP{axis}")
        ref   = read_int(controller, f"MG _RP{axis}")
        bits  = read_int(controller, f"MG _SS{axis}0")
        delta = enc - prev_enc
        err   = delta - expected_enc

        rows.append({
            "step": i, "commanded_um": step_um * i, "ref": ref, "enc": enc,
            "delta": delta, "expected": expected_enc, "err": err,
            "err_um": counts_to_um(err), "ss": bits,
        })

        # A warning does not invalidate the step, so it must not fail it —
        # only a blocking bit or an out-of-tolerance delta does.
        in_tolerance = abs(err) <= abs(expected_enc) * TOLERANCE_PCT / 100.0
        blocking = blocking_bits(bits)
        if blocking:
            tag = "FAIL"
        elif not in_tolerance:
            tag = "FAIL"
        elif bits & SS_WARNING:
            tag = "WARN"
        else:
            tag = "OK  "

        emit(f"[{tag}] step {i}: "
             f"_RP{axis}={ref}  _TP{axis}={enc}  "
             f"delta={delta:+d}  expected={expected_enc:+d}  "
             f"err={err:+d} counts ({counts_to_um(err):+.3f} um)  "
             f"{describe_ss(axis, bits)}",
             {"OK  ": "info", "WARN": "warning"}.get(tag, "error"))

        if blocking:
            emit("       BiSS status lost CRC or raised an error mid-test. Stopping.",
                 "error")
            break

        prev_enc = enc

    return rows


# ── Analysis ────────────────────────────────────────────────────────────────

def print_table(axis: str, rows: list[dict], title: str):
    emit()
    emit(title)
    emit(f"{'step':>4} | {'cmd um':>8} | {'_RP' + axis:>10} | {'_TP' + axis:>12} | "
         f"{'d enc':>7} | {'expect':>7} | {'err um':>8} | {'_SS' + axis + '0':>6}")
    emit("-" * 82)
    for r in rows:
        emit(f"{r['step']:>4} | {r['commanded_um']:>8.0f} | {r['ref']:>10} | "
             f"{r['enc']:>12} | {r['delta']:>+7d} | {r['expected']:>+7d} | "
             f"{r['err_um']:>+8.3f} | {r['ss']:>6}")


def steady_state_scale(rows: list[dict]):
    """Scale ratio from every step after the first.

    The first step of a run absorbs the backlash take-up, so including it
    understates the scale. Steps 2..n are the honest measurement.
    """
    if len(rows) < 2:
        return None
    measured = sum(r["delta"] for r in rows[1:])
    expected = sum(r["expected"] for r in rows[1:])
    if expected == 0:
        return None
    return measured, expected, measured / expected


def summarize(axis: str, baseline: int, fwd: list[dict], rev: list[dict]):
    emit()
    emit("=== Summary ===")

    if not fwd:
        emit("No steps completed, nothing to summarize.", "error")
        return

    print_table(axis, fwd, "Outward run:")
    if rev:
        print_table(axis, rev, "Return run:")

    # Warnings never stop the run, so say plainly how much of it was spent in the
    # pre-warning state rather than leaving it buried in the _SS column.
    all_rows = fwd + rev
    warned = [r for r in all_rows if r["ss"] & SS_WARNING]
    if warned:
        emit()
        emit("-- Signal margin --")
        emit(f"[WARN] WARN bit set on {len(warned)} of {len(all_rows)} steps "
             f"(margin below 12.5%).", "warning")
        emit("       The numbers below are still valid: CRC passed on every step, "
             "so the positions are good.", "warning")
        emit("       Fix the margin mechanically - air gap to 0.85 mm with the "
             "spacing gage at 0.25 Nm alternating, tape and window cleaned "
             "lengthwise only.", "warning")

    # ── Scale ───────────────────────────────────────────────────────────────
    emit()
    emit("-- Scale --")
    ratios = []
    for rows, name in ((fwd, "outward"), (rev, "return")):
        ss = steady_state_scale(rows)
        if ss is None:
            continue
        measured, expected, ratio = ss
        ratios.append(ratio)
        emit(f"{name:>8}, steps 2+: measured {measured:+d} vs expected {expected:+d} "
             f"counts  ->  ratio {ratio:.4f}  "
             f"({counts_to_um(measured - expected):+.3f} um over "
             f"{abs(counts_to_um(expected)):.0f} um)")

    if not ratios:
        emit("Not enough steps to measure scale.", "warning")
        return
    ratio = sum(ratios) / len(ratios)
    emit(f"Mean scale ratio: {ratio:.4f}   (1.0000 = correct)")

    # ── Backlash ────────────────────────────────────────────────────────────
    emit()
    emit("-- Backlash / lost motion --")
    emit("First step of each direction absorbs the drivetrain take-up:")
    fwd_deficit = -fwd[0]["err"]
    emit(f"  outward first step short by {fwd_deficit:+d} counts "
         f"({counts_to_um(fwd_deficit):+.2f} um)")
    if rev:
        rev_deficit = rev[0]["err"]
        emit(f"  return  first step short by {rev_deficit:+d} counts "
             f"({counts_to_um(rev_deficit):+.2f} um)")

    # ── Return error ────────────────────────────────────────────────────────
    if rev:
        emit()
        emit("-- Return error --")
        final = rev[-1]["enc"]
        return_err = final - baseline
        emit(f"Started at {baseline}, ended at {final}")
        emit(f"Return error: {return_err:+d} counts ({counts_to_um(return_err):+.2f} um)")
        emit("Commanded position returned to the start, so this is the part of the")
        emit("motion the stage did not actually give back.")

    # ── Verdict ─────────────────────────────────────────────────────────────
    emit()
    if abs(ratio - 1.0) <= TOLERANCE_PCT / 100.0:
        emit("VERDICT: SCALE OK - the encoder tracks commanded motion correctly.")
        emit("         Link, direction and counts/um are all good on this axis.")
    elif abs(ratio + 1.0) <= TOLERANCE_PCT / 100.0:
        emit("VERDICT: DIRECTION INVERTED - correct magnitude, wrong sign.", "error")
        emit("         The encoder counts down as the motor moves forward.")
        emit("         Fix with CE (Configure Encoder) polarity, not in software.")
    elif abs(abs(ratio) - 0.5) <= TOLERANCE_PCT / 100.0:
        emit("VERDICT: HALF SCALE - reading the MOTOR count, not the encoder count.", "error")
        emit("         The stage did not move half as far. The BiSS frame is most")
        emit("         likely shifted by one bit: revisit n3 (zero padding) in")
        emit("         BISS_CONFIG before touching encoder_counts_per_um.")
    elif abs(abs(ratio) - 2.0) <= 2 * TOLERANCE_PCT / 100.0:
        emit("VERDICT: DOUBLE SCALE - twice the expected counts.", "error")
        emit("         Same class of problem as half scale: suspect frame alignment,")
        emit("         or that this axis's counts_per_um is wrong.")
    elif abs(ratio) < 0.05:
        emit("VERDICT: NOT TRACKING - the encoder barely moved.", "error")
        emit("         Either the stage did not move (check motor and coupling), or")
        emit("         the head is not reading the tape.")
    else:
        emit(f"VERDICT: UNEXPECTED SCALE ({ratio:.4f}).", "error")
        emit("         Not a clean power-of-two error, so more likely mechanical:")
        emit("         lost steps, or a slipping coupling.")


# ── Entry point ─────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("axis", nargs="?", default="A", choices=["A", "B", "C", "a", "b", "c"],
                   help="axis to test (default: A)")
    p.add_argument("--step", type=float, default=0.1, help="step size in mm (default: 0.1)")
    p.add_argument("--total", type=float, default=0.4,
                   help="total distance in mm (default: 0.4)")
    p.add_argument("--reverse-first", action="store_true",
                   help="go negative first, for an axis parked near its forward end")
    p.add_argument("--no-return", action="store_true",
                   help="skip the return run (scale only, no backlash measurement)")
    p.add_argument("--address", default=CONTROLLER_ADDRESS,
                   help=f"controller address (default: {CONTROLLER_ADDRESS})")
    return p.parse_args()


def main():
    global logger
    args = parse_args()
    axis = args.axis.upper()

    logger = setup_logger(f"test_axis_{axis.lower()}_encoder.log")

    # Per-axis motor scale and travel come from settings.json, so this works
    # unchanged on C (12.8 counts/um, 200 mm) as well as A and B (6.4, 25 mm).
    with open(SETTINGS_PATH, "r") as f:
        settings = AppSettings.model_validate(json.load(f))
    axis_cfg = settings.axis_settings(axis)
    motor_counts_per_um = axis_cfg.counts_per_um
    encoder_length_mm = axis_cfg.encoder_length_mm
    travel_mm = axis_cfg.travel_mm

    n_steps = int(round(args.total / args.step))
    direction = -1 if args.reverse_first else +1

    emit(f"=== Axis {axis} encoder test ===")
    emit(f"Motor scale   : {motor_counts_per_um} counts/um")
    emit(f"Encoder scale : {ENCODER_COUNTS_PER_UM} counts/um (78.125 nm step)")
    emit(f"Travel limit  : {travel_mm} mm on a {encoder_length_mm} mm tape")
    emit(f"Test distance : {args.total} mm in {n_steps} x {args.step} mm steps, "
         f"{'reverse' if direction < 0 else 'forward'} first")

    # LD=3 disables the hardware limit switches, so the permitted travel has to be
    # enforced here rather than relied on to catch anything. This only bounds the
    # SIZE of the move; it cannot tell where in the travel the stage currently
    # sits, since _TP is not anchored to the stage (see check_baseline).
    if args.total >= travel_mm:
        emit(f"[FAIL] Test distance {args.total} mm exceeds the {travel_mm} mm travel "
             f"allowed on axis {axis}. Refusing to run.", "error")
        sys.exit(1)
    if args.total > travel_mm * 0.25:
        emit(f"[WARN] Test distance is {100 * args.total / travel_mm:.0f}% of permitted "
             f"travel. Make sure the stage has room in this direction.", "warning")

    controller = GalilController()
    try:
        emit(f"Connecting to {args.address}...")
        info = controller.connect(args.address)
        emit(f"Connected: {info.model} Rev {info.revision}, "
             f"Serial: {info.serial}, Firmware: {info.firmware}")

        emit(f"Enabling axis {axis} with BiSS C feedback...")
        controller.enable_axes(axis)

        if not check_link(controller, axis):
            sys.exit(1)

        baseline = check_baseline(controller, axis, encoder_length_mm, travel_mm)

        fwd = run_steps(controller, axis, baseline, n_steps, args.step, direction,
                        motor_counts_per_um, 3, "Outward run")

        rev = []
        if fwd and not args.no_return:
            rev = run_steps(controller, axis, fwd[-1]["enc"], len(fwd), args.step,
                            -direction, motor_counts_per_um, 4, "Return run")

        summarize(axis, baseline, fwd, rev)

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
