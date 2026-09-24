"""
Live view of the limit switches. Press one and watch its row change.

READ ONLY. Nothing moves, nothing is written to the controller, and the link is
closed without the ST + MO that GalilController.disconnect() sends. Safe to run
whenever the GUI is closed - it holds COM4.

Each axis has ONE switch, tripped mechanically at EITHER end of travel. It
cannot say which end, so the software decides from the LAST COMMANDED DIRECTION:
drive - into it and the reverse flag rises, drive + into it and the forward one
does. That is why the table shows the last direction - with the switch pressed,
it is the only thing deciding which flag you get.

Why this exists: this rig uses NORMALLY CLOSED switches, and wiring one up is a
two-part job. Getting the electrical polarity right is only half of it; the other
half is telling the software the switch exists AND which terminal it is on, and
nothing else will tell you if you get it wrong.

  - CN=1 (limit inputs ACTIVE HIGH) is what NC needs. At rest an NC switch
    grounds its input and reads clear; pressing it opens the circuit and reads
    ACTIVE. So does a BROKEN WIRE, which is the whole point of NC - the failure
    looks like a limit and stops the axis rather than silently removing your
    end-stop protection. enable_axes() sets this; the controller's own default
    is CN=-1, which inverts every reading.
  - An input with NOTHING on it is open too, so under CN=1 it reads ACTIVE as
    well, and the controller cannot tell it from a tripped switch. That is what
    LIMIT_INPUT in controller/galil_controller.py is for: it names the DMC input
    each axis's one switch is wired to, and an axis not listed is ignored.

That same asymmetry is what lets this script WORK OUT where a switch is wired:
at rest, the input reading CLEAR is the one with the switch on it, and the open
one reads ACTIVE. So the "looks like" column tells you what to put in
LIMIT_INPUT, and flags a MISMATCH if the declaration disagrees - which matters,
because declaring the wrong terminal leaves the axis reading permanently at a
limit it never reached.

    python check_limits.py              # live, Ctrl-C to stop
    python check_limits.py --once       # one snapshot and exit

The jog panel reverses B and C, so the stage end shown here is not simply the
controller's sign - it comes from AXIS_SIGN.
"""
import argparse
import sys
import time

import controller.galil_controller as gc

CONTROLLER_ADDRESS = "COM4"
# AXIS_NAME and stage_end carry the Y/Z reversal, so the stage end shown here is
# not simply the controller's sign. They used to be imported from the jog panel
# behind a try/except, because that module pulls in PyQt5; utils.stage_axes is
# pure Python and needs no guard.
from utils.stage_axes import AXIS_NAME as STAGE, stage_end


def which_terminal(lf: int, lr: int):
    """Which terminal has a switch on it, judged from the two raw readings.

    Under CN=1 an UNWIRED input is open and reads ACTIVE (0); a normally-closed
    switch at rest grounds its input and reads clear (1). So the input reading
    CLEAR is the wired one. Returns None when it cannot tell - both open means
    either no switch at all, or a switch that is pressed right now.
    """
    clear = [t for t, v in (("FWD", lf), ("REV", lr)) if v == 1]
    return clear[0] if len(clear) == 1 else None


def snapshot(c) -> list[tuple]:
    """One row per AXIS, not per input - there is only one switch each.

    BOTH raw inputs are read here, unlike the poll, so a switch wired to the
    terminal LIMIT_INPUT does not name can be spotted and reported.
    """
    rows = []
    for ax in ("A", "B", "C"):
        terminal = gc.LIMIT_INPUT.get(ax)
        lf = int(float(c.send(f"MG _LF{ax}").strip()))
        lr = int(float(c.send(f"MG _LR{ax}").strip()))
        rows.append((ax, terminal, lf, lr, which_terminal(lf, lr),
                     gc.last_dir.get(ax, 0),
                     c.limit_active(ax, +1), c.limit_active(ax, -1)))
    return rows


def render(rows) -> str:
    out = [f"{'axis':>5} {'_LF':>4} {'_LR':>4} {'declared':>9} {'looks like':>10} "
           f"{'last dir':>9} {'at end':>7}  note",
           "-" * 88]
    for ax, terminal, lf, lr, looks_like, ldir, fwd, rev in rows:
        end = stage_end(ax, +1) if fwd else stage_end(ax, -1) if rev else "-"
        if terminal is None:
            note = f"not declared - {STAGE[ax]} has no end stop"
            if looks_like:
                note = (f"NOT DECLARED, but a switch looks wired to {looks_like}"
                        f" - add {ax!r}: {looks_like!r} to LIMIT_INPUT")
        elif looks_like and looks_like != terminal:
            note = (f"MISMATCH: declared {terminal}, switch looks wired to "
                    f"{looks_like} - {STAGE[ax]} will read permanently limited")
        elif fwd or rev:
            note = f"*** AT THE {end} END ***"
        else:
            note = ""
        out.append(f"{ax:>5} {lf:>4} {lr:>4} {str(terminal):>9} "
                   f"{str(looks_like):>10} {ldir:>+9d} {end:>7}  {note}")
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--once", action="store_true", help="one snapshot, then exit")
    p.add_argument("--address", default=CONTROLLER_ADDRESS)
    args = p.parse_args()

    c = gc.GalilController()
    try:
        c.connect(args.address)
    except Exception as e:
        print(f"Could not connect: {e}")
        print("If the GUI is running it holds the port - close it and retry.")
        print("If the port does not exist at all, the controller is powered off.")
        return 1

    try:
        cn = float(c.send("MG _CN0").strip())
        print(f"_CN0 = {cn:+.0f}  ", end="")
        if cn == 1:
            print("limit inputs ACTIVE HIGH - correct for normally closed switches")
        else:
            print("limit inputs ACTIVE LOW - WRONG for normally closed switches.")
            print("      Every reading below is inverted, including the")
            print("      'looks like' column. enable_axes() sets CN 1; connect")
            print("      through the GUI, or press Enable Motors, to apply it.")
        print(f"LIMIT_INPUT = {gc.LIMIT_INPUT or 'nothing declared'}")
        print("raw: 1 = clear, 0 = ACTIVE (an open input reads ACTIVE under CN=1)")
        print()

        if args.once:
            print(render(snapshot(c)))
            return 0

        print("Press a switch to watch its row change. Ctrl-C to stop.\n")
        previous = None
        while True:
            rows = snapshot(c)
            if rows != previous:
                print(f"\n[{time.strftime('%H:%M:%S')}]")
                print(render(rows))
                previous = rows
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nstopped")
        return 0
    finally:
        try:
            c._g.GClose()      # NOT disconnect(): that sends ST + MO
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
