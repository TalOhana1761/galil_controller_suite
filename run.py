"""
Galil DMC-4133 Control Suite — main entry point.

Usage:
    python run.py             # connect to real controller
    python run.py --mock      # mock controller (no hardware required)
    python run.py --address COM3
    python run.py --address 192.168.0.40
"""
import sys
import os
import argparse

# Make sure sibling packages resolve correctly when run from any directory
sys.path.insert(0, os.path.dirname(__file__))

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt

from ui.main_window import GalilMainWindow
from utils.logger_setup import setup_logger, LogBridge


def main():
    parser = argparse.ArgumentParser(description="Galil DMC-4133 Control Suite")
    parser.add_argument("--mock", action="store_true",
                        help="Run with simulated controller (no hardware)")
    parser.add_argument("--address", type=str, default=None,
                        help="Controller address override (e.g. COM3 or 192.168.0.40)")
    args = parser.parse_args()

    # High-DPI support
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("Galil DMC-4133 Control Suite")

    # Load dark theme
    qss_path = os.path.join(os.path.dirname(__file__), "ui", "dark_theme.qss")
    try:
        with open(qss_path, encoding="utf-8") as f:
            app.setStyleSheet(f.read())
    except FileNotFoundError:
        print(f"Warning: stylesheet not found at {qss_path}")

    setup_logger()

    if args.mock:
        from controller.mock_controller import MockGalilController
        controller = MockGalilController()
        print("Running in MOCK MODE — no hardware required.")
        print("All axis positions simulated with realistic encoder drift.")
    else:
        from controller.galil_controller import GalilController
        controller = GalilController()
        if args.address:
            # Pre-populate the last_address so the connection panel shows it
            try:
                import json
                cfg_path = os.path.join(os.path.dirname(__file__), "config", "settings.json")
                with open(cfg_path) as f:
                    cfg = json.load(f)
                cfg["last_address"] = args.address
                with open(cfg_path, "w") as f:
                    json.dump(cfg, f, indent=2)
            except Exception:
                pass

    window = GalilMainWindow(controller, use_mock=args.mock)
    window.setWindowTitle("Galil DMC-4133 — Motion Control Suite")
    window.setMinimumSize(1280, 800)
    window.resize(1400, 860)
    window.show()

    # Wire loguru → UI log panel (must happen after window is built)
    bridge = LogBridge()
    setup_logger(bridge=bridge)
    bridge.log_line.connect(window.log_panel.on_log_line)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
