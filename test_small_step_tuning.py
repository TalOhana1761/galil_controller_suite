"""
Find the motion parameters that make SMALL steps as accurate and repeatable as
this drivetrain allows, on one axis.

Read this first - it bounds what any tuning can achieve:

    One motor microstep = 1 / 6.4 um = 0.15625 um.

The SDM-44140 is fixed at 1/64 microstepping (YA only declares it, it cannot
change it), so 0.15625 um is the position QUANTUM. Reachable positions lie on
that grid and nothing in between:

    10 um  =  64 microsteps exactly   -> 0% error is possible
    50 um  = 320 microsteps exactly   -> 0% error is possible
   100 um  = 640 microsteps exactly   -> 0% error is possible
     1 um  =   6.4 microsteps         -> OFF GRID. Best case is 6 steps
                                         (0.9375 um, -6.3%) or 7 steps
                                         (1.09375 um, +9.4%).

That is why small steps look worse: the error is roughly FIXED in microns, so
as a percentage it grows as the step shrinks. No value of SP, AC, DC or KS
changes it - only finer mechanics would. The 2026-09-17 run already hit this
floor: its one live 1 um move landed at 1.09375 um, exactly 7 microsteps.

So this script does not chase the quantisation. It goes after the three things
that ARE tunable, and that dominated the small-step numbers:

  1. TAKE-UP and STICTION, which are different and get confused. Two 1 um moves
     produced no encoder motion at all on axis C - that was take-up, paid once
     because the test reversed whatever direction the stage was last driven, not
     stiction. Phase 3 measures each separately.
  2. SETTLING TAIL. KS is at 16, the MAXIMUM smoothing Galil allows (range
     0.5-16, default 2). Heavy smoothing stretches the pulse train well past
     motion-complete, which is what made the loop overshoot. Phase 4 measures
     how long each setting takes to actually stop.
  3. DETERMINISM. Whether the same command delivers the same distance every
     time. This is the one that matters for positioning, and it is what the
     ranking below optimises for.

A 6-count move never reaches SP - at AC=20000 it peaks near 346 counts/s - so
SP cannot affect a 1 um step at all. AC/DC and KS can. The sweep is staged
accordingly, one factor at a time, rather than a full factorial that would take
an hour.

Phases:
  1. BiSS link check                      (refuses to move if the frame is bad)
  2. Encoder drift check at rest          (bounds what "settled" can mean)
  3. Post-move relaxation profile         (how long the stage keeps creeping)
  4. Response thresholds                  (4a stiction, 4b take-up on reversal)
  5. KS sweep, then AC/DC sweep, then SP sweep, each at a small and a large step
  6. Closed-loop check at the winner: move_microns() at 1, 2, 5, 10 um
  7. Recommendation, as the exact lines to change

Every measurement is raw PR/BG - no closed loop - except phase 5, so the
parameters are measured on their own rather than through the correction loop.

Run directly against real hardware (not part of the pytest suite):
    python test_small_step_tuning.py C
    python test_small_step_tuning.py A --repeats 7
    python test_small_step_tuning.py C --reverse
    python test_small_step_tuning.py C --quick      # KS sweep only

Results go to test_small_step_tuning_{axis}.log. Runs append, and loguru
rotates at 10 MB, so older content may sit in a timestamped sibling file.
"""
import argparse
import statistics
import sys
import time

import gclib

from controller.galil_controller import (
    GalilController, BISS_CONFIG,
    MOTOR_COUNTS_PER_UM, ENCODER_COUNTS_PER_UM, AXIS_TRAVEL_MM,
)
from utils.logger_setup import setup_logger

CONTROLLER_ADDRESS = "COM4"

# Galil's KS range is 0.5 to 16. The project currently hardcodes 16 in
# enable_axes(), which is maximum smoothing; the Galil default is 2.
KS_VALUES   = [0.5, 1, 2, 4, 8, 16]
ACDC_VALUES = [2000, 5000, 20000, 100000, 500000]
SP_VALUES   = [500, 1000, 2000, 4000, 8000]

BASE_KS   = 16          # what enable_axes() sets today - the starting point
BASE_ACDC = 20000       # settings.json accel/decel
BASE_SP   = 2000        # what the move_microns test used

SMALL_STEP_UM = 1.0     # the problem case
LARGE_STEP_UM = 10.0    # reaches cruise speed, so SP can show up

REPEATS = 5
PRELOAD_UM = 20.0       # take up backlash before measuring anything
SETTLE_TOL = 1.0        # encoder counts
SETTLE_WINDOW_MS = 200  # must stay inside SETTLE_TOL across this whole window
SETTLE_TIMEOUT_S = 3.0
MOVE_TIMEOUT = 30
SESSION_TIMEOUT_MS = 15000   # one generous gclib timeout for the whole run
CREEP_DWELL_S = 0.05         # dwell after a single microstep before reading
RELAX_SAMPLE_S = 0.02        # sample gap for the first RELAX_FAST_S
RELAX_FAST_S = 0.6           # ...after which the decay is slow enough to sample
RELAX_SLOW_SAMPLE_S = 0.25   # ...at this rate, cutting the link load ~8x
SETTLE_SAMPLE_S = 0.005      # gap between settle reads; stops the link choking

