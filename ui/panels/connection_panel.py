"""
Panel 1 — Connection & Controller Info.
Handles GOpen / GClose and displays controller identity.
Connection is established in a one-shot QThread so the UI stays responsive.
"""
import time
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QGroupBox, QLabel, QPushButton, QLineEdit,
    QComboBox, QSpinBox, QRadioButton, QCheckBox,
    QListWidget, QListWidgetItem, QFrame, QSizePolicy,
)
from PyQt5.QtCore import QThread, pyqtSignal, Qt, QTimer
from PyQt5.QtGui import QFont


class _ConnectThread(QThread):
    success = pyqtSignal(object)    # ControllerInfo
    failure = pyqtSignal(str)

    def __init__(self, controller, address):
        super().__init__()
        self.controller = controller
        self.address = address

    def run(self):
        try:
            info = self.controller.connect(self.address)
            self.success.emit(info)
        except Exception as e:
            self.failure.emit(str(e))


class _ScanThread(QThread):
    found = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, controller):
        super().__init__()
        self.controller = controller

    def run(self):
        try:
            self.found.emit(self.controller.discover())
        except Exception as e:
            self.error.emit(str(e))


class ConnectionPanel(QWidget):
    connected    = pyqtSignal(object, object)   # controller, worker
    disconnected = pyqtSignal()
    settings_changed = pyqtSignal(str)          # new address

    def __init__(self, controller, worker, settings, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.worker = worker
        self.settings = settings
        self._connect_thread = None
        self._scan_thread = None
        self._uptime_t0 = None

        self._build_ui()

        self._uptime_timer = QTimer(self)
        self._uptime_timer.timeout.connect(self._update_uptime)
        self._uptime_timer.setInterval(1000)

    # ── UI construction ─────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(12)

        # Title
        title = QLabel("CONNECTION")
        title.setFont(QFont("Segoe UI", 13, QFont.Bold))
        title.setObjectName("axis_A")
        root.addWidget(title)

        # Mode selector
        mode_row = QHBoxLayout()
        self._rb_eth = QRadioButton("Ethernet / USB")
        self._rb_ser = QRadioButton("Serial (COM)")
        self._rb_eth.setChecked(True)
        self._rb_eth.toggled.connect(self._toggle_mode)
        mode_row.addWidget(self._rb_eth)
        mode_row.addWidget(self._rb_ser)
        mode_row.addStretch()
        root.addLayout(mode_row)

        # Ethernet row
        self._eth_grp = QGroupBox("Ethernet / USB Connection")
        eg = QGridLayout(self._eth_grp)
        eg.addWidget(QLabel("Address / Port:"), 0, 0)
        self._addr_input = QLineEdit(self.settings.last_address)
        self._addr_input.setPlaceholderText("e.g. 192.168.0.40 or COM3")
        eg.addWidget(self._addr_input, 0, 1)
        eg.addWidget(QLabel("Subscribe:"), 1, 0)
        self._sub_combo = QComboBox()
        self._sub_combo.addItems(["(none)", "EI — interrupts", "ALL — messages+interrupts+data"])
        eg.addWidget(self._sub_combo, 1, 1)
        root.addWidget(self._eth_grp)

        # Serial row
        self._ser_grp = QGroupBox("Serial Port Settings")
        sg = QGridLayout(self._ser_grp)
        sg.addWidget(QLabel("COM Port:"), 0, 0)
        self._com_combo = QComboBox()
        self._com_combo.addItems(["COM1","COM2","COM3","COM4","COM5","COM6","COM7","COM8"])
        sg.addWidget(self._com_combo, 0, 1)
        sg.addWidget(QLabel("Baud Rate:"), 1, 0)
        self._baud_combo = QComboBox()
        self._baud_combo.addItems(["115200","19200","9600"])
        sg.addWidget(self._baud_combo, 1, 1)
        self._ser_grp.setVisible(False)
        root.addWidget(self._ser_grp)

        # Timeout
        to_row = QHBoxLayout()
        to_row.addWidget(QLabel("Timeout (ms):"))
        self._timeout_spin = QSpinBox()
        self._timeout_spin.setRange(500, 30000)
        self._timeout_spin.setValue(5000)
        self._timeout_spin.setSingleStep(500)
        to_row.addWidget(self._timeout_spin)
        self._auto_reconnect = QCheckBox("Auto-reconnect on drop")
        self._auto_reconnect.setChecked(self.settings.auto_reconnect)
        to_row.addWidget(self._auto_reconnect)
        to_row.addStretch()
        root.addLayout(to_row)

        # Connect / Disconnect / Scan buttons
        btn_row = QHBoxLayout()
        self._btn_connect = QPushButton("CONNECT")
        self._btn_connect.setObjectName("btn_enable")
        self._btn_connect.clicked.connect(self._on_connect)
        self._btn_disconnect = QPushButton("DISCONNECT")
        self._btn_disconnect.clicked.connect(self._on_disconnect)
        self._btn_disconnect.setEnabled(False)
        self._btn_scan = QPushButton("Scan Network")
        self._btn_scan.clicked.connect(self._on_scan)
        btn_row.addWidget(self._btn_connect)
        btn_row.addWidget(self._btn_disconnect)
        btn_row.addWidget(self._btn_scan)
        root.addLayout(btn_row)

        # Status label
        self._status_lbl = QLabel("● Disconnected")
        self._status_lbl.setObjectName("status_disconnected")
        self._status_lbl.setFont(QFont("Segoe UI", 11))
        root.addWidget(self._status_lbl)

        # Scan results
        scan_grp = QGroupBox("Discovered Controllers")
        sl = QVBoxLayout(scan_grp)
        self._scan_list = QListWidget()
        self._scan_list.setMaximumHeight(80)
        self._scan_list.itemDoubleClicked.connect(self._on_scan_item_selected)
        sl.addWidget(self._scan_list)
        sl.addWidget(QLabel("Double-click to populate address field"))
        root.addWidget(scan_grp)

        # Controller info
        info_grp = QGroupBox("Controller Info")
        ig = QGridLayout(info_grp)
        self._info_labels = {}
        for i, (key, lbl) in enumerate([
            ("model",    "Model"),
            ("revision", "Revision"),
            ("serial",   "Serial No."),
            ("firmware", "Firmware"),
            ("address",  "Address"),
            ("latency",  "Latency"),
            ("uptime",   "Uptime"),
        ]):
            ig.addWidget(QLabel(f"{lbl}:"), i, 0)
            val = QLabel("—")
            val.setFont(QFont("Consolas", 10))
            ig.addWidget(val, i, 1)
            self._info_labels[key] = val
        root.addWidget(info_grp)
        root.addStretch()

    # ── Mode toggle ─────────────────────────────────────────────────────────

    def _toggle_mode(self, ethernet: bool):
        self._eth_grp.setVisible(ethernet)
        self._ser_grp.setVisible(not ethernet)

    def _build_address(self) -> str:
        if self._rb_eth.isChecked():
            addr = self._addr_input.text().strip()
            sub = self._sub_combo.currentIndex()
            if sub == 1:
                addr += " -s EI"
            elif sub == 2:
                addr += " --subscribe ALL"
            return addr
        else:
            port = self._com_combo.currentText()
            baud = self._baud_combo.currentText()
            if baud != "115200":
                return f"{port} --baud {baud}"
            return port

    # ── Connect / Disconnect ─────────────────────────────────────────────────

    def _on_connect(self):
        addr = self._build_address()
        if not addr:
            self._status_lbl.setText("● Enter an address first")
            return
        self._btn_connect.setEnabled(False)
        self._btn_connect.setText("Connecting…")
        self._status_lbl.setText("● Connecting…")
        self._status_lbl.setObjectName("status_disconnected")
        self._status_lbl.style().unpolish(self._status_lbl)
        self._status_lbl.style().polish(self._status_lbl)

        self._connect_thread = _ConnectThread(self.controller, addr)
        self._connect_thread.success.connect(self._on_connected)
        self._connect_thread.failure.connect(self._on_connect_failed)
        self._connect_thread.start()

    def _on_connected(self, info):
        self._btn_connect.setText("CONNECT")
        self._btn_connect.setEnabled(False)
        self._btn_disconnect.setEnabled(True)
        self._status_lbl.setText(f"● Connected  {info.model}")
        self._status_lbl.setObjectName("status_connected")
        self._status_lbl.style().unpolish(self._status_lbl)
        self._status_lbl.style().polish(self._status_lbl)
        self._info_labels["model"].setText(info.model)
        self._info_labels["revision"].setText(info.revision)
        self._info_labels["serial"].setText(info.serial)
        self._info_labels["firmware"].setText(info.firmware)
        self._info_labels["address"].setText(info.address)
        self._uptime_t0 = time.time()
        self._uptime_timer.start()
        if self.worker:
            self.worker.start()
        self.connected.emit(self.controller, self.worker)

    def _on_connect_failed(self, err: str):
        self._btn_connect.setText("CONNECT")
        self._btn_connect.setEnabled(True)
        self._status_lbl.setText(f"● Failed: {err}")

    def _on_disconnect(self):
        self._uptime_timer.stop()
        if self.worker and self.worker.isRunning():
            self.worker.stop()
        try:
            self.controller.disconnect()
        except Exception:
            pass
        self._btn_connect.setEnabled(True)
        self._btn_disconnect.setEnabled(False)
        self._status_lbl.setText("● Disconnected")
        self._status_lbl.setObjectName("status_disconnected")
        self._status_lbl.style().unpolish(self._status_lbl)
        self._status_lbl.style().polish(self._status_lbl)
        for lbl in self._info_labels.values():
            lbl.setText("—")
        self.disconnected.emit()

    # ── Scan ────────────────────────────────────────────────────────────────

    def _on_scan(self):
        self._btn_scan.setEnabled(False)
        self._btn_scan.setText("Scanning…")
        self._scan_list.clear()
        self._scan_thread = _ScanThread(self.controller)
        self._scan_thread.found.connect(self._on_scan_done)
        self._scan_thread.error.connect(lambda e: self._scan_list.addItem(f"Error: {e}"))
        self._scan_thread.finished.connect(lambda: (
            self._btn_scan.__setattr__("text", "Scan Network") or
            self._btn_scan.setEnabled(True)
        ))
        self._scan_thread.start()

    def _on_scan_done(self, addrs: dict):
        self._scan_list.clear()
        self._btn_scan.setText("Scan Network")
        self._btn_scan.setEnabled(True)
        if not addrs:
            self._scan_list.addItem("No controllers found")
            return
        for addr, info in addrs.items():
            item = QListWidgetItem(f"{addr}   {info}".strip())
            item.setData(Qt.UserRole, addr.split()[0])
            self._scan_list.addItem(item)

    def _on_scan_item_selected(self, item: QListWidgetItem):
        addr = item.data(Qt.UserRole)
        if addr:
            self._addr_input.setText(addr)

    # ── Uptime ──────────────────────────────────────────────────────────────

    def _update_uptime(self):
        if self._uptime_t0 is None:
            return
        elapsed = int(time.time() - self._uptime_t0)
        h, m = divmod(elapsed, 3600)
        m, s = divmod(m, 60)
        self._info_labels["uptime"].setText(f"{h:02d}:{m:02d}:{s:02d}")

    def update_latency(self, ms: float):
        self._info_labels["latency"].setText(f"{ms:.1f} ms")
