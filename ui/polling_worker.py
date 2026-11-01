"""
PollingWorker — background QThread that drives all hardware I/O.

Architecture rule: NEVER call gclib functions from the main (Qt) thread.
All controller operations go through this worker via cmd_queue.

Polling cycle (every POLL_MS):
  1. Drain command queue (execute any pending motor commands).
  2. Read all axis status synchronously from the encoder.
  3. Emit status_updated signal → UI panels update their displays.
"""
import time
from queue import Queue, Empty
from loguru import logger
from PyQt5.QtCore import QThread, pyqtSignal


# The digital input the touch alarm is wired to: DI1, pin 2 of the 44-pin I/O
# connector. Pulled up, so 1 = clear and 0 = the manipulator hand touched metal.
TOUCH_ALARM_INPUT = 1


class PollingWorker(QThread):
    status_updated = pyqtSignal(dict)    # {axis: AxisStatus}
    error_occurred = pyqtSignal(str)
    jog_button_state = pyqtSignal(str, str, bool)  # axis, direction (+/-), enabled
    # Touch alarm: True = in alarm, False = clear, None = could not be read.
    touch_alarm = pyqtSignal(object)
    # axis, raw _TP: a position read INSIDE a blocking closed-loop move. The move
    # runs on this thread, so status_updated is silent until it returns; this is
    # what keeps the readout moving meanwhile. Position only - limit flags are
    # not re-read, so it must not be treated as a poll.
    move_progress = pyqtSignal(str, float)

    POLL_MS = 100

    # A desynchronised link reports every reading as the PREVIOUS command's
    # answer, so one bad reading is not noise - everything after it is wrong
    # until the connection is rebuilt. Two in a row means it is not a one-off.
    FAULTS_BEFORE_RECONNECT = 2
    MAX_RECONNECT_ATTEMPTS  = 3

    def __init__(self, controller, soft_limits=None):
        super().__init__()
        self.controller = controller
        # Emitting from this thread is safe: Qt queues it to the GUI thread.
        # The mock has no such hook (its moves land instantly), hence hasattr.
        if hasattr(controller, "position_listener"):
            controller.position_listener = self.move_progress.emit
        # The touch alarm, read from INSIDE a blocking move as well as by the
        # poll, so it does not go stale for the length of a HOME or GO TO. Same
        # method, so the same signal, the same log-on-change and the same
        # failed-read-is-unknown rule. It only reports: what a touch should DO
        # has not been decided, and when it is, _poll_touch_alarm() is where both
        # paths meet.
        if hasattr(controller, "motion_tick"):
            controller.motion_tick = self._poll_touch_alarm
        # utils.soft_limits.SoftLimits, or None for no software bounds. Applied
        # to the poll result BEFORE it is emitted or guarded, so a soft limit is
        # indistinguishable from a switch to everything downstream - buttons,
        # limit table, escape hints and _guard_jog_limits alike.
        self.soft_limits = soft_limits
        # Axes whose BiSS status word to read each cycle, on top of the normal
        # poll. Empty by default: it is one extra round trip per axis and the
        # cycle length is what sets the jog overshoot. The Z retract/insert adds
        # "C" while it needs to know whether the head is back on the tape.
        self.watch_encoder: set = set()
        # Digital input the touch alarm is wired to, or None to not read it. One
        # extra round trip per cycle (~16 ms), which lengthens the cycle and so
        # the jog overshoot by the same fraction.
        self.touch_alarm_input = TOUCH_ALARM_INPUT
        self._touch_prev = None
        self.cmd_queue  = Queue()
        self._running   = True
        self._consecutive_faults = 0
        self._reconnect_attempts = 0
        # Per-axis state for transition detection
        self._prev_sc:  dict[str, int]  = {"A": 10, "B": 10, "C": 10}
        self._prev_fwd: dict[str, bool] = {"A": False, "B": False, "C": False}
        self._prev_rev: dict[str, bool] = {"A": False, "B": False, "C": False}

    def run(self):
        while self._running:
            # 1. Execute any queued motor commands
            while not self.cmd_queue.empty():
                try:
                    fn, args, kwargs = self.cmd_queue.get_nowait()
                    fn(*args, **kwargs)
                except Empty:
                    break
                except Exception as e:
                    self.error_occurred.emit(str(e))
                    if self._is_link_fault(e):
                        self._note_fault(e)

            # 2. Synchronous encoder read — blocks until each response arrives
            try:
                status = self.controller.poll_all_axes()
                for ax in tuple(self.watch_encoder):
                    st = status.get(ax)
                    if st is None:
                        continue
                    try:
                        st.enc_status = self.controller.encoder_status(ax)
                    except Exception as e:
                        logger.warning(f"[{ax}] encoder status read failed: {e}")
                if self.soft_limits is not None:
                    status = self.soft_limits.apply(status)
                self.status_updated.emit(status)
                self._poll_touch_alarm()
                self._consecutive_faults = 0
                self._reconnect_attempts = 0
                self._guard_jog_limits(status)

                # Log position on motion-complete and limit-trip transitions
                for ax, s in status.items():
                    sc = s.stop_code
                    prev = self._prev_sc.get(ax, 10)
                    if prev not in (0, 10) and sc == 10:
                        logger.info(f"[{ax}] Motion complete → {s.pos:.1f} counts")
                    if not self._prev_fwd.get(ax) and s.fwd_limit:
                        logger.warning(f"[{ax}] FWD limit active → {s.pos:.1f} counts")
                    if not self._prev_rev.get(ax) and s.rev_limit:
                        logger.warning(f"[{ax}] REV limit active → {s.pos:.1f} counts")
                    self._prev_sc[ax]  = sc
                    self._prev_fwd[ax] = s.fwd_limit
                    self._prev_rev[ax] = s.rev_limit

            except Exception as e:
                self.error_occurred.emit(f"Poll error: {e}")
                self._note_fault(e)

            self.msleep(self.POLL_MS)

    def _poll_touch_alarm(self):
        """Read the touch alarm input and emit it; log only on a change.

        Pulled up, so 1 is clear and 0 is the alarm. A read failure reports None
        rather than a guess - "unknown" must not display as "Off".
        """
        n = self.touch_alarm_input
        if n is None or not hasattr(self.controller, "digital_input"):
            return
        try:
            active = self.controller.digital_input(n) == 0
        except Exception as e:
            if self._touch_prev is not None:
                logger.warning(f"Touch alarm (DI{n}) could not be read: {e}")
            active = None
        if active != self._touch_prev:
            if active:
                logger.warning(f"TOUCH ALARM ON (DI{n} = 0): the manipulator hand is touching metal")
            elif active is False and self._touch_prev:
                logger.info(f"Touch alarm cleared (DI{n} = 1)")
            self._touch_prev = active
        self.touch_alarm.emit(active)

    # ── Jog safety ───────────────────────────────────────────────────────────

    def _guard_jog_limits(self, status: dict):
        """Stop a continuous jog that has reached a limit.

        A jog has no target, and LD=3 disables the controller's own limit
        action, so nothing else stops it. This is the only thing between a held
        jog key and an end stop.

        Reaction time is one poll cycle, ~100 ms: about 60 um of overtravel at
        the default 4000 counts/s, ~780 um at the slider's 50000 maximum. That
        is the argument for keeping the jog speed modest, not for removing the
        guard.
        """
        for ax, s in status.items():
            direction = self.controller.is_jogging(ax)
            if not direction:
                continue
            # One rule for every axis. Each has ONE switch and the controller
            # has already resolved which END it means from last_direction, so at
            # most one flag is set and the opposite direction stays free. A/B
            # used to need a special case here that forced into_limit whenever
            # either flag showed - since 2026-09-24 that would halt an escape
            # jog, because the flags no longer both rise together.
            into_limit = (s.fwd_limit and direction > 0) or (s.rev_limit and direction < 0)
            if into_limit:
                logger.warning(f"[{ax}] limit reached while jogging - halting")
                try:
                    self.controller.halt_axis(ax)
                except Exception as e:
                    logger.error(f"[{ax}] could not halt on limit: {e}")
                self.error_occurred.emit(f"{ax}: limit reached, jog stopped")

    # ── Link fault handling ──────────────────────────────────────────────────

    @staticmethod
    def _is_link_fault(exc: Exception) -> bool:
        """Is this the link itself failing, rather than a refused move?

        A RuntimeError from a limit guard is a normal refusal and must not
        trigger a reconnect; a timeout or a desynchronised reply is the link.
        """
        text = str(exc).lower()
        return ("timed out" in text or "timeout" in text
                or "desynchronised" in text or "could not convert string" in text)

    def _note_fault(self, exc: Exception):
        """Count link faults and rebuild the connection once they persist.

        poll_all_axes() catches per-axis failures itself and returns defaults, so
        a fault reaching here has already got past that. What it cannot do is
        clear a desync - only reopening the connection does that, because gclib
        has no flush and every subsequent reply stays shifted by one.
        """
        if not self._is_link_fault(exc):
            self._consecutive_faults = 0
            return

        self._consecutive_faults += 1
        logger.warning(f"Link fault {self._consecutive_faults}"
                       f"/{self.FAULTS_BEFORE_RECONNECT}: {exc}")
        if self._consecutive_faults < self.FAULTS_BEFORE_RECONNECT:
            return

        if self._reconnect_attempts >= self.MAX_RECONNECT_ATTEMPTS:
            # Stop trying: reconnecting in a tight loop against dead hardware
            # buries the log and achieves nothing.
            return

        self._reconnect_attempts += 1
        self.error_occurred.emit(
            f"Link desynchronised — reconnecting "
            f"({self._reconnect_attempts}/{self.MAX_RECONNECT_ATTEMPTS})")
        try:
            if self.controller.reconnect():
                self._consecutive_faults = 0
                self.error_occurred.emit("Reconnected")
            elif self._reconnect_attempts >= self.MAX_RECONNECT_ATTEMPTS:
                self.error_occurred.emit(
                    "Reconnect failed — check the cable and reconnect manually")
        except Exception as e:
            logger.error(f"Reconnect raised: {e}")

    def queue_command(self, fn, *args, **kwargs):
        """Thread-safe: schedule a controller operation for next cycle."""
        self.cmd_queue.put((fn, args, kwargs))

    def clear_queue(self):
        """Discard all pending commands (used by HALT/E-STOP)."""
        while not self.cmd_queue.empty():
            try:
                self.cmd_queue.get_nowait()
            except Empty:
                break

    def stop(self):
        self._running = False
        self.wait(2000)