SS_WARNING = 4

logger = None


# ── Output helpers ──────────────────────────────────────────────────────────

def emit(line: str = "", level: str = "info"):
    print(line)
    getattr(logger, level)(line if line else " ")


def read_int(controller: GalilController, cmd: str) -> int:
    return int(float(controller.send(cmd).strip()))


def counts_to_um(counts: float) -> float:
    return counts / ENCODER_COUNTS_PER_UM


# ── Measurement primitives ──────────────────────────────────────────────────

def settle(controller: GalilController, axis: str):
    """Sample _TP until it stops changing. Returns (position, ms_waited, samples).

    Same criterion as GalilController._wait_until_still: the whole spread across
    a window must stay inside one count. Judging consecutive reads instead lets a
    slow crawl through - reads are only ~16 ms apart.
    """
    t0 = time.perf_counter()
    window_s = SETTLE_WINDOW_MS / 1000.0
    samples = []

    while True:
        time.sleep(SETTLE_SAMPLE_S)
        now = time.perf_counter()
        samples.append((now, float(controller.send(f"MG _TP{axis}").strip())))
        recent = [s for s in samples if now - s[0] <= window_s]
        if len(recent) >= 3 and now - samples[0][0] >= window_s:
            span = max(p for _, p in recent) - min(p for _, p in recent)
            if span <= SETTLE_TOL:
                return recent[-1][1], (now - t0) * 1000.0, samples
        if now - t0 > SETTLE_TIMEOUT_S:
            return samples[-1][1], (now - t0) * 1000.0, samples


def guarded(controller: GalilController, axis: str, address: str, fn, *args):
    """Run one measurement; on a link timeout, reconnect and try once more.

    A four-minute sweep should not be lost to a single dropped reply, and after a
    timeout the replies are shifted by one so carrying on regardless would record
    fiction.
    """
    try:
        return fn(controller, axis, *args)
    except gclib.GclibError as e:
        emit(f"[WARN] {e}", "warning")
        resync(controller, axis, address)
        controller.enable_axes(axis)
        return fn(controller, axis, *args)


def raw_step(controller: GalilController, axis: str, motor_counts: int) -> dict:
    """One open-loop PR/BG step, fully characterised.

    Deliberately NOT through move_microns(): the point is to see what the
    parameters do on their own, without a correction loop hiding it.
    """
    start, _, _ = settle(controller, axis)

    controller.send(f"PR{axis}={motor_counts}")
    controller.send(f"BG{axis}")
    motion_complete(controller, axis)

    # Where the encoder is the instant the profiler says it is done...
    at_complete = float(controller.send(f"MG _TP{axis}").strip())
    # ...and where it ends up once the stage has actually stopped.
    final, settle_ms, _ = settle(controller, axis)

    expected = motor_counts * ENCODER_COUNTS_PER_UM / MOTOR_COUNTS_PER_UM[axis]
    delivered = final - start
    return {
        "expected": expected,
        "delivered": delivered,
        "error": delivered - expected,
        "tail": final - at_complete,      # arrived after motion complete
        "settle_ms": settle_ms,
        "ratio": delivered / expected if expected else float("nan"),
    }


def motion_complete(controller: GalilController, axis: str):
    """Wait for the profiler, under the session-wide timeout.

    Deliberately does NOT do the GTimeout(x)/GTimeout(-1) dance: -1 reverts to
    the value GOpen was given, i.e. the 5 s default, so any call that forgot to
    set it first ran with 5 s. The session sets one generous timeout at connect
    and everything uses it.
    """
    controller._g.GMotionComplete(axis)


def resync(controller: GalilController, axis: str, address: str):
    """Recover from a timeout by reopening the link.

    After a timed-out command the replies stay shifted by one - the next command
    receives the previous command's answer - so every reading afterwards is a
    lie. There is no flush in gclib, so the only trustworthy fix is a fresh
    connection.
    """
    emit("[WARN] command timed out; reopening the connection to resynchronise",
         "warning")
    try:
        controller._g.GClose()
    except Exception:
        pass
    time.sleep(0.5)
    controller._g = gclib.py()
    controller._g.GOpen(address)
    controller._g.GTimeout(SESSION_TIMEOUT_MS)
    emit("[OK  ] reconnected")


def apply_config(controller: GalilController, axis: str, ks, sp, ac, dc):
    controller.send(f"KS{axis}={ks}")
    controller.send(f"SP{axis}={sp}")
    controller.send(f"AC{axis}={ac}")
    controller.send(f"DC{axis}={dc}")


# ── Phases ──────────────────────────────────────────────────────────────────

