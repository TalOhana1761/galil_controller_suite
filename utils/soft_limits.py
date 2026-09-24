"""
Soft travel limits: positions at which the software raises a limit flag itself.

The operator sets a negative and a positive bound per axis, in STAGE MICRONS
measured from the micron origin - the same number the position display shows. If
the axis reaches one, the corresponding limit flag is set exactly as a real
switch would set it, so everything downstream reacts the same way: the jog and
step buttons for that direction grey out, the limit table lights up, the escape
hint names the way out, and PollingWorker._guard_jog_limits() halts a jog that is
still running.

This exists because LD=3 disables the controller's own limit action, and as of
2026-09-24 only axis C has a physical switch at all. X and Y have none, so
software bounds are the ONLY end-stop protection they have.

Three things it is deliberately NOT:

  - NOT a replacement for a switch. A soft limit is only as good as the encoder
    reading and the micron origin behind it; a switch is a physical fact. Set
    soft limits INSIDE the switches, not instead of them.
  - NOT enforced inside GalilController. That class is pure Python with no idea
    of micron origins or the stage/controller reversal, both of which a soft
    limit needs. The flags are merged into the poll result instead, which is the
    one place every consumer already reads.
  - NOT a guard on the commanded distance. AxisSettings.travel_mm still does
    that, and it catches a single runaway move rather than an absolute position.

Directions here are the usual two kinds and must not be conflated:
  neg_um/pos_um and the position are STAGE microns - what the operator sees.
  fwd_limit/rev_limit are CONTROLLER flags. Y and Z are reversed, so the stage's
  negative end is the controller's FORWARD limit on those two.
"""
from loguru import logger

from utils.stage_axes import AXES, stage_end, to_controller, to_stage


def flags_for(stage_um: float, neg_um, pos_um, axis: str) -> tuple[bool, bool]:
    """(fwd_limit, rev_limit) in CONTROLLER terms for one axis.

    Either bound may be None, meaning "no limit that way". The comparison is
    inclusive: reaching exactly the bound counts as being at it.
    """
    at_neg = neg_um is not None and stage_um <= neg_um
    at_pos = pos_um is not None and stage_um >= pos_um

    fwd = rev = False
    for reached, stage_sign in ((at_neg, -1), (at_pos, +1)):
        if not reached:
            continue
        # Which CONTROLLER end is the stage end we just hit?
        if to_controller(axis, stage_sign) > 0:
            fwd = True
        else:
            rev = True
    return fwd, rev


class SoftLimits:
    """Evaluates the configured bounds against a poll result.

    Holds references, not copies, so editing the settings takes effect on the
    next poll with nothing to re-wire. `zeros` is deliberately the SAME
    ZeroOffsets instance the jog panel displays from - two instances would drift
    the moment one of them wrote.
    """

    def __init__(self, settings, zeros):
        self.settings = settings
        self.zeros = zeros
        self._announced: dict[str, tuple[bool, bool]] = {}
        # Axes whose bounds are suspended. A soft limit is computed from _TP, so
        # it is only as meaningful as the encoder reading behind it; while the Z
        # head is deliberately driven OFF the scale tape by the retract, _TP is
        # invalid and a bound evaluated against it would fire on garbage and
        # halt the very move that put it there.
        self._suspended: set = set()

    def _axis_settings(self, axis: str):
        return getattr(self.settings, f"axis_{axis.lower()}")

    def bounds(self, axis: str) -> tuple:
        a = self._axis_settings(axis)
        return a.soft_limit_neg_um, a.soft_limit_pos_um

    def stage_um(self, axis: str, raw_counts: float) -> float:
        """Raw _TP -> the micron reading the operator sees for this axis."""
        cpu = self.settings.encoder_counts_per_um
        if not cpu:
            return 0.0
        return to_stage(axis, self.zeros.apply_um(axis, raw_counts)) / cpu

    def suspend(self, axis: str):
        """Stop applying this axis's bounds until resume() - see _suspended."""
        if axis not in self._suspended:
            self._suspended.add(axis)
            logger.info(f"[{axis}] soft travel limits suspended")

    def resume(self, axis: str):
        if axis in self._suspended:
            self._suspended.discard(axis)
            self._announced.pop(axis, None)
            logger.info(f"[{axis}] soft travel limits back in force")

    def is_suspended(self, axis: str) -> bool:
        return axis in self._suspended

    def flags(self, axis: str, raw_counts: float) -> tuple[bool, bool]:
        if axis in self._suspended:
            return False, False
        neg, pos = self.bounds(axis)
        if neg is None and pos is None:
            return False, False
        return flags_for(self.stage_um(axis, raw_counts), neg, pos, axis)

    def apply(self, status: dict) -> dict:
        """Merge the soft flags into a poll result, in place.

        OR'd with the hardware flags rather than replacing them: a soft limit is
        an ADDITIONAL bound inside the switches, and a real switch must never be
        masked by a software one that has not been reached.
        """
        for axis, st in status.items():
            if axis not in AXES:
                continue
            try:
                fwd, rev = self.flags(axis, st.pos)
            except Exception as e:                      # never let this kill a poll
                logger.warning(f"Soft limit check failed on {axis}: {e}")
                continue
            self._announce(axis, fwd, rev)
            st.fwd_limit = st.fwd_limit or fwd
            st.rev_limit = st.rev_limit or rev
        return status

    def _announce(self, axis: str, fwd: bool, rev: bool):
        """Log each transition once, not every 100 ms poll.

        The cleared state is recorded too, so coming back inside and going out
        again logs a second time rather than being swallowed as unchanged.
        """
        if self._announced.get(axis) == (fwd, rev):
            return
        self._announced[axis] = (fwd, rev)
        if not (fwd or rev):
            logger.info(f"[{axis}] back inside its soft travel limits")
            return
        # fwd/rev are CONTROLLER flags; name the STAGE end the operator sees.
        ends = [stage_end(axis, d) for d, on in ((+1, fwd), (-1, rev)) if on]
        neg, pos = self.bounds(axis)
        logger.warning(
            f"[{axis}] soft travel limit reached at the {' and '.join(ends)} "
            f"bound (limits {neg}, {pos} um); that direction is now blocked"
        )
