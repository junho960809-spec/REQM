from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


FIELDS = ("id", "name", "channel", "recipient", "phone", "zipcode", "address", "message")


def location_file() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "REQM"
    return base / "duty_free_locations.json"


def load_locations() -> list[dict[str, str]]:
    path = location_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        return [
            {key: str(row.get(key, "") or "").strip() for key in FIELDS}
            for row in data
            if isinstance(row, dict) and row.get("name")
        ]
    except (OSError, ValueError, TypeError):
        return []


def save_locations(locations: list[dict[str, Any]]) -> None:
    path = location_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = [
        {key: str(row.get(key, "") or "").strip() for key in FIELDS}
        for row in locations
        if row.get("name")
    ]
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
