"""
Pydantic data models for the Galil DMC-4133 controller suite.
Stepper-motor focused: all position values are command counts and enabled state is reported per axis.
"""
from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional


class AxisStatus(BaseModel):
    """Live status snapshot for one axis."""
    pos: float = 0.0           # _TPA — actual axis position (counts)
    pos_ref: float = 0.0       # _RPA — commanded (reference) position (counts)
    vel: float = 0.0           # _TVA — actual velocity (counts/sec)
    err: float = 0.0           # _TEA — position error = actual − commanded (counts)
    torque: float = 0.0        # _TTA — DAC output (−10 to +10 V represented as %)
    stop_code: int = 0         # _SCA — 0=off 1=ST 2=AB 3=pos_err 6=fwd_lim 7=rev_lim 10=done
    servo_on: bool = False     # _MOA == 0  (0 = enabled, 1 = disabled in Galil)
    fwd_limit: bool = False    # _LFA == 0  (0 = ACTIVE, 1 = clear — inverted!)
    rev_limit: bool = False    # _LRA == 0
    home_switch: bool = False  # _HMA == 0
    last_direction: int = 0    # software-tracked: +1=forward, -1=reverse, 0=unknown


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
    accel: float = 50000.0        # AC — counts/sec²
    decel: float = 50000.0        # DC — counts/sec²
    error_limit: float = 500.0    # ER — position error limit (counts); motor off if exceeded
    fwd_limit_type: str = "NO"    # NO = normally-open, NC = normally-closed
    rev_limit_type: str = "NO"
    home_limit_type: str = "NO"
    counts_per_um: float = 6.4  # encoder resolution for unit conversion


class AppSettings(BaseModel):
    """Global application settings (persisted to settings.json)."""
    axis_a: AxisSettings = Field(default_factory=AxisSettings)
    axis_b: AxisSettings = Field(default_factory=AxisSettings)
    axis_c: AxisSettings = Field(default_factory=AxisSettings)
    poll_interval_ms: int = 100
    position_history_depth: int = 600   # data points (60 s at 100 ms)
    log_file: str = "galil_log.txt"
    display_units: str = "counts"       # "um" or "counts"
    auto_reconnect: bool = True
    last_address: str = "COM3"

    def axis_settings(self, axis: str) -> AxisSettings:
        return {"A": self.axis_a, "B": self.axis_b, "C": self.axis_c}[axis.upper()]


class PositionSlot(BaseModel):
    """A named position memory slot."""
    name: str
    pos_a: float = 0.0
    pos_b: float = 0.0
    pos_c: float = 0.0
    timestamp: str = ""
