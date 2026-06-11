"""
Phase 2 smoke test — minimal PyQt5 + gclib threading test.

Use this to verify:
  - gclib connects and reads positions without blocking the UI
  - QThread polling loop works at 100 ms
  - Limit-switch indicators update correctly
  - Motor enable / jog / stop commands reach the controller

Usage:
    python run_smoke_test.py                     # real hardware
    python run_smoke_test.py --mock              # no hardware needed
    python run_smoke_test.py --address COM3
    python run_smoke_test.py --address 192.168.0.40
"""
import sys
import argparse
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QGroupBox, QGridLayout,
)
from PyQt5.QtCore import QThread, pyqtSignal, Qt, pyqtSlot
from PyQt5.QtGui import QFont


# ── Minimal polling worker (standalone, no GalilController dependency) ──────

class _SmokePollThread(QThread):
    """Polls all three axes synchronously every 100 ms."""
    status_updated = pyqtSignal(dict)   # {'A': {...}, 'B': {...}, 'C': {...}}
    error_occurred = pyqtSignal(str)

    def __init__(self, g):
        super().__init__()
        self._g = g
        self._running = True

    def run(self):
        while self._running:
            try:
                data = {}
                for ax in ("A", "B", "C"):
                    data[ax] = {
                        "pos": float(self._g.GCommand(f"MG _TP{ax}").strip()),
                        "err": float(self._g.GCommand(f"MG _TE{ax}").strip()),
                        "lf":  int(float(self._g.GCommand(f"MG _LF{ax}").strip())),
                        "lr":  int(float(self._g.GCommand(f"MG _LR{ax}").strip())),
                        "sc":  int(float(self._g.GCommand(f"MG _SC{ax}").strip())),
                        "mo":  int(float(self._g.GCommand(f"MG _MO{ax}").strip())),
                    }
                self.status_updated.emit(data)
            except Exception as e:
                self.error_occurred.emit(str(e))
            self.msleep(100)

    def stop(self):
        self._running = False
        self.wait(3000)


# ── Smoke-test window ────────────────────────────────────────────────────────