def check_link(controller: GalilController, axis: str) -> bool:
    emit()
    emit("=== 1. BiSS link check ===")
    emit(f"Configured with SS{axis}={BISS_CONFIG}")
    bits = read_int(controller, f"MG _SS{axis}0")
    if bits & ~SS_WARNING:
        emit(f"[FAIL] _SS{axis}0={bits}: position cannot be trusted. Not moving.",
             "error")
        return False
    emit(f"[OK  ] _SS{axis}0={bits}"
         + ("  (WARN: signal margin below 12.5%)" if bits & SS_WARNING else ""))
    return True


def check_drift(controller: GalilController, axis: str):
    """How quiet is the encoder with nothing moving? Everything else is measured
    against this - a tail smaller than the noise is not a tail."""
    emit()
    emit("=== 2. Encoder at rest ===")
    samples = []
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 1.0:
        samples.append(float(controller.send(f"MG _TP{axis}").strip()))
    spread = max(samples) - min(samples)
    emit(f"{len(samples)} samples over 1 s, spread {spread:.0f} counts "
         f"({counts_to_um(spread):.4f} um)")
    if spread > SETTLE_TOL:
        emit(f"[WARN] Resting noise exceeds the {SETTLE_TOL:.0f}-count settle "
             f"tolerance; settle times below will be pessimistic.", "warning")
    else:
        emit("[OK  ] Quiet at rest.")
    return spread


def creep_until_response(controller: GalilController, axis: str, direction: int,
                         tries: int) -> tuple:
    """Command one microstep at a time until the encoder moves.

    Returns (microsteps_taken or None, encoder counts it jumped).
    """
    start, _, _ = settle(controller, axis)
    for i in range(1, tries + 1):
        controller.send(f"PR{axis}={direction}")
        controller.send(f"BG{axis}")
        motion_complete(controller, axis)

        # A full settle after every microstep is ~13 commands each and thousands
        # across the phase, which is what saturated the serial link and timed it
        # out. One read after a short dwell is enough to notice a count of
        # motion; settle properly only once something has actually moved.
        time.sleep(CREEP_DWELL_S)
        pos = float(controller.send(f"MG _TP{axis}").strip())
        # Signed, not abs(): the stage can still be creeping from the previous
        # move, and creep the OTHER way must not be mistaken for a response.
        if (pos - start) * direction >= 1.0:
            pos, _, _ = settle(controller, axis)
            return i, pos - start
    return None, 0.0


def _relax_round(controller: GalilController, axis: str, step_um: float,
                 cpm: float, direction: int, seconds: float, marks) -> tuple:
    """One move, then watch the encoder for `seconds`. Returns (drifts, rate).

    Split out so a round that dies on a link timeout can simply be retried after
    a reconnect, instead of taking the whole phase down with it.
    """
    settle(controller, axis)
    counts = int(round(step_um * cpm)) * direction
    controller.send(f"PR{axis}={counts}")
    controller.send(f"BG{axis}")
    motion_complete(controller, axis)

    t0 = time.perf_counter()
    first = float(controller.send(f"MG _TP{axis}").strip())
    trace = [(0.0, first)]
    while time.perf_counter() - t0 < seconds:
        # Dense early, sparse later. The marks below 0.5 s need the fast rate;
        # after that the curve is slow and sampling it at ~28 Hz only loads the
        # link - which is what timed it out on the 2026-09-23 cold run, two
        # rounds into nine.
        elapsed = time.perf_counter() - t0
        time.sleep(RELAX_SAMPLE_S if elapsed < RELAX_FAST_S else RELAX_SLOW_SAMPLE_S)
        trace.append((time.perf_counter() - t0,
                      float(controller.send(f"MG _TP{axis}").strip())))

    drifts = []
    for m in marks:
        upto = [p for t, p in trace if t <= m]
        drifts.append(counts_to_um(upto[-1] - first) if upto else 0.0)

    # Is it still moving at the end? Rate over the final second.
    tail = [(t, p) for t, p in trace if t >= seconds - 1.0]
    rate = 0.0
    if len(tail) >= 2:
        rate = counts_to_um(tail[-1][1] - tail[0][1]) / max(tail[-1][0] - tail[0][0], 1e-6)
    return drifts, rate


