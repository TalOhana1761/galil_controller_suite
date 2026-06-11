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


class PollingWorker(QThread):
    status_updated = pyqtSignal(dict)    # {axis: AxisStatus}
    error_occurred = pyqtSignal(str)
    log_line       = pyqtSignal(str, str, str)   # timestamp, direction, text
    jog_button_state = pyqtSignal(str, str, bool)  # axis, direction (+/-), enabled

    POLL_MS = 100

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.cmd_queue  = Queue()
        self._running   = True
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

            # 2. Synchronous encoder read — blocks until each response arrives
            try:
                status = self.controller.poll_all_axes()
                self.status_updated.emit(status)

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

            self.msleep(self.POLL_MS)

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
