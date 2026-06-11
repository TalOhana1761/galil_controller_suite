"""
Mock controller for UI development and testing without hardware.
Simulates axis motion with a trapezoidal velocity profile.
Drop-in replacement for GalilController.

Motion model (each poll_all_axes() call = POLL_DT seconds of real time):
  - Axes accelerate at AC counts/s² up to SP (position-move) or JG (jog) speed.
  - Axes decelerate at DC counts/s² when within braking distance of target.
  - Average-velocity integration gives smooth position; tiny encoder noise added.
  - Position is hard-clamped at trip boundaries; motion stops at the boundary.
  - wait=True in move commands snaps position directly for test compatibility.

Limit switch model (matches physical wiring):
  A, B (XY): one switch wired to BOTH FWD+REV DMC inputs.
             Both fwd_limit and rev_limit become True simultaneously when
             the position crosses either boundary.  last_direction indicates
             which end was reached.
  C   (Z):   two separate switches — REV at start/home, FWD at end-of-travel.
             Only the relevant limit trips.

Trip point defaults (configurable via set_trip_points):
  A, B: fwd = +50 000 counts,  rev = −50 000 counts
  C:    fwd = +100 000 counts, rev =    −500 counts
"""
import random
from loguru import logger
from .data_models import AxisStatus, ControllerInfo

AXES = ["A", "B", "C"]

# Real time per poll cycle — must match PollingWorker.POLL_MS = 100 ms
POLL_DT = 0.1   # seconds

# Default motion parameters matching config/settings.json
_DEFAULT_SPEED = 5_000.0    # counts/s
_DEFAULT_ACCEL = 50_000.0   # counts/s²
_DEFAULT_DECEL = 50_000.0   # counts/s²

# Position boundaries for automatic limit trips (counts)
_DEFAULT_FWD_TRIP = {"A":  50_000.0, "B":  50_000.0, "C": 100_000.0}
_DEFAULT_REV_TRIP = {"A": -50_000.0, "B": -50_000.0, "C":    -500.0}


