"""
Unit tests for PositionMemory (no hardware required).
Run: pytest tests/test_position_memory.py -v
"""
import sys, os, tempfile, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from utils.position_memory import PositionMemory


@pytest.fixture
def mem(tmp_path):
    path = str(tmp_path / "positions.json")
    return PositionMemory(path)


def test_save_and_get(mem):
    mem.save("home", 0.0, 0.0, 0.0)
    slot = mem.get("home")
    assert slot is not None
    assert slot.name == "home"
    assert slot.pos_a == 0.0


def test_overwrite(mem):
    mem.save("p1", 100.0, 200.0, 300.0)
    mem.save("p1", 999.0, 0.0, 0.0)
    slot = mem.get("p1")
    assert slot.pos_a == 999.0


def test_delete(mem):
    mem.save("to_delete", 1.0, 2.0, 3.0)
    mem.delete("to_delete")
    assert mem.get("to_delete") is None


def test_all_slots(mem):
    mem.save("a", 1.0, 2.0, 3.0)
    mem.save("b", 4.0, 5.0, 6.0)
    slots = mem.all_slots()
    assert len(slots) == 2
    names = {s.name for s in slots}
    assert names == {"a", "b"}


def test_persistence(tmp_path):
    path = str(tmp_path / "pos.json")
    m1 = PositionMemory(path)
    m1.save("persistent", 10.0, 20.0, 30.0)

    m2 = PositionMemory(path)
    slot = m2.get("persistent")
    assert slot is not None
    assert slot.pos_b == 20.0


def test_missing_file_does_not_crash(tmp_path):
    path = str(tmp_path / "nonexistent.json")
    mem = PositionMemory(path)
    assert mem.all_slots() == []
