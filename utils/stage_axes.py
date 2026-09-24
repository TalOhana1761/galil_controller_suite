"""
Stage axis names and directions - the one place that knows how the operator's
axes relate to the controller's.

This lived in ui/panels/jog_panel.py until 2026-09-24. It moved here once a
third caller needed it (the soft travel limits, and check_limits.py), because
the alternative was a second copy of the sign map - and a sign map that
disagreed with itself would silently invert a limit or a jog.
"""

AXES = ("A", "B", "C")

# Controller axis -> the name on the operator's controls. The controller knows
# them as A/B/C and every operand, log line and hardware script uses those.
AXIS_NAME = {"A": "X", "B": "Y", "C": "Z"}

# Stage direction vs controller direction. The manipulator's physical +Y and +Z
# are the controller's -B and -C: commanding +C drove the stage the way the
# operator calls DOWN. The reversal is applied at the UI boundary, and not at the
# controller or in the wiring, because the loop is closed on an ABSOLUTE encoder
# and nothing can flip that encoder's counting direction to match: MT=2.5 and a
# motor phase swap both reverse the motor alone, leaving _TP counting the old
# way, which turns every correction in move_microns() into positive feedback on
# axes where LD=3 makes software the only limit protection. CE reverses
# quadrature encoders only, and SS (BiSS) has no direction argument at all - its
# n1 sign picks absolute vs continuous rollover, not direction.
#
# So everything the operator sees or presses is in STAGE terms - the buttons, the
# keys, the two position columns, the saved-position list, the soft travel limits
# - and everything the controller sees is converted at the boundary. _TP,
# zero_offsets.json, positions.json and every log line stay in CONTROLLER terms,
# untouched: they are places, not directions, and a place has no sign to flip.
AXIS_SIGN = {"A": +1, "B": -1, "C": -1}


def to_controller(axis: str, value: float) -> float:
    """A signed STAGE quantity - a distance or a speed - in controller terms."""
    return value * AXIS_SIGN[axis]


def to_stage(axis: str, value: float) -> float:
    """A signed CONTROLLER quantity in stage terms.

    The same multiply, because the sign is its own inverse. It is a separate
    name so the call site says which way the conversion goes.
    """
    return value * AXIS_SIGN[axis]


def stage_end(axis: str, controller_direction: int) -> str:
    """The stage end a controller direction points at: ("C", +1) -> "Z-"."""
    return AXIS_NAME[axis] + ("+" if to_stage(axis, controller_direction) > 0 else "-")
