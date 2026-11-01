"""
Report the amplifier current settings against each axis's motor rating.

READ ONLY. Nothing moves, nothing is written to the controller. Safe to run with
the GUI closed at any time.

Why this exists: the motors run hot to the touch even when idle, and nothing in
the system can tell you whether that is normal.

  - A stepper is HOTTEST AT STANDSTILL. It holds position by pushing continuous
    current through the windings, with no back-EMF and no airflow. Warm-while-
    idle is normal stepper behaviour; the question is the magnitude.
  - The SDM-44140 protects against over-current and under-voltage only (manual
    A10). There is NO motor temperature sensing anywhere in this system, so the
    absence of a warning is not evidence that the motor is within its rating.
  - enable_axes() sets AG per axis since 2026-11-01 (AXIS_AMP_GAIN in
    galil_controller.py) but still NOT LC, which stays whatever the controller's
    EEPROM holds. Run this with the GUI closed AFTER a GUI session to see the
    values the app applied; a fresh power-on shows the EEPROM values instead.

AG on the D4140 sets AMPS PER PHASE, not a gain in any resolution sense:

    AG   0      1      2      3
         0.5 A  1.0 A  2.0 A  3.0 A

The motors are NOT all the same (datasheets, 2026-11-01): X and Y are 1 A
motors, Z is a 3 A motor. So AG=1 is right for A/B and AG=3 for C. Anything above
an axis's rating overdrives it, and heating goes as current squared. (Until
2026-11-01 this script assumed one 1.2 A motor on every axis, which hid that Z
was running at a third of its rating.)

LC sets the holding current after a move completes:

    n<0   always 100%          (hottest at rest)
    n=0   25% after a move     (the factory default)
    n=1   0% after n samples
    n>1   25% after n samples

    python check_motor_current.py
    python check_motor_current.py --address COM4
"""
import argparse
import sys

import gclib

CONTROLLER_ADDRESS = "COM4"
AXES = ("A", "B", "C")

# SDM-44140 / D4140, amps per phase. Manual A10, and the AG command reference.
AG_AMPS = {0: 0.5, 1: 1.0, 2: 2.0, 3: 3.0}

# Per-axis motor rating, A/phase, from the motor datasheets (user, 2026-11-01).
MOTOR_RATED_A = {"A": 1.0, "B": 1.0, "C": 3.0}

LC_MEANING = {
    "negative": "always 100% holding current - hottest at rest",
    0: "25% holding current immediately after a move (factory default)",
    1: "0% holding current after 1 sample",
}

# TA bit meanings for the stepper drive (manual A10: over-current, under-voltage).
TA_BITS = {
    0: "axis A/B over-current or short",
    1: "axis C/D over-current or short",
    4: "under-voltage",
}


def read(g, cmd, default=None):
    try:
        return g.GCommand(cmd).strip()
    except Exception as e:
        return default if default is not None else f"<{e}>"


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--address", default=CONTROLLER_ADDRESS)
    p.add_argument("--rated-amps", type=float, default=None,
                   help=f"override every axis's rating, A/phase "
                        f"(default per axis: {MOTOR_RATED_A})")
    args = p.parse_args()
    rated = ({ax: args.rated_amps for ax in AXES} if args.rated_amps
             else MOTOR_RATED_A)

    g = gclib.py()
    try:
        g.GOpen(args.address)
    except Exception as e:
        print(f"Could not open {args.address}: {e}")
        print("If the GUI is running it holds the port - close it and retry.")
        return 1

    try:
        print(g.GInfo().strip())
        print("\nMotor rating assumed: "
              + ", ".join(f"{ax} {rated[ax]} A" for ax in AXES) + "\n")

        print(f"{'axis':>5} | {'MT':>4} | {'AG':>4} | {'amps/phase':>11} | "
              f"{'vs rating':>10} | {'heating':>8} | {'LC':>5}")
        print("-" * 70)

        worst = 0.0
        for ax in AXES:
            mt = read(g, f"MG _MT{ax}")
            ag_raw = read(g, f"MG _AG{ax}")
            lc_raw = read(g, f"MG _LC{ax}")
            try:
                ag = int(float(ag_raw))
                amps = AG_AMPS.get(ag)
            except ValueError:
                ag, amps = None, None

            if amps is None:
                print(f"{ax:>5} | {mt:>4} | {ag_raw:>4} | {'?':>11} | "
                      f"{'?':>10} | {'?':>8} | {lc_raw:>5}")
                continue

            ratio = amps / rated[ax]
            heating = ratio ** 2
            worst = max(worst, ratio)
            flag = "  <-- OVER" if ratio > 1.0 else ""
            print(f"{ax:>5} | {mt:>4} | {ag:>4} | {amps:>10.1f}A | "
                  f"{ratio:>9.2f}x | {heating:>7.2f}x | {lc_raw:>5}{flag}")

        # ── Holding current, which is what makes an idle motor hot ──────────
        print()
        print("Holding current at rest (what heats an idle, enabled motor):")
        for ax in AXES:
            try:
                ag = int(float(read(g, f"MG _AG{ax}")))
                lc = int(float(read(g, f"MG _LC{ax}")))
            except ValueError:
                continue
            amps = AG_AMPS.get(ag)
            if amps is None:
                continue
            if lc < 0:
                frac, note = 1.00, "LC<0: always 100%"
            elif lc == 0:
                frac, note = 0.25, "LC=0: 25% after a move (default)"
            elif lc == 1:
                frac, note = 0.00, "LC=1: 0% after 1 sample"
            else:
                frac, note = 0.25, f"LC={lc}: 25% after {lc} samples"
            hold = amps * frac
            print(f"  {ax}: {hold:.2f} A at rest "
                  f"({100 * frac:.0f}% of {amps:.1f} A)   {note}")
            if hold > rated[ax]:
                print(f"     ^ ABOVE the {rated[ax]} A rating while doing nothing")

        # ── Amplifier error status ──────────────────────────────────────────
        ta = read(g, "MG _TA0")
        print(f"\nAmplifier error status (TA): {ta}")
        try:
            bits = int(float(ta))
            active = [name for bit, name in TA_BITS.items() if bits & (1 << bit)]
            print("  " + ("no faults latched" if not active else ", ".join(active)))
        except ValueError:
            pass
        print("  NOTE: over-current and under-voltage only. There is no motor")
        print("        temperature sensing, so a clean status says nothing about heat.")

        # ── What to do ──────────────────────────────────────────────────────
        print()
        if worst > 1.0:
            print(f"VERDICT: the drive is set above the motor rating "
                  f"({worst:.2f}x), which is {worst**2:.1f}x the rated heating.")
            print("         Expected: AG=1 (1.0 A) on A/B, AG=3 (3.0 A) on C, which")
            print("         enable_axes() applies from AXIS_AMP_GAIN. If this differs,")
            print("         the axes were not enabled by the app this power cycle.")
            print("         Do NOT issue BN unless you mean to burn it to EEPROM.")
        elif worst > 0:
            print(f"VERDICT: the drive is within the motor rating ({worst:.2f}x).")
            print("         Warm-at-standstill is then normal stepper behaviour.")
            print("         If it is too hot to keep a hand on, the next lever is LC")
            print("         (lower holding current), or disabling idle axes.")
        return 0
    finally:
        try:
            g.GClose()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