def profile_relaxation(controller: GalilController, axis: str, direction: int,
                       cpm: float, seconds: float = 10.0, rounds: int = 3,
                       step_sizes=(1.0, 10.0, 100.0), address: str = "") -> dict:
    """Watch the encoder for `seconds` AFTER a move, and report how it decays.

    This is the measurement that decides how tight the closed loop can get. The
    residual overshoot left after the KS fix is motion arriving once the loop has
    stopped looking: a drift of one count per 200 ms window reads as "still" but
    is 0.4 um/s, and about a second passes before the confirming read. With
    nothing commanded the encoder is quiet to 0 counts over 1 s, so this is
    relaxation after motion, not drift.

    The first hardware round (2026-09-22, axis C, 10 um step) gave
    +0.312 / +0.469 / +0.547 / +0.625 / +0.703 um at 0.2 / 0.5 / 1 / 2 / 5 s -
    still climbing at five seconds. So the question is no longer "what settle
    window" but "does it ever stop, and does it scale with the move".

    That second part decides the fix. If relaxation is proportional to the
    distance travelled it can be compensated feed-forward, by aiming short and
    letting the stage creep in. If it is a fixed amount per move, the
    compensation is a constant. If it is neither, it is a bias to accept.
    """
    emit()
    emit("=== 3. Post-move relaxation ===")
    emit(f"For each step size: move, then watch _TP for {seconds:.0f} s without "
         f"touching the stage.")
    emit("Drift is measured from the first reading after motion completed.")

    marks = tuple(m for m in (0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0) if m <= seconds)
    out = {}

    for step_um in step_sizes:
        emit()
        emit(f"-- after a {step_um:g} um step --")
        emit(f"{'round':>5} | " + " | ".join(f"{m:>7.1f}s" for m in marks)
             + " | last-second rate")
        emit("-" * (8 + 10 * len(marks) + 18))

        per_round = []
        rates = []
        for rnd in range(1, rounds + 1):
            try:
                one = _relax_round(controller, axis, step_um, cpm, direction,
                                   seconds, marks)
            except gclib.GclibError as e:
                # Losing one round is not losing the phase. The 2026-09-23 run
                # abandoned seven remaining measurements over a single timeout.
                emit(f"[WARN] round {rnd}: {e} - reconnecting and retrying once",
                     "warning")
                resync(controller, axis, address)
                controller.enable_axes(axis)
                try:
                    one = _relax_round(controller, axis, step_um, cpm, direction,
                                       seconds, marks)
                except gclib.GclibError as e2:
                    emit(f"[WARN] round {rnd} failed again ({e2}) - skipping",
                         "warning")
                    continue
            drifts, rate = one
            rates.append(rate)
            per_round.append(drifts)
            emit(f"{rnd:>5} | " + " | ".join(f"{d:>+8.3f}" for d in drifts)
                 + f" | {rate:+.4f} um/s")

        if not per_round:
            emit(f"[WARN] no usable rounds at {step_um:g} um", "warning")
            continue

        means = [statistics.fmean(r[i] for r in per_round) for i in range(len(marks))]
        mean_rate = statistics.fmean(rates)
        emit(f"{'mean':>5} | " + " | ".join(f"{m:>+8.3f}" for m in means)
             + f" | {mean_rate:+.4f} um/s")
        out[step_um] = {"means": means, "rate": mean_rate, "total": means[-1]}


    # ── What it means ───────────────────────────────────────────────────────
    half_step = 0.5 / cpm
    emit()
    emit("-- Reading --")
    emit(f"{'step':>8} | {'total drift':>12} | {'as % of step':>12} | "
         f"{'still moving?':>14}")
    emit("-" * 56)
    for step_um, d in out.items():
        moving = "yes" if abs(d["rate"]) > half_step / 10 else "settled"
        emit(f"{step_um:>8.1f} | {d['total']:>+12.3f} | "
             f"{100 * d['total'] / step_um:>11.2f}% | {moving:>14}")

    emit()
    if all(abs(d["total"]) <= half_step for d in out.values()):
        emit("[OK  ] Relaxation is inside half a microstep at every step size. "
             "The settle window is not what limits the loop.")
        return out

    # Which model fits? Compare the two COMPETING descriptions rather than
    # testing each against an absolute threshold.
    #
    #   proportional -> drift/distance is constant across step sizes
    #   constant     -> drift itself is constant across step sizes
    #
    # Whichever varies less is the better description. The first version of this
    # asked "is the spread under half a microstep" of each in turn, which is
    # tighter than the encoder can even resolve, so real data failed both tests
    # and got reported as "neither" - on 2026-09-22 it called a 1.5x spread
    # "neither" when the competing model spread 89x.
    def _cv(xs):
        m = statistics.fmean(xs)
        return abs((max(xs) - min(xs)) / m) if m else float("inf")

    fracs = [d["total"] / s for s, d in out.items() if s]
    totals = [d["total"] for d in out.values()]
    cv_prop, cv_const = _cv(fracs), _cv(totals)

    emit(f"[INFO] Relaxation exceeds half a microstep ({half_step:.4f} um).")
    emit(f"       proportional model: drift/step varies {max(fracs)/min(fracs):.1f}x "
         f"(spread/mean {cv_prop:.2f})")
    emit(f"       constant model    : drift varies {max(totals)/min(totals):.1f}x "
         f"(spread/mean {cv_const:.2f})")

    if cv_prop < cv_const:
        emit(f"       -> PROPORTIONAL, about {100 * statistics.fmean(fracs):.2f}% "
             f"of the distance travelled.")
        emit("       Compensable feed-forward: aim short by that fraction and "
             "let the stage creep onto the target.")
    else:
        emit(f"       -> CONSTANT, about {statistics.fmean(totals):+.3f} um per "
             f"move regardless of distance.")
        emit("       Physically sensible: elastic wind-up depends on the FORCE "
             "(friction, bellows preload), which does not change with distance.")
        emit("       Compensable as a fixed offset in the approach direction - "
             "but see the settling time below before applying one.")

    if any(abs(d["rate"]) > half_step / 10 for d in out.values()):
        emit(f"       NOTE: still moving at {seconds:.0f} s. Re-run with "
             f"--relax-seconds 60 to find where it actually stops.")
    return out


