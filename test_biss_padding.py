"""
Determine what the SS command's n3 (zero padding) argument does to the reading.

NOTHING MOVES. The axes are left in MO for the whole run. Each axis is parked, so
its true position is constant; only the controller's interpretation changes. Any
change in _TP between n3 values is therefore purely a decoding difference.

Why this matters. Per the Galil SS documentation, n1 is the TOTAL bit count and
n3 is how many of those are zero padding, so position bits = n1 - n3. Galil's own
example is explicit:

    '24 total bits, 22 position data, 2 zero padding bits
    SSA= 1,24,0,2<9

Our LAK encoders send a 32-bit data word (part number LAK14-710A-RO, ordering key
field 5 = "1" = 32 bit). So the correct configuration is 32 POSITION bits. With
n1=33 that means n3=1, not n3=0 - and n1=33 is the only width whose CRC validates,
because the 33rd bit is the BiSS C CDS bit.

If padding is stripped from the LSB end, each increment of n3 halves the reported
position. That would make the previously used "1,33,0,0<25" report exactly DOUBLE
the true position, which is the size of the discrepancy seen on axis C.

This script sweeps n3 = 0..7 at n1=33 on each axis and prints the reported
position and the ratio against n3=0, which answers the question directly.

Run directly against real hardware (not part of the pytest suite):
    python test_biss_padding.py            # all three axes
    python test_biss_padding.py C
    python test_biss_padding.py ABC --restore 1,33,0,1<9

Results are written to test_biss_padding.log.
"""
import argparse
import sys
import time

import gclib

from controller.galil_controller import GalilController
from test_axis_encoder import ENCODER_COUNTS_PER_UM, blocking_bits, describe_ss
from utils.logger_setup import setup_logger

CONTROLLER_ADDRESS = "COM4"
N1            = 33      # total bits; the only width whose CRC validates
N3_RANGE      = range(0, 8)
CLOCK_DIVIDER = 9       # f = 10/(9+1) = 1 MHz; Galil's recommended starting point
SETTLE_S      = 0.15

logger = setup_logger("test_biss_padding.log")


def emit(line: str = "", level: str = "info"):
    print(line)
    getattr(logger, level)(line if line else " ")


def read_int(controller: GalilController, cmd: str):
    try:
        return int(float(controller.send(cmd).strip()))
    except (gclib.GclibError, ValueError):
        return None


def sweep_axis(controller: GalilController, axis: str) -> list[dict]:
    emit()
    emit(f"=== Axis {axis} ===")
    controller.send(f"MO{axis}")      # axis must be in MO before SS
    controller.send(f"SY{axis}=0")    # WARN/ERROR active low, per the LAK

    rows = []
    for n3 in N3_RANGE:
        cmd = f"SS{axis}=1,{N1},0,{n3}<{CLOCK_DIVIDER}"
        try:
            controller.send(cmd)
        except gclib.GclibError as e:
            emit(f"[FAIL] n3={n3}  {cmd} rejected: {e}", "error")
            rows.append({"n3": n3, "pos": None, "ss": None})
            continue

        time.sleep(SETTLE_S)
        bits = read_int(controller, f"MG _SS{axis}0")
        pos = read_int(controller, f"MG _TP{axis}")
        init = read_int(controller, f"MG _SS{axis}1")
        rows.append({"n3": n3, "pos": pos, "ss": bits, "init": init,
                     "bits_of_position": N1 - n3})

        tag = "OK  " if (bits == 0) else ("WARN" if bits is not None
                                          and not blocking_bits(bits) else "FAIL")
        emit(f"[{tag}] n3={n3} ({N1 - n3} position bits)  _TP{axis}={pos}  "
             f"_SS{axis}1={init}  {describe_ss(axis, bits) if bits is not None else 'status unreadable'}",
             {"OK  ": "info", "WARN": "warning"}.get(tag, "error"))
    return rows


