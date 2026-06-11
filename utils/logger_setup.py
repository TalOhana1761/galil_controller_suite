"""
Loguru logging configuration.
Writes to file + optional Qt UI bridge for live log panel updates.

Architecture:
  LogBridge is a QObject that acts as a loguru sink.  It receives every
  logger.info/warning/error call (from any thread) and re-emits the message
  as a Qt signal so the LogPanel can display it safely on the main thread.

  Usage in run.py:
      bridge = LogBridge()
      setup_logger(bridge=bridge)
      bridge.log_line.connect(window.log_panel.on_log_line)
"""
import os
from loguru import logger


# ── LogBridge ─────────────────────────────────────────────────────────────────
# Imported lazily so this module remains importable without PyQt5 installed
# (e.g. in headless test environments).

class LogBridge:
    """
    Loguru sink that forwards log records to the UI LogPanel via a Qt signal.

    Because loguru calls the sink from whatever thread emitted the log message,
    and Qt signals are automatically queued when crossing thread boundaries,
    this is thread-safe without any additional locking.
    """

    # Level name → LogPanel direction label
    _LEVEL_TO_DIR = {
        "DEBUG":    "INFO",
        "INFO":     "INFO",
        "SUCCESS":  "INFO",
        "WARNING":  "WARN",
        "ERROR":    "ERROR",
        "CRITICAL": "ERROR",
    }

    def __init__(self):
        # Deferred Qt import so tests without PyQt5 still work
        from PyQt5.QtCore import QObject, pyqtSignal

        class _Bridge(QObject):
            log_line = pyqtSignal(str, str, str)   # timestamp, direction, text

        self._obj = _Bridge()
        self.log_line = self._obj.log_line   # expose signal directly on LogBridge

    def __call__(self, message):
        """Called by loguru for every matching log record."""
        record    = message.record
        ts        = record["time"].strftime("%H:%M:%S")
        direction = self._LEVEL_TO_DIR.get(record["level"].name, "INFO")
        text      = record["message"]
        self._obj.log_line.emit(ts, direction, text)


# ── setup_logger ──────────────────────────────────────────────────────────────

def setup_logger(log_file: str = "galil_log.txt", bridge: "LogBridge | None" = None):
    """Configure loguru sinks.

    Args:
        log_file:  Path for the rotating file sink.
        bridge:    Optional LogBridge instance; when provided its __call__ is
                   added as a loguru sink at INFO level so all INFO+ messages
                   appear in the UI log panel.
    """
    logger.remove()

    log_path = os.path.abspath(log_file)
    logger.add(
        log_path,
        rotation="10 MB",
        retention="7 days",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {message}",
        level="DEBUG",
    )

    if bridge is not None:
        logger.add(bridge, level="INFO", format="{message}")

    logger.info(f"Logger initialized → {log_path}")
    return logger