def measure_thresholds(controller: GalilController, axis: str, direction: int,
                       dwell_s: float = 2.0, rounds: int = 3) -> dict:
    """Two different "it did not move" numbers, measured separately.

    They get confused constantly, and they have different fixes:

      STICTION  - axis already loaded in this direction, left to stand, then
                  nudged. Static friction only. Small.
      TAKE-UP   - axis loaded the OTHER way, then reversed. The nut, screw and
                  bellows have to be driven across their slop before the stage
                  moves at all. Large.

    The two 1 um moves that vanished on axis C were take-up, not stiction: the
    test started by reversing whatever direction the stage was last driven.
    Take-up is paid once per direction change, stiction on every move from rest.
    """
    out = {}
    emit()
    emit("=== 4. Response thresholds ===")
    emit(f"One microstep = {counts_to_um(2):.4f} um. Commanding them one at a "
         f"time until the encoder moves.")

    # ── Stiction: same direction, after standing still ──────────────────────
    emit()
    emit(f"4a. Stiction (already loaded this way, {dwell_s:.0f} s dwell first)")
    stiction = []
    for rnd in range(1, rounds + 1):
        time.sleep(dwell_s)       # let it stand: static friction builds with dwell
        steps, jumped = creep_until_response(controller, axis, direction, tries=12)
        if steps is None:
            emit(f"[WARN] round {rnd}: no response after 12 microsteps", "warning")
            stiction.append(13)
        else:
            emit(f"round {rnd}: moved on microstep {steps} "
                 f"({counts_to_um(2 * steps):.3f} um commanded), "
                 f"encoder jumped {jumped:+.0f} counts "
                 f"({counts_to_um(jumped):+.3f} um)")
            stiction.append(steps)
    out["stiction"] = max(stiction)
    emit(f"Stiction, worst of {rounds}: {out['stiction']} microstep(s) = "
         f"{counts_to_um(2 * out['stiction']):.3f} um")

    # ── Take-up: reverse, and count the slop ────────────────────────────────
    emit()
    emit("4b. Take-up on a direction reversal")
    steps, jumped = creep_until_response(controller, axis, -direction, tries=200)
    if steps is None:
        emit("[WARN] no response after 200 microsteps of reversal", "warning")
        out["takeup"] = None
    else:
        out["takeup"] = steps
        emit(f"Reversed and moved on microstep {steps} "
             f"({counts_to_um(2 * steps):.3f} um commanded), "
             f"encoder jumped {jumped:+.0f} counts")
        emit(f"Take-up: {counts_to_um(2 * steps):.3f} um of lost motion per "
             f"direction change on this axis.")

    # Put the drivetrain back under load in the test direction, so the sweep
    # below is not measuring take-up all over again.
    emit()
    emit(f"Re-loading {PRELOAD_UM * direction:+.0f} um in the test direction...")
    raw_step(controller, axis, int(round(PRELOAD_UM * MOTOR_COUNTS_PER_UM[axis])) * direction)

    emit()
    emit("What this means for step size:")
    emit(f"  - Smallest move that responds from rest: "
         f"{counts_to_um(2 * out['stiction']):.3f} um (stiction).")
    if out["takeup"]:
        emit(f"  - A move that reverses direction loses "
             f"{counts_to_um(2 * out['takeup']):.3f} um before anything happens, "
             f"so always approach from one side.")
    return out


def sweep(controller: GalilController, axis: str, address: str, label: str,
          configs: list, step_counts: dict, repeats: int, phase: str) -> list:
    """Run every config over the given step sizes and tabulate the result."""
    emit()
    emit(f"=== {phase}. {label} ===")
    emit(f"{repeats} repeats per config per step size.")
    emit()
    emit(f"{'config':<28} | {'step':>6} | {'mean err':>9} | {'spread':>8} | "
         f"{'ratio':>6} | {'tail':>7} | {'settle':>8}")
    emit("-" * 92)

    rows = []
    for cfg in configs:
        apply_config(controller, axis, cfg["ks"], cfg["sp"], cfg["ac"], cfg["dc"])
        entry = {"cfg": cfg, "by_step": {}}

        for step_name, counts in step_counts.items():
            runs = [guarded(controller, axis, address, raw_step, counts)
                    for _ in range(repeats)]
            errs = [r["error"] for r in runs]
            delivered = [r["delivered"] for r in runs]
            stat = {
                "expected_counts": abs(runs[0]["expected"]),
                "mean_err_um": counts_to_um(statistics.fmean(errs)),
                "spread_um": counts_to_um(max(delivered) - min(delivered)),
                "ratio": statistics.fmean(r["ratio"] for r in runs),
                "tail_um": counts_to_um(statistics.fmean(r["tail"] for r in runs)),
                "settle_ms": statistics.fmean(r["settle_ms"] for r in runs),
            }
            entry["by_step"][step_name] = stat
            emit(f"{cfg['name']:<28} | {step_name:>6} | "
                 f"{stat['mean_err_um']:>+9.4f} | {stat['spread_um']:>8.4f} | "
                 f"{stat['ratio']:>6.3f} | {stat['tail_um']:>+7.3f} | "
                 f"{stat['settle_ms']:>7.0f}ms")

        rows.append(entry)

    return rows


