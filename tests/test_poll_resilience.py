"""
poll_all_axes() must survive a bad reading on one axis.

Both failures here were seen in a real session on 2026-09-22, after the serial
link desynchronised:

  - the whole poll died with "cannot access local variable 'last_direction'",
    because the file write referenced names that are only bound inside the try
  - and when a LATER axis failed instead of the first, those names still held the
    PREVIOUS axis's values, so one axis silently reported another's position.
    Nothing reported that one.

The file write that caused both went away on 2026-09-23 with
config/last_positions.json; the same properties are now asserted on the status
the poll returns.

Run:  pytest tests/test_poll_resilience.py -v
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))   # for FakeGalil next door

import pytest

from controller.galil_controller import GalilController
from test_move_microns import FakeGalil

POSITIONS = {"A": 3230950.0, "B": 3844318.0, "C": 5855464.0}


class FlakyLink(FakeGalil):
    """A link that fails on one nominated axis."""

    def __init__(self, bad_axis: str, mode: str):
        super().__init__()
        self.tp = dict(POSITIONS)
        self.bad_axis = bad_axis
        self.mode = mode

    def GCommand(self, cmd: str) -> str:
        if cmd.strip() == f"MG _TP{self.bad_axis}":
            if self.mode == "merged":
                # Two replies glued together: the signature of a shifted link.
                return "3230950.0000\r\n: 1.0000"
            raise RuntimeError("device timed out")
        return super().GCommand(cmd)


@pytest.mark.parametrize("bad_axis", ["A", "B", "C"])
def test_poll_survives_a_bad_axis(bad_axis):
    """Whichever axis fails, the other two still report."""
    c = GalilController()
    c._g = FlakyLink(bad_axis, "timeout")
    status = c.poll_all_axes()

    assert sorted(status) == ["A", "B", "C"], "poll did not return every axis"
    assert status[bad_axis].pos == 0.0, "bad axis should fall back to defaults"
    for ax in ("A", "B", "C"):
        if ax != bad_axis:
            assert status[ax].pos == POSITIONS[ax]


@pytest.mark.parametrize("bad_axis", ["A", "B", "C"])
def test_a_failed_axis_never_reports_another_axis_position(bad_axis):
    """The silent one: an axis inheriting the previous axis's reading.

    This used to be checked against config/last_positions.json, which the poll
    wrote. That file was dropped on 2026-09-23, so the same property is asserted
    on what the poll RETURNS - which is what every caller actually consumes.
    """
    c = GalilController()
    c._g = FlakyLink(bad_axis, "timeout")
    status = c.poll_all_axes()

    assert status[bad_axis].pos == 0.0,         "failed axis reported another axis's position instead of defaults"
    for ax in ("A", "B", "C"):
        if ax != bad_axis:
            assert status[ax].pos == POSITIONS[ax]


def test_a_merged_reply_is_named_as_a_desync():
    """"could not convert string to float" says nothing; name the real fault."""
    c = GalilController()
    c._g = FlakyLink("A", "merged")
    with pytest.raises(RuntimeError, match="desynchronised"):
        c._read_float("MG _TPA")


def test_poll_does_not_raise_on_a_merged_reply():
    c = GalilController()
    c._g = FlakyLink("A", "merged")
    status = c.poll_all_axes()
    assert status["B"].pos == POSITIONS["B"]
