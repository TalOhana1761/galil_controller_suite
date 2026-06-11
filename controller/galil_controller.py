"""
Pure-Python Galil DMC-4133 controller class.
No Qt imports. Stepper-motor build: all positions are command counts and motion commands use Galil stepper-capable motion instructions.

Polling reads:
  _TPA  actual axis position
  _RPA  commanded (reference) position
  _TVA  velocity
  _TEA  position error
  _TTA  torque output
  _SCA  stop code
  _MOA  motor state (0 = enabled, 1 = disabled)
  _LFA  forward limit (1 = clear, 0 = ACTIVE)
  _LRA  reverse limit
  _HMA  home switch
"""

import os
import json
from pathlib import Path

import time
import gclib
import numpy as np
from loguru import logger
from .data_models import AxisStatus, ControllerInfo


# POSITION_PATH = "config/last_positions.json"
POSITION_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "last_positions.json")
SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "settings.json")

MOTOR_BACKLASH = {"A": 0, "B": 0, "C": 0}

AXES = ["A", "B", "C"]

last_dir = {"A": 0, "B": 0, "C": 0}
last_pos = {"A": 0.0, "B": 0.0, "C": 0.0}

with open(POSITION_PATH, "r") as f:
    data = json.load(f)

with open(SETTINGS_PATH, "r") as f:
    settings = json.load(f)

MOTOR_COUNTS_TO_MICRONS_XY = settings["axis_a"]["counts_per_um"]
MOTOR_COUNTS_TO_MICRONS_Z = settings["axis_c"]["counts_per_um"]

for ax in AXES:
    last_dir[ax] = data[ax]["last_direction"]
    last_pos[ax] = data[ax]["last_position"]

