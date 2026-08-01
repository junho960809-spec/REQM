import json
import os
from pathlib import Path
from typing import Any


USER_STORE_PATH = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "REQM" / "ecount_users.json"


def normalize_user_profile(profile: dict[str, Any]) -> dict[str, str]:
    return {
        "user_id": str(profile.get("user_id", "")).strip(),
        "employee_code": str(profile.get("employee_code", "")).strip(),
        "display_name": str(profile.get("display_name", "")).strip(),
    }


def load_ecount_users(path: Path = USER_STORE_PATH) -> list[dict[str, str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    profiles = [normalize_user_profile(row) for row in data if isinstance(row, dict)]
    return sorted(
        [row for row in profiles if row["user_id"] and row["employee_code"]],
        key=lambda row: row["user_id"].casefold(),
    )


def save_ecount_users(users: list[dict[str, Any]], path: Path = USER_STORE_PATH) -> None:
    normalized = []
    seen = set()
    for profile in users:
        row = normalize_user_profile(profile)
        key = row["user_id"].casefold()
        if not row["user_id"] or not row["employee_code"] or key in seen:
            continue
        seen.add(key)
        normalized.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def upsert_ecount_user(profile: dict[str, Any], path: Path = USER_STORE_PATH) -> dict[str, str]:
    row = normalize_user_profile(profile)
    if not row["user_id"] or not row["employee_code"]:
        raise ValueError("이카운트 사용자 ID와 담당자코드를 입력하세요.")
    users = load_ecount_users(path)
    key = row["user_id"].casefold()
    updated = False
    for index, current in enumerate(users):
        if current["user_id"].casefold() == key:
            users[index] = row
            updated = True
            break
    if not updated:
        users.append(row)
    save_ecount_users(users, path)
    return row


def delete_ecount_user(user_id: str, path: Path = USER_STORE_PATH) -> None:
    key = str(user_id or "").strip().casefold()
    save_ecount_users([row for row in load_ecount_users(path) if row["user_id"].casefold() != key], path)