class SmokeTestWindow(QMainWindow):
    def __init__(self, address: str | None = None, mock: bool = False):
        super().__init__()
        self._address = address or "COM3"
        self._mock = mock
        self._worker: _SmokePollThread | None = None

        if mock:
            from controller.mock_controller import MockGalilController
            self._ctrl = MockGalilController()
        else:
            self._ctrl = None   # set on connect

        self._build_ui()
        self.setWindowTitle("Galil Smoke Test")
        self.setMinimumSize(640, 480)

        if mock:
            self._simulate_connect()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(10)

        # Connection row
        conn_grp = QGroupBox("Connection")
        cg = QHBoxLayout(conn_grp)
        cg.addWidget(QLabel("Address:"))
        self._addr_input = QLineEdit(self._address)
        self._addr_input.setPlaceholderText("e.g. 192.168.0.40 or COM3")
        cg.addWidget(self._addr_input)
        self._btn_connect = QPushButton("CONNECT")
        self._btn_connect.clicked.connect(self._on_connect)
        cg.addWidget(self._btn_connect)
        self._btn_disconnect = QPushButton("DISCONNECT")
        self._btn_disconnect.clicked.connect(self._on_disconnect)
        self._btn_disconnect.setEnabled(False)
        cg.addWidget(self._btn_disconnect)
        root.addWidget(conn_grp)

        # Status
        self._status_lbl = QLabel("● Disconnected")
        self._status_lbl.setFont(QFont("Consolas", 11))
        root.addWidget(self._status_lbl)

        # Position readout
        pos_grp = QGroupBox("Live Encoder Positions (100 ms polling)")
        pg = QGridLayout(pos_grp)
        self._pos_lbls: dict[str, QLabel] = {}
        self._lim_lbls: dict[str, tuple[QLabel, QLabel]] = {}
        for i, (ax, color) in enumerate([("A", "#00C9A7"), ("B", "#9B59B6"), ("C", "#F0B429")]):
            ax_lbl = QLabel(f"Axis {ax}:")
            ax_lbl.setStyleSheet(f"color: {color}; font-weight: bold;")
            pg.addWidget(ax_lbl, i, 0)
            pos_v = QLabel("—")
            pos_v.setFont(QFont("Consolas", 14, QFont.Bold))
            pg.addWidget(pos_v, i, 1)
            pg.addWidget(QLabel("counts"), i, 2)
            fwd_lbl = QLabel("FWD: CLR")
            rev_lbl = QLabel("REV: CLR")
            fwd_lbl.setStyleSheet("color: #3DBA6F;")
            rev_lbl.setStyleSheet("color: #3DBA6F;")
            pg.addWidget(fwd_lbl, i, 3)
            pg.addWidget(rev_lbl, i, 4)
            self._pos_lbls[ax] = pos_v
            self._lim_lbls[ax] = (fwd_lbl, rev_lbl)
        root.addWidget(pos_grp)

        # Commands
        cmd_grp = QGroupBox("Test Commands")
        cmds = QHBoxLayout(cmd_grp)
        for label, fn in [
            ("Enable", self._cmd_sh),
            ("MO — Disable", self._cmd_mo),
            ("A+ 100", self._cmd_ap),
            ("A− 100", self._cmd_am),
            ("ST — Stop", self._cmd_st),
            ("AB — Abort", self._cmd_ab),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(fn)
            cmds.addWidget(btn)
        root.addWidget(cmd_grp)

        # Raw command input
        raw_grp = QGroupBox("Raw DMC Command")
        rg = QHBoxLayout(raw_grp)
        self._cmd_input = QLineEdit()
        self._cmd_input.setPlaceholderText("e.g. TP or MG _TPA or PRA=500;BGA")
        self._cmd_input.setFont(QFont("Consolas", 10))
        self._cmd_input.returnPressed.connect(self._send_raw)
        rg.addWidget(self._cmd_input)
        btn_send = QPushButton("SEND")
        btn_send.clicked.connect(self._send_raw)
        rg.addWidget(btn_send)
        self._resp_lbl = QLabel("")
        self._resp_lbl.setFont(QFont("Consolas", 10))
        rg.addWidget(self._resp_lbl)
        root.addWidget(raw_grp)

    # ── Mock connect ─────────────────────────────────────────────────────────

    def _simulate_connect(self):
        """Start polling for mock controller."""
        self._status_lbl.setText(f"● MOCK — {self._ctrl.info.model}")
        self._status_lbl.setStyleSheet("color: #F0B429;")
        self._btn_connect.setEnabled(False)
        self._btn_disconnect.setEnabled(True)
        self._start_worker_mock()

    def _start_worker_mock(self):
        """Mock worker polling using MockGalilController.poll_all_axes()."""
        from PyQt5.QtCore import QTimer
        self._mock_timer = QTimer(self)
        self._mock_timer.setInterval(100)
        self._mock_timer.timeout.connect(self._mock_poll)
        self._mock_timer.start()

    def _mock_poll(self):
        status = self._ctrl.poll_all_axes()
        mock_data = {}
        for ax, s in status.items():
            mock_data[ax] = {
                "pos": s.pos,
                "err": s.err,
                "lf":  0 if s.fwd_limit else 1,
                "lr":  0 if s.rev_limit else 1,
                "sc":  s.stop_code,
                "mo":  0 if s.servo_on else 1,
            }
        self._on_status_updated(mock_data)

    # ── Real connect ─────────────────────────────────────────────────────────

    def _on_connect(self):
        if self._mock:
            self._simulate_connect()
            return
        try:
            import gclib
            g = gclib.py()
            addr = self._addr_input.text().strip()
            g.GOpen(addr)
            self._g = g
            info = g.GInfo()
            self._status_lbl.setText(f"● {info}")
            self._status_lbl.setStyleSheet("color: #3DBA6F;")
            self._btn_connect.setEnabled(False)
            self._btn_disconnect.setEnabled(True)
            self._worker = _SmokePollThread(g)
            self._worker.status_updated.connect(self._on_status_updated_raw)
            self._worker.error_occurred.connect(
                lambda e: self._status_lbl.setText(f"Poll error: {e}"))
            self._worker.start()
        except Exception as e:
            self._status_lbl.setText(f"● FAILED: {e}")
            self._status_lbl.setStyleSheet("color: #FF4C4C;")

    @pyqtSlot(dict)
    def _on_status_updated_raw(self, data: dict):
        self._on_status_updated(data)

    def _on_status_updated(self, data: dict):
        for ax, d in data.items():
            if ax in self._pos_lbls:
                self._pos_lbls[ax].setText(f"{d['pos']:+12.1f}")
            if ax in self._lim_lbls:
                fwd_lbl, rev_lbl = self._lim_lbls[ax]
                if d["lf"] == 0:
                    fwd_lbl.setText("FWD: HIT")
                    fwd_lbl.setStyleSheet("color: #FF4C4C; font-weight: bold;")
                else:
                    fwd_lbl.setText("FWD: CLR")
                    fwd_lbl.setStyleSheet("color: #3DBA6F;")
                if d["lr"] == 0:
                    rev_lbl.setText("REV: HIT")
                    rev_lbl.setStyleSheet("color: #FF4C4C; font-weight: bold;")
                else:
                    rev_lbl.setText("REV: CLR")
                    rev_lbl.setStyleSheet("color: #3DBA6F;")

    def _on_disconnect(self):
        if hasattr(self, "_mock_timer"):
            self._mock_timer.stop()
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self._worker = None
        if not self._mock:
            try:
                self._g.GCommand("ST")
                self._g.GCommand("MO")
                self._g.GClose()
            except Exception:
                pass
        self._status_lbl.setText("● Disconnected")
        self._status_lbl.setStyleSheet("color: #8B949E;")
        self._btn_connect.setEnabled(True)
        self._btn_disconnect.setEnabled(False)

    # ── Commands ─────────────────────────────────────────────────────────────

    def _send(self, cmd: str):
        if self._mock:
            self._ctrl.send(cmd)
            return ":"
        if not hasattr(self, "_g"):
            return ""
        try:
            return self._g.GCommand(cmd)
        except Exception as e:
            self._status_lbl.setText(f"CMD ERR: {e}")
            return ""

    def _cmd_sh(self):
        # Stepper enable is handled by the controller configuration; no SH command is sent.
        self._status_lbl.setText("Stepper enable: no Galil SH command sent")

    def _cmd_mo(self):  self._send("MO")
    def _cmd_st(self):  self._send("ST")
    def _cmd_ab(self):  self._send("AB"); self._send("MO")

    def _cmd_ap(self):
        self._send("SPA=1000"); self._send("ACA=10000"); self._send("DCA=10000")
        self._send("PRA=100"); self._send("BGA")

    def _cmd_am(self):
        self._send("SPA=1000"); self._send("ACA=10000"); self._send("DCA=10000")
        self._send("PRA=-100"); self._send("BGA")

    def _send_raw(self):
        cmd = self._cmd_input.text().strip()
        if not cmd:
            return
        resp = self._send(cmd)
        self._resp_lbl.setText(f"<< {resp.strip()}")
        self._cmd_input.clear()

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        self._on_disconnect()
        event.accept()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Galil Smoke Test")
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--address", type=str, default=None)
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("Galil Smoke Test")

    # Load dark theme if available
    import os
    qss_path = os.path.join(os.path.dirname(__file__), "ui", "dark_theme.qss")
    try:
        with open(qss_path, encoding="utf-8") as f:
            app.setStyleSheet(f.read())
    except FileNotFoundError:
        pass

    win = SmokeTestWindow(address=args.address, mock=args.mock)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
