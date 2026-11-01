"""Suite-wide isolation from the rig's own config/.

Every JogPanel reads config/z_retract_state.json when it is built. Only the
retract and touch-alarm fixtures used to redirect it, so the key-jog tests
failed whenever the RIG was genuinely retracted (2026-10-08: a real retract at
16:23 left off_tape there, Z locked, nine tests failed). The reverse is worse:
a test that writes the real file fabricates where the head is. Redirect it for
every test; fixtures that set their own path still win, being more specific.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


@pytest.fixture(autouse=True)
def _isolate_z_retract_state(tmp_path, monkeypatch):
    from utils import z_retract_state
    monkeypatch.setattr(z_retract_state, "STATE_PATH",
                        str(tmp_path / "z_retract_state.json"))
