"""
Per-axis display offsets for the jog panel, persisted to config/zero_offsets.json.

There are TWO independent origins per axis, and they are not the same thing:

  counts_zero   What the ZERO X / ZERO Y / ZERO Z buttons set. A working zero,
                re-set whenever it is convenient, applied to the COUNTS column
                only. Press it wherever you are and the counts read 0 from there.

  um_origin     The "true zero" of the stage. Set ONCE, at the position the rig
                calls zero, and left alone. The MICRON column is measured from
                it, so microns always answer "where is the stage", regardless of
                how often the counts column gets re-zeroed.

Keeping them apart is the point. If one offset drove both columns, re-zeroing the
counts mid-job would silently move the micron origin too, and the one reading
that is supposed to be stable would not be.

Neither touches the controller and neither moves anything. _TP is left alone: it
is fed from the BiSS frame and IS an absolute coordinate in the encoder's 1200 mm
PRC code space (axis A sits around 3,226,657 counts), so every log, test and
hardware script stays comparable with the encoder's own numbers. The offsets
exist so the SCREEN reads something meaningful.

Why not the controller's DP command, which the old "ZERO ALL (DP=0)" buttons used:

  1. It would not persist. DP lives in controller RAM, does not survive a power
     cycle, and enable_axes() does not restore it - so keeping a zero across
     restarts needs a file either way, and then there would be two mechanisms to
     hold in agreement instead of one.
  2. DP shifts _TP itself, so it cannot give two different origins to two
     columns, and it would break the correspondence with the logs.

move_microns() targets _TP + microns x ENCODER_COUNTS_PER_UM, so relative motion
and the limit logic are unaffected by either offset.
"""
import json
import os

from loguru import logger
from utils.app_paths import config_path

DEFAULT_PATH = config_path("zero_offsets.json")

AXES = ("A", "B", "C")


class ZeroOffsets:
    """Two display origins per axis, in encoder counts."""

    def __init__(self, path: str = DEFAULT_PATH):
        self.path = path
        self._counts_zero: dict[str, float] = {ax: 0.0 for ax in AXES}
        self._um_origin: dict[str, float] = {ax: 0.0 for ax in AXES}
        self.load()

    # ── Persistence ─────────────────────────────────────────────────────────

    def load(self):
        try:
            with open(self.path, "r") as f:
                data = json.load(f)
        except FileNotFoundError:
            logger.info("No zero offsets file yet; all axes start unzeroed")
            return
        except Exception as e:
            # A corrupt file must not stop the app starting: an offset of zero
            # just means the display shows the raw encoder reading.
            logger.warning(f"Zero offsets unreadable ({e}); using 0 for all axes")
            return

        if "counts_zero" in data or "um_origin" in data:
            for ax in AXES:
                self._counts_zero[ax] = float(data.get("counts_zero", {}).get(ax, 0.0))
                self._um_origin[ax] = float(data.get("um_origin", {}).get(ax, 0.0))
        else:
            # Old flat format {"A": 123.0, ...}, written when one offset drove
            # the counts column alone. Those values were recorded at the position
            # the rig calls zero, so they seed BOTH origins - which is exactly
            # the one-time micron zero, without needing the stage to be parked
            # anywhere in particular to migrate.
            for ax in AXES:
                v = float(data.get(ax, 0.0))
                self._counts_zero[ax] = v
                self._um_origin[ax] = v
            logger.info("Migrated flat zero offsets: existing values now seed "
                        "both the counts zero and the micron origin")
            self.save()

        logger.info(f"Zero offsets loaded - counts {self._counts_zero}, "
                    f"micron origin {self._um_origin}")

    def save(self):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(self.path, "w") as f:
                json.dump({"counts_zero": self._counts_zero,
                           "um_origin": self._um_origin}, f, indent=2)
        except Exception as e:
            logger.error(f"Could not save zero offsets: {e}")

    # ── Counts zero: the ZERO buttons ───────────────────────────────────────

    def set_zero(self, axis: str, raw_counts: float):
        """Make `raw_counts` read 0 in the COUNTS column. Microns are untouched."""
        axis = axis.upper()
        self._counts_zero[axis] = float(raw_counts)
        self.save()
        logger.info(f"Zeroed {axis} counts display at {raw_counts:.0f}")

    def get(self, axis: str) -> float:
        return self._counts_zero.get(axis.upper(), 0.0)

    def apply(self, axis: str, raw_counts: float) -> float:
        """Raw encoder counts -> the COUNTS column."""
        return float(raw_counts) - self.get(axis)

    def clear(self, axis: str):
        axis = axis.upper()
        self._counts_zero[axis] = 0.0
        self.save()
        logger.info(f"Cleared counts zero for {axis}")

    # ── Micron origin: the true zero, set once ──────────────────────────────

    def set_um_origin(self, axis: str, raw_counts: float):
        """Declare `raw_counts` to be the stage's true zero for this axis.

        Deliberately not on a button: this is the reference every micron reading
        is measured from, and re-setting it by accident would silently move the
        meaning of every position on screen.
        """
        axis = axis.upper()
        self._um_origin[axis] = float(raw_counts)
        self.save()
        logger.info(f"Micron origin for {axis} set at {raw_counts:.0f} counts")

    def get_um_origin(self, axis: str) -> float:
        return self._um_origin.get(axis.upper(), 0.0)

    def apply_um(self, axis: str, raw_counts: float) -> float:
        """Raw encoder counts -> counts relative to the true zero.

        Still counts; the caller divides by the encoder scale to get microns.
        """
        return float(raw_counts) - self.get_um_origin(axis)