def analyse(axis: str, rows: list[dict]):
    usable = [r for r in rows if r["pos"] not in (None, 0)]
    if not usable:
        emit(f"Axis {axis}: no usable readings.", "error")
        return

    base = next((r for r in usable if r["n3"] == 0), usable[0])
    emit()
    emit(f"Axis {axis} — reported position vs n3 (nothing moved, so all of this "
         f"is decoding)")
    emit(f"{'n3':>3} | {'pos bits':>8} | {'_TP':>12} | {'vs n3=0':>9} | "
         f"{'as mm':>10} | {'_SS0':>5}")
    emit("-" * 62)
    for r in rows:
        if r["pos"] is None:
            emit(f"{r['n3']:>3} | {'-':>8} | {'rejected':>12} | {'-':>9} | "
                 f"{'-':>10} | {'-':>5}")
            continue
        ratio = r["pos"] / base["pos"] if base["pos"] else float("nan")
        mm = r["pos"] / ENCODER_COUNTS_PER_UM / 1000.0
        emit(f"{r['n3']:>3} | {N1 - r['n3']:>8} | {r['pos']:>12} | "
             f"{ratio:>9.4f} | {mm:>10.3f} | {r['ss']:>5}")

    # Does each increment of n3 halve the reading?
    halving = []
    for a, b in zip(usable, usable[1:]):
        if a["pos"] and b["n3"] == a["n3"] + 1:
            halving.append(b["pos"] / a["pos"])

    emit()
    if halving and all(abs(h - 0.5) < 0.01 for h in halving):
        emit(f"[FOUND] Each increment of n3 HALVES the reported position.",
             "warning")
        emit("        So n3 strips padding bits from the LSB end, and the old")
        emit("        '1,33,0,0<25' was reporting exactly DOUBLE the true position.")
        emit("        n3=1 (32 position bits) is correct for this 32-bit encoder.")
        emit("        IMPORTANT: axes A and B were verified under n3=0, so their")
        emit("        scale results are suspect and must be re-measured.", "warning")
    elif halving and all(abs(h - 1.0) < 0.001 for h in halving):
        emit("[FOUND] n3 does NOT change the reported position.")
        emit("        Padding is not being stripped from the value, so n3 cannot")
        emit("        explain the 2x on axis C. Look at the drivetrain instead:")
        emit("        the Boomerax Z stage's 2:1 gear option, or drive microstepping.")
    else:
        emit(f"[INFO ] n3 changes the reading, but not by a clean factor of two.")
        emit(f"        Ratios between consecutive n3: "
             f"{', '.join(f'{h:.4f}' for h in halving)}")
        emit("        Read the table above before concluding anything.")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("axes", nargs="?", default="ABC",
                   help="axes to sweep (default: ABC)")
    p.add_argument("--restore", default=f"1,{N1},0,1<{CLOCK_DIVIDER}",
                   help="SS configuration to leave the axes in when done")
    p.add_argument("--address", default=CONTROLLER_ADDRESS)
    args = p.parse_args()
    axes = args.axes.upper()

    emit("=== BiSS zero-padding (n3) sweep ===")
    emit("Nothing moves: axes stay in MO throughout.")
    emit(f"n1={N1} total bits, n3 swept {N3_RANGE.start}..{N3_RANGE.stop - 1}, "
         f"clock divider {CLOCK_DIVIDER} ({10 / (CLOCK_DIVIDER + 1):.2f} MHz)")

    controller = GalilController()
    try:
        emit(f"Connecting to {args.address}...")
        info = controller.connect(args.address)
        emit(f"Connected: {info.model} Rev {info.revision}, Serial: {info.serial}")

        results = {ax: sweep_axis(controller, ax) for ax in axes}

        emit()
        emit("=== Summary ===")
        for ax in axes:
            analyse(ax, results[ax])

    except Exception as e:
        emit(f"Fatal error: {e}", "error")
        sys.exit(1)

    finally:
        try:
            for ax in axes:
                controller.send_safe(f"SS{ax}={args.restore}")
            controller.send_safe(f"MO{axes}")
            controller.disconnect()
            emit()
            emit(f"Axes restored to SS={args.restore}, left in MO. Disconnected.")
        except Exception as e:
            logger.warning(f"Error during cleanup: {e}")


if __name__ == "__main__":
    main()
