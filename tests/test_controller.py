"""
Unit tests using MockGalilController — no hardware required.
Run: pytest tests/test_controller.py -v
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from controller.mock_controller import MockGalilController
from controller.data_models import AxisStatus


@pytest.fixture
def ctrl():
    return MockGalilController()


def test_connect(ctrl):
    info = ctrl.connect("MOCK")
    assert info.connected
    assert "DMC" in info.model


def test_poll_returns_all_axes(ctrl):
    status = ctrl.poll_all_axes()
    assert set(status.keys()) == {"A", "B", "C"}
    for ax, s in status.items():
        assert isinstance(s, AxisStatus)


def test_enable_disable(ctrl):
    ctrl.enable_axes("ABC")
    status = ctrl.poll_all_axes()
    for ax in ["A", "B", "C"]:
        assert status[ax].servo_on is True

    ctrl.disable_axes("ABC")
    status = ctrl.poll_all_axes()
    for ax in ["A", "B", "C"]:
        assert status[ax].servo_on is False


def test_move_relative(ctrl):
    ctrl.enable_axes("A")
    ctrl.move_relative("A", 1000, wait=True)
    status = ctrl.poll_all_axes()
    assert abs(status["A"].pos - 1000) < 5  # mock has some drift


def test_move_absolute(ctrl):
    ctrl.enable_axes("B")
    ctrl.move_absolute("B", 2500, wait=True)
    status = ctrl.poll_all_axes()
    assert status["B"].pos == pytest.approx(2500, abs=5)


def test_define_zero(ctrl):
    ctrl.enable_axes("ABC")
    ctrl.move_relative("A", 500, wait=True)
    ctrl.define_zero("A")
    # Mock adds Gaussian noise each poll; allow small tolerance
    status = ctrl.poll_all_axes()
    assert abs(status["A"].pos) < 2.0


def test_halt_does_not_raise(ctrl):
    ctrl.enable_axes("A")
    ctrl.jog("A", 1000)
    ctrl.halt_axis("A")


def test_halt_all_does_not_raise(ctrl):
    ctrl.halt_all()


def test_emergency_stop_disables_all(ctrl):
    ctrl.enable_axes("ABC")
    ctrl.emergency_stop()
    status = ctrl.poll_all_axes()
    for ax in ["A", "B", "C"]:
        assert status[ax].servo_on is False


def test_fwd_limit_simulation(ctrl):
    ctrl.simulate_fwd_limit("A", True)
    status = ctrl.poll_all_axes()
    assert status["A"].fwd_limit is True
    ctrl.simulate_fwd_limit("A", False)
    status = ctrl.poll_all_axes()
    assert status["A"].fwd_limit is False


def test_move_all_absolute(ctrl):
    ctrl.enable_axes("ABC")
    ctrl.move_all_absolute({"A": 100, "B": 200, "C": 300}, wait=True)
    status = ctrl.poll_all_axes()
    assert status["A"].pos == pytest.approx(100, abs=5)
    assert status["B"].pos == pytest.approx(200, abs=5)
    assert status["C"].pos == pytest.approx(300, abs=5)


def test_rev_limit_simulation(ctrl):
    ctrl.simulate_rev_limit("B", True)
    status = ctrl.poll_all_axes()
    assert status["B"].rev_limit is True
    ctrl.simulate_rev_limit("B", False)
    status = ctrl.poll_all_axes()
    assert status["B"].rev_limit is False


def test_estop_clears_servo_and_stop_code(ctrl):
    ctrl.enable_axes("ABC")
    ctrl.emergency_stop()
    status = ctrl.poll_all_axes()
    for ax in ["A", "B", "C"]:
        assert status[ax].servo_on is False
        assert status[ax].stop_code == 0
