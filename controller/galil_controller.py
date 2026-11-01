"""
Pure-Python Galil DMC-4133 controller class.
No Qt imports. Stepper-motor build: all positions are command counts and motion commands use Galil stepper-capable motion instructions.

Polling reads, per axis, kept to the minimum so the poll cycle stays fast:
  _TPA  actual axis position                      - always
  _LFA / _LRA  the axis's ONE limit switch        - only if LIMIT_INPUT has it

Every axis has ONE switch, tripped at either end of travel. Which DMC input it
is wired to is in LIMIT_INPUT below; which END it means is decided by the last
commanded direction, not by the input. An axis with no switch is reported clear
without being read. The remaining AxisStatus fields (vel, stop_code, servo_on,
home_switch) are not polled here and stay at their defaults; only
MockGalilController sets them.
"""

import os
import json
from pathlib import Path

import time
import gclib
import numpy as np
from loguru import logger
from .data_models import AxisStatus, ControllerInfo
from utils.app_paths import config_path


SETTINGS_PATH = config_path("settings.json")

AXES = ["A", "B", "C"]

# Last commanded direction per axis: +1 forward, -1 reverse, 0 unknown.
#
# This is what tells the software WHICH END of travel A or B is sitting at, since
# those two share one switch wired to both DMC inputs and an active limit only
# says "at an end". It is in memory only: it used to be seeded at import from
# config/last_positions.json, which was dropped on 2026-09-23 along with the rest
# of that file.
#
# It is PERSISTED, in config/last_direction.json, and written whenever a move
# changes it. It has to survive a restart: it is the only thing that says which
# end of travel an axis is on, and that is not derivable from position while _TP
# remains an unanchored PRC coordinate.
#
# LAST_DIR_ON_START is a one-time bootstrap, used only when the file does not
# exist yet - which was the case on 2026-09-23 after config/last_positions.json
# (the old home of this value) was deleted. Without some value, an axis sitting
# on a limit could not be moved at all: move_microns() refuses to guess which way
# is out. +1 makes the app bootable; the first move on each axis replaces it with
# the truth and writes it down.
LAST_DIR_PATH = config_path("last_direction.json")
LAST_DIR_ON_START = +1


def _load_last_dir() -> dict:
    try:
        with open(LAST_DIR_PATH, "r") as f:
            data = json.load(f)
        out = {}
        for ax in ("A", "B", "C"):
            v = int(data.get(ax, LAST_DIR_ON_START))
            out[ax] = v if v in (-1, +1) else LAST_DIR_ON_START
        logger.info(f"Last direction loaded: {out}")
        return out
    except FileNotFoundError:
        logger.warning(
            f"No last-direction file; seeding every axis at {LAST_DIR_ON_START:+d}. "
            f"This is an ASSUMPTION - move each axis once to record the truth."
        )
    except Exception as e:
        logger.warning(f"Last direction unreadable ({e}); seeding "
                       f"{LAST_DIR_ON_START:+d}")
    return {ax: LAST_DIR_ON_START for ax in ("A", "B", "C")}


