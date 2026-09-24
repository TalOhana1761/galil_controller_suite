"""
Pydantic data models for the Galil DMC-4133 controller suite.
Stepper-motor focused: all position values are command counts and enabled state is reported per axis.
"""
from __future__ import annotations
import os

from pydantic import BaseModel, Field
from typing import Optional
from utils.app_paths import config_path

SETTINGS_PATH = config_path("settings.json")


class AxisStatus(BaseModel):
    """Live status snapshot for one axis.

    GalilController.poll_all_axes() only queries _TPA and _LFA each cycle (to keep
    the 100 ms poll fast), so only pos, fwd_limit, rev_limit and last_direction are
    live values for real hardware; vel, stop_code, servo_on and home_switch stay at
    their defaults below unless set elsewhere. MockGalilController populates those.

    pos_ref, err and torque were removed on 2026-09-23: nothing read them. Only the
    mock ever set them, and no panel, worker or test consumed them. vel/stop_code/
    servo_on stay because the test suite asserts on them.
    """
    pos: float = 0.0           # _TPA — actual axis position (counts)
    vel: float = 0.0           # _TVA — actual velocity (counts/sec) — not polled
    stop_code: int = 0         # _SCA — 0=off 1=ST 2=AB 3=pos_err 6=fwd_lim 7=rev_lim 10=done — not polled
    servo_on: bool = False     # _MOA == 0  (0 = enabled, 1 = disabled in Galil) — not polled
    fwd_limit: bool = False    # _LFA == 0  (0 = ACTIVE, 1 = clear — inverted!)
    rev_limit: bool = False    # _LRA == 0 — read only where LIMIT_WIRED says a switch exists
    home_switch: bool = False  # _HMA == 0 — not polled
    last_direction: int = 0    # software-tracked: +1=forward, -1=reverse, 0=unknown
    # _SSm0, the BiSS status word: bit 0 timeout, 1 CRC invalid, 2 warning,
    # 3 error. 0 means the encoder is functioning properly. None means nobody
    # asked this cycle - it is NOT polled by default, because one more read per
    # axis is ~16 ms and the poll cycle already sets the jog overshoot. The
    # worker reads it only for axes in PollingWorker.watch_encoder.
    enc_status: int | None = None


class ControllerInfo(BaseModel):
    """Connection and identity data for the controller."""
    address: str = ""
    model: str = ""
    revision: str = ""
    serial: str = ""
    firmware: str = ""
    connected: bool = False
    latency_ms: float = 0.0


class AxisSettings(BaseModel):
    """Per-axis motion and display parameters (persisted to settings.json)."""
    speed: float = 5000.0         # SP — counts/sec
    accel: float = 50000.0        # AC — accel (counts/sec²)
    decel: float = 50000.0        # DC — decel (counts/sec²)
    counts_per_um: float = 6.4  # MOTOR counts per micron, for commanded moves

    # Removed 2026-09-23, all orphaned when the settings panel was deleted:
    #   error_limit     ER was only ever sent by that panel's APPLY, which had
    #                   been raising AttributeError, so it never reached the
    #                   controller at all. Re-add it to enable_axes() alongside
    #                   SP/AC/DC if the error limit is genuinely wanted - note
    #                   that on a stepper it only means anything under SPM (YS),
    #                   which nothing enables.
    #   fwd/rev/home_limit_type   NO/NC wiring, read by nothing. _LF polarity is
    #                   hardcoded (0 = ACTIVE) in poll_all_axes().
    travel_mm: float = 20.0           # how far this axis is allowed to move
    encoder_length_mm: float = 45.0   # physical length of the LAK scale tape

    # Soft travel limits, in STAGE MICRONS from the micron origin - the same
    # number the position display shows, so what the operator types is what they
    # read. Reaching one raises the matching limit flag exactly as a switch
    # would; see utils/soft_limits.py. None means no bound that way.
    #
    # These are ABSOLUTE POSITIONS, unlike travel_mm, which bounds the length of
    # a single commanded move. Both exist and neither replaces the other.
    soft_limit_neg_um: float | None = None
    soft_limit_pos_um: float | None = None

    # Three different lengths here, none of them interchangeable:
    #
    #   travel_mm          how far the stage may move (20 mm). This is the soft
    #                      limit that matters, because enable_axes() issues LD=3,
    #                      which disables the hardware limit switches inside the
    #                      controller — software is the only protection left.
    #   encoder_length_mm  physical length of the scale tape (45 mm), deliberately
    #                      longer than the travel so the head never runs off it.
    #   the PRC code space the absolute Pseudo Random Code is encoded over 1200 mm
    #                      (ordering key field "Encoding of position: 0"). A short
    #                      tape is a SECTION cut from that code space, so _TP says
    #                      where the head sits in the 1200 mm coding scheme, not
    #                      where it sits on this strip. Axis A reads ~3,226,000
    #                      counts = 252 mm on a 45 mm tape for exactly that
    #                      reason. It is a legal PRC coordinate, not an error.
    #
    # Consequence: neither length anchors _TP to a stage coordinate. Converting an
    # absolute reading into stage position needs this tape's own origin, found by
    # traversing the travel and recording min/max _TP per axis. Nothing records
    # that yet — see the encoder note in CLAUDE.md.


class AppSettings(BaseModel):
    """Global application settings (persisted to settings.json)."""
    axis_a: AxisSettings = Field(default_factory=AxisSettings)
    axis_b: AxisSettings = Field(default_factory=AxisSettings)
    axis_c: AxisSettings = Field(default_factory=AxisSettings)
    # Removed 2026-09-23 together with the settings panel, which was the only
    # thing that wrote them and nothing ever read:
    #   poll_interval_ms        PollingWorker.POLL_MS is a class constant
    #   position_history_depth  belonged to the deleted dashboard chart
    #   log_file                run.py calls setup_logger() with its own default
    #   display_units           the jog panel always shows both columns
    #   auto_reconnect          reconnect policy is constants in PollingWorker
    #
    # The address of the LAST SUCCESSFUL connection, written by the connection
    # panel when a connect succeeds. Empty means no connection has ever been
    # made on this install, and the connection panel then presents nothing
    # rather than guessing a port.
    last_address: str = ""
    serial_baud: int = 0   # 0 = leave it to gclib's default; see check_link_speed.py
    encoder_counts_per_um: float = 12.8  # global encoder resolution (counts/µm) used to display encoder positions in microns

    def axis_settings(self, axis: str) -> AxisSettings:
        return {"A": self.axis_a, "B": self.axis_b, "C": self.axis_c}[axis.upper()]

    def save_to_file(self, path: str | None = None) -> str:
        """Write these settings to settings.json. Returns the path written."""
        import json
        import os

        path = os.path.abspath(path or SETTINGS_PATH)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.model_dump(), f, indent=2)
        return path


class PositionSlot(BaseModel):
    """A named position memory slot."""
    name: str
    pos_a: float = 0.0
    pos_b: float = 0.0
    pos_c: float = 0.0
    timestamp: str = ""
