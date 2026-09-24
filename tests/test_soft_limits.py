"""Soft travel limits: positions at which the software raises a limit flag.

The operator sets a negative and a positive bound per axis in STAGE MICRONS from
the micron origin - the same number the position display shows. Reaching one
raises the matching limit flag exactly as a physical switch would, so everything
downstream reacts identically: the button for that direction greys out, the limit
table lights up, and a running jog is halted.

The trap these tests exist for is the two direction conventions. The bounds and
the position are STAGE microns; fwd_limit/rev_limit are CONTROLLER flags. Y and Z
are reversed, so the STAGE's negative end is the CONTROLLER's FORWARD limit on
those two. Getting that backwards would block the wrong button and, worse, leave
the axis free to keep driving past its bound.

Run:  pytest tests/test_soft_limits.py -v
"""
import sys, os
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, ".."))

import pytest

from controller.data_models import AppSettings, AxisStatus
from utils.soft_limits import SoftLimits, flags_for
from utils.stage_axes import AXIS_SIGN

ENC = 12.8          # encoder counts per micron
ORIGIN = 1_000_000  # an arbitrary PRC origin, as the real ones are


class FakeZeros:
    """ZeroOffsets stand-in: every axis has its micron origin at ORIGIN."""

    def __init__(self, origin=ORIGIN):
        self.origin = origin

    def apply_um(self, axis, raw_counts):
        return float(raw_counts) - self.origin


@pytest.fixture
def settings():
    s = AppSettings()
    s.encoder_counts_per_um = ENC
    return s


@pytest.fixture
def soft(settings):
    return SoftLimits(settings, FakeZeros())


def at_um(axis, stage_um):
    """The raw _TP that reads `stage_um` on the display for this axis."""
    return ORIGIN + stage_um * ENC * AXIS_SIGN[axis]


def set_limits(settings, axis, neg=None, pos=None):
    a = getattr(settings, f"axis_{axis.lower()}")
    a.soft_limit_neg_um = neg
    a.soft_limit_pos_um = pos


def status_at(axis, stage_um):
    return {axis: AxisStatus(pos=at_um(axis, stage_um))}


# -- The scenario from the request -------------------------------------------

def test_x_negative_limit_raises_the_reverse_flag(settings, soft):
    """X negative limit -1000 um. Jog X from 0 in the - direction; at -1000 the
    REVERSE flag is set. X is not reversed, so stage - is controller -.
    """
    set_limits(settings, "A", neg=-1000.0)

    st = soft.apply(status_at("A", 0.0))["A"]
    assert (st.fwd_limit, st.rev_limit) == (False, False), "flagged at the start"

    st = soft.apply(status_at("A", -999.9))["A"]
    assert (st.fwd_limit, st.rev_limit) == (False, False), "flagged before the bound"

    st = soft.apply(status_at("A", -1000.0))["A"]
    assert st.rev_limit is True, "the reverse flag should be set at the bound"
    assert st.fwd_limit is False, "the forward direction must stay free to escape"

    st = soft.apply(status_at("A", -1200.0))["A"]
    assert st.rev_limit is True, "still set past the bound"


def test_the_flag_clears_when_the_axis_comes_back_inside(settings, soft):
    set_limits(settings, "A", neg=-1000.0)
    assert soft.apply(status_at("A", -1000.0))["A"].rev_limit is True
    assert soft.apply(status_at("A", -999.0))["A"].rev_limit is False


# -- The reversal, which is where this would go wrong ------------------------

@pytest.mark.parametrize("axis,expect_flag_at_neg", [
    ("A", "rev"),      # X is not reversed: stage - == controller -
    ("B", "fwd"),      # Y IS reversed: stage - == controller +
    ("C", "fwd"),      # Z likewise
])
def test_the_stage_negative_end_maps_to_the_right_controller_flag(
        settings, soft, axis, expect_flag_at_neg):
    set_limits(settings, axis, neg=-1000.0, pos=+1000.0)

    st = soft.apply(status_at(axis, -1000.0))[axis]
    assert getattr(st, f"{expect_flag_at_neg}_limit") is True, (
        f"{axis}: the stage's negative end should raise {expect_flag_at_neg}_limit")
    other = "fwd" if expect_flag_at_neg == "rev" else "rev"
    assert getattr(st, f"{other}_limit") is False

    # ...and the positive end is the other flag.
    st = soft.apply(status_at(axis, +1000.0))[axis]
    assert getattr(st, f"{other}_limit") is True
    assert getattr(st, f"{expect_flag_at_neg}_limit") is False


def test_a_reversed_axis_is_not_flagged_at_the_mirror_position(settings, soft):
    """The failure a sign slip produces: Z bounded below at -1000 would flag at
    +1000 instead, leaving the axis free to run past the end it was guarding."""
    set_limits(settings, "C", neg=-1000.0)
    st = soft.apply(status_at("C", +1000.0))["C"]
    assert (st.fwd_limit, st.rev_limit) == (False, False)


# -- Unset bounds ------------------------------------------------------------

def test_no_bounds_means_no_flags(settings, soft):
    for pos_um in (-1e6, 0.0, 1e6):
        st = soft.apply(status_at("A", pos_um))["A"]
        assert (st.fwd_limit, st.rev_limit) == (False, False)


def test_one_bound_can_be_set_without_the_other(settings, soft):
    set_limits(settings, "A", pos=+500.0)
    assert soft.apply(status_at("A", -1e6))["A"].rev_limit is False, (
        "an unset negative bound must not block")
    assert soft.apply(status_at("A", +500.0))["A"].fwd_limit is True


# -- Merging with the hardware flags -----------------------------------------

def test_a_hardware_flag_is_never_masked(settings, soft):
    """OR'd, not replaced: a switch is a physical fact and a soft bound that has
    not been reached must not clear it."""
    set_limits(settings, "A", neg=-1000.0, pos=+1000.0)
    st = {"A": AxisStatus(pos=at_um("A", 0.0), fwd_limit=True)}
    out = soft.apply(st)["A"]
    assert out.fwd_limit is True, "cleared a hardware limit"
    assert out.rev_limit is False


def test_a_soft_flag_is_added_alongside_a_hardware_one(settings, soft):
    set_limits(settings, "A", neg=-1000.0)
    st = {"A": AxisStatus(pos=at_um("A", -1000.0), fwd_limit=True)}
    out = soft.apply(st)["A"]
    assert out.fwd_limit is True and out.rev_limit is True


# -- Robustness --------------------------------------------------------------

def test_a_bad_axis_does_not_kill_the_poll(settings, soft):
    """The poll must survive this the way it survives a bad axis read."""
    soft.zeros = None                       # will raise inside flags()
    out = soft.apply(status_at("A", -1e6))
    assert "A" in out, "dropped the axis entirely"


def test_the_pure_function_needs_no_settings():
    """flags_for() is the whole rule and is directly testable."""
    assert flags_for(-1000.0, -1000.0, None, "A") == (False, True)
    assert flags_for(-1000.0, -1000.0, None, "C") == (True, False)
    assert flags_for(-999.0, -1000.0, None, "A") == (False, False)
    assert flags_for(0.0, None, None, "A") == (False, False)