def save_last_dir():
    """Persist the directions. Called when a move changes one, not on a timer."""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(LAST_DIR_PATH)), exist_ok=True)
        with open(LAST_DIR_PATH, "w") as f:
            json.dump(last_dir, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not save last direction: {e}")


last_dir = _load_last_dir()

with open(SETTINGS_PATH, "r") as f:
    settings = json.load(f)

# Per-axis MOTOR counts per micron, read straight from settings.json. All three
# axes are 6.4 today (200 steps/rev x 64 microsteps / 2000 um screw pitch), but
# each axis keeps its own entry so re-gearing one axis is a settings.json edit
# and nothing else. This replaced an older MOTOR_COUNTS_TO_MICRONS_XY/_Z pair
# that read axis_a and axis_c and ignored axis_b entirely.
MOTOR_COUNTS_PER_UM = {
    "A": settings["axis_a"]["counts_per_um"],
    "B": settings["axis_b"]["counts_per_um"],
    "C": settings["axis_c"]["counts_per_um"],
}

# Permitted travel per axis (mm). enable_axes() issues LD=3, which disables the
# controller's own limit switching, so a software check is the only thing that
# stands between a commanded move and an end stop.
AXIS_TRAVEL_MM = {
    "A": settings["axis_a"]["travel_mm"],
    "B": settings["axis_b"]["travel_mm"],
    "C": settings["axis_c"]["travel_mm"],
}

# Per-axis motion parameters from settings.json. Until 2026-09-22 nothing applied
# these: enable_axes() set MT/SY/SS/YA/LD/KS and left SP/AC/DC alone, so the
# controller ran on its power-on defaults (SP=25000, AC=DC=256000) and every
# measurement taken before that date was made at AC=DC=256000.
AXIS_SPEED = {ax: settings[f"axis_{ax.lower()}"]["speed"] for ax in AXES}
AXIS_ACCEL = {ax: settings[f"axis_{ax.lower()}"]["accel"] for ax in AXES}
AXIS_DECEL = {ax: settings[f"axis_{ax.lower()}"]["decel"] for ax in AXES}

# ENCODER counts per micron: 1 / 78.125 nm measuring step on the LAK14-710A-RO,
# same head on all three axes. Used only to report how far the encoder saw the
# stage actually go - motion itself is commanded in motor counts.
ENCODER_COUNTS_PER_UM = settings.get("encoder_counts_per_um", 12.8)

# Serial baud for GOpen. 0 means "say nothing and take gclib's default", which is
# what this project did until 2026-09-23. Set it in settings.json once
# check_link_speed.py has measured the real rate - an explicit value is better
# than an unrecorded default, and it fails loudly if the two ends disagree.
SERIAL_BAUD = int(settings.get("serial_baud", 0) or 0)

# BiSS C frame for the NUMERIK JENA LAK encoders, as SSm= n0,n1,n2,n3 <o.
# In Galil's SS command n1 is the TOTAL bit count and n3 is how many of those are
# zero padding, so POSITION BITS = n1 - n3. Galil's own example makes this
# explicit: "SSA= 1,24,0,2<9" is documented as "24 total bits, 22 position data,
# 2 zero padding bits".
#
#   n0=1  -> serial position fills the main position register (_TP)
#   n1=33 -> total bits between the start bit and the E/W/CRC field. 33 is the
#            ONLY width at which the CRC validates - verified by sweeping
#            n1=24..38 on all three axes (test_biss_config.py); every other width
#            returns CRC invalid and the controller rejects SS with error 140.
#   n2=0  -> reserved
#   n3=0  -> no padding to strip. The 33rd bit is the BiSS C CDS bit, and the LAK
#            manual's frame diagram (6.6.2) puts it BEFORE the position data:
#            Start, CDS, position MSB..LSB, E, W, CRC. So the extra bit lands at
#            the MSB end and reads 0, making the 33-bit value equal to the 32-bit
#            position. Nothing needs shifting out. This matches the LAK datasheet,
#            which specifies 32 bits with no zero padding.
#   o=9   -> clock divider; f = 10/(9+1) = 1 MHz. Galil's manual calls o=9 "a good
#            starting value"; the LAK allows 2 MHz without line-delay compensation.
#
# Galil's frame model is [position][padding] with padding TRAILING after the LSB,
# and n3 shifts it off (n1 is "Data bits, including zero padding bits"). Where
# padding exists, n3=0 really would double the value. It does not exist here:
#
#   LSB parity test. A trailing zero pad forces every reading even. Across all 38
#   _TP readings recorded at n3=0 on all three axes, 18 were odd and 20 even - a
#   coin flip, so the LSB is genuine position data. n3=1 would shift off a real
#   bit and halve the position.
#
# n3 does right-shift (test_biss_padding.py: every increment halves the reading,
# identically on all three axes), so this is not about whether n3 works - it is
# about there being nothing here for it to strip.
#
# CRC stays clean at every n3, because CRC validates the frame, not how its bits
# are interpreted. A clean _SSm0 proves nothing about n3; use the parity check.
BISS_CONFIG = "1,33,0,0<9"

# ── Limit switch wiring ──────────────────────────────────────────────────────
#
# This rig uses NORMALLY CLOSED limit switches, so enable_axes() sets CN=1,
# limit inputs active HIGH. That is the polarity NC wants: at rest the switch
# grounds its input and reads clear, while BOTH pressing it and a broken wire
# open the circuit and read ACTIVE. Failing safe on a broken wire is the entire
# point of NC, and it only works at this polarity.
#
# Measured 2026-09-24 with CN=-1 (the controller's default, active low) the
# reading was inverted exactly as expected: C's wired input read ACTIVE while
# the switch sat unpressed, and went clear when it was pressed.
#
# THE CATCH: an input with nothing on it is also open, so under CN=1 it reads
# ACTIVE too, indistinguishably from a tripped switch. Flipping CN without this
# map would pin all five unwired inputs at a permanent limit and make X and Y
# unmovable. This set is what separates a real switch from an empty input -
# only the inputs listed here are believed.
#
# ADD AN ENTRY THE MOMENT A SWITCH IS PHYSICALLY CONNECTED. An input that is not
# listed is reported clear and blocks nothing, and LD=3 means the controller
# takes no action of its own either - so an unlisted input is an end of travel
# with NO protection beyond the AXIS_TRAVEL_MM guard in _move_microns().
#
# EVERY AXIS HAS EXACTLY ONE SWITCH. It is tripped mechanically at EITHER end of
# travel, so the signal says "this axis is at an end" and nothing more - there is
# no second switch to say which end, and no per-end input to read. The LAST
# COMMANDED DIRECTION is what resolves it: drive - into a switch and you are at
# the reverse end, drive + into it and you are at the forward end.
#
# This maps each axis to the DMC input its one switch happens to be wired to.
# That is a wiring detail and nothing more - it says WHERE TO READ the signal,
# never which end of travel it means. An axis absent from this map has no switch
# at all: it is reported clear without being read, which under CN=1 is the only
# way to tell an empty input from a tripped one (see above).
#
# ADD AN ENTRY THE MOMENT A SWITCH IS PHYSICALLY CONNECTED. An axis that is not
# listed blocks nothing, and LD=3 means the controller takes no limit action of
# its own either - so it has NO end-stop protection beyond the AXIS_TRAVEL_MM
# guard in _move_microns().
LIMIT_INPUT = {
    "A": "FWD",          # NC switch on A's forward DMC input, wired 2026-10-12
    "B": "FWD",         # NC switch on B's forward DMC input, wired 2026-09-24
    "C": "FWD",          # NC switch on C's forward DMC input, wired 2026-09-24
}

# Step motor smoothing. Galil's range is 0.5 to 16 and its default is 2; this
# project ran at 16, the MAXIMUM, until 2026-09-22.
#
# KS is what decides how much motion arrives AFTER the profiler reports the move
# complete, and the tuning sweep on axis C measured it directly, as the encoder
# counts still to come once GMotionComplete returned on a 10 um step:
#
#     KS   0.5     1       2       4       8       16
#     tail 0.031   0.094   0.156   0.156   0.281   1.406 um
#
# KS=16 left 1.41 um still arriving; KS=2 leaves 0.156 um, one microstep, with no
# sign of dropped steps (delivered/expected 0.994). That tail is what made the
# closed loop correct against a stale reading and overshoot, so 2 it is. Going
# lower buys little and risks exciting the drive.
KS_SMOOTHING = 2

# Drive current per axis, as the SDM-44140's AG setting (2026-11-01). AG is amps
# per phase, NOT a resolution gain: n = 0/1/2/3 -> 0.5/1.0/2.0/3.0 A (box manual
# A10, Table A9.4). Matched to the motor datasheets:
#
#     X (A), Y (B)   1 A motors   AG=1 -> 1.0 A
#     Z (C)          3 A motor    AG=3 -> 3.0 A
#
# Nothing set AG before this, so every axis ran on the EEPROM value, read as AG=1
# on 2026-09-23 - which left Z at a third of its rated current. The earlier
# analysis had assumed one 1.2 A motor on all three axes.
#
# LC is still not set, so holding current stays at the EEPROM's LC=0: 25% after a
# move, i.e. 0.75 A on Z at rest instead of 0.25 A. Expect Z to run warmer.
# AG needs the axis in MO, which enable_axes() guarantees by sending MO first.
AXIS_AMP_GAIN = {"A": 1, "B": 1, "C": 3}

# Speed for the final approach, in motor counts/s. The sweep on axis C measured
# SP=500 as giving both the tightest spread on a 10 um step (0.70 um against
# 1.0-3.7 at higher speeds) and the smallest settling tail. It cannot be the
# global speed - 200 mm of C travel at 500 counts/s is 43 minutes - so
# move_microns() uses the configured speed for the bulk of a move and drops to
# this only once the remaining error is small.
FINE_SPEED = 500.0
FINE_BELOW_UM = 5.0   # remaining error under this switches to FINE_SPEED

# Settle window for interactive moves (the jog panel), in ms.
#
# Every measurement inside move_microns() costs one settle window, and a move
# needs three or four of them, so the 200 ms default works out near a second per
# jog click - too slow to jog with. 60 ms is the compromise: it still catches the
# bulk of the tail, which KS=2 and the slow final approach already shrank, and it
# gets a jog step down to roughly a third of a second.
#
# It cannot be tightened to nothing, and no window makes the move exact: the
# post-move relaxation measured on axis C was still climbing at 5 s (+0.70 um
# after a 10 um step). Waiting is not a cure for it, which is precisely why
# interactive moves should not pay for a long wait.
JOG_SETTLE_WINDOW_MS = 60

# How often a running closed-loop move reports its position to the UI, in s.
# Matches the poll's own POLL_MS, so the readout moves at the same rate during a
# step move as it does during a jog. See GalilController.position_listener.
PROGRESS_INTERVAL_S = 0.1

# ── Post-move relaxation ────────────────────────────────────────────────────
# Measured on axis C, 2026-09-22, watching _TP for 10 s after a move with the
# stage untouched:
#
#     step     1 um     10 um    100 um
#     drift   +0.443   +0.651   +0.495 um
#
# Across a 100x range in distance the drift varies 1.5x, so it is a CONSTANT per
# move, not proportional - elastic wind-up depends on the force in the drivetrain
# (friction, bellows preload), which does not change with distance. It settles:
# by 10 s the rate is under one encoder count per second.
#
# It always runs WITH the direction of travel, which is why every closed-loop
# move overshoots, and why waiting longer makes the overshoot bigger:
#
#     10 um move, relaxation at 0.2 s = +0.26, at 1 s = +0.44, at 10 s = +0.65 um
#
# That rules out correcting it after the fact: the stage ends up PAST the target,
# and reversing costs 7-11 um of take-up on this axis to fix 0.2 um. The only
# workable order is to aim SHORT and let the relaxation carry the stage in, then
# verify and close any remainder in the same direction.
RELAX_SETTLE_S = 1.0          # wait this long before verifying (~70% arrived)
RELAX_UNDERSHOOT_UM = 0.45    # aim this short; the relaxation makes up the rest
VERIFY_MAX_PASSES = 3

class GalilController:
    def __init__(self):
        self._g = gclib.py()
        self.info = ControllerInfo()
        # Software-tracked last commanded direction per axis: +1=fwd, -1=rev.
        # Seeded from what was persisted; see last_dir above.
        self._last_dir: dict[str, int] = dict(last_dir)
        # The direction that was being travelled when each limit switch closed,
        # latched on the transition and held until it opens again. See
        # limit_tripped() for why _last_dir cannot serve.
        self._limit_trip_dir: dict[str, int] = {ax: 0 for ax in AXES}
        self._last_target: dict[str, float] = {ax: 0.0 for ax in AXES}
        # Direction of an active continuous jog per axis, 0 when not jogging.
        # Software-tracked so the poll loop can stop a jog that reaches a limit.
        self._jogging: dict[str, int] = {ax: 0 for ax in AXES}
        # Called as listener(axis, counts) with every _TP reading taken while a
        # closed-loop move runs. A move blocks the polling thread for its whole
        # duration, so without this the display freezes until it ends. None (the
        # default, and what the hardware scripts get) also skips the extra _TP
        # reads _wait_motion() makes for it, so their timing is unchanged.
        self.position_listener = None
        # Called with no arguments on the same throttled cadence, from inside a
        # move, on the thread running it - so it may issue its own commands. The
        # polling worker hangs its touch alarm read here, because the move holds
        # off the poll that would otherwise do it. None = nothing extra is read.
        self.motion_tick = None
        self._last_progress = 0.0
        # Axes enable_axes() has configured and armed IN THIS SESSION. Not read
        # from the controller: what matters is that this session ran the full
        # configuration (CN/LD/SS/...), and a motor left on by another PC has not
        # had that. Cleared by disable_axes(), emergency_stop() and (dis)connect.
        # The Z retract button keys off it - a retract queued with C off moves
        # nothing, but the button used to flip anyway and record a state that
        # was not true.
        self.enabled_axes: set = set()

    # ── Connection ─────────────────────────────────────────────────────────

    def connect(self, address: str) -> ControllerInfo:
        """Open the link. `address` may carry gclib options, e.g. a baud rate.

        The app has always passed a bare "COM4", so the link runs at whatever
        gclib defaults to - which is nowhere recorded. SERIAL_BAUD (from
        settings.json) appends `--baud N` when set, so the rate can be pinned
        once check_link_speed.py has established what it actually is. Both ends
        must agree: the controller's own serial rate is a controller-side
        setting, so opening at a baud it is not using simply fails.
        """
        if SERIAL_BAUD and "--baud" not in address and not address[0].isdigit():
            address = f"{address} --baud {SERIAL_BAUD}"
            logger.info(f"Opening with an explicit baud: {address}")
        address = self._open(address)
        self.enabled_axes = set()
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

    def _open(self, address: str) -> str:
        """GOpen, retrying a failed SERIAL open as a direct connection.

        gclib routes every open through Galil's gcaps service when one is
        installed, and only goes direct when none is. This PC has gcaps and it
        works; a peer's laptop could not open a port that was plainly present
        (2026-10-08), and a stale or mismatched gcaps from an older Galil install
        there is one way that happens. `--direct` takes gcaps out of the path.

        Serial only: over Ethernet, gcaps is what shares the controller's few
        handles between programs, so bypassing it is not a harmless retry.
        Returns the address that actually opened, so reconnect() reuses it.
        """
        try:
            self._g.GOpen(address)
            return address
        except Exception as first:
            tokens = address.split()
            serial = bool(tokens) and tokens[0].upper().startswith("COM")
            if not serial or "-d" in tokens or "--direct" in tokens:
                raise
            direct = f"{address} --direct"
            logger.warning(f"Open {address} failed ({first}); retrying as {direct}")
            try:
                self._g.GOpen(direct)
            except Exception as second:
                raise RuntimeError(
                    f"{first} (also failed with --direct: {second})") from second
            logger.warning(f"Opened {direct}: the gcaps route failed and the "
                           f"direct one works on this PC")
            return direct

    def disconnect(self):
        try:
            self._g.GCommand("ST")
            self._g.GCommand("MO")
            self._g.GClose()
        except Exception as e:
            logger.warning(f"Disconnect: {e}")
        self.enabled_axes = set()
        self.info.connected = False
        logger.info("Disconnected")

    def reconnect(self) -> bool:
        """Close and reopen the link. The only way to clear a desync.

        After a command times out its reply still arrives, just late, so every
        following read returns the PREVIOUS command's answer - a position reading
        where a limit reading was asked for, and so on. gclib has no flush, so
        the buffer cannot be drained; the connection has to be rebuilt.

        Re-runs enable_axes() afterwards, because the axes were configured on the
        old connection (MT/SY/SS/YA/LD/KS/SP/AC/DC) and a fresh GOpen does not
        carry that. Returns True if the link came back.
        """
        address = self.info.address
        if not address:
            logger.error("Cannot reconnect: no address recorded")
            return False

        logger.warning(f"Reconnecting to {address} to clear a desynchronised link")
        try:
            self._g.GClose()
        except Exception:
            pass          # it is already broken; that is the point

        time.sleep(0.5)
        try:
            self._g = gclib.py()
            self._g.GOpen(address)
            self.enable_axes("ABC")
            self.info.connected = True
            logger.info(f"Reconnected to {address}")
            return True
        except Exception as e:
            logger.error(f"Reconnect failed: {e}")
            self.info.connected = False
            return False

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
        """Read every axis once. A failure on one axis must not poison the rest.

        The persisted record is only updated for axes that actually read, and the
        file is written ONCE per cycle rather than once per axis.

        Both of those were bugs. The write used to sit outside the try/except and
        reference `pos` and `last_direction`, which are only assigned inside it:

          - if the FIRST axis failed, those names were unbound and the whole poll
            died with "cannot access local variable 'last_direction'";
          - if a LATER axis failed, they still held the PREVIOUS axis's values, so
            axis B silently recorded axis A's position. That one is worse,
            because nothing reports it.

        Both showed up in a real session on 2026-09-22 after the serial link
        desynchronised.
        """
        result = {}
        for ax in AXES:
            try:
                pos = np.int64(self._read_float(f"MG _TP{ax}"))
                # ONE switch per axis, so at most one of these two is True and
                # the last commanded direction is what picks which. An axis with
                # no switch is not read at all. Both flags used to be the same
                # _LF reading copied twice, which said nothing about which end.
                result[ax] = AxisStatus(
                    pos            = pos,
                    fwd_limit      = self.limit_active(ax, +1),
                    rev_limit      = self.limit_active(ax, -1),
                    last_direction = last_dir[ax],
                )
            except Exception as e:
                logger.warning(f"Poll axis {ax}: {e}")
                result[ax] = AxisStatus()

        return result

    # ── Motor enable / disable ─────────────────────────────────────────────
    # YA=64 declares the step drive's microstepping resolution. The controller's
    # -D4140 module is the Galil SDM-44140, which the YA documentation describes as
    # "always configured for 64th microstepping" / "Drive fixed at 1/64 step" - so
    # 64 is a hardware fact, not a choice, and YA does not change it. YA only feeds
    # Stepper Position Maintenance (YS/SPM) mode, and its default of 2 would make
    # SPM compare against a 32x-wrong resolution. Nothing enables SPM today; this
    # is set so that it is correct if anything ever does.
    #
    # The same 1/64 is what makes counts_per_um 6.4 on every axis:
    #   200 steps/rev (1.8 deg) x 64 microsteps x 1 (gear) / 2000 um pitch = 6.4
    def enable_axes(self, axes: str = "ABC"):
        self.send(f"MO{axes}")  # axes must be off before MT/SY/SS can be changed
        self.enabled_axes -= set(axes)       # and stay "off" unless all of it lands

        # Limit inputs active HIGH, for the NORMALLY CLOSED switches this rig
        # uses - see LIMIT_INPUT. CN is GLOBAL, one setting for all six inputs
        # on all three axes, which is why the unwired ones need the map rather
        # than a per-axis CN. Only n0 is given, so the home/latch/abort
        # arguments keep whatever they are set to.
        #
        # The controller's own default is CN -1 (active low), and nothing here
        # ever set it before 2026-09-24, so it sat at the default - the same
        # unmanaged-parameter situation SP/AC/DC were in until 2026-09-22.
        self.send("CN 1")

        for ax in axes:
            logger.info(f"Enable {ax}")
            self.send(f"MT{ax}=2")            # stepper, active-low step pulses (set before SS)
            self.send(f"AG{ax}={AXIS_AMP_GAIN[ax]}")  # drive current; see AXIS_AMP_GAIN
            self.send(f"SY{ax}=0")            # BiSS WARN/ERROR bits: active low
            self.send(f"SS{ax}={BISS_CONFIG}")  # BiSS -> _TP{ax}; see BISS_CONFIG note above
            self.send(f"YA{ax}=64")           # declare the drive's microstepping; see note below
            self.send(f"LD{ax}=3")  # actual limit switching is disabled, software read limit switch state and acts.
            self.send(f"KS{ax}={KS_SMOOTHING}")   # see the KS note below
            # Apply the configured motion parameters. Without these the axis runs
            # on Galil's power-on defaults and settings.json is decorative.
            self.send(f"SP{ax}={AXIS_SPEED[ax]}")
            self.send(f"AC{ax}={AXIS_ACCEL[ax]}")
            self.send(f"DC{ax}={AXIS_DECEL[ax]}")
        self.send(f'SH{axes}')
        self.enabled_axes |= set(axes)
        logger.info(f"Enable {axes} as steppers with BiSS C encoder feedback")

    def disable_axes(self, axes: str = "ABC"):
        self.enabled_axes -= set(axes)
        self.send(f"MO{axes}")
        logger.info(f"Disable {axes}")

    # ── BiSS encoder setup (NUMERIK JENA LAK series, ordered for BiSS C) ────
    # Datasheet facts used below (LAK user manual, section 6.6 "BiSS C Interface"):
    #   - BiSS C (unidirectional) is hardware-compatible with SSI plus WARN/ERROR
    #     bits and a CRC-6 checksum.
    #   - Max BiSS clock: 2.0 MHz without line-delay compensation, 5.0 MHz with.
    #   - WARN/ERROR bits are reported active LOW (1 = no warning/error, 0 = active).
    #
    # GDK command reference (DMC-4103 Firmware Command Reference):
    #   MT  Motor Type             - axis must be in motor-off (MO) state to set it,
    #                                 and it is set before SS so the axis is in its
    #                                 final mode before the encoder frame is configured.
    #   SY  Serial encoder BiSS active level
    #         n=0 -> Warning bit active low, Error bit active low  (matches LAK spec above)
    #   SS  Configure the special Galil BiSS feature - see BISS_CONFIG at module level
    #                                 for the frame layout and why n1 is 33.
    #   CN  Configure             - limit inputs ACTIVE HIGH (CN 1), for the normally
    #                                 closed switches this rig uses. GLOBAL, not per
    #                                 axis - see LIMIT_INPUT at module level.
    #   LD  Limit Disable          - hardware limit switches are disabled (LD=3), so
    #                                 software is the only thing that acts on a limit;
    #                                 it reads each axis's one switch per LIMIT_INPUT
    #                                 and resolves the end from the last direction.
    #   KS  Step Motor Smoothing   - filters stepper pulse output for smoother motion.
    #   SH  Servo Here             - enables (arms) the axis.
    #
    # Deprecated: identical to enable_axes(), kept only so existing callers keep
    # working. Use enable_axes(); this delegates to it.
    def enable_biss(self, axes: str = "ABC"):
        """Deprecated alias for enable_axes(). Both configure BiSS C feedback."""
        self.enable_axes(axes)

    # ── Motion stop commands ────────────────────────────────────────────────
    # HALT (ST): decelerated stop; axes remain enabled after stopping.
    # E-STOP (AB + MO): immediate cut; no decel; motors disabled.

    def halt_axis(self, axis: str):
        self._jogging[axis.upper()] = 0
        self.send(f"ST{axis}")
        logger.info(f"Halt {axis}")

    def halt_all(self):
        for ax in AXES:
            self._jogging[ax] = 0
        self.send("ST")
        logger.info("Halt ALL")

    stop_axis = halt_axis
    stop_all  = halt_all

    def emergency_stop(self):
        self.enabled_axes = set()
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


    def jog(self, axis: str, speed: float):
        """Continuous jog at `speed` MOTOR COUNTS PER SECOND, until halted.

        Galil JG + BG: the axis runs at a constant speed with no target, and
        stops on ST (halt_axis) or when the limit guard in PollingWorker trips.

        This replaced a function of the same name that took MICRONS and did a
        relative PR move (2026-09-23). The jog panel had always passed it the
        speed slider's value, so holding a jog button commanded a move of that
        many microns: 4 mm at the default 4000, and up to 50 mm at the slider's
        maximum, on an axis with 20 mm of travel and no travel guard on that
        path. It also set KS=16, undoing the KS=2 tuning, and switched on
        Stepper Position Maintenance (YS=1) that nothing ever switched off.

        Refuses to start into an active limit, the same rule move_microns()
        applies: LD=3 means nothing in hardware would stop it.
        """
        axis = axis.upper()
        if axis not in AXES:
            raise ValueError(f"Unknown axis {axis!r}; expected one of {AXES}")

        speed = float(speed)
        if speed == 0:
            return self.halt_axis(axis)
        direction = +1 if speed > 0 else -1

        if self.limit_active(axis, direction):
            raise RuntimeError(
                f"{axis}: limit active - jogging {'+' if direction > 0 else '-'} "
                f"drives further into it"
            )

        self._last_dir[axis] = direction
        if last_dir.get(axis) != direction:
            last_dir[axis] = direction
            save_last_dir()
        self._jogging[axis] = direction

        self.send(f"JG{axis}={int(round(speed))}")
        self.send(f"BG{axis}")
        logger.info(f"Jog {axis} at {speed:+.0f} counts/s "
                    f"({speed / MOTOR_COUNTS_PER_UM[axis]:+.0f} um/s)")

    def is_jogging(self, axis: str) -> int:
        """Direction of an active jog on this axis, or 0. Software-tracked."""
        return self._jogging.get(axis.upper(), 0)

    # ── Micron-unit motion ──────────────────────────────────────────────────

    def _read_tp(self, axis: str) -> float:
        """Current encoder reading for one axis, in encoder counts."""
        tp = float(self._g.GCommand(f"MG _TP{axis}").strip())
        # Throttled: _wait_until_still() reads every ~16 ms, far more often
        # than a readout needs repainting.
        if self._progress_due():
            self._progress(axis, tp)
        return tp

    def _progress(self, axis: str, counts: float):
        """Run the mid-move hooks. Neither is allowed to break the move."""
        self._last_progress = time.perf_counter()
        listener = getattr(self, "position_listener", None)
        if listener is not None:
            try:
                listener(axis, counts)
            except Exception as e:
                logger.debug(f"{axis}: position listener raised: {e}")
        tick = getattr(self, "motion_tick", None)
        if tick is not None:
            try:
                tick()
            except Exception as e:
                logger.debug(f"{axis}: motion tick raised: {e}")

    def _progress_due(self) -> bool:
        """Are the mid-move hooks owed a call?

        _wait_motion() reads only _BG and the switch, so a long bulk step would
        otherwise show nothing until it ended. One _TP read per PROGRESS_INTERVAL_S
        costs one round trip (~16 ms) every few polls, plus whatever motion_tick
        reads (the touch alarm: one more) - the limit watch reacts that much
        later on those polls only.
        """
        hooked = (getattr(self, "position_listener", None) is not None
                  or getattr(self, "motion_tick", None) is not None)
        return (hooked and
                time.perf_counter() - self._last_progress >= PROGRESS_INTERVAL_S)

    def _wait_motion(self, axis: str, watch_limit: bool,
                     timeout_s: float = 30.0) -> bool:
        """Wait for a step to finish, stopping the axis if the limit trips.

        Returns True if the limit tripped and the move was aborted.

        This replaces GMotionComplete during a move because LD=3 disables the
        controller's own limit action - nothing in hardware stops the axis - and
        GMotionComplete would happily block until a bulk step of several hundred
        microns had finished. The limit has to be watched WHILE the axis moves,
        with ST issued the moment it trips.

        watch_limit is False when the move started with the limit already active,
        i.e. an escape move driving off the switch. Aborting on a level that is
        active from the outset would make escaping impossible, so only a
        clear->active transition counts.

        Reaction distance is one poll, about 16 ms on this link: ~2.5 um at the
        500 counts/s fine speed, ~20 um at 4000. Better than the whole step, and
        the software check is all there is.
        """
        t0 = time.perf_counter()
        while True:
            moving, limit_active = self._read_motion_state(axis)

            if watch_limit and limit_active:
                self._g.GCommand(f"ST{axis}")
                logger.warning(f"{axis}: limit tripped mid-move - stopping")
                # Let the deceleration finish so the caller measures a stopped axis.
                while True:
                    still_moving, _ = self._read_motion_state(axis)
                    if not still_moving or time.perf_counter() - t0 > timeout_s:
                        break
                return True

            if not moving:
                return False

            if self._progress_due():
                self._read_tp(axis)

            if time.perf_counter() - t0 > timeout_s:
                self._g.GCommand(f"ST{axis}")
                logger.error(f"{axis}: motion did not complete in {timeout_s:.0f} s; stopped")
                return False

    def encoder_status(self, axis: str) -> int:
        """_SSm0, the BiSS status word. 0 means the encoder is healthy.

        Bit 0 timeout, 1 CRC invalid, 2 warning (degraded signal, data still
        valid), 3 ERROR (data not valid). Only the error bit says the position
        cannot be trusted - B and C both sit at warning on this rig at rest.

        Not part of poll_all_axes(): it is one more round trip per axis, and the
        poll cycle is what sets the jog overshoot. Read it when it matters.
        """
        return int(self._read_float(f"MG _SS{axis.upper()}0"))

    def digital_input(self, n: int) -> int:
        """@IN[n], a general digital input: 1 or 0, as the controller reports it.

        Optoisolated and active low: 0 means current is flowing through the
        input, i.e. it has been pulled to the other side of INCOM.
        """
        return int(self._read_float(f"MG @IN[{int(n)}]"))

    def limit_tripped(self, axis: str) -> bool:
        """Is this axis's ONE switch pressed? Says nothing about WHICH end.

        The raw signal, and the right question WHILE MOVING: the mid-move watch
        wants "has the switch just closed", and _wait_motion's arming is what
        handles the case of starting on it. Asking limit_active() there would
        abort an escape the moment the move updated last_dir.

        An axis with no switch answers False WITHOUT being read - under CN=1 an
        empty input reads ACTIVE, and believing it would pin the axis at a limit
        that does not exist. Not reading it also keeps the poll cycle short.

        _LF/_LR are not raw input readouts. The manual is explicit that they are
        derived and affected by CN: 1 means the limit state WILL allow motion
        that way, 0 means it will not. So 0 is "active" whatever CN is set to;
        CN decides which electrical level produces it.
        """
        which = LIMIT_INPUT.get(axis)
        if which is None:
            return False
        operand = "_LF" if which == "FWD" else "_LR"
        tripped = self._read_float(f"MG {operand}{axis}") == 0.0

        # Latch WHICH END this is, on the clear->pressed transition only.
        #
        # The switch cannot say which end it is, so the direction of travel that
        # reached it is the answer - but that has to be remembered, not re-read.
        # _last_dir is the LAST COMMANDED direction, and a move that drives OFF
        # the switch sets it the moment it starts, which silently re-labels the
        # still-pressed switch as belonging to the direction now being
        # travelled. The guard then halts the escape on the switch it is
        # escaping. Observed on the rig 2026-09-24: Insert Z ran for 128 ms and
        # stopped, the same reading at 8581970 counts reported FWD before the
        # jog and REV after it.
        if tripped:
            if not self._limit_trip_dir.get(axis):
                self._limit_trip_dir[axis] = self._last_dir.get(axis, 0)
        else:
            self._limit_trip_dir[axis] = 0
        return tripped

    def declare_limit_end(self, axis: str, direction: int):
        """Say which END the axis is at, from evidence other than this session.

        The switch cannot say which end it is, and the latch in limit_tripped()
        only knows the direction travelled IN THIS PROCESS. After a restart - or
        on a different PC, whose last_direction.json says nothing about what the
        rig did elsewhere - a switch that was already pressed gets labelled from
        a stale guess, and the wrong label blocks the one direction that escapes.

        The Z retract check uses this once it has established, from the encoder,
        that the head is parked off the tape: that end is the retract direction.
        `direction` is in CONTROLLER terms. If the switch is not pressed the
        latch clears itself on the next read, leaving only the direction.
        """
        axis = axis.upper()
        direction = +1 if direction > 0 else -1
        self._last_dir[axis] = direction
        self._limit_trip_dir[axis] = direction
        if last_dir.get(axis) != direction:
            last_dir[axis] = direction
            save_last_dir()
        logger.info(f"{axis}: limit end declared {'+' if direction > 0 else '-'}")

    def limit_active(self, axis: str, direction: int) -> bool:
        """Is the end stop at `direction` on `axis` active?

        THE single rule, asked by the poll and by every motion path before it
        starts. With one switch per axis a trip means "at an end", and the
        direction that REACHED it is the only thing that says which end - so
        this is also exactly the question "would moving `direction` drive
        further in".

        That direction is the one latched when the switch closed, NOT the last
        commanded one; see limit_tripped(). Using _last_dir here is what made an
        escape stop itself, because starting the escape rewrote it.

        `direction` is in CONTROLLER terms. The opposite direction always
        answers False, which is what lets the axis drive back off the switch;
        blocking both would strand it there with no way out.
        """
        if not self.limit_tripped(axis):
            return False
        # The latch is 0 only if the switch was already closed before this
        # object existed and nothing has moved since; _last_dir is then the best
        # available guess, seeded from last_direction.json.
        return direction == (self._limit_trip_dir.get(axis)
                             or self._last_dir.get(axis, 0))

    def _read_motion_state(self, axis: str) -> tuple[bool, bool]:
        """(moving, limit_active), one operand per command.

        This used to ask for both in one command - "MG _BGx, _LFx" - to save a
        16 ms round trip during a move. That optimisation is withdrawn: a
        combined MG whose reply the host does not consume exactly as sent leaves
        a response in the buffer, and from then on every read returns the
        PREVIOUS command's answer. That is the desynchronisation seen on
        2026-09-22, where a poll read came back as '3230950.0000

: 1.0000' -
        a position and a limit reading merged into one reply - after a timeout.

        Whether the combined form caused it is not proven, but it is the only
        place that assumed a multi-operand reply, and 16 ms is not worth the
        risk of scrambling every subsequent reading.
        """
        moving = self._read_float(f"MG _BG{axis}") != 0.0
        # The RAW switch state, deliberately: see limit_tripped(). Resolving it
        # against last_dir here would abort an escape move, because the move
        # rewrites last_dir as it steps.
        return moving, self.limit_tripped(axis)

    @staticmethod
    def _parse_reply(cmd: str, resp: str) -> float:
        """One number from one reply, with a clear error if the link has shifted.

        A desynchronised link returns the previous command's answer glued to this
        one ('3230950.0000

: 1.0000'). Left to float() that surfaces as
        "could not convert string to float", which says nothing about the real
        problem, so name it.
        """
        text = resp.strip()
        cleaned = text.rstrip(":").strip()
        if ":" in cleaned or len(cleaned.split()) > 1:
            raise RuntimeError(
                f"desynchronised reply to {cmd!r}: {resp!r}. The link is "
                f"returning a previous command's answer; reconnect to resync."
            )
        return float(cleaned)

    def _read_float(self, cmd: str) -> float:
        return self._parse_reply(cmd, self._g.GCommand(cmd))

    def _wait_until_still(self, axis: str, tol_counts: float = 1.0,
                          window_ms: int = 200, timeout_s: float = 3.0) -> float:
        """Block until _TP stops changing, and return the settled reading.

        GMotionComplete returns when the PROFILER is done, which is not when the
        stage has stopped. KS smoothing and the mechanical tail keep the encoder
        moving after it returns - measured on axis C at up to 56 encoder counts
        (4.4 um) on a 100 um move, and 376 counts (29 um) on a 500 um one, the
        deficit scaling with how fast the axis was travelling when the profile
        ended. Reading at that moment makes the loop correct against a stale
        position and then overshoot when the rest of the motion arrives.

        A fixed dwell cannot fix this: the tail is 0-94 ms depending on the move.
        Waiting for the encoder to go quiet adapts to it. Note that WT is NOT a
        way to do this - it is a program-flow command, and in immediate mode it
        returns straight away without delaying anything (measured: WT=20 has the
        same 15 ms round trip as PR and BG).

        Stillness is judged over a WINDOW, not between consecutive reads. Reads
        are about 16 ms apart on this link, so "two reads within one count" also
        passes for a steady crawl of 62 counts/s - and that is exactly what got
        through the first version of this check: every move on axis C still
        gained 3 counts (0.234 um) after it returned. Requiring the whole spread
        across window_ms to stay inside tol_counts closes that gap, at the cost
        of window_ms per measurement.
        """
        t0 = time.perf_counter()
        window_s = window_ms / 1000.0
        samples: list[tuple[float, float]] = []

        while True:
            now = time.perf_counter()
            samples.append((now, self._read_tp(axis)))
            recent = [s for s in samples if now - s[0] <= window_s]

            # Observed for at least a full window, with enough samples in it to
            # see a trend, and nothing moved across the whole of it.
            if len(recent) >= 3 and now - samples[0][0] >= window_s:
                span = max(p for _, p in recent) - min(p for _, p in recent)
                if span <= tol_counts:
                    return recent[-1][1]

            if now - t0 > timeout_s:
                logger.warning(
                    f"{axis}: encoder still moving after {timeout_s:.1f} s; "
                    f"measuring anyway at {samples[-1][1]:.0f}"
                )
                return samples[-1][1]

    def move_microns(self, axis: str, microns: float, speed=None,
                     tol_counts: float = 1.0, gain: float = 0.9,
                     max_steps: int = 40, settle_ms: int = 20,
                     timeout: int = 30, snap: bool = True,
                     fine_speed=None, fine_below_um: float = FINE_BELOW_UM,
                     settle_window_ms: int = 200,
                     button_state_callback=None,
                     absolute_target=None) -> float:
        """Move one axis a signed distance in microns (+ forward, - reverse),
        closing the loop on the encoder.

        The target is set once, in ENCODER counts:

            target = _TP + microns * ENCODER_COUNTS_PER_UM

        and then approached in steps. Each step reads _TP, takes the remaining
        error, converts it to motor counts with THAT axis's own counts_per_um,
        commands `gain` of it, waits for motion to complete, and measures again.
        The move ends when the error is inside tol_counts, not when some
        precomputed number of counts has been issued - so a step the motor drops,
        or a count lost to rounding, gets corrected instead of accumulating.

        gain < 1 deliberately undershoots, which keeps the whole move
        unidirectional. That matters more than speed of convergence: a reversal
        pays the axis's lost motion (~3 um on B, 8.9-16.9 um on C) before the
        encoder moves at all, far more than any error being corrected. If the
        axis does overshoot, the loop stops and says so rather than reversing.

        Speed is staged: the configured axis speed (or `speed`) for the bulk of
        the move, dropping to `fine_speed` once the remaining error is inside
        `fine_below_um`. Speed feeds the settling tail, and the tail is what the
        loop has to wait out between steps, so the corrections are deliberately
        slower than the approach.

        snap (default True) rounds the request to a whole microstep first. Only
        those positions exist - 1 um is 6.4 microsteps and simply cannot be
        reached - so without it the loop converges on a neighbour and reports a
        permanent error that no amount of tuning removes. With it, the target is
        the nearest reachable point (1 um -> 1.09375 um, 7 microsteps) and the
        reported error is the real one. Pass snap=False to see the raw residual.

        settle_ms is a dwell after each step, and _wait_until_still() then waits
        for the encoder to actually stop before measuring - motion complete does
        not mean stopped on this drivetrain.

        settle_window_ms is the speed/accuracy dial. Every measurement costs one
        window, and a move takes three or four of them, so 200 ms means roughly a
        second per move. Interactive callers (the jog panel) pass something much
        shorter and accept a slightly looser landing; see JOG_SETTLE_WINDOW_MS.

        The loop ends on the first of four conditions:
          - error within tol_counts                      (converged)
          - the next step rounds to 0 motor counts       (resolution floor)
          - three steps in a row fail to shrink the error (stiction/dropped steps)
          - max_steps                                    (runaway guard)

        RESOLUTION FLOOR: the motor is 6.4 counts/um and the encoder 12.8, so one
        microstep - the smallest step the SDM-44140 can take - moves the encoder
        2 counts. Anything under that cannot be corrected, only overshot, so the
        loop stops there instead of dithering. tol_counts=1 is therefore the
        useful floor, and the achieved error is +-1 count (0.078 um) whenever the
        last correction divides into whole microsteps.

        Returns the encoder-measured distance actually travelled, in microns.
        """
        axis = axis.upper()
        if axis not in AXES:
            raise ValueError(f"Unknown axis {axis!r}; expected one of {AXES}")

        cpm_axis = MOTOR_COUNTS_PER_UM[axis]
        microns = float(microns)
        if microns == 0.0:
            return 0.0

        # The jog panel greys out the button that started the move until it ends.
        btn_dir = "+" if microns > 0 else "-"
        if button_state_callback is not None:
            button_state_callback(axis, btn_dir, False)
        try:
            return self._move_microns(
                axis, microns, cpm_axis, speed, tol_counts, gain, max_steps,
                settle_ms, timeout, snap, fine_speed, fine_below_um,
                settle_window_ms, absolute_target)
        finally:
            if button_state_callback is not None:
                button_state_callback(axis, btn_dir, True)

    def _move_microns(self, axis, microns, cpm_axis, speed, tol_counts, gain,
                      max_steps, settle_ms, timeout, snap, fine_speed,
                      fine_below_um, settle_window_ms,
                      absolute_target=None) -> float:
        """The loop itself; move_microns() wraps it for button state.

        absolute_target pins the encoder position to drive to, instead of
        deriving it from `microns`. move_to_counts() uses it so an absolute move
        cannot drift: if the target were recomputed from a fresh reading, any
        creep between the caller's reading and this one would be added to it.
        `microns` is still used for the travel and limit guards.
        """

        if snap:
            # Only whole microsteps are reachable, so an off-grid request cannot
            # be honoured however long the loop runs: asking for 1 um (6.4
            # microsteps) it would converge on 6 or 7 and report a permanent
            # error. Rounding the TARGET to the grid first makes the residual
            # real - and picks the nearer neighbour deterministically instead of
            # letting the approach direction decide.
            snapped = round(microns * cpm_axis) / cpm_axis
            if snapped == 0.0:
                logger.info(
                    f"{axis}: {microns:+.4f} um is under half a microstep "
                    f"({1.0 / cpm_axis:.5f} um); nothing to command"
                )
                return 0.0
            if snapped != microns:
                logger.info(
                    f"{axis}: {microns:+.4f} um is {microns * cpm_axis:.2f} "
                    f"microsteps - not reachable. Targeting {snapped:+.5f} um "
                    f"({round(microns * cpm_axis):+d} microsteps) instead."
                )
            microns = snapped

        travel_um = AXIS_TRAVEL_MM[axis] * 1000.0
        if abs(microns) > travel_um:
            raise ValueError(
                f"{axis}: {microns:+.1f} um exceeds the axis travel of "
                f"{travel_um:.0f} um, and LD=3 leaves no hardware limit to catch it"
            )

        direction = +1 if microns > 0 else -1

        # One switch per axis, so an active limit says "at an end" and the last
        # commanded direction says which. limit_active() carries that, and the
        # opposite direction stays free so this move can be the escape.
        if self.limit_active(axis, direction):
            raise RuntimeError(
                f"{axis}: limit active - {microns:+.1f} um drives further into it"
            )

        cpm = cpm_axis
        enc_per_motor = ENCODER_COUNTS_PER_UM / cpm   # 2.0 encoder counts per microstep
        tol = max(float(tol_counts), 0.0)

        # Speed staging. The first step covers the bulk of the distance and wants
        # to be quick; the corrections after it are a few microsteps and want to
        # be gentle, because speed is what feeds the settling tail. Sending SP
        # only when the band changes keeps it to one extra command per move.
        coarse_sp = float(speed) if speed is not None else float(AXIS_SPEED[axis])
        fine_sp = float(fine_speed) if fine_speed is not None else min(coarse_sp,
                                                                      FINE_SPEED)
        fine_below_counts = abs(fine_below_um) * ENCODER_COUNTS_PER_UM
        current_sp = None

        start_tp = self._wait_until_still(axis, window_ms=settle_window_ms)
        target_tp = (float(absolute_target) if absolute_target is not None
                     else start_tp + microns * ENCODER_COUNTS_PER_UM)
        logger.info(
            f"Move {axis} {microns:+.3f} um: {start_tp:.0f} -> {target_tp:.0f} encoder "
            f"counts, tolerance {tol:.0f} counts ({tol / ENCODER_COUNTS_PER_UM:.3f} um)"
        )

        error = target_tp - start_tp
        no_progress = 0

        # If the switch is already active the pre-move guard has established that
        # this move drives OFF it, so the mid-motion watch must ignore a level
        # that is active from the start - otherwise escaping is impossible. Only
        # a clear->active transition aborts.
        _, limit_at_start = self._read_motion_state(axis)
        watch_limit = not limit_at_start
        if limit_at_start:
            logger.info(f"{axis}: starting on an active limit - escape move, "
                        f"limit watch disarmed for this move")

        for step in range(1, max_steps + 1):
            if abs(error) <= tol:
                break

            # Remaining encoder error -> motor counts, damped by gain.
            counts = int(round(error / enc_per_motor * gain))

            if counts == 0:
                # What is left is finer than one microstep, so the smallest step
                # the drive can take would overshoot it. This is the floor.
                logger.info(
                    f"{axis}: {error:+.0f} encoder counts "
                    f"({error / ENCODER_COUNTS_PER_UM:+.4f} um) left, below one "
                    f"microstep - at the resolution floor"
                )
                break

            if (counts > 0) != (direction > 0):
                # Overshot. Correcting means reversing, and a reversal pays the
                # axis's lost motion before the encoder moves at all - so the
                # correction would cost more than the error. Stop and report it;
                # re-approaching deliberately from one side is the caller's job.
                logger.warning(
                    f"{axis}: overshot by {-error / ENCODER_COUNTS_PER_UM:+.4f} um; "
                    f"not reversing into backlash to fix it"
                )
                break

            new_dir = +1 if counts > 0 else -1
            self._last_dir[axis] = new_dir
            if last_dir.get(axis) != new_dir:
                last_dir[axis] = new_dir     # what poll_all_axes reports
                save_last_dir()              # only on change, not every step

            want_sp = fine_sp if abs(error) <= fine_below_counts else coarse_sp
            if want_sp != current_sp:
                self.send(f"SP{axis}={want_sp:g}")
                current_sp = want_sp

            self.send(f"PR{axis}={counts}")
            self.send(f"BG{axis}")

            # Watches the limit while the axis moves and stops it on a trip.
            # GMotionComplete cannot do this: it blocks until the step is over,
            # and with LD=3 nothing in hardware would have stopped the axis.
            tripped = self._wait_motion(axis, watch_limit, timeout_s=timeout)

            if tripped:
                settled = self._wait_until_still(axis, window_ms=settle_window_ms)
                error = target_tp - settled
                logger.warning(
                    f"{axis}: limit reached, stopping "
                    f"{error / ENCODER_COUNTS_PER_UM:+.3f} um short of target"
                )
                break

            if settle_ms:
                # Dwell in the HOST, not with WT: WT is a program-flow command
                # and does not delay an immediate-mode call at all.
                time.sleep(settle_ms / 1000.0)

            # Then wait for the stage itself, because motion complete does not
            # mean stopped. Everything below measures the settled position.
            new_error = target_tp - self._wait_until_still(
                axis, window_ms=settle_window_ms)
            logger.debug(
                f"Move {axis} step {step}: commanded {counts:+d} counts, error "
                f"{error:+.0f} -> {new_error:+.0f} encoder counts"
            )

            # Closing in means the error magnitude shrank. Anything else - a step
            # too small to overcome stiction, or oscillation about a target finer
            # than one microstep - is the loop's signal to stop.
            if abs(new_error) < abs(error):
                no_progress = 0
            else:
                no_progress += 1
                if no_progress >= 3:
                    logger.warning(
                        f"{axis}: no progress in 3 steps, stopping at "
                        f"{new_error:+.0f} encoder counts "
                        f"({new_error / ENCODER_COUNTS_PER_UM:+.3f} um) of error"
                    )
                    error = new_error
                    break
            error = new_error
        else:
            logger.warning(
                f"{axis}: hit max_steps={max_steps} with {error:+.0f} encoder counts "
                f"({error / ENCODER_COUNTS_PER_UM:+.3f} um) still to go"
            )

        moved_um = (self._wait_until_still(axis, window_ms=settle_window_ms)
                    - start_tp) / ENCODER_COUNTS_PER_UM
        logger.info(
            f"Move {axis} done: commanded {microns:+.3f} um, encoder saw "
            f"{moved_um:+.3f} um (error {error / ENCODER_COUNTS_PER_UM:+.4f} um)"
        )
        return moved_um

    def post_move_verify(self, axis: str, target_counts: float,
                         relax_s: float = RELAX_SETTLE_S,
                         tol_counts: float = 1.0,
                         max_passes: int = VERIFY_MAX_PASSES,
                         settle_window_ms: int = 200, **kwargs) -> float:
        """Wait out the post-move relaxation, then close what is left.

        A move ends where the loop last measured, but the stage keeps creeping
        for seconds afterwards - a constant ~0.5 um on axis C, always in the
        direction of travel (see the RELAX_* constants for the measurement). This
        waits `relax_s` for that to arrive, re-measures, and corrects.

        It can only correct an UNDERSHOOT. If the stage has drifted PAST the
        target, fixing it means reversing, and a reversal costs 7-11 um of
        take-up on this axis to recover 0.2 um of error - so it is reported and
        left alone. Aim short in the first place: move_to_counts(verify=True)
        does that, and this closes the remainder in the same direction.

        Returns the residual error in microns (signed, + means short of target).
        """
        axis = axis.upper()
        if axis not in AXES:
            raise ValueError(f"Unknown axis {axis!r}; expected one of {AXES}")

        approach = self._last_dir.get(axis, 0)
        last_error = None

        for p in range(1, max_passes + 1):
            time.sleep(relax_s)
            pos = self._wait_until_still(axis, window_ms=settle_window_ms)
            error = float(target_counts) - pos

            if abs(error) <= tol_counts:
                logger.info(
                    f"{axis}: verified after {p} pass(es), "
                    f"{error / ENCODER_COUNTS_PER_UM:+.4f} um from target"
                )
                return error / ENCODER_COUNTS_PER_UM

            # Would correcting this drive the other way?
            if approach and (error > 0) != (approach > 0):
                logger.warning(
                    f"{axis}: settled {-error / ENCODER_COUNTS_PER_UM:+.4f} um "
                    f"PAST the target. Not correcting - a reversal costs 7-11 um "
                    f"of take-up to recover {abs(error) / ENCODER_COUNTS_PER_UM:.3f} um. "
                    f"Aim shorter (RELAX_UNDERSHOOT_UM) if this repeats."
                )
                return error / ENCODER_COUNTS_PER_UM

            # Not converging: another pass would only add more relaxation.
            if last_error is not None and abs(error) >= abs(last_error):
                logger.warning(
                    f"{axis}: verify stopped improving at "
                    f"{error / ENCODER_COUNTS_PER_UM:+.4f} um; stopping"
                )
                return error / ENCODER_COUNTS_PER_UM

            # A correction is itself a move, so it starts its own relaxation -
            # about RELAX_UNDERSHOOT_UM of fresh creep in the same direction.
            # Correcting a shortfall SMALLER than that trades a known small
            # error for an unknown larger one; simulation showed it turning a
            # +0.22 um residual into +0.32. Leave it to the relaxation.
            error_um = error / ENCODER_COUNTS_PER_UM
            if abs(error_um) < RELAX_UNDERSHOOT_UM:
                logger.info(
                    f"{axis}: {error_um:+.4f} um short, less than the "
                    f"{RELAX_UNDERSHOOT_UM:.2f} um of creep a correction would "
                    f"itself cause - leaving it"
                )
                return error_um

            logger.info(
                f"{axis}: verify pass {p}, {error_um:+.4f} um "
                f"short after {relax_s:.1f} s - closing it"
            )
            last_error = error
            self.move_to_counts(axis, target_counts, verify=False,
                                tol_counts=tol_counts,
                                settle_window_ms=settle_window_ms, **kwargs)

        pos = self._wait_until_still(axis, window_ms=settle_window_ms)
        residual = (float(target_counts) - pos) / ENCODER_COUNTS_PER_UM
        logger.warning(f"{axis}: verify used all {max_passes} passes, "
                       f"{residual:+.4f} um remaining")
        return residual

    def move_to_counts(self, axis: str, target_counts: float, verify: bool = False,
                       undershoot_um: float = RELAX_UNDERSHOOT_UM,
                       relax_s: float = RELAX_SETTLE_S, **kwargs) -> float:
        """Closed-loop move to an ABSOLUTE encoder reading (`_TP`).

        verify=True aims `undershoot_um` SHORT of the target, waits `relax_s` for
        the post-move relaxation to carry the stage the rest of the way, then
        runs post_move_verify() to close any remainder. Without it the move ends
        where the loop last measured and then creeps ~0.5 um past the target over
        the next few seconds. Costs about `relax_s` per pass, so it is for
        deliberate moves (HOME, go-to-position), not for jogging.

        The same loop as move_microns() - step, measure, correct, stop inside
        tolerance - but aimed at a fixed encoder position instead of a distance.
        That is what a saved position is: `_TP` is absolute in the encoder's PRC
        code space, so a stored reading identifies a physical place and does not
        need to be turned into a relative move by the caller.

        Returns the distance travelled in microns, as move_microns() does.

        Snapping is off: the target is a place, not a distance, so rounding it to
        the microstep grid would be rounding the destination. The loop lands
        within a microstep of it, which is the floor regardless.
        """
        axis = axis.upper()
        if axis not in AXES:
            raise ValueError(f"Unknown axis {axis!r}; expected one of {AXES}")

        current = self._wait_until_still(
            axis, window_ms=kwargs.get("settle_window_ms", 200))
        error_counts = float(target_counts) - current

        if abs(error_counts) <= kwargs.get("tol_counts", 1.0):
            logger.info(f"{axis}: already within tolerance of {target_counts:.0f}")
            return 0.0

        aim = float(target_counts)
        if verify:
            # Stop short along the direction of travel. Capped at half the move
            # so a short hop is not turned into something under the stiction
            # threshold (0.31 um measured) that would not move at all.
            short = min(abs(undershoot_um) * ENCODER_COUNTS_PER_UM,
                        abs(error_counts) / 2.0)
            aim -= short * (1 if error_counts > 0 else -1)
            logger.info(
                f"{axis}: aiming {short / ENCODER_COUNTS_PER_UM:.3f} um short of "
                f"{target_counts:.0f} and letting the relaxation carry it in"
            )

        moved = self.move_microns(
            axis, (aim - current) / ENCODER_COUNTS_PER_UM,
            snap=False, absolute_target=aim, **kwargs)

        if verify:
            self.post_move_verify(axis, float(target_counts), relax_s=relax_s,
                                  **kwargs)
            moved = (self._wait_until_still(
                axis, window_ms=kwargs.get("settle_window_ms", 200))
                - current) / ENCODER_COUNTS_PER_UM
        return moved

    def move_relative(self, axis: str, counts: float, speed=None,
                      wait: bool = False, timeout: int = 30):
        direction = +1 if counts >= 0 else -1
        if counts != 0 and self.limit_active(axis, direction):
            raise RuntimeError(
                f"{'Forward' if direction > 0 else 'Reverse'} limit active on {axis}")
        self._last_dir[axis] = direction
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
        if self.limit_active(axis, direction):
            raise RuntimeError(
                f"{'Forward' if direction > 0 else 'Reverse'} limit active on {axis}")
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

    def move_all_absolute(self, positions: dict, speeds: dict = None,
                          wait: bool = True, timeout: int = 60,
                          verify: bool = False, **kwargs):
        """Drive several axes to absolute encoder readings, one after another.

        `positions` maps axis -> target `_TP`. It used to subtract the current
        position from each value and hand the difference to
        move_relative_closed_loop(); the jog panel had ALREADY subtracted the
        current position before calling, so the move came out as
        (target - current) - current. It also inherited that function's stale
        x1.25 encoder factor. Both are gone: the targets are absolute and
        move_to_counts() does the work.

        Sequential, not simultaneous - each move closes its own loop, and
        interleaving them would mean sharing the link between three correction
        loops at once.
        """
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
                logger.error(f"move_all_absolute {ax}: {e}")
                results[ax] = 0.0
        return results

    def define_zero(self, axes: str = "ABC"):
        for ax in axes:
            self.send(f"DP{ax}=0")
        logger.info(f"Define zero {axes}")

