"""
Unit conversion helpers: encoder counts ↔ microns.
counts_per_um is set per-axis in AxisSettings.
"""


def counts_to_um(counts: float, counts_per_um: float) -> float:
    if counts_per_um == 0:
        return 0.0
    return counts / counts_per_um


def um_to_counts(um: float, counts_per_um: float) -> float:
    return um * counts_per_um


def format_position(counts: float, units: str, counts_per_um: float) -> str:
    if units == "um":
        val = counts_to_um(counts, counts_per_um)
        return f"{val:.3f} µm"
    return f"{counts:.1f} counts"
