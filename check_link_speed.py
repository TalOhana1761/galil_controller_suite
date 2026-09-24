"""
Measure the actual serial throughput to the controller, and infer the baud rate.

The app opens a bare `GOpen("COM4")` with no options, so the link runs at
whatever gclib defaults to - which is not written down anywhere in this project.
Every command round-trips in about 16 ms, which is slow for short ASCII
exchanges: at 115200 baud a 20-character exchange should take well under 2 ms.
That gap is the reason to check.

Why it matters beyond speed: the desynchronisation seen on 2026-09-22 - where a
timed-out command's late reply shifts every subsequent read by one - gets more
likely the longer each exchange occupies the link.

Method: send `MG "AAA..."` with response lengths from short to long, time each,
and fit time against bytes. The SLOPE is the transfer rate; the INTERCEPT is the
fixed per-command overhead (driver, USB/serial latency timer, controller parse),
which no baud change will remove. Separating the two is the point - if the
intercept dominates, raising the baud buys little.

    python check_link_speed.py                 # measure at the current default
    python check_link_speed.py --baud 115200   # try opening at a specific baud
    python check_link_speed.py --address COM4

Nothing moves. It only sends MG commands.
"""
import argparse
import statistics
import sys
import time

import gclib

CONTROLLER_ADDRESS = "COM4"
LENGTHS = (1, 8, 16, 32, 48, 64)    # response bytes; Galil lines cap near 80
REPEATS = 15
COMMON_BAUDS = (9600, 19200, 38400, 57600, 115200, 230400)


def measure(g, lengths=LENGTHS, repeats=REPEATS):
    """Median round-trip time for each response length, in milliseconds."""
    out = []
    for n in lengths:
        cmd = 'MG "' + "A" * n + '"'
        g.GCommand(cmd)                      # warm up, discard
        times = []
        for _ in range(repeats):
            t0 = time.perf_counter()
            g.GCommand(cmd)
            times.append((time.perf_counter() - t0) * 1000.0)
        # Median, not mean: the occasional OS scheduling stall would drag a mean.
        out.append((len(cmd) + n, statistics.median(times)))
    return out


def fit(points):
    """Least-squares slope and intercept of ms against bytes."""
    n = len(points)
    sx = sum(b for b, _ in points)
    sy = sum(t for _, t in points)
    sxx = sum(b * b for b, _ in points)
    sxy = sum(b * t for b, t in points)
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0, sy / n
    slope = (n * sxy - sx * sy) / denom
    return slope, (sy - slope * sx) / n


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--address", default=CONTROLLER_ADDRESS)
    p.add_argument("--baud", type=int, default=None,
                   help="open at this baud instead of the gclib default")
    args = p.parse_args()

    address = args.address
    if args.baud:
        address = f"{args.address} --baud {args.baud}"

    g = gclib.py()
    print(f"Opening {address!r} ...")
    try:
        g.GOpen(address)
    except Exception as e:
        print(f"  failed: {e}")
        if args.baud:
            print("  The controller is probably not set to that baud. Its serial")
            print("  rate is a controller-side setting, so both ends must agree.")
        return 1

    try:
        print(f"  {g.GInfo().strip()}")
        print()
        print(f"{'bytes':>7} | {'median ms':>10}")
        print("-" * 21)
        points = measure(g)
        for nbytes, ms in points:
            print(f"{nbytes:>7} | {ms:>10.2f}")

        slope_ms_per_byte, intercept_ms = fit(points)
        print()
        print(f"fixed overhead : {intercept_ms:6.2f} ms per command")
        print(f"transfer       : {slope_ms_per_byte:6.4f} ms per byte")

        if slope_ms_per_byte <= 0:
            print()
            print("Transfer time is not measurable above the overhead - every")
            print("exchange is dominated by fixed per-command cost, so the baud")
            print("is not what limits this link. Raising it would change little.")
            return 0

        bytes_per_s = 1000.0 / slope_ms_per_byte
        inferred = bytes_per_s * 10          # 8N1: 10 bits on the wire per byte
        nearest = min(COMMON_BAUDS, key=lambda b: abs(b - inferred))
        print(f"               = {bytes_per_s:7.0f} bytes/s  ->  ~{inferred:,.0f} baud")
        print(f"nearest standard rate: {nearest:,}"
              f"  ({100 * abs(inferred - nearest) / nearest:.0f}% away)")

        print()
        share = intercept_ms / (intercept_ms + slope_ms_per_byte * 20)
        print(f"On a typical 20-byte exchange, {100 * share:.0f}% of the time is "
              f"fixed overhead.")
        if share > 0.8:
            print("Raising the baud would barely help - the overhead dominates.")
            print("Ethernet would, because it removes the per-command overhead.")
        else:
            print("Raising the baud would help materially if the controller")
            print("supports a higher rate.")
        return 0
    finally:
        try:
            g.GClose()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
