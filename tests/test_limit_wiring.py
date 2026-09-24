"""One limit switch per axis, and which end it means.

The rig has ONE switch per axis, tripped mechanically at EITHER end of travel.
The signal says "this axis is at an end" and nothing more - there is no second
switch and no per-end input. THE LAST COMMANDED DIRECTION is what resolves it:
drive - into a switch and you are at the reverse end, drive + into it and you
are at the forward end.

LIMIT_INPUT maps an axis to the DMC input its one switch is wired to. That says
WHERE TO READ the signal, never which end it means. An axis absent from it has
no switch and is reported clear without being read - which under CN=1 is the
only way to tell an empty input from a tripped one, since both read ACTIVE.

Polarity: CN=1 (active high) is what the NORMALLY CLOSED switches need. At rest
an NC switch grounds its input and reads clear; pressing it opens the circuit
and reads ACTIVE, and so does a broken wire - failing safe is the point of NC.

Run:  pytest tests/test_limit_wiring.py -v
"""
import sys, os
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, ".."))
sys.path.insert(0, _HERE)          # FakeGalil lives beside this file

import pytest

from test_move_microns import FakeGalil


@pytest.fixture
def gc():
    import controller.galil_controller as mod
    return mod


@pytest.fixture
def hw(tmp_path, gc):
    """The real controller on a fake device, with NO switch on any axis.

    Each test declares the wiring it wants, so none of them depend on what is
    bolted to the real stage today.
    """
    original_path, original_input = gc.LAST_DIR_PATH, gc.LIMIT_INPUT
    gc.LAST_DIR_PATH = str(tmp_path / "last_direction.json")
    gc.LIMIT_INPUT = {}
    c = gc.GalilController()
    c._g = FakeGalil()
    yield c
    gc.LAST_DIR_PATH, gc.LIMIT_INPUT = original_path, original_input


def wire(gc, hw, axis, terminal="FWD"):
    """Declare `axis` to have its one switch on `terminal`, and trip it."""
    gc.LIMIT_INPUT = {axis: terminal}
    (hw._g.lf if terminal == "FWD" else hw._g.lr)[axis] = 0


# -- The polarity the controller is told to use ------------------------------

def test_enable_axes_sets_the_limit_inputs_active_high(hw):
    """Without CN=1 the NC switches read inverted - not pressed showed ACTIVE,
    which is what was seen on the rig on 2026-09-24."""
    hw.enable_axes("ABC")
    assert "CN 1" in hw._g.commands, (
        "enable_axes did not set the limit polarity; it would run on the "
        "controller's CN=-1 default")


def test_cn_is_sent_once_not_per_axis(hw):
    """CN is global - one setting for all six inputs on all three axes."""
    hw.enable_axes("ABC")
    assert hw._g.commands.count("CN 1") == 1


# -- THE RULE: the same switch means a different end depending on direction ---

@pytest.mark.parametrize("axis", ["A", "B", "C"])
@pytest.mark.parametrize("last_dir,expect_fwd,expect_rev", [
    (+1, True, False),      # drove forward into it -> we are at the forward end
    (-1, False, True),      # drove reverse into it -> we are at the reverse end
])
def test_the_last_direction_decides_which_end(hw, gc, axis, last_dir,
                                              expect_fwd, expect_rev):
    """Identical hardware signal, opposite conclusions. This is the whole point:
    one switch cannot say which end it is, so the software decides."""
    wire(gc, hw, axis)
    hw._last_dir[axis] = last_dir
    gc.last_dir[axis] = last_dir

    assert hw.limit_active(axis, +1) is expect_fwd
    assert hw.limit_active(axis, -1) is expect_rev

    st = hw.poll_all_axes()[axis]
    assert st.fwd_limit is expect_fwd
    assert st.rev_limit is expect_rev


@pytest.mark.parametrize("axis", ["A", "B", "C"])
def test_never_both_flags_at_once(hw, gc, axis):
    """The switch is at one end or the other, never both - and 'both' is what
    the jog panel would read as "blocked in both directions"."""
    wire(gc, hw, axis)
    for d in (+1, -1):
        hw._last_dir[axis] = d
        gc.last_dir[axis] = d
        st = hw.poll_all_axes()[axis]
        assert not (st.fwd_limit and st.rev_limit)
        assert st.fwd_limit or st.rev_limit, "the switch is pressed; say so"


def test_an_untripped_switch_raises_neither_flag(hw, gc):
    gc.LIMIT_INPUT = {"C": "FWD"}
    hw._g.lf["C"] = 1
    st = hw.poll_all_axes()["C"]
    assert st.fwd_limit is False and st.rev_limit is False


# -- Escaping ----------------------------------------------------------------

@pytest.mark.parametrize("last_dir,escape_um", [(+1, -10.0), (-1, +10.0)])
def test_the_opposite_direction_stays_free_to_escape(hw, gc, last_dir, escape_um):
    wire(gc, hw, "C")
    hw._last_dir["C"] = last_dir
    gc.last_dir["C"] = last_dir

    assert hw.limit_active("C", last_dir) is True, "let a move drive further in"
    assert hw.limit_active("C", -last_dir) is False, "blocked the only way out"

    with pytest.raises(RuntimeError):
        hw.move_microns("C", -escape_um)          # further in: refused
    hw.move_microns("C", escape_um)               # out: allowed
    assert hw._g.tp["C"] != 0, "the escape move commanded nothing"


