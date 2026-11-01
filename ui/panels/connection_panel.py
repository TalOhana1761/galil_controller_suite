"""
Panel 1 — Connection & Controller Info.
Handles GOpen / GClose and displays controller identity.
Connection is established in a one-shot QThread so the UI stays responsive.
"""
import re
import time

from loguru import logger

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QGroupBox, QLabel, QPushButton, QLineEdit,
    QComboBox, QRadioButton,
    QListWidget, QListWidgetItem,
)
from PyQt5.QtCore import QThread, pyqtSignal, Qt, QTimer
from PyQt5.QtGui import QFont

_SERIAL_RE = re.compile(r"COM\d+", re.IGNORECASE)

# Offered when the real port list cannot be read (not Windows, registry denied).
_FALLBACK_PORTS = [f"COM{n}" for n in range(1, 9)]


def _com_sort_key(port: str):
    m = re.search(r"\d+", port)
    return int(m.group()) if m else 0


def _read_serialcomm():
    r"""{port: kernel device name} from the registry, or None if unreadable.

    The device name says what kind of port it is: \Device\VCP0 is an FTDI
    USB-serial adapter (the rig's is an FT232R, VID 0403 PID 6001),
    \Device\Serial0 a built-in port, \Device\BthModem0 Bluetooth.
    """
    try:
        import winreg
    except ImportError:
        return None
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"HARDWARE\DEVICEMAP\SERIALCOMM")
    except OSError:
        return {}           # key absent = no serial ports at all
    found = {}
    try:
        i = 0
        while True:
            try:
                device, value, _ = winreg.EnumValue(key, i)
            except OSError:
                break
            if isinstance(value, str) and _SERIAL_RE.fullmatch(value.strip()):
                found[value.strip().upper()] = device
            i += 1
    finally:
        winreg.CloseKey(key)
    return found


def list_serial_ports():
    """The COM ports that exist on THIS PC right now, e.g. ['COM3', 'COM11'].

    Read from the registry key Windows itself keeps for serial devices, which
    includes USB-to-serial adapters while they are plugged in. Returns None if
    it cannot be read - not the same as an empty list, which means "no ports".

    A fixed COM1-COM8 list was offered before 2026-10-08. On another PC the
    controller's adapter can be COM9 or above and so could not be selected at
    all, while the list happily offered ports that did not exist there.
    """
    found = _read_serialcomm()
    if found is None:
        return None
    return sorted(found, key=_com_sort_key)


_PORT_KINDS = [            # (substring of the device name, what to call it)
    ("vcp", "USB serial adapter (FTDI) - the rig uses one of these"),
    ("usbser", "USB serial device"),
    ("prolific", "USB serial adapter (Prolific)"),
    ("silab", "USB serial adapter (Silicon Labs)"),
    ("ch341", "USB serial adapter (CH340)"),
    ("bth", "Bluetooth - not the controller"),
    ("serial", "built-in serial port"),
]


def describe_serial_port(port: str) -> str:
    """What kind of port `port` is, or "" if that cannot be told."""
    found = _read_serialcomm() or {}
    device = found.get((port or "").upper(), "").lower()
    if not device:
        return ""
    for needle, label in _PORT_KINDS:
        if needle in device:
            return label
    return device.rsplit("\\", 1)[-1]


def connect_failure_hint(address: str, err: str) -> str:
    """Turn a bare gclib error into something an operator can act on."""
    port = address.split()[0] if address.strip() else address
    if _SERIAL_RE.fullmatch(port or ""):
        present = list_serial_ports()
        if present is not None and port.upper() not in present:
            where = ", ".join(present) if present else "none"
            return (f"{err}\n{port} does not exist on this PC "
                    f"(ports present: {where}). Select the controller's port.")
        kind = describe_serial_port(port)
        return (f"{err}\nCould not open {port}"
                f"{' (' + kind + ')' if kind else ''}. Check that it is the "
                f"controller's port (in Device Manager the rig's adapter is "
                f"'USB Serial Port (COMn)'), that the controller is powered, "
                f"that no other program (GDK, GalilTools, another copy of this "
                f"app) has it open, and that the baud rate is 115200.")
    return err


