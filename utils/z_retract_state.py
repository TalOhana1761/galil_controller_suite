"""
Whether the Z head is parked off the scale tape, remembered across restarts.

Retract Z drives the head past the end of the tape to the limit switch, and
Insert Z brings it back. Nothing on the rig can be asked which of those is true:
_TP is not a valid reading while the head is off, so the software has to
remember. Without this the panel offered Retract Z on every launch, which after
a retract is the one move that makes no sense.

Only the two STABLE states are stored, and the mapping is deliberately
asymmetric:

    ON_TAPE   written ONLY when an insert has completed - i.e. when the encoder
              itself said its data is valid again, which is the one moment the
              head is known to be back.
    OFF_TAPE  written the moment a retract STARTS, and kept for the whole of
              retracting / parked / inserting.

So a crash or a kill at any point during the round trip leaves OFF_TAPE on disk.
That is the safe way round: OFF_TAPE offers Insert Z, which is the move that
recovers, and it costs only a short blind jog if the head happened to be on the
tape after all - the insert stops as soon as the encoder reads valid.

The reverse mistake is the expensive one. A stale ON_TAPE offers Retract Z, whose
first act is a closed-loop move to 0 um; against an off-tape _TP that lands
hundreds of mm away and the travel guard in _move_microns() refuses it, so it
fails loudly rather than driving anywhere - but it is still a state nobody wants
to be in.
"""
import json
import os

from loguru import logger
from utils.app_paths import config_path

STATE_PATH = config_path("z_retract_state.json")

ON_TAPE = "on_tape"
OFF_TAPE = "off_tape"
_VALID = (ON_TAPE, OFF_TAPE)


def load(path: str | None = None) -> str:
    """The stored state, or ON_TAPE if there is nothing readable.

    ON_TAPE is the default for a fresh install because that is where a stage
    that has never been retracted sits. It is also the assumption that fails
    loudly rather than quietly: see the module docstring.
    """
    path = path or STATE_PATH
    try:
        with open(path, "r") as f:
            state = json.load(f).get("state")
        if state in _VALID:
            logger.info(f"Z retract state loaded: {state}")
            return state
        logger.warning(f"Z retract state {state!r} unrecognised; assuming {ON_TAPE}")
    except FileNotFoundError:
        logger.info(f"No Z retract state file; assuming {ON_TAPE}")
    except Exception as e:
        logger.warning(f"Z retract state unreadable ({e}); assuming {ON_TAPE}")
    return ON_TAPE


def save(state: str, path: str | None = None):
    """Persist ON_TAPE or OFF_TAPE. Called on a change, not on a timer."""
    if state not in _VALID:
        raise ValueError(f"Unknown Z retract state {state!r}; expected {_VALID}")
    path = path or STATE_PATH
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"state": state}, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not save Z retract state: {e}")
