"""
Position memory — loads/saves named position slots to config/positions.json.
"""
import json
import os
from datetime import datetime
from controller.data_models import PositionSlot
from utils.app_paths import config_path

DEFAULT_PATH = config_path("positions.json")


class PositionMemory:
    def __init__(self, path: str = DEFAULT_PATH):
        self._path = os.path.abspath(path)
        self._slots: dict[str, PositionSlot] = {}
        self._load()

    def _load(self):
        try:
            with open(self._path) as f:
                data = json.load(f)
            for item in data.get("slots", []):
                slot = PositionSlot(**item)
                self._slots[slot.name] = slot
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save(self):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        data = {"slots": [s.model_dump() for s in self._slots.values()]}
        with open(self._path, "w") as f:
            json.dump(data, f, indent=2)

    def save(self, name: str, pos_a: float, pos_b: float, pos_c: float):
        self._slots[name] = PositionSlot(
            name=name,
            pos_a=pos_a,
            pos_b=pos_b,
            pos_c=pos_c,
            timestamp=datetime.now().isoformat(timespec="seconds"),
        )
        self._save()

    def get(self, name: str) -> PositionSlot | None:
        return self._slots.get(name)

    def delete(self, name: str):
        self._slots.pop(name, None)
        self._save()

    def all_slots(self) -> list[PositionSlot]:
        return list(self._slots.values())

    def load_file(self, path: str):
        self._path = os.path.abspath(path)
        self._slots = {}
        self._load()