def test_an_escape_move_is_not_aborted_by_its_own_switch(hw, gc):
    """The trap that made this worth a test.

    _move_microns rewrites last_dir as it steps. If the mid-move watch resolved
    the switch against last_dir, an escape would start matching partway through
    and abort itself on the switch it is driving off. The watch therefore reads
    the RAW switch state and is disarmed when the move starts on it.
    """
    wire(gc, hw, "C")
    hw._last_dir["C"] = +1
    gc.last_dir["C"] = +1
    hw._g.lf["C"] = 0                 # still pressed for the whole move

    moved = hw.move_microns("C", -10.0)
    assert hw._last_dir["C"] == -1, "the move should have recorded its direction"
    assert moved < -5.0, (
        f"escape move only travelled {moved:.2f} um - it aborted on the switch "
        f"it was driving off")


# -- An axis with no switch --------------------------------------------------

@pytest.mark.parametrize("direction", [+1, -1])
def test_an_axis_with_no_switch_is_not_even_read(hw, direction):
    hw._g.lf["A"] = 0          # what an open/unwired input reads under CN=1
    hw._g.lr["A"] = 0
    hw._g.commands.clear()

    assert hw.limit_active("A", direction) is False
    assert not any("_LF" in c or "_LR" in c for c in hw._g.commands), (
        "read an input that has no switch on it; that costs a round trip "
        "every poll and the answer is meaningless")


def test_an_axis_with_no_switch_does_not_refuse_a_move(hw):
    """The failure this prevents: CN=1 with unwired inputs made every axis look
    permanently at-limit, so no move could start at all."""
    hw._g.lf["A"] = 0
    hw._g.lr["A"] = 0
    hw.move_microns("A", 10.0)                    # must not raise
    assert hw._g.tp["A"] > 0, "the move was refused by a limit that is not there"


def test_an_axis_with_no_switch_polls_clear(hw):
    hw._g.lf["A"] = 0
    hw._g.lr["A"] = 0
    st = hw.poll_all_axes()["A"]
    assert st.fwd_limit is False and st.rev_limit is False


# -- Which terminal the one switch happens to sit on -------------------------

@pytest.mark.parametrize("terminal,operand", [("FWD", "_LF"), ("REV", "_LR")])
def test_the_switch_is_read_from_the_terminal_it_is_wired_to(hw, gc, terminal, operand):
    """LIMIT_INPUT says where to READ, not which end it means - so a switch on
    the REV terminal still reports the forward end after a forward move."""
    wire(gc, hw, "C", terminal)
    hw._last_dir["C"] = +1
    gc.last_dir["C"] = +1
    hw._g.commands.clear()

    assert hw.limit_active("C", +1) is True
    assert any(operand in c for c in hw._g.commands), f"never read {operand}C"
    assert hw.poll_all_axes()["C"].fwd_limit is True


# -- Which end a switch is stays put once it has closed ----------------------

def test_starting_an_escape_does_not_relabel_the_switch(hw, gc):
    """The Insert Z bug, 2026-09-24.

    The switch cannot say which end it is, so the direction that REACHED it is
    the answer - and that has to be latched, not re-read. _last_dir is the LAST
    COMMANDED direction, and starting a move that drives OFF the switch sets it
    immediately. Reading it afterwards re-labels the still-pressed switch as the
    end being driven towards, and the guard then halts the escape on the very
    switch it is escaping.

    On the rig: Insert Z ran 128 ms and stopped, the same reading at 8581970
    counts logged FWD before the jog and REV after it.
    """
    wire(gc, hw, "C")
    hw._last_dir["C"] = +1                  # we drove + into the switch
    assert hw.limit_active("C", +1) is True     # latches the trip direction
    assert hw.limit_active("C", -1) is False

    hw._last_dir["C"] = -1                  # ...what jog() does to escape it

    assert hw.limit_active("C", -1) is False, (
        "starting the escape re-labelled the switch as the end being driven "
        "towards; the guard halts the escape on the switch it is escaping")
    assert hw.limit_active("C", +1) is True, (
        "the switch still marks the + end - nothing has driven off it yet")


def test_the_latch_clears_when_the_switch_opens(hw, gc):
    wire(gc, hw, "C")
    hw._last_dir["C"] = +1
    assert hw.limit_active("C", +1) is True

    hw._g.lf["C"] = 1                        # drove off it
    assert hw.limit_active("C", +1) is False
    assert hw.limit_active("C", -1) is False

    # Reaching it from the OTHER side now means the other end.
    hw._last_dir["C"] = -1
    hw._g.lf["C"] = 0
    assert hw.limit_active("C", -1) is True
    assert hw.limit_active("C", +1) is False


def test_an_escape_jog_is_allowed_and_stays_allowed(hw, gc):
    """End to end through jog(), which is where the rig failed."""
    wire(gc, hw, "C")
    hw._last_dir["C"] = +1
    hw.poll_all_axes()                       # a poll latches the trip direction

    hw.jog("C", -500)                        # must not raise
    assert hw.is_jogging("C") == -1

    st = hw.poll_all_axes()["C"]
    assert st.fwd_limit is True, "the switch still marks the + end"
    assert st.rev_limit is False, (
        "the escape direction was flagged, so _guard_jog_limits would halt it "
        "one poll after it started")
