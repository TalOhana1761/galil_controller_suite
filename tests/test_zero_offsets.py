"""
Tests for the display-only zero offsets behind the jog panel's ZERO buttons.

The contract, which is narrower than it looks:
  - zeroing changes what the COUNTS column shows, nothing else
  - it never touches the controller and never moves the stage
  - it survives a restart

Run:  pytest tests/test_zero_offsets.py -v
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
import pytest
from utils.zero_offsets import ZeroOffsets


@pytest.fixture
def zeros(tmp_path):
    return ZeroOffsets(path=str(tmp_path / "zero_offsets.json"))


def test_starts_unzeroed(zeros):
    for ax in ("A", "B", "C"):
        assert zeros.get(ax) == 0.0
        assert zeros.apply(ax, 12345.0) == 12345.0


def test_zeroing_makes_the_current_reading_read_zero(zeros):
    zeros.set_zero("A", 3_230_957.0)
    assert zeros.apply("A", 3_230_957.0) == 0.0


def test_relative_motion_still_reads_correctly(zeros):
    zeros.set_zero("A", 3_230_957.0)
    assert zeros.apply("A", 3_230_957.0 + 128.0) == 128.0
    assert zeros.apply("A", 3_230_957.0 - 64.0) == -64.0


def test_axes_are_independent(zeros):
    zeros.set_zero("A", 1000.0)
    assert zeros.apply("B", 1000.0) == 1000.0
    assert zeros.apply("C", 1000.0) == 1000.0


def test_survives_a_restart(tmp_path):
    path = str(tmp_path / "zero_offsets.json")
    ZeroOffsets(path=path).set_zero("C", 5_840_801.0)
    assert ZeroOffsets(path=path).apply("C", 5_840_801.0 + 64.0) == 64.0


def test_clear_returns_the_raw_reading(zeros):
    zeros.set_zero("A", 1000.0)
    zeros.clear("A")
    assert zeros.apply("A", 1000.0) == 1000.0


def test_a_corrupt_file_does_not_stop_startup(tmp_path):
    """A bad file must degrade to "unzeroed", not take the app down."""
    path = tmp_path / "zero_offsets.json"
    path.write_text("{ this is not json")
    assert ZeroOffsets(path=str(path)).get("A") == 0.0


def test_missing_file_is_normal(tmp_path):
    assert ZeroOffsets(path=str(tmp_path / "nope.json")).get("A") == 0.0


def test_it_writes_what_it_says(tmp_path):
    path = tmp_path / "zero_offsets.json"
    z = ZeroOffsets(path=str(path))
    z.set_zero("B", 42.0)
    z.set_um_origin("B", 7.0)
    written = json.loads(path.read_text())
    assert written["counts_zero"]["B"] == 42.0
    assert written["um_origin"]["B"] == 7.0


# ── The two origins are independent ─────────────────────────────────────────

def test_zero_button_does_not_move_the_micron_origin(zeros):
    """The whole point of keeping them apart.

    The ZERO buttons are a working zero for the counts column and get pressed
    whenever convenient. If that also moved the micron origin, the one reading
    that is meant to be stable would silently change meaning.
    """
    zeros.set_um_origin("A", 3_226_664.0)          # the rig's true zero
    zeros.set_zero("A", 3_226_664.0 + 1280.0)      # working zero, 100 um along

    here = 3_226_664.0 + 1280.0
    assert zeros.apply("A", here) == 0.0           # counts zeroed
    assert zeros.apply_um("A", here) == 1280.0     # microns still absolute


def test_micron_origin_survives_repeated_counts_zeroing(zeros):
    zeros.set_um_origin("C", 6_329_144.0)
    for offset in (128.0, 640.0, 1280.0):
        zeros.set_zero("C", 6_329_144.0 + offset)
    assert zeros.apply_um("C", 6_329_144.0 + 64.0) == 64.0


def test_both_origins_persist(tmp_path):
    path = str(tmp_path / "zero_offsets.json")
    z = ZeroOffsets(path=path)
    z.set_um_origin("B", 3_834_226.0)
    z.set_zero("B", 3_834_226.0 + 500.0)

    again = ZeroOffsets(path=path)
    assert again.get_um_origin("B") == 3_834_226.0
    assert again.get("B") == 3_834_226.0 + 500.0


def test_old_flat_file_seeds_both_origins(tmp_path):
    """Migration: the flat format was written at the rig's zero position.

    Those values were recorded where the stage's zero is, so they are the right
    seed for the micron origin as well - the one-time micron zero, without the
    stage needing to be parked anywhere to migrate.
    """
    path = tmp_path / "zero_offsets.json"
    path.write_text(json.dumps({"A": 3_226_664.0, "B": 3_834_226.0, "C": 6_329_144.0}))

    z = ZeroOffsets(path=str(path))
    assert z.get("A") == 3_226_664.0
    assert z.get_um_origin("A") == 3_226_664.0

    # ...and it is rewritten in the new shape, so migration happens once.
    written = json.loads(path.read_text())
    assert set(written) == {"counts_zero", "um_origin"}


def test_micron_origin_defaults_to_raw_when_never_set(zeros):
    """Unset origin means the micron column shows the raw PRC coordinate."""
    assert zeros.apply_um("A", 3_226_664.0) == 3_226_664.0


# ── Homing target ───────────────────────────────────────────────────────────

def test_home_target_is_the_counts_zero(zeros):
    """HOME drives to where the counts display reads 0, i.e. the counts zero.

    Not raw _TP = 0: _TP is an absolute PRC coordinate, so axis A's zero sits
    around 3,226,664 counts and a literal 0 would ask for 252 mm of travel on a
    20 mm axis.
    """
    zeros.set_zero("A", 3_226_664.0)
    assert zeros.get("A") == 3_226_664.0
    assert zeros.apply("A", zeros.get("A")) == 0.0


def test_unzeroed_axis_has_no_home_target(zeros):
    """0 means "never zeroed", not "the zero is at _TP 0".

    An absolute encoder on this rig reads millions; it never reads 0. The panel
    refuses to home an axis in this state rather than driving across the travel.
    """
    assert zeros.get("B") == 0.0


def test_home_target_follows_the_latest_zero(zeros):
    """Re-zeroing moves where HOME goes. That is the point of the working zero.

    HOME reads the same value ZERO writes, so pressing ZERO anywhere makes that
    spot the new home target immediately. The micron origin does not move with
    it - the two are independent on purpose.
    """
    zeros.set_um_origin("A", 3_226_664.0)      # true zero, fixed
    zeros.set_zero("A", 3_226_664.0)
    assert zeros.get("A") == 3_226_664.0       # home target #1

    zeros.set_zero("A", 3_239_465.0)           # re-zero 1000 um along
    assert zeros.get("A") == 3_239_465.0       # HOME follows
    assert zeros.get_um_origin("A") == 3_226_664.0   # true zero unmoved


def test_home_target_persists_across_a_restart(tmp_path):
    """The working zero has to outlive the session, like the true zero."""
    path = str(tmp_path / "zero_offsets.json")
    ZeroOffsets(path=path).set_zero("C", 6_329_144.0)
    assert ZeroOffsets(path=path).get("C") == 6_329_144.0