def score(entry: dict, key_step: str) -> float:
    """Rank a config. Lower is better.

    Determinism first (spread is what limits positioning), then accuracy, then
    settling time. Step loss is disqualifying: a config that delivers less than
    it was told is dropping microsteps, however tidy its numbers look.
    """
    stat = entry["by_step"][key_step]
    penalty = 0.0
    # Step loss is only diagnosable over enough counts that stiction cannot
    # explain the shortfall. On a 6-count step, losing 0.5 counts to stiction is
    # a ratio of 0.92 and says nothing about the drive.
    if stat["expected_counts"] >= 50 and stat["ratio"] < 0.97:
        penalty += 100.0
    return (penalty
            + 10.0 * stat["spread_um"]
            + 5.0 * abs(stat["mean_err_um"])
            + 3.0 * abs(stat["tail_um"])     # the cleanest signal KS produced
            + 0.001 * stat["settle_ms"])


def best_of(rows: list, key_step: str) -> dict:
    return min(rows, key=lambda e: score(e, key_step))


def verify_closed_loop(controller: GalilController, axis: str, address: str,
                       direction: int, repeats: int, phase: str) -> list:
    """End-to-end check with the winning parameters, through move_microns()."""
    emit()
    emit(f"=== {phase}. Closed-loop check at the winning parameters ===")
    emit(f"{'commanded':>10} | {'mean moved':>11} | {'mean err':>10} | "
         f"{'spread':>8} | {'% err':>7}")
    emit("-" * 60)

    rows = []
    for dist in (1.0, 2.0, 5.0, 10.0):
        moved = []
        for _ in range(repeats):
            def one(ctrl, ax, d=dist):
                before, _, _ = settle(ctrl, ax)
                ctrl.move_microns(ax, d * direction, timeout=MOVE_TIMEOUT)
                after, _, _ = settle(ctrl, ax)
                return counts_to_um(after - before)
            moved.append(guarded(controller, axis, address, one))
        mean = statistics.fmean(moved)
        spread = max(moved) - min(moved)
        err = mean - dist * direction
        rows.append({"dist": dist, "mean": mean, "spread": spread, "err": err})
        emit(f"{dist * direction:>+10.2f} | {mean:>+11.4f} | {err:>+10.4f} | "
             f"{spread:>8.4f} | {100 * err / (dist * direction):>+6.2f}%")

    # The only fair verdict: is the error at the quantisation floor, or above it?
    microstep_um = 1.0 / MOTOR_COUNTS_PER_UM[axis]
    emit()
    emit(f"{'commanded':>10} | {'floor':>9} | {'measured':>9} | verdict")
    emit("-" * 52)
    for r in rows:
        steps = r["dist"] * MOTOR_COUNTS_PER_UM[axis]
        floor = abs(round(steps) - steps) / MOTOR_COUNTS_PER_UM[axis]
        at_floor = abs(r["err"]) <= max(floor, microstep_um / 2) + 1e-9
        emit(f"{r['dist']:>+10.2f} | {floor:>9.4f} | {abs(r['err']):>9.4f} | "
             + ("AT THE FLOOR - nothing left to tune"
                if at_floor else
                f"{abs(r['err']) / microstep_um:.1f} microsteps above the floor"))
    return rows


# ── Entry point ─────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("axis", nargs="?", default="C",
                   choices=["A", "B", "C", "a", "b", "c"])
    p.add_argument("--repeats", type=int, default=REPEATS)
    p.add_argument("--reverse", action="store_true",
                   help="run in the negative direction")
    p.add_argument("--quick", action="store_true",
                   help="KS sweep only, then verify")
    p.add_argument("--address", default=CONTROLLER_ADDRESS)
    p.add_argument("--relax-only", action="store_true",
                   help="run ONLY the post-move relaxation profile and stop")
    p.add_argument("--relax-seconds", type=float, default=10.0,
                   help="how long to watch the encoder after each move")
    p.add_argument("--relax-steps", default="1,10,100",
                   help="step sizes in um to profile relaxation after")
    p.add_argument("--debug", action="store_true",
                   help="log every controller command (large: ~10 MB per run)")
    return p.parse_args()