class MockGalilController:
    def __init__(self):
        self.info = ControllerInfo(
            address="MOCK",
            model="DMC-4133",
            revision="Rev 1.3k3-SER",
            serial="33619-MOCK",
            firmware="1.3k3",
            connected=True,
        )
        self._pos      = {"A": 0.0, "B": 0.0, "C": 0.0}
        self._target   = {"A": 0.0, "B": 0.0, "C": 0.0}
        self._cur_vel  = {"A": 0.0, "B": 0.0, "C": 0.0}  # current velocity counts/s
        self._servo    = {"A": False, "B": False, "C": False}
        self._home_sw  = {"A": False, "B": False, "C": False}
        self._last_dir = {"A": 0, "B": 0, "C": 0}         # +1=fwd, -1=rev, 0=unknown

        # Motion parameters — stored by set_params() and jog(), affect simulation
        self._sp     = {ax: _DEFAULT_SPEED for ax in AXES}  # position-move cruise speed
        self._jog_sp = {ax: _DEFAULT_SPEED for ax in AXES}  # jog speed (JG arg, separate from SP)
        self._ac     = {ax: _DEFAULT_ACCEL for ax in AXES}
        self._dc     = {ax: _DEFAULT_DECEL for ax in AXES}
        self._in_jog = {ax: False for ax in AXES}            # True while in jog mode

        # Auto-limit trip positions
        self._fwd_trip = dict(_DEFAULT_FWD_TRIP)
        self._rev_trip = dict(_DEFAULT_REV_TRIP)

        # Manual limit overrides (None = use auto-detection from position).
        # Calling simulate_*() sets an explicit True/False that takes precedence.
        self._fwd_override: dict = {"A": None, "B": None, "C": None}
        self._rev_override: dict = {"A": None, "B": None, "C": None}

        self._log: list[str] = []

    # ── Connection ──────────────────────────────────────────────────────────

    def connect(self, address: str, **kwargs) -> ControllerInfo:
        self.info.address = address
        return self.info

    def disconnect(self):
        self.info.connected = False

    def discover(self) -> dict:
        return {"MOCK": "DMC-4133-MOCK Rev 1.3k3-SER"}

    def send(self, cmd: str) -> str:
        self._log.append(f">> {cmd}")
        return ":"

    def send_safe(self, cmd: str) -> str:
        return self.send(cmd)

    # ── Polling ─────────────────────────────────────────────────────────────

    def _update_motion(self, ax: str) -> float:
        """Advance one POLL_DT step with trapezoidal velocity profile.

        Returns the new position (includes tiny encoder noise while moving).
        """
        pos     = self._pos[ax]
        vel     = self._cur_vel[ax]   # signed velocity counts/s
        tgt     = self._target[ax]
        diff    = tgt - pos
        abs_diff = abs(diff)
        abs_vel  = abs(vel)

        # Already settled — no drift
        if abs_diff < 0.5 and abs_vel < 1.0:
            self._cur_vel[ax] = 0.0
            self._in_jog[ax]  = False
            return pos

        direction = 1 if diff > 0 else -1
        sp = self._jog_sp[ax] if self._in_jog[ax] else self._sp[ax]
        ac = self._ac[ax]
        dc = self._dc[ax]

        # Braking distance needed to stop from current speed
        brake_dist = (abs_vel * abs_vel) / (2.0 * dc) if dc > 0 else 0.0

        if brake_dist >= abs_diff:
            # Need to start decelerating
            new_abs_vel = max(0.0, abs_vel - dc * POLL_DT)
        else:
            # Accelerate toward cruise speed
            new_abs_vel = min(sp, abs_vel + ac * POLL_DT)

        # Integrate using trapezoidal rule (average of old and new velocity)
        avg_vel = (abs_vel + new_abs_vel) / 2.0
        new_pos = pos + direction * avg_vel * POLL_DT

        # Overshoot clamp: don't travel past target
        if direction > 0 and new_pos >= tgt:
            new_pos     = tgt
            new_abs_vel = 0.0
            self._in_jog[ax] = False
        elif direction < 0 and new_pos <= tgt:
            new_pos     = tgt
            new_abs_vel = 0.0
            self._in_jog[ax] = False

        self._cur_vel[ax] = direction * new_abs_vel

        # Tiny encoder noise proportional to speed (zero when stopped)
        noise = random.gauss(0, min(0.5, new_abs_vel * 0.0001)) if new_abs_vel > 0 else 0.0
        return new_pos + noise

    def poll_all_axes(self) -> dict:
        result = {}
        for ax in AXES:
            # 1. Advance position using trapezoidal velocity profile
            if self._servo[ax]:
                new_pos = self._update_motion(ax)
            else:
                new_pos = self._pos[ax]
                self._cur_vel[ax] = 0.0

            pos = new_pos

            # 2. Auto-detect limit trips from current position
            if ax in ("A", "B"):
                # XY: single switch wired to BOTH inputs — both trip together
                auto_fwd = (pos >= self._fwd_trip[ax]) or (pos <= self._rev_trip[ax])
                auto_rev = auto_fwd
            else:
                # Z: two separate switches
                auto_fwd = pos >= self._fwd_trip[ax]
                auto_rev = pos <= self._rev_trip[ax]

            # 3. Apply manual overrides (simulate_* calls take precedence)
            fwd = self._fwd_override[ax] if self._fwd_override[ax] is not None else auto_fwd
            rev = self._rev_override[ax] if self._rev_override[ax] is not None else auto_rev

            # 4. Hard-clamp at limit boundary — stop motion
            if fwd or rev:
                self._target[ax]  = pos
                self._cur_vel[ax] = 0.0
                self._in_jog[ax]  = False

            self._pos[ax] = pos

            # 5. Determine stop code
            if not self._servo[ax]:
                sc = 0
            elif fwd and rev:
                sc = 6 if self._last_dir[ax] >= 0 else 7
            elif fwd:
                sc = 6
            elif rev:
                sc = 7
            elif abs(self._target[ax] - pos) < 2.0 and abs(self._cur_vel[ax]) < 1.0:
                sc = 10   # at rest / motion complete
            else:
                sc = 1    # motion in progress

            result[ax] = AxisStatus(
                pos            = pos,
                pos_ref        = self._target[ax],
                vel            = self._cur_vel[ax],
                err            = random.gauss(0, 0.3) if self._servo[ax] else 0.0,
                torque         = random.gauss(0, 0.1) if self._servo[ax] else 0.0,
                stop_code      = sc,
                servo_on       = self._servo[ax],
                fwd_limit      = fwd,
                rev_limit      = rev,
                home_switch    = self._home_sw[ax],
                last_direction = self._last_dir[ax],
            )
        return result

    # ── Motor enable / disable ──────────────────────────────────────────────

    def enable_axes(self, axes: str = "ABC"):
        for ax in axes:
            if ax in AXES:
                self._servo[ax] = True
        logger.info(f"[MOCK] Enable {axes}")

    def disable_axes(self, axes: str = "ABC"):
        for ax in axes:
            if ax in AXES:
                self._servo[ax]   = False
                self._cur_vel[ax] = 0.0
                self._in_jog[ax]  = False
        logger.info(f"[MOCK] Disable {axes}")

    # ── Motion stop commands ────────────────────────────────────────────────

    def halt_axis(self, axis: str):
        """Decelerated stop: set target so axis decelerates naturally to rest."""
        v  = self._cur_vel[axis]
        dc = self._dc[axis]
        if dc > 0 and abs(v) > 0:
            stop_dist = (v * v) / (2.0 * dc)
            self._target[axis] = self._pos[axis] + stop_dist * (1 if v >= 0 else -1)
        else:
            self._target[axis] = self._pos[axis]
        self._in_jog[axis] = False
        logger.info(f"[MOCK] Halt {axis}")

    def halt_all(self):
        for ax in AXES:
            self.halt_axis(ax)
        logger.info("[MOCK] Halt ALL")

    stop_axis = halt_axis
    stop_all  = halt_all

    def emergency_stop(self):
        self.disable_axes("ABC")
        for ax in AXES:
            self._target[ax] = self._pos[ax]
        logger.warning("[MOCK] EMERGENCY STOP")

    # ── Parameters ──────────────────────────────────────────────────────────

    def set_params(self, axis: str, speed=None, accel=None, decel=None, err_limit=None):
        parts = []
        if speed is not None:
            self._sp[axis] = float(speed)
            parts.append(f"SP={speed}")
        if accel is not None:
            self._ac[axis] = float(accel)
            parts.append(f"AC={accel}")
        if decel is not None:
            self._dc[axis] = float(decel)
            parts.append(f"DC={decel}")
        if parts:
            logger.info(f"[MOCK] Set params {axis}: {', '.join(parts)}")

    # ── Motion commands ─────────────────────────────────────────────────────

    def jog(self, axis: str, speed: float):
        """Continuous jog toward effectively infinite target; call halt_axis() to stop."""
        self._jog_sp[axis] = abs(float(speed)) if speed != 0 else _DEFAULT_SPEED
        self._in_jog[axis] = True
        if speed >= 0:
            self._target[axis]   = self._pos[axis] + 1_000_000.0
            self._last_dir[axis] = +1
        else:
            self._target[axis]   = self._pos[axis] - 1_000_000.0
            self._last_dir[axis] = -1
        direction = "FWD" if speed >= 0 else "REV"
        logger.info(f"[MOCK] Jog {axis} {direction} @ {abs(float(speed)):.0f} counts/s")

    def move_relative(self, axis: str, counts: float, speed=None,
                      wait: bool = False, timeout: int = 30):
        self._in_jog[axis] = False
        if speed is not None:
            self._sp[axis] = float(speed)
        self._last_dir[axis] = +1 if counts >= 0 else -1
        self._target[axis] = self._pos[axis] + counts
        if wait:
            self._pos[axis]    = self._target[axis]
            self._cur_vel[axis] = 0.0
        logger.info(f"[MOCK] Move relative {axis}: {counts:+.0f} counts")

    def move_absolute(self, axis: str, counts: float, speed=None,
                      wait: bool = False, timeout: int = 30):
        self._in_jog[axis] = False
        if speed is not None:
            self._sp[axis] = float(speed)
        self._last_dir[axis] = +1 if counts >= self._pos[axis] else -1
        self._target[axis] = float(counts)
        if wait:
            self._pos[axis]    = self._target[axis]
            self._cur_vel[axis] = 0.0
        logger.info(f"[MOCK] Move absolute {axis} → {float(counts):.0f} counts")

    def move_all_absolute(self, positions: dict, speeds: dict = None,
                          wait: bool = False, timeout: int = 60):
        for ax, pos in positions.items():
            self._in_jog[ax] = False
            if speeds and ax in speeds:
                self._sp[ax] = float(speeds[ax])
            self._last_dir[ax] = +1 if pos >= self._pos[ax] else -1
            self._target[ax] = float(pos)
            if wait:
                self._pos[ax]    = float(pos)
                self._cur_vel[ax] = 0.0

    def define_zero(self, axes: str = "ABC"):
        for ax in axes:
            if ax in AXES:
                self._pos[ax]     = 0.0
                self._target[ax]  = 0.0
                self._cur_vel[ax] = 0.0
                self._in_jog[ax]  = False
        logger.info(f"[MOCK] Define zero {axes}")

    def home_axis(self, axis: str):
        self._last_dir[axis] = -1
        self._target[axis] = 0.0

    # ── Simulation helpers ──────────────────────────────────────────────────

    def simulate_fwd_limit(self, axis: str, active: bool = True):
        """Manually force the fwd_limit state (overrides position-based auto-detection)."""
        self._fwd_override[axis] = active

    def simulate_rev_limit(self, axis: str, active: bool = True):
        """Manually force the rev_limit state (overrides position-based auto-detection)."""
        self._rev_override[axis] = active

    def simulate_home_switch(self, axis: str, active: bool = True):
        """Set the home switch state."""
        self._home_sw[axis] = active

    def set_trip_points(self, axis: str, fwd: float, rev: float):
        """Configure the position boundaries for automatic limit trips (counts)."""
        self._fwd_trip[axis] = fwd
        self._rev_trip[axis] = rev

    def get_log(self) -> list[str]:
        return self._log.copy()
