"""
Stork Controller (Galil DMC-4133) — main entry point.

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

# Both of these must run BEFORE the controller modules are imported: they build
# their scale tables from settings.json at IMPORT time, and galil_controller
# pulls in gclib, which resolves its DLLs the moment it loads. Running from
# source both calls are no-ops - see utils/app_paths.py.
from utils.app_paths import (seed_user_config, add_bundled_dll_dir,
                             install_root, config_path, icon_path,
                             DISPLAY_NAME)
seed_user_config()
add_bundled_dll_dir()

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIcon

from ui.main_window import GalilMainWindow
from utils.logger_setup import setup_logger, LogBridge


def _fit_to_screen(window):
    """Open at the content's size, clamped to the screen, and centred on it."""
    screen = QApplication.primaryScreen()
    want = window.sizeHint()
    if screen is None:
        window.resize(want)
        return
    avail = screen.availableGeometry()
    # Leave room for the title bar and frame, which availableGeometry() does
    # not subtract and the window size does not include.
    # The +4 absorbs fractional-scale rounding (125 %, 150 %), which otherwise
    # leaves the panel a pixel short and shows a pointless scroll bar.
    w = min(want.width() + 4, avail.width() - 16)
    h = min(want.height() + 4, avail.height() - 40)
    window.resize(w, h)
    window.move(avail.x() + (avail.width() - w) // 2,
                avail.y() + max(0, (avail.height() - h - 30) // 2))


def main():
    parser = argparse.ArgumentParser(description=DISPLAY_NAME)
    parser.add_argument("--mock", action="store_true",
                        help="Run with simulated controller (no hardware)")
    parser.add_argument("--address", type=str, default=None,
                        help="Controller address override (e.g. COM3 or 192.168.0.40)")
    args = parser.parse_args()

    # High-DPI support. All three must be set BEFORE the QApplication exists.
    #
    # The rounding policy is the one that matters on other PCs. Qt 5 defaults
    # to ROUNDING the Windows scale factor to a whole number, so 125 % became
    # 1x and 150 % became 2x. At 1x the point-sized fonts still grew by 25 %
    # while every pixel size (sidebar width, button heights, the D-pad) did
    # not, so text got clipped; at 2x the window came out twice the size and
    # did not fit the screen. PassThrough scales by the real factor (1.25,
    # 1.5), so fonts and pixel sizes stay in proportion on every screen.
    # This PC runs at 100 %, the one setting where rounding changes nothing,
    # which is why it never showed here.
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    if hasattr(QApplication, "setHighDpiScaleFactorRoundingPolicy"):   # Qt >= 5.14
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    # Its own taskbar identity, so Windows shows the stork there rather than
    # grouping the window under python.exe when run from source.
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "Technion.LightInjector.StorkController")
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setApplicationName(DISPLAY_NAME)
    # Set on the application, so every window and dialog carries the stork.
    # The frozen .exe has it embedded too, but from source the process is
    # python.exe, whose icon Qt would otherwise show in the title bar.
    if os.path.isfile(icon_path()):
        app.setWindowIcon(QIcon(icon_path()))

    # Load dark theme
    qss_path = os.path.join(install_root(), "ui", "dark_theme.qss")
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
                cfg_path = config_path("settings.json")
                with open(cfg_path) as f:
                    cfg = json.load(f)
                cfg["last_address"] = args.address
                with open(cfg_path, "w") as f:
                    json.dump(cfg, f, indent=2)
            except Exception:
                pass

    window = GalilMainWindow(controller, use_mock=args.mock)
    window.setWindowTitle(DISPLAY_NAME)
    # Open at the size the content asks for (the largest panel, i.e. the jog
    # panel) rather than a fixed one: a hardcoded 1400x860 left a large empty
    # band below every panel. The layouts' own minimums stop it shrinking
    # further, and the user can still enlarge or maximise it.
    #
    # ...but never larger than the screen it opens on. A small laptop screen
    # at 125-150 % has less room than the jog panel asks for; each panel sits in
    # a scroll area (see main_window), so a clamped window scrolls rather than
    # hiding the bottom of the panel - and HALT/E-STOP, which live outside the
    # scroll area, stay on screen.
    _fit_to_screen(window)
    window.show()

    # Wire loguru → UI log panel (must happen after window is built)
    bridge = LogBridge()
    setup_logger(bridge=bridge)
    bridge.log_line.connect(window.log_panel.on_log_line)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
