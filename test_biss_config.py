"""
Sweeps the SS command's n1 (data bit count) parameter to find a BiSS frame width
that produces a valid link.

For each axis, issues SS{ax}=1,n,0,0<25 for n = N_MIN..N_MAX and records the
_SS{ax}0 status bitmask after each attempt:
    bit0 = timeout, bit1 = CRC invalid, bit2 = warning bit active, bit3 = error bit active
A value of 0 means the encoder is functioning properly at that n.

Run directly against real hardware (not part of the pytest suite):
    python test_biss_config.py
"""
import sys

import gclib

from controller.galil_controller import GalilController
from utils.logger_setup import setup_logger

CONTROLLER_ADDRESS = "COM4"  # update if your controller enumerates elsewhere
AXES = "ABC"
N_MIN = 24
N_MAX = 38
CLOCK_DIVIDER = 25

logger = setup_logger("test_biss_config.log")


def report(ok: bool, label: str, detail: str = ""):
    tag = "OK  " if ok else "FAIL"
    line = f"[{tag}] {label}" + (f"  -> {detail}" if detail else "")
    print(line)
    (logger.info if ok else logger.error)(line)


def read_ss_status(controller: GalilController, ax: str):
    """Read _SSm0: bit0=timeout, bit1=CRC invalid, bit2=warning, bit3=error. None on query failure."""
    try:
        return int(float(controller.send(f"MG _SS{ax}0")))
    except gclib.GclibError as e:
        print(f"    _SS{ax}0 query failed: {e}")
        logger.error(f"    _SS{ax}0 query failed: {e}")
        return None


def sweep_axis(controller: GalilController, ax: str):
    print(f"\n=== Sweeping n1 for axis {ax} ===")
    controller.send(f"MO{ax}")     # axis must be off before SS/SY can be issued
    controller.send(f"SY{ax}=0")   # BiSS WARN/ERROR bits: active low (per LAK datasheet)
    results = []
    for n in range(N_MIN, N_MAX + 1):
        cmd = f"SS{ax}=1,{n},0,0<{CLOCK_DIVIDER}"
        try:
            controller.send(cmd)
        except gclib.GclibError as e:
            print(f"    {cmd} rejected: {e}")
            logger.error(f"    {cmd} rejected: {e}")
        controller.send("WT 100")
        bits = read_ss_status(controller, ax)
        if bits is None:
            report(False, f"n={n:>2}  {cmd}", "_SS{}0 query failed".format(ax))
        else:
            detail = (f"_SS{ax}0={bits}  timeout={bits & 1}  crc_invalid={(bits >> 1) & 1}  "
                      f"warning={(bits >> 2) & 1}  error={(bits >> 3) & 1}")
            report(bits == 0, f"n={n:>2}  {cmd}", detail)
        results.append((n, bits))
        controller.send("WT 100")  # let the controller settle before the next n
    return results


def main():
    controller = GalilController()
    all_results = {}
    try:
        print(f"Connecting to {CONTROLLER_ADDRESS}...")
        info = controller.connect(CONTROLLER_ADDRESS)
        print(f"Connected: {info.model} Rev {info.revision}, Serial: {info.serial}, Firmware: {info.firmware}")

        for ax in AXES:
            all_results[ax] = sweep_axis(controller, ax)

        print("\n=== Summary: _SS{axis}0 by n1 ===")
        header = "n1  | " + " | ".join(f"axis {ax}" for ax in AXES)
        print(header)
        for n in range(N_MIN, N_MAX + 1):
            row = [f"{n:>2} "]
            for ax in AXES:
                bits = dict(all_results[ax])[n]
                row.append(f"{bits!s:>6}" if bits is not None else "  ERR")
            print(" | ".join(row))

    except Exception as e:
        print(f"Fatal error: {e}")
        logger.error(f"Fatal error: {e}")
        sys.exit(1)

    finally:
        try:
            controller.send_safe(f"MO{AXES}")
            controller.disconnect()
            print("\nDisconnected.")
        except Exception as e:
            print(f"Error during cleanup: {e}")
            logger.warning(f"Error during cleanup: {e}")


if __name__ == "__main__":
    main()
