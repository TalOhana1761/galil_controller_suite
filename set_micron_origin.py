"""
Declare the stage's TRUE ZERO - the position the micron display is measured from.

This is deliberately not a button in the GUI. The ZERO X / ZERO Y / ZERO Z
buttons are a working zero for the counts column and get pressed whenever it is
convenient; the micron origin is the reference every micron reading on screen is
measured against, and moving it by accident would silently change the meaning of
every position shown.

Park the axes where the rig's zero is, then run this. Nothing moves: it only
records where the axes are.

    python set_micron_origin.py                 # all three axes
    python set_micron_origin.py A C             # just these
    python set_micron_origin.py --show          # print the current origins

It reads the encoder over the controller connection, so the GUI must be closed
first - it holds COM4. (There used to be a --from-file mode that read the
positions the GUI's poll loop had written to config/last_positions.json; that
file was dropped on 2026-09-23.)
"""
import argparse
import sys

from utils.zero_offsets import ZeroOffsets, AXES

CONTROLLER_ADDRESS = "COM4"


def positions_from_controller(address: str) -> dict:
    from controller.galil_controller import GalilController
    c = GalilController()
    try:
        c.connect(address)
        return {ax: float(c.send(f"MG _TP{ax}").strip()) for ax in AXES}
    finally:
        try:
            c.disconnect()
        except Exception:
            pass


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("axes", nargs="*", default=None,
                   help="axes to set (default: all three)")
    p.add_argument("--show", action="store_true", help="print the origins and exit")
    p.add_argument("--address", default=CONTROLLER_ADDRESS)
    args = p.parse_args()

    zeros = ZeroOffsets()

    if args.show:
        print(f"{'axis':>5} | {'micron origin':>15} | {'counts zero':>13}")
        print("-" * 40)
        for ax in AXES:
            print(f"{ax:>5} | {zeros.get_um_origin(ax):>15.0f} | {zeros.get(ax):>13.0f}")
        return 0

    wanted = [a.upper() for a in (args.axes or AXES)]
    bad = [a for a in wanted if a not in AXES]
    if bad:
        print(f"Unknown axis/axes: {bad}. Expected any of {list(AXES)}.")
        return 1

    try:
        positions = positions_from_controller(args.address)
        source = f"the controller on {args.address}"
    except Exception as e:
        print(f"Could not read positions: {e}")
        print("If the GUI is running it holds the port - close it and retry.")
        return 1

    print(f"Positions read from {source}:")
    for ax in wanted:
        if ax not in positions:
            print(f"  {ax}: no reading, skipped")
            continue
        old = zeros.get_um_origin(ax)
        zeros.set_um_origin(ax, positions[ax])
        print(f"  {ax}: micron origin {old:.0f} -> {positions[ax]:.0f} counts")

    print("\nThe micron column now reads 0 at this position. Nothing moved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