def _split_address(addr: str):
    """Split a gclib address into (port, baud).

    What is stored in settings.last_address is what GOpen was handed on the last
    successful connect, so it can carry gclib options: `--baud N` from the serial
    form here, or appended by GalilController.connect() from settings.serial_baud.
    Subscribe flags are dropped — which messages to subscribe to is a per-session
    choice, not part of identifying the port.
    """
    tokens = addr.strip().split()
    if not tokens:
        return "", ""
    baud = ""
    for i, tok in enumerate(tokens):
        if tok == "--baud" and i + 1 < len(tokens):
            baud = tokens[i + 1]
    return tokens[0], baud


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

    def __init__(self, controller, worker, settings, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.worker = worker
        self.settings = settings
        self._connect_thread = None
        self._scan_thread = None
        self._uptime_t0 = None

        self._build_ui()
        self._restore_last_address()

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
        self._addr_input = QLineEdit()
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
        self._com_combo.setPlaceholderText("— select a port —")
        self._populate_com_ports()
        sg.addWidget(self._com_combo, 0, 1)
        self._btn_refresh_ports = QPushButton("Refresh")
        self._btn_refresh_ports.setToolTip(
            "Re-read the COM ports present on this PC\n"
            "(e.g. after plugging in the USB-serial adapter)")
        self._btn_refresh_ports.clicked.connect(self._populate_com_ports)
        sg.addWidget(self._btn_refresh_ports, 0, 2)
        # Which KIND of port is selected: a laptop lists Bluetooth and built-in
        # ports too, and nothing in "COM5" says which one is the rig's adapter.
        self._port_kind_lbl = QLabel("")
        self._port_kind_lbl.setObjectName("port_kind")
        self._com_combo.currentTextChanged.connect(self._show_port_kind)
        sg.addWidget(self._port_kind_lbl, 2, 1, 1, 2)
        sg.addWidget(QLabel("Baud Rate:"), 1, 0)
        self._baud_combo = QComboBox()
        self._baud_combo.addItems(["115200","19200","9600"])
        sg.addWidget(self._baud_combo, 1, 1)
        self._ser_grp.setVisible(False)
        root.addWidget(self._ser_grp)

        # Removed 2026-09-23: a "Timeout (ms)" spin box and an "Auto-reconnect on
        # drop" checkbox. Neither was ever read. The gclib timeout is whatever the
        # library defaults to, and reconnection is unconditional - PollingWorker
        # rebuilds the link after FAULTS_BEFORE_RECONNECT consecutive link faults,
        # capped at MAX_RECONNECT_ATTEMPTS. A checkbox implying it could be turned
        # off was worse than no checkbox.

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
            ("uptime",   "Uptime"),
        ]):
            ig.addWidget(QLabel(f"{lbl}:"), i, 0)
            val = QLabel("—")
            val.setFont(QFont("Consolas", 10))
            ig.addWidget(val, i, 1)
            self._info_labels[key] = val
        root.addWidget(info_grp)
        root.addStretch()

    # ── COM port list ───────────────────────────────────────────────────────

    def _populate_com_ports(self, *_):
        """Fill the COM combo with the ports present on this PC.

        Keeps the current selection if that port is still there; otherwise
        nothing is selected, rather than silently offering a different port.
        """
        current = self._com_combo.currentText()
        ports = list_serial_ports()
        self._ports_known = ports is not None
        if ports is None:
            ports = list(_FALLBACK_PORTS)
        self._com_combo.blockSignals(True)
        self._com_combo.clear()
        self._com_combo.addItems(ports)
        self._com_combo.setCurrentIndex(
            ports.index(current) if current in ports else -1)
        self._com_combo.blockSignals(False)
        self._show_port_kind(self._com_combo.currentText())

    def _show_port_kind(self, port: str):
        if hasattr(self, "_port_kind_lbl"):
            self._port_kind_lbl.setText(describe_serial_port(port))

    def _select_com_port(self, port: str):
        """Select `port` in the COM combo, adding it if it is not listed."""
        port = port.upper()
        if self._com_combo.findText(port) < 0:
            self._com_combo.addItem(port)
        self._com_combo.setCurrentText(port)

    # ── Last connected address ──────────────────────────────────────────────

    def _restore_last_address(self):
        """Present the address of the last SUCCESSFUL connection.

        If none is recorded — a fresh install, or nothing has ever connected —
        nothing is presented: the address field stays empty and the COM combo
        stays unselected, rather than offering a port that was never tested.
        """
        port, baud = _split_address(self.settings.last_address)
        if not port:
            return

        if _SERIAL_RE.fullmatch(port):
            port = port.upper()
            # A remembered port that does not exist on THIS PC is not offered.
            # The installer used to ship this rig's "COM4", so on a peer's PC
            # the panel opened on a port that was never theirs.
            if self._ports_known and self._com_combo.findText(port) < 0:
                self._rb_ser.setChecked(True)
                return
            self._select_com_port(port)
            if baud:
                if self._baud_combo.findText(baud) < 0:
                    self._baud_combo.addItem(baud)   # a rate outside the usual three
                self._baud_combo.setCurrentText(baud)
            self._rb_ser.setChecked(True)       # also flips the visible group
        else:
            self._addr_input.setText(port)

    def _remember_address(self, addr: str):
        """Record the address that just connected, for the next launch.

        Written as soon as the connection succeeds rather than at shutdown:
        closeEvent does not run if the app is killed or crashes, and a port that
        connected is exactly the thing worth keeping from such a session.
        """
        port, baud = _split_address(addr)
        if not port:
            return
        stored = f"{port} --baud {baud}" if baud else port
        if stored == self.settings.last_address:
            return
        self.settings.last_address = stored
        try:
            self.settings.save_to_file()
        except Exception as e:
            logger.warning(f"Could not persist the last address ({stored}): {e}")

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
        self._remember_address(info.address)
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
        addr = self._connect_thread.address if self._connect_thread else ""
        msg = connect_failure_hint(addr, err)
        logger.error(f"Connect to '{addr}' failed: {msg}")
        self._btn_connect.setText("CONNECT")
        self._btn_connect.setEnabled(True)
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setText(f"● Failed ({addr}): {msg}")

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
        self._populate_com_ports()
        if not addrs:
            self._scan_list.addItem("No controllers found")
            return
        for addr, info in addrs.items():
            item = QListWidgetItem(f"{addr}   {info}".strip())
            item.setData(Qt.UserRole, addr.split()[0])
            self._scan_list.addItem(item)

    def _on_scan_item_selected(self, item: QListWidgetItem):
        """Put the chosen address where CONNECT will actually read it.

        This used to write into the Ethernet field unconditionally. With the
        panel in Serial mode - which it is whenever the last address was a COM
        port - that field is hidden and CONNECT reads the COM combo instead, so
        double-clicking the controller's port and pressing CONNECT opened the
        OLD port. A COM address now switches to Serial and selects the port.
        """
        addr = item.data(Qt.UserRole)
        if not addr:
            return
        if _SERIAL_RE.fullmatch(addr):
            self._select_com_port(addr)
            self._rb_ser.setChecked(True)
        else:
            self._addr_input.setText(addr)
            self._rb_eth.setChecked(True)

    # ── Uptime ──────────────────────────────────────────────────────────────

    def _update_uptime(self):
        if self._uptime_t0 is None:
            return
        elapsed = int(time.time() - self._uptime_t0)
        h, m = divmod(elapsed, 3600)
        m, s = divmod(m, 60)
        self._info_labels["uptime"].setText(f"{h:02d}:{m:02d}:{s:02d}")

    # update_latency() was removed on 2026-09-23 along with the Latency row: it
    # had no callers, so the field never showed anything. ControllerInfo.latency_ms
    # is still measured on every command and still goes to the DEBUG log; if it is
    # wanted on screen again, the worker has to push it after a poll.