def main():
    global logger
    args = parse_args()
    axis = args.axis.upper()
    logger = setup_logger(f"test_small_step_tuning_{axis.lower()}.log")
    if not args.debug:
        # send() logs every command at DEBUG, and the settle loops issue tens of
        # thousands of them. Left on, one run passed loguru's 10 MB rotation
        # partway through and the results ended up split across two files.
        from loguru import logger as _lg
        _lg.remove()      # no stdout sink: emit() already prints
        _lg.add(f"test_small_step_tuning_{axis.lower()}.log", level="INFO",
                format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {message}")
        logger = _lg

    direction = -1 if args.reverse else +1
    cpm = MOTOR_COUNTS_PER_UM[axis]
    microstep_um = 1.0 / cpm

    small_counts = int(round(SMALL_STEP_UM * cpm)) * direction
    large_counts = int(round(LARGE_STEP_UM * cpm)) * direction
    step_counts = {f"{SMALL_STEP_UM:g}um": small_counts,
                   f"{LARGE_STEP_UM:g}um": large_counts}

    emit(f"=== Small-step tuning, axis {axis} ===")
    emit(f"Motor    : {cpm} counts/um  ->  one microstep = {microstep_um:.5f} um")
    emit(f"Encoder  : {ENCODER_COUNTS_PER_UM} counts/um  "
         f"({ENCODER_COUNTS_PER_UM / cpm:.1f} encoder counts per microstep)")
    emit(f"Direction: {'reverse' if direction < 0 else 'forward'}")
    emit()
    emit("Quantisation floor, before any tuning:")
    for um in (SMALL_STEP_UM, 2.0, 5.0, LARGE_STEP_UM):
        steps = um * cpm
        nearest = round(steps)
        err = (nearest - steps) / cpm
        grid = "on grid" if abs(steps - nearest) < 1e-9 else "OFF GRID"
        emit(f"  {um:>5.1f} um = {steps:>6.1f} microsteps  {grid:<9} "
             f"best possible error {err:+.4f} um ({100 * err / um:+.2f}%)")

    # Rough travel budget, so a sweep cannot walk the stage into an end stop.
    n_cfgs = len(KS_VALUES) + (0 if args.quick else len(ACDC_VALUES) + len(SP_VALUES))
    budget_um = (n_cfgs * args.repeats * (SMALL_STEP_UM + LARGE_STEP_UM)
                 + PRELOAD_UM + 4 * args.repeats * 5 + 100)
    travel_um = AXIS_TRAVEL_MM[axis] * 1000.0
    emit()
    emit(f"Travel budget: about {budget_um:.0f} um of {travel_um:.0f} um permitted")
    if budget_um >= travel_um * 0.5:
        emit(f"[FAIL] That is more than half the travel on {axis}. Refusing to run.",
             "error")
        sys.exit(1)

    controller = GalilController()
    saved = {}
    try:
        emit(f"Connecting to {args.address}...")
        info = controller.connect(args.address)
        emit(f"Connected: {info.model} Rev {info.revision}, Serial: {info.serial}")
        controller._g.GTimeout(SESSION_TIMEOUT_MS)
        controller.enable_axes(axis)

        if not check_link(controller, axis):
            sys.exit(1)

        for p in ("KS", "SP", "AC", "DC"):
            try:
                saved[p] = controller.send(f"MG _{p}{axis}").strip()
            except Exception:
                pass
        emit(f"Current parameters: {saved}")

        # Baseline from what the CONTROLLER actually has, not from settings.json.
        # enable_axes() sets MT/SY/SS/YA/LD/KS but never SP/AC/DC, so those sit at
        # Galil's power-on defaults (SP=25000, AC=DC=256000) and settings.json's
        # speed/accel/decel have never reached the hardware. Measured 2026-09-22.
        def saved_num(key, fallback):
            try:
                return float(saved.get(key, ""))
            except ValueError:
                return float(fallback)

        base_ks = saved_num("KS", BASE_KS)
        base_sp = saved_num("SP", BASE_SP)
        base_acdc = saved_num("AC", BASE_ACDC)
        emit(f"Sweeping around the live values: KS={base_ks:g} SP={base_sp:g} "
             f"AC=DC={base_acdc:g}")
        if base_acdc not in ACDC_VALUES:
            ACDC_VALUES.append(base_acdc)      # always test the status quo
            ACDC_VALUES.sort()

        check_drift(controller, axis)

        # Take up the drivetrain in the test direction first, so that stiction is
        # measured from a loaded state and not confused with backlash.
        emit()
        emit(f"Pre-loading {PRELOAD_UM * direction:+.0f} um to take up the "
             f"drivetrain in the test direction...")
        apply_config(controller, axis, base_ks, base_sp, base_acdc, base_acdc)
        raw_step(controller, axis, int(round(PRELOAD_UM * cpm)) * direction)

        try:
            relax_steps = [float(x) for x in args.relax_steps.split(",") if x.strip()]
        except ValueError:
            emit(f"Bad --relax-steps: {args.relax_steps!r}", "error")
            sys.exit(1)

        try:
            profile_relaxation(controller, axis, direction, cpm,
                               seconds=args.relax_seconds, step_sizes=relax_steps,
                               address=args.address)
        except gclib.GclibError as e:
            emit(f"[WARN] relaxation phase failed: {e}", "warning")
            resync(controller, axis, args.address)
            controller.enable_axes(axis)

        if args.relax_only:
            emit()
            emit("--relax-only: stopping here, the sweeps were not run.")
            return

        try:
            thresholds = measure_thresholds(controller, axis, direction)
        except gclib.GclibError as e:
            emit(f"[WARN] thresholds phase failed: {e}", "warning")
            resync(controller, axis, args.address)
            controller.enable_axes(axis)
            thresholds = {}

        # Stage 1: KS, the parameter currently pinned at its maximum.
        ks_cfgs = [{"name": f"KS={ks:g} (SP={base_sp:g} AC=DC={base_acdc:g})", "ks": ks,
                    "sp": base_sp, "ac": base_acdc, "dc": base_acdc}
                   for ks in KS_VALUES]
        key_step = f"{SMALL_STEP_UM:g}um"
        ks_rows = sweep(controller, axis, args.address,
                        "KS sweep (step smoothing)", ks_cfgs,
                        step_counts, args.repeats, "5a")
        best_ks = best_of(ks_rows, key_step)["cfg"]["ks"]
        emit(f"--> best KS for the {key_step} step: {best_ks}")

        best_acdc, best_sp = base_acdc, base_sp
        if not args.quick:
            # Stage 2: AC/DC. For a 6-count move this is the only thing that
            # shapes the profile, since SP is never reached.
            ac_cfgs = [{"name": f"AC=DC={v:g} (KS={best_ks:g} SP={base_sp:g})", "ks": best_ks,
                        "sp": base_sp, "ac": v, "dc": v} for v in ACDC_VALUES]
            ac_rows = sweep(controller, axis, args.address,
                            "Acceleration / deceleration sweep",
                            ac_cfgs, step_counts, args.repeats, "5b")
            best_acdc = best_of(ac_rows, key_step)["cfg"]["ac"]
            emit(f"--> best AC/DC for the {key_step} step: {best_acdc}")

            # Stage 3: SP. Only judged on the large step - a 1 um move never
            # gets near cruise speed, so SP cannot affect it.
            sp_cfgs = [{"name": f"SP={v:g} (KS={best_ks:g} AC=DC={best_acdc:g})",
                        "ks": best_ks, "sp": v, "ac": best_acdc, "dc": best_acdc}
                       for v in SP_VALUES]
            sp_rows = sweep(controller, axis, args.address, "Speed sweep", sp_cfgs,
                            {f"{LARGE_STEP_UM:g}um": large_counts},
                            args.repeats, "5c")
            best_sp = best_of(sp_rows, f"{LARGE_STEP_UM:g}um")["cfg"]["sp"]
            emit(f"--> best SP for the {LARGE_STEP_UM:g}um step: {best_sp}")

        emit()
        emit(f"Winning parameters: KS={best_ks}  SP={best_sp}  AC=DC={best_acdc}")
        apply_config(controller, axis, best_ks, best_sp, best_acdc, best_acdc)

        verify_closed_loop(controller, axis, args.address, direction,
                           args.repeats, "6")

        # ── Recommendation ──────────────────────────────────────────────────
        emit()
        emit("=== 7. Recommendation ===")
        emit(f"In controller/galil_controller.py, enable_axes():")
        emit(f"    self.send(f\"KS{{ax}}={best_ks:g}\")     # currently {base_ks:g}")
        emit(f"In config/settings.json, axis_{axis.lower()}:")
        emit(f"    \"speed\": {best_sp:g}, \"accel\": {best_acdc:g}, \"decel\": {best_acdc:g}")
        emit()
        emit("Remember what tuning cannot do: a 1 um step is 6.4 microsteps, so")
        emit(f"the best any parameter set can manage is {microstep_um * 0.4:.4f} um "
             f"short (6 steps) or {microstep_um * 0.6:.4f} um long (7 steps).")
        emit("Steps that are whole multiples of "
             f"{microstep_um:.5f} um can land exactly; others cannot.")

    except KeyboardInterrupt:
        emit("Interrupted - halting.", "warning")
        controller.send_safe(f"ST{axis}")
    except Exception as e:
        emit(f"Fatal error: {e}", "error")
        sys.exit(1)
    finally:
        try:
            # Put the controller back the way it was found.
            for p, v in saved.items():
                if v:
                    controller.send_safe(f"{p}{axis}={v}")
            emit(f"Restored: {saved}")
            controller.send_safe(f"ST{axis}")
            controller.send_safe(f"MO{axis}")
            controller.disconnect()
            emit()
            emit("Disconnected.")
        except Exception as e:
            logger.warning(f"Cleanup: {e}")


if __name__ == "__main__":
    main()
