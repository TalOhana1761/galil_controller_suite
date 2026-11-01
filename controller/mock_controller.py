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

Limit switch model (matches physical wiring, revised 2026-09-24):
  EVERY axis has exactly ONE switch, tripped mechanically at EITHER end of
  travel. Crossing either boundary trips it, and LAST_DIRECTION is what decides
  which flag is raised: last_direction >= 0 sets fwd_limit, < 0 sets rev_limit.
  Never both - the switch cannot say which end it is, so the software decides.

  Before this, A/B raised BOTH flags together and C was modelled with two
  independent switches. C never had two; the second was never wired.

Trip point defaults (configurable via set_trip_points):
  A, B: fwd = +50 000 counts,  rev = −50 000 counts
  C:    fwd = +100 000 counts, rev =    −500 counts
"""
import json
from utils.app_paths import config_path
import os
import random
from loguru import logger
from .data_models import AxisStatus, ControllerInfo

AXES = ["A", "B", "C"]

# Same per-axis scale tables GalilController builds, so move_microns() converts
# identically in mock and on hardware. Falls back to the AxisSettings defaults if
# settings.json is missing, since the mock must import without any config.
_SETTINGS_PATH = config_path("settings.json")
try:
    with open(_SETTINGS_PATH, "r") as _f:
        _settings = json.load(_f)
except Exception:
    _settings = {}

MOTOR_COUNTS_PER_UM = {
    ax: _settings.get(f"axis_{ax.lower()}", {}).get("counts_per_um", 6.4) for ax in AXES
}
AXIS_TRAVEL_MM = {
    ax: _settings.get(f"axis_{ax.lower()}", {}).get("travel_mm", 20.0) for ax in AXES
}

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
        self._jogging = {"A": 0, "B": 0, "C": 0}          # direction of an active jog

        # Motion parameters — stored by set_params() and jog(), affect simulation
        self._sp     = {ax: _DEFAULT_SPEED for ax in AXES}  # position-move cruise speed
        self._jog_sp = {ax: _DEFAULT_SPEED for ax in AXES}  # jog speed (JG arg, separate from SP)
        self._ac     = {ax: _DEFAULT_ACCEL for ax in AXES}
        self._dc     = {ax: _DEFAULT_DECEL for ax in AXES}
        self._in_jog = {ax: False for ax in AXES}            # True while in jog mode

        # BiSS status word per axis; 0 = healthy. See encoder_status().
        self._enc_status = {ax: 0 for ax in AXES}
        self._inputs: dict = {}       # @IN[n]; unset inputs read 1 (nothing pulling them)
        # Direction of travel when each switch closed, latched on the
        # transition. See _limit_state().
        self._limit_trip_dir = {ax: 0 for ax in AXES}
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
        self.disable_axes("ABC")
        self.info.connected = False

    @property
    def enabled_axes(self) -> set:
        """Mirror of GalilController.enabled_axes, from the simulated servo state."""
        return {ax for ax in AXES if self._servo[ax]}

    def reconnect(self) -> bool:
        """Mock twin of GalilController.reconnect(); nothing to clear."""
        logger.info("[MOCK] Reconnect requested")
        self.info.connected = True
        return True

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

    def _limit_state(self, ax: str, pos: float) -> tuple[bool, bool]:
        """(fwd, rev) limit state at `pos`, with simulate_*() overrides applied.

        ONE switch per axis: it trips at either boundary and says only "at an
        end". The last commanded direction resolves which end, so at most one of
        the two flags is ever True. An override forces the switch pressed AND
        declares the end, since with one switch a caller cannot say "forward
        limit" without also saying which way the axis was travelling.
        """
        if self._fwd_override[ax]:
            return True, False
        if self._rev_override[ax]:
            return False, True
        if self._fwd_override[ax] is False or self._rev_override[ax] is False:
            return False, False        # explicitly cleared

        tripped = (pos >= self._fwd_trip[ax]) or (pos <= self._rev_trip[ax])
        if not tripped:
            self._limit_trip_dir[ax] = 0
            return False, False
        # Latch which end on the transition and HOLD it. The last commanded
        # direction cannot serve: a move that drives off the switch sets it the
        # moment it starts, which would re-label the still-pressed switch as the
        # end being driven towards and halt the escape. See the real
        # controller's limit_tripped() for the hardware observation.
        if not self._limit_trip_dir[ax]:
            self._limit_trip_dir[ax] = +1 if self._last_dir[ax] >= 0 else -1
        forward = self._limit_trip_dir[ax] > 0
        return forward, not forward

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

            # 2-3. Limit state at this position (manual overrides included)
            fwd, rev = self._limit_state(ax, pos)

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
                vel            = self._cur_vel[ax],
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

    def enable_biss(self, axes: str = "ABC"):
        """Mock stand-in for GalilController.enable_biss (no real BiSS hardware to configure)."""
        self.enable_axes(axes)
        logger.info(f"[MOCK] Enable {axes} (BiSS)")

    def disable_axes(self, axes: str = "ABC"):
        for ax in axes:
            if ax in AXES:
                self._servo[ax]   = False
                self._cur_vel[ax] = 0.0
                self._in_jog[ax]  = False
        logger.info(f"[MOCK] Disable {axes}")

    # ── Motion stop commands ────────────────────────────────────────────────

    def is_jogging(self, axis: str) -> int:
        return self._jogging.get(axis.upper(), 0)

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
        self._jogging[axis] = 0
        logger.info(f"[MOCK] Halt {axis}")

    def halt_all(self):
        for _ax in AXES:
            self._jogging[_ax] = 0
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
        """Continuous jog toward effectively infinite target; call halt_axis() to stop.

        Matches GalilController.jog(): `speed` is MOTOR COUNTS PER SECOND. The
        real one used to take microns and do a PR move, which is why the two
        disagreed until 2026-09-23.
        """
        self._jogging[axis] = +1 if speed >= 0 else -1
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

    def move_microns(self, axis: str, microns: float, speed=None,
                     tol_counts: float = 1.0, gain: float = 0.9,
                     max_steps: int = 40, settle_ms: int = 20,
                     timeout: int = 30, snap: bool = True,
                     fine_speed=None, fine_below_um: float = 5.0,
                     settle_window_ms: int = 200,
                     button_state_callback=None,
                     absolute_target=None) -> float:
        """Move one axis a signed distance in microns, closing the loop on the
        simulated position — mock twin of GalilController.move_microns().

        Same signature, same conversion table, same guards and the same
        step/measure/correct loop. One difference is unavoidable: the mock has no
        separate encoder, so its position IS the motor count and one commanded
        count moves it by one count. On hardware the encoder reads 12.8 counts/um
        against the motor's 6.4, so a single microstep moves the encoder 2 counts
        and tol_counts is measured against that finer scale.
        """
        axis = axis.upper()
        if axis not in AXES:
            raise ValueError(f"Unknown axis {axis!r}; expected one of {AXES}")

        microns = float(microns)
        if microns == 0.0:
            return 0.0

        # Accepted and ignored, so this stays a drop-in: the mock has no settling
        # tail to wait out and no real speed, so settle_window_ms, fine_speed and
        # fine_below_um have nothing to act on here.
        btn_dir = "+" if microns > 0 else "-"
        if button_state_callback is not None:
            button_state_callback(axis, btn_dir, False)
        try:
            return self._move_microns(axis, microns, speed, tol_counts, gain,
                                      max_steps, timeout, snap)
        finally:
            if button_state_callback is not None:
                button_state_callback(axis, btn_dir, True)

    def _move_microns(self, axis, microns, speed, tol_counts, gain, max_steps,
                      timeout, snap) -> float:
        cpm_axis = MOTOR_COUNTS_PER_UM[axis]
        if snap:
            # Only whole microsteps exist; see GalilController.move_microns.
            snapped = round(microns * cpm_axis) / cpm_axis
            if snapped == 0.0:
                logger.info(f"[MOCK] {axis}: {microns:+.4f} um is under half a "
                            f"microstep ({1.0 / cpm_axis:.5f} um); nothing to command")
                return 0.0
            if snapped != microns:
                logger.info(f"[MOCK] {axis}: {microns:+.4f} um is not a whole "
                            f"microstep; targeting {snapped:+.5f} um instead")
            microns = snapped

        travel_um = AXIS_TRAVEL_MM[axis] * 1000.0
        if abs(microns) > travel_um:
            raise ValueError(
                f"{axis}: {microns:+.1f} um exceeds the axis travel of "
                f"{travel_um:.0f} um, and LD=3 leaves no hardware limit to catch it"
            )

        direction = +1 if microns > 0 else -1
        fwd, rev = self._limit_state(axis, self._pos[axis])
        if (fwd and direction > 0) or (rev and direction < 0):
            raise RuntimeError(
                f"{axis}: limit active - {microns:+.1f} um drives further into it"
            )

        cpm = MOTOR_COUNTS_PER_UM[axis]
        tol = max(float(tol_counts), 0.0)

        start = self._pos[axis]
        target = start + microns * cpm
        logger.info(
            f"[MOCK] Move {axis} {microns:+.3f} um: {start:.0f} -> {target:.0f} counts, "
            f"tolerance {tol:.0f} counts ({tol / cpm:.3f} um)"
        )

        error = target - start
        no_progress = 0

        for step in range(1, max_steps + 1):
            if abs(error) <= tol:
                break

            fwd, rev = self._limit_state(axis, self._pos[axis])
            if (fwd and error > 0) or (rev and error < 0):
                logger.warning(
                    f"[MOCK] {axis}: limit tripped during move, stopping "
                    f"{error / cpm:+.3f} um short of target"
                )
                break

            counts = int(round(error * gain))
            if counts == 0:
                logger.info(
                    f"[MOCK] {axis}: {error:+.0f} counts "
                    f"({error / cpm:+.4f} um) left, below one step - "
                    f"at the resolution floor"
                )
                break
            if (counts > 0) != (direction > 0):
                logger.warning(
                    f"[MOCK] {axis}: overshot by {-error / cpm:+.4f} um; "
                    f"not reversing into backlash to fix it"
                )
                break

            self.move_relative(axis, counts, speed=speed, wait=True, timeout=timeout)
            new_error = target - self._pos[axis]
            logger.debug(
                f"[MOCK] Move {axis} step {step}: commanded {counts:+d} counts, "
                f"error {error:+.0f} -> {new_error:+.0f} counts"
            )

            if abs(new_error) < abs(error):
                no_progress = 0
            else:
                no_progress += 1
                if no_progress >= 3:
                    logger.warning(
                        f"[MOCK] {axis}: no progress in 3 steps, stopping at "
                        f"{new_error:+.0f} counts ({new_error / cpm:+.3f} um) of error"
                    )
                    error = new_error
                    break
            error = new_error
        else:
            logger.warning(
                f"[MOCK] {axis}: hit max_steps={max_steps} with {error:+.0f} counts "
                f"({error / cpm:+.3f} um) still to go"
            )

        moved_um = (self._pos[axis] - start) / cpm
        logger.info(
            f"[MOCK] Move {axis} done: commanded {microns:+.3f} um, measured "
            f"{moved_um:+.3f} um (error {error / cpm:+.4f} um)"
        )
        return moved_um

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

    def post_move_verify(self, axis: str, target_counts: float, **kwargs) -> float:
        """Mock twin of GalilController.post_move_verify().

        The mock has no relaxation to wait out - its moves land where they are
        told - so this only re-measures and reports. It exists so the same call
        works in --mock without special-casing, and so the tests can exercise the
        callers.
        """
        axis = axis.upper()
        if axis not in AXES:
            raise ValueError(f"Unknown axis {axis!r}; expected one of {AXES}")
        error = (float(target_counts) - self._pos[axis]) / MOTOR_COUNTS_PER_UM[axis]
        logger.info(f"[MOCK] {axis}: verified, {error:+.4f} um from target")
        return error

    def move_to_counts(self, axis: str, target_counts: float, verify: bool = False,
                       undershoot_um: float = 0.45, relax_s: float = 1.0,
                       **kwargs) -> float:
        """Closed-loop move to an absolute position — mock twin of
        GalilController.move_to_counts().

        verify/undershoot_um/relax_s are accepted so the signature matches; the
        mock has no relaxation, so aiming short would simply land short. It
        therefore goes straight to the target and verifies.
        """
        axis = axis.upper()
        if axis not in AXES:
            raise ValueError(f"Unknown axis {axis!r}; expected one of {AXES}")

        error = float(target_counts) - self._pos[axis]
        if abs(error) <= kwargs.get("tol_counts", 1.0):
            logger.info(f"[MOCK] {axis}: already within tolerance of "
                        f"{target_counts:.0f}")
            return 0.0
        moved = self.move_microns(axis, error / MOTOR_COUNTS_PER_UM[axis],
                                  snap=False, **kwargs)
        if verify:
            self.post_move_verify(axis, float(target_counts))
        return moved

    def move_all_absolute(self, positions: dict, speeds: dict = None,
                          wait: bool = True, timeout: int = 60,
                          verify: bool = False, **kwargs):
        """Drive several axes to absolute readings, one after another."""
        # `wait` is accepted and ignored: a closed loop is blocking by nature,
        # there is no non-blocking form to choose. It stays in the signature so
        # existing callers keep working.
        results = {}
        for ax, target in positions.items():
            per_axis = dict(kwargs)
            if speeds and ax in speeds:
                per_axis["speed"] = speeds[ax]
            per_axis.setdefault("verify", verify)
            try:
                results[ax] = self.move_to_counts(ax, target, **per_axis)
            except (RuntimeError, ValueError) as e:
                logger.error(f"[MOCK] move_all_absolute {ax}: {e}")
                results[ax] = 0.0
        return results

    def define_zero(self, axes: str = "ABC"):
        for ax in axes:
            if ax in AXES:
                self._pos[ax]     = 0.0
                self._target[ax]  = 0.0
                self._cur_vel[ax] = 0.0
                self._in_jog[ax]  = False
        logger.info(f"[MOCK] Define zero {axes}")

    # ── Simulation helpers ──────────────────────────────────────────────────

    def encoder_status(self, axis: str) -> int:
        """_SSm0 stand-in. 0 = healthy; simulate_encoder_status() overrides it.

        The real one reports bit 3 when the head is off the scale tape, which is
        what the Z insert waits to clear.
        """
        return int(self._enc_status.get(axis.upper(), 0))

    def declare_limit_end(self, axis: str, direction: int):
        """Mirror of GalilController.declare_limit_end(): which end the switch is."""
        axis = axis.upper()
        direction = +1 if direction > 0 else -1
        self._last_dir[axis] = direction
        self._limit_trip_dir[axis] = direction

    def digital_input(self, n: int) -> int:
        """@IN[n] stand-in. 1 unless simulate_digital_input() set it."""
        return int(self._inputs.get(int(n), 1))

    def simulate_digital_input(self, n: int, value: int):
        self._inputs[int(n)] = int(value)

    def simulate_encoder_status(self, axis: str, value: int):
        """Force the BiSS status word, e.g. 8 (error) for a head off the tape."""
        self._enc_status[axis.upper()] = int(value)

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
