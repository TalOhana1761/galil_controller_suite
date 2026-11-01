"""Connection panel: the COM port list, scan results and failure messages.

A peer's PC found the controller's port with Scan Network and still could not
connect. Two panel bugs could each produce exactly that, and both are pinned
here:

* a double-clicked scan result was written into the ETHERNET field even while
  the panel was in Serial mode, so CONNECT opened the old COM port instead;
* the COM list was a fixed COM1-COM8 plus the port remembered from the build
  machine, so it offered ports that did not exist there and could not offer an
  adapter at COM9 or above.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication, QListWidgetItem   # noqa: E402
from PyQt5.QtCore import Qt                                  # noqa: E402

from controller.data_models import AppSettings               # noqa: E402
from controller.mock_controller import MockGalilController   # noqa: E402
from ui.panels import connection_panel as cp                  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def ports(monkeypatch):
    """The COM ports 'present on this PC'. Mutate the list to change them."""
    present = ["COM3", "COM11"]
    monkeypatch.setattr(cp, "list_serial_ports", lambda: list(present))
    return present


def make_panel(app, last_address=""):
    s = AppSettings()
    s.last_address = last_address
    # No test here connects, so _remember_address() never writes config/
    return cp.ConnectionPanel(MockGalilController(), None, s)


def combo_items(panel):
    c = panel._com_combo
    return [c.itemText(i) for i in range(c.count())]


# ── the port list ─────────────────────────────────────────────────────────────

def test_combo_lists_the_ports_present_not_a_fixed_range(app, ports):
    p = make_panel(app)
    assert combo_items(p) == ["COM3", "COM11"]
    assert p._com_combo.currentIndex() == -1          # nothing preselected


def test_falls_back_to_com1_to_8_when_ports_cannot_be_read(app, monkeypatch):
    monkeypatch.setattr(cp, "list_serial_ports", lambda: None)
    p = make_panel(app)
    assert combo_items(p) == [f"COM{n}" for n in range(1, 9)]


def test_refresh_picks_up_a_newly_plugged_adapter_and_keeps_the_selection(app, ports):
    p = make_panel(app)
    p._com_combo.setCurrentText("COM3")
    ports.append("COM12")
    p._btn_refresh_ports.click()
    assert "COM12" in combo_items(p)
    assert p._com_combo.currentText() == "COM3"


def test_a_remembered_port_present_here_is_restored(app, ports):
    p = make_panel(app, "COM11")
    assert p._rb_ser.isChecked()
    assert p._com_combo.currentText() == "COM11"
    assert p._build_address() == "COM11"


def test_a_remembered_port_absent_here_is_not_offered(app, ports):
    """The installer shipped the build PC's COM4; a peer has no COM4."""
    p = make_panel(app, "COM4")
    assert p._rb_ser.isChecked()
    assert "COM4" not in combo_items(p)
    assert p._com_combo.currentIndex() == -1


# ── scan results ──────────────────────────────────────────────────────────────

def scan_item(addr):
    item = QListWidgetItem(addr)
    item.setData(Qt.UserRole, addr)
    return item


def test_double_clicking_a_com_port_is_what_connect_then_opens(app, ports):
    """The bug: panel in Serial mode on COM3, user double-clicks COM11 from the
    scan, CONNECT opened COM3 because the address went to the hidden field."""
    p = make_panel(app, "COM3")
    p._on_scan_item_selected(scan_item("COM11"))
    assert p._rb_ser.isChecked()
    assert p._build_address() == "COM11"


def test_double_clicking_a_port_not_in_the_list_adds_it(app, ports):
    p = make_panel(app)
    p._on_scan_item_selected(scan_item("COM20"))
    assert p._build_address() == "COM20"


def test_double_clicking_an_ip_switches_to_ethernet(app, ports):
    p = make_panel(app, "COM3")
    p._on_scan_item_selected(scan_item("192.168.0.40"))
    assert p._rb_eth.isChecked()
    assert p._build_address() == "192.168.0.40"


# ── failure messages ──────────────────────────────────────────────────────────

def test_hint_names_a_port_that_does_not_exist_and_lists_the_real_ones(ports):
    msg = cp.connect_failure_hint("COM4", "-1100 open error")
    assert "COM4 does not exist" in msg
    assert "COM3" in msg and "COM11" in msg


def test_hint_for_a_present_port_suggests_what_else_holds_it(ports):
    msg = cp.connect_failure_hint("COM3 --baud 115200", "-1100 open error")
    assert "Could not open COM3" in msg
    assert "baud" in msg


def test_hint_leaves_a_network_error_alone(ports):
    assert cp.connect_failure_hint("192.168.0.40", "timeout") == "timeout"


def test_registry_reader_returns_com_names_or_none():
    got = cp.list_serial_ports()
    assert got is None or all(cp._SERIAL_RE.fullmatch(p) for p in got)


# ── port kinds ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("device, expect", [
    (r"\Device\VCP0", "FTDI"),
    (r"\Device\BthModem2", "Bluetooth"),
    (r"\Device\Serial0", "built-in"),
])
def test_port_kind_tells_the_rigs_adapter_from_the_rest(monkeypatch, device, expect):
    monkeypatch.setattr(cp, "_read_serialcomm", lambda: {"COM5": device})
    assert expect in cp.describe_serial_port("COM5")


def test_selecting_a_port_shows_its_kind(app, monkeypatch):
    found = {"COM3": r"\Device\BthModem0", "COM5": r"\Device\VCP0"}
    monkeypatch.setattr(cp, "_read_serialcomm", lambda: dict(found))
    p = make_panel(app)
    p._com_combo.setCurrentText("COM5")
    assert "FTDI" in p._port_kind_lbl.text()
    p._com_combo.setCurrentText("COM3")
    assert "Bluetooth" in p._port_kind_lbl.text()


# ── the --direct retry in GalilController.connect() ───────────────────────────

class _OpenOnly:
    """gclib stand-in that only knows how to open, and only some addresses."""
    def __init__(self, works):
        self.works = works
        self.tried = []

    def GOpen(self, address):
        self.tried.append(address)
        if not self.works(address):
            raise RuntimeError("connection to hardware not established")

    def GInfo(self):
        return "COM5, DMC4133 Rev 1.3k, 12345"

    def GCommand(self, cmd):
        return "1.3k"


@pytest.fixture
def real_controller(monkeypatch):
    gc = pytest.importorskip("controller.galil_controller")
    monkeypatch.setattr(gc, "SERIAL_BAUD", 0)
    return gc.GalilController()


def test_a_failed_serial_open_is_retried_direct(real_controller):
    fake = _OpenOnly(lambda a: "--direct" in a)
    real_controller._g = fake
    info = real_controller.connect("COM5")
    assert fake.tried == ["COM5", "COM5 --direct"]
    assert info.address == "COM5 --direct"      # what reconnect() will reuse


def test_a_working_open_is_not_retried(real_controller):
    fake = _OpenOnly(lambda a: True)
    real_controller._g = fake
    real_controller.connect("COM5")
    assert fake.tried == ["COM5"]


def test_both_failures_are_reported(real_controller):
    real_controller._g = _OpenOnly(lambda a: False)
    with pytest.raises(RuntimeError, match="also failed with --direct"):
        real_controller.connect("COM5")


def test_an_ethernet_open_is_never_retried_direct(real_controller):
    """Over Ethernet gcaps shares the controller's handles; do not bypass it."""
    fake = _OpenOnly(lambda a: False)
    real_controller._g = fake
    with pytest.raises(RuntimeError):
        real_controller.connect("192.168.0.40")
    assert fake.tried == ["192.168.0.40"]