class GalilController:
    def __init__(self):
        self._g = gclib.py()
        self.info = ControllerInfo()
        # Software-tracked last commanded direction per axis: +1=fwd, -1=rev, 0=unknown
        self._last_dir: dict[str, int] = {ax: 0 for ax in AXES}
        self._last_target: dict[str, float] = {ax: 0.0 for ax in AXES}
        self.forward_compensation = 0.0

    # ── Connection ─────────────────────────────────────────────────────────

    def connect(self, address: str) -> ControllerInfo:
        self._g.GOpen(address)
        gi = self._g.GInfo()
        parts = [p.strip() for p in gi.split(",")]
        model_rev = parts[1].split(" Rev ") if len(parts) >= 2 else ["", ""]
        serial = parts[2] if len(parts) >= 3 else ""
        try:
            firmware = self._g.GCommand("MG _GV").strip()
        except Exception:
            firmware = "unknown"
        self.info = ControllerInfo(
            address=address,
            model=model_rev[0],
            revision=model_rev[1] if len(model_rev) > 1 else "",
            serial=serial,
            firmware=firmware,
            connected=True,
        )
        logger.info(f"Connected: {gi}")
        return self.info

    def disconnect(self):
        try:
            self._g.GCommand("ST")
            self._g.GCommand("MO")
            self._g.GClose()
        except Exception as e:
            logger.warning(f"Disconnect: {e}")
        self.info.connected = False
        logger.info("Disconnected")

    def discover(self) -> dict:
        return self._g.GAddresses()

    # ── Raw command ─────────────────────────────────────────────────────────

    def send(self, cmd: str) -> str:
        t0 = time.perf_counter()
        resp = self._g.GCommand(cmd)  # ensure command is fully processed before returning
        self.info.latency_ms = (time.perf_counter() - t0) * 1000
        logger.debug(f">> {cmd}  ({self.info.latency_ms:.1f} ms)")
        logger.debug(f"<< {resp.strip()}")
        return resp

    def send_safe(self, cmd: str) -> str:
        try:
            return self.send(cmd)
        except gclib.GclibError as e:
            logger.error(f"CMD ERR [{cmd}]: {e}")
            return ""

    # ── Synchronous encoder polling ─────────────────────────────────────────
    # Called exclusively from the PollingWorker background thread.
    # Each GCommand call is blocking — responses arrive in order.
    # For stepper motors _TPA is the actual axis position, typically in step/count units.

    def poll_all_axes(self) -> dict:
        result = {}
        for ax in AXES:
            try:
                # pos    = float(self._g.GCommand(f"MG _TP{ax}").strip())
                pos = last_pos[ax]
                pos_ref= float(self._g.GCommand(f"MG _RP{ax}").strip())
                vel    = float(self._g.GCommand(f"MG _TV{ax}").strip())
                err    = float(self._g.GCommand(f"MG _TE{ax}").strip())
                torque = float(self._g.GCommand(f"MG _TT{ax}").strip())
                sc     = int(float(self._g.GCommand(f"MG _SC{ax}").strip()))
                mo     = int(float(self._g.GCommand(f"MG _MO{ax}").strip()))
                lf     = int(float(self._g.GCommand(f"MG _LF{ax}").strip()))
                lr     = lf # no hardware reverse limit. software handles it based on last direction
                hm     = int(float(self._g.GCommand(f"MG _HM{ax}").strip()))
                last_direction = last_dir[ax]
                result[ax] = AxisStatus(
                    pos            = pos,
                    pos_ref        = pos_ref,
                    vel            = vel,
                    err            = err,
                    torque         = torque,
                    stop_code      = sc,
                    servo_on       = (mo == 0),   # 0 = motor ON
                    fwd_limit      = (lf == 0),   # 0 = limit ACTIVE
                    rev_limit      = (lr == 0),
                    home_switch    = (hm == 0),
                    last_direction = last_direction,
                )
            except Exception as e:
                logger.warning(f"Poll axis {ax}: {e}")
                result[ax] = AxisStatus()
            
            with open(POSITION_PATH, "r") as f:
                data = json.load(f)

            data[ax]["last_position"] = pos
            data[ax]["last_direction"] = last_direction

            with open(POSITION_PATH, "w") as f:
                json.dump(data, f, indent=2)

        return result

    # ── Motor enable / disable ─────────────────────────────────────────────
    def enable_axes(self, axes: str = "ABC"):
        self.send(f"MO{axes}")
        for ax in axes:
            self.send(f"LD{ax}=3") # actual limit switching is disabled, software read limit switch state and acts.
            self.send(f"MT{ax}={2}")
            self.send(f"CEA{axes}={0}")  # configure encoder as normal
            self.send(f"KS{ax}=16")   # smoothing steps
        self.send(f'SH{axes}')
        logger.info(f"Enable {axes} as steppers")

    def disable_axes(self, axes: str = "ABC"):
        self.send(f"MO{axes}")
        logger.info(f"Disable {axes}")

    # ── Motion stop commands ────────────────────────────────────────────────
    # HALT (ST): decelerated stop; axes remain enabled after stopping.
    # E-STOP (AB + MO): immediate cut; no decel; motors disabled.

    def halt_axis(self, axis: str):
        self.send(f"ST{axis}")
        logger.info(f"Halt {axis}")

    def halt_all(self):
        self.send("ST")
        logger.info("Halt ALL")

    stop_axis = halt_axis
    stop_all  = halt_all

    def emergency_stop(self):
        try:
            self._g.GCommand("AB")
            self._g.GCommand("MO")
            logger.warning("EMERGENCY STOP: AB + MO")
        except Exception as e:
            logger.error(f"E-stop error: {e}")

    # ── Motion parameters ───────────────────────────────────────────────────

    def set_params(self, axis: str, speed=None, accel=None, decel=None, err_limit=None):
        parts = []
        if speed is not None:
            self.send(f"SP{axis}={speed}")
            parts.append(f"SP={speed}")
        if accel is not None:
            self.send(f"AC{axis}={accel}")
            parts.append(f"AC={accel}")
        if decel is not None:
            self.send(f"DC{axis}={decel}")
            parts.append(f"DC={decel}")
        if err_limit is not None:
            self.send(f"ER{axis}={err_limit}")
        if parts:
            logger.info(f"Set params {axis}: {', '.join(parts)}")

    # ── Motion commands ─────────────────────────────────────────────────────


    def jog(self, axis: str, signed_microns: float): # FIX THIS

        # Handles signed displacement (e.g., 10 or -10).
        # Ensures encoder and stepper registers are aligned to prevent 'runaway' error.

        # 10um = 64 counts | 1 count = 3.2 steps | 1um = 20.48 steps
        STEPS_PER_MICRON = 20.48 
        distance_steps = int(signed_microns * STEPS_PER_MICRON)

        # 1. Update Hardware Scaling
        self.send(f"YA{axis}=64")
        self.send(f"YB{axis}=200")
        self.send(f"YC{axis}=4000")
        self.send(f"KS{axis}=16")

        # 2. THE CRITICAL SYNCHRONIZATION
        # Read current encoder position
        current_counts = int(float(self._g.GCommand(f"TP{axis}").strip()))
        
        # Align the internal stepper register (DP) to the encoder (TP) 
        # multiplied by the 3.2 step/count ratio[cite: 1].
        # This 'zeros out' any historical error before the move starts.
        equivalent_steps = int(current_counts * 3.2)
        self.send(f"DP{axis}={equivalent_steps}")

        # 3. Enable SPM Mode
        # YS=1 clears the _QS error register to 0 based on the new DP[cite: 1].
        self.send(f"YS{axis}=1")

        # 4. Movement
        # PR handles the sign of distance_steps automatically[cite: 1].
        self.send(f"PR{axis}={distance_steps}")
        self.send(f"BG{axis}")
        self.send(f"AM{axis}")

        # 5. Error Correction
        # _QS measures the lag/overshoot of the move in steps[cite: 1].
        spm_error = int(float(self._g.GCommand(f"MG _QS{axis}").strip()))
        
        if abs(spm_error) > 0:
            # YR applies the correction in the proper direction automatically[cite: 1].
            self.send(f"YR{axis}={spm_error}")
            self.send(f"AM{axis}")

        final_pos = self._g.GCommand(f"TP{axis}").strip()
        return final_pos
    
    def determine_settle_speed(self, axis: str, error: float):
        self.send(f'WT={1}')
        # if abs(error) >= 100:
        #     self.send(f'WT={1}')
        #     #time.sleep(float(5/1000))
        # if abs(error) >= 20:
        #     self.send(f'WT={2}')
        #     #time.sleep(float(5/1000))
        # if abs(error) <= 5:  # slower speed for fine correction
        #     self.send(f'WT={100}')
        #     #time.sleep(float(120/1000))
        #     return
        # else:
        #     self.send(f'WT={10}')
        #     #time.sleep(float(15/1000))
    
    def determine_motion_step(self, axis: str, error: float):
        if error >= 500:
            self.send(f'PR{axis}=450')
            self.send(f'BG{axis}')
            self.forward_compensation += 0.7
        elif error < 500 and error >= 300:
            self.send(f'PR{axis}=120')
            self.send(f'BG{axis}')
            self.forward_compensation += 0.24
        elif error < 300 and error >= 100:
            self.send(f'PR{axis}=60')
            self.send(f'BG{axis}')
            self.forward_compensation += 0.24
        elif error < 100 and error >= 40:
            self.send(f'PR{axis}=30')
            self.send(f'BG{axis}')
            self.forward_compensation += 0.14
        elif error < 40 and error >= 10:
            self.send(f'PR{axis}=2')
            self.send(f'BG{axis}')
            self.forward_compensation += 0.054
        elif error < 10 and error > 0:
            self.send(f'PR{axis}=1')
            self.send(f'BG{axis}')
            self.forward_compensation += 0.058
        elif error <= -500:
            self.send(f'PR{axis}=-450')
            self.send(f'BG{axis}')
            self.forward_compensation += -0.7
        elif error > -500 and error <= -300:
            self.send(f'PR{axis}=-120')
            self.send(f'BG{axis}')
            self.forward_compensation += -0.24
        elif error > -300 and error <= -100:
            self.send(f'PR{axis}=-60')
            self.send(f'BG{axis}')
            self.forward_compensation += -0.24
        elif error > -100 and error <= -40:
            self.send(f'PR{axis}=-30')
            self.send(f'BG{axis}')
            self.forward_compensation += -0.14
        elif error > -40 and error <= -10:
            self.send(f'PR{axis}=-2')
            self.send(f'BG{axis}')
            self.forward_compensation += -0.054
        elif error > -10 and error < 0:
            self.send(f'PR{axis}=-1')
            self.send(f'BG{axis}')
            self.forward_compensation += -0.058
        self._g.GMotionComplete(axis)

    def move_relative_closed_loop(self, axis: str, counts: float, speed=None,
                                  button_state_callback=None):

        # logger.info(f"limit switch states before move: LF={self._g.GCommand(f'MG _LF{axis}').strip()}")

        begining_pos = float(self._g.GCommand(f"MG _TP{axis}").strip()) / 1.25

        direction = "+" if counts > 0 else "-" if counts < 0 else None
        if button_state_callback is not None and direction is not None:
            button_state_callback(axis, direction, False)

        try:

            lf = int(float(self._g.GCommand(f"MG _LF{axis}").strip()))

            if axis != "C":
                target_pos = float(self._g.GCommand(f"MG _TP{axis}").strip()) + counts*1.25
            else:
                target_pos = float(self._g.GCommand(f"MG _TP{axis}").strip()) + counts*1.25*2

            logger.info(f"Moving {axis} forward: step 0, target {target_pos:.0f} counts, "f"current {self._g.GCommand(f'MG _TP{axis}').strip()} counts")

            if counts > 0:  # positive move:
                for i in range(1, int(round(counts)) + 100):
                    error = target_pos - float(self._g.GCommand(f"MG _TP{axis}").strip())
                    if error > 1 and int(float(self._g.GCommand(f"MG _LF{axis}").strip())) == 1:# check forward limit before each step
                        self.determine_motion_step(axis, error)
                        self.determine_settle_speed(axis, error)
                        logger.info(
                            f"Moving {axis} forward: step {i}, target {target_pos:.0f} counts, "
                            f"current {self._g.GCommand(f'MG _TP{axis}').strip()} counts"
                        )
                    else:
                        break
            else:  # negative move:
                for i in range(1, int(round(counts)) - 100, -1):
                    error = target_pos - float(self._g.GCommand(f"MG _TP{axis}").strip())
                    if error < -1 and int(float(self._g.GCommand(f"MG _LF{axis}").strip())) == 1: # check forward limit before each step
                        self.determine_motion_step(axis, error)
                        self.determine_settle_speed(axis, error)
                        logger.info(
                            f"Moving {axis} backward: step {i}, target {target_pos:.0f} counts, "
                            f"current {self._g.GCommand(f'MG _TP{axis}').strip()} counts"
                        )
                    else:
                        break
        finally:
            if button_state_callback is not None and direction is not None:
                button_state_callback(axis, direction, True)
        
        # logger.info(f"states are: lf={lf} actual limit is {self._g.GCommand(f'MG _LF{axis}').strip()}")
        
        actual_comp = np.round(self.forward_compensation)
        logger.info(f"forward compensation is {actual_comp}")

        if abs(actual_comp) >= 1:
            if axis != "C":
                self.send(f'PR{axis}={actual_comp*MOTOR_COUNTS_TO_MICRONS_XY}')
                self.send(f'BG{axis}')
            else:
                self.send(f'PR{axis}={actual_comp*MOTOR_COUNTS_TO_MICRONS_Z}')
                self.send(f'BG{axis}')
        self._g.GMotionComplete(axis)
        self.send(f'WT={1}')

        self.forward_compensation = 0

        if lf == 0 and int(float(self._g.GCommand(f"MG _LF{axis}").strip())) == 0:
            return
        else:
            last_dir[axis] = int(float(self._g.GCommand(f"MG _PR{axis}").strip()))

        end_pos =float(self._g.GCommand(f"MG _TP{axis}").strip())/1.25
        last_pos[axis] += end_pos-begining_pos
        logger.info(f"last position is {last_pos[axis]}")
        return
    

    def move_if_at_limit(self, axis: str, counts: float, speed=None):
        logger.info("moving at limit")

        target_pos = float(self._g.GCommand(f"MG _TP{axis}").strip()) + counts

        logger.info(f"counts is {counts}, target positions is {target_pos}")

        if counts > 0:  # positive move:
            for i in range(1, int(round(counts)) + 100):
                error = target_pos - float(self._g.GCommand(f"MG _TP{axis}").strip())
                if error > 1:  # check forward limit before each step
                    self.determine_motion_step(axis, error)
                    self.determine_settle_speed(axis, error)
                    logger.info(
                        f"Moving {axis} forward: step {i}, target {target_pos:.0f} counts, "
                        f"current {self._g.GCommand(f'MG _TP{axis}').strip()} counts"
                    )
                else:
                    break
        else:  # negative move:
            for i in range(1, int(round(counts)) - 100, -1):
                error = target_pos - float(self._g.GCommand(f"MG _TP{axis}").strip())
                if error < -1:  # check forward limit before each step
                    self.determine_motion_step(axis, error)
                    self.determine_settle_speed(axis, error)
                    logger.info(
                        f"Moving {axis} backward: step {i}, target {target_pos:.0f} counts, "
                        f"current {self._g.GCommand(f'MG _TP{axis}').strip()} counts"
                    )
                else:
                    break

        return

    def move_opposite_direction(self, axis: str, counts):
        # print(f"move in oppsite direction function called.")
        
        if axis == "C":
            cpm = MOTOR_COUNTS_TO_MICRONS_Z
        else:
            cpm = MOTOR_COUNTS_TO_MICRONS_XY
        
        overshoot = 50 # in microns
        sign = np.sign(counts)
        counts_with_overshoot = counts + overshoot*cpm*sign
        #print(f"\n")
        self.move_relative_closed_loop(axis,counts_with_overshoot)
        self.move_relative_closed_loop(axis,-(counts_with_overshoot - counts))

        return

    def move_relative(self, axis: str, counts: float, speed=None,
                      wait: bool = False, timeout: int = 30):
        lf = int(float(self._g.GCommand(f"MG _LF{axis}").strip()))
        lr = int(float(self._g.GCommand(f"MG _LR{axis}").strip()))
        if counts > 0 and lf == 0:
            raise RuntimeError(f"Forward limit active on {axis}")
        if counts < 0 and lr == 0:
            raise RuntimeError(f"Reverse limit active on {axis}")
        self._last_dir[axis] = +1 if counts >= 0 else -1
        if speed is not None:
            self.send(f"SP{axis}={speed}")
        self.send(f"PR{axis}={counts}")
        self.send(f"BG{axis}")
        if wait:
            self._g.GTimeout(timeout * 1000)
            self._g.GMotionComplete(axis)
            self._g.GTimeout(-1)

    def move_absolute(self, axis: str, counts: float, speed=None,
                      wait: bool = False, timeout: int = 30):
        direction = +1 if counts >= self._last_target.get(axis, 0.0) else -1
        lf = int(float(self._g.GCommand(f"MG _LF{axis}").strip()))
        lr = int(float(self._g.GCommand(f"MG _LR{axis}").strip()))
        if direction > 0 and lf == 0:
            raise RuntimeError(f"Forward limit active on {axis}")
        if direction < 0 and lr == 0:
            raise RuntimeError(f"Reverse limit active on {axis}")
        self._last_dir[axis] = direction
        self._last_target[axis] = float(counts)
        if speed is not None:
            self.send(f"SP{axis}={speed}")
        self.send(f"PA{axis}={counts}")
        self.send(f"BG{axis}")
        if wait:
            self._g.GTimeout(timeout * 1000)
            self._g.GMotionComplete(axis)
            self._g.GTimeout(-1)

    def move_all_absolute(self, positions: dict):  
        with open(POSITION_PATH, "r") as r:
            data = json.load(r)

        for ax, pos in positions.items():
            self.move_relative_closed_loop(ax, pos - data[ax]["last_position"])
        return

    def define_zero(self, axes: str = "ABC"):
        for ax in axes:
            self.send(f"DP{ax}=0")
        logger.info(f"Define zero {axes}")

    def home_axis(self, axis: str):
        """Issue Galil built-in home routine (HM + BG)."""
        self.send(f"HM{axis}")
        self.send(f"BG{axis}")
