from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def format_file() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "REQM"
    return base / "order_file_formats.json"


def load_formats() -> list[dict[str, Any]]:
    path = format_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [row for row in data if isinstance(row, dict) and row.get("name") and row.get("mapping")]
    except (OSError, ValueError, TypeError):
        return []


def save_formats(formats: list[dict[str, Any]]) -> None:
    path = format_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(formats, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def upsert_format(profile: dict[str, Any]) -> None:
    formats = load_formats()
    profile_id = str(profile.get("id", ""))
    index = next((i for i, row in enumerate(formats) if str(row.get("id", "")) == profile_id), -1)
    if index >= 0:
        formats[index] = profile
    else:
        formats.append(profile)
    save_formats(formats)
