from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any


DEFAULT_FORMAT = {"id": "default_b2c", "name": "기본 B2C 양식", "builtin": True}
B2C_PURCHASE_FORMAT = {
    "id": "b2c_purchase",
    "name": "B2C 사입형 출고건",
    "builtin": True,
    "headers": [
        "주문번호", "상품번호", "상품명", "옵션명", "수량", "판매단가", "판매금액",
        "수령자", "전화", "핸드폰", "우편번호", "주소", "배송메세지", "배송비", "송장출력갯수",
    ],
    "mapping": {
        "order_number": "주문번호",
        "product_name": "상품명",
        "options": "옵션명",
        "quantity": "수량",
        "recipient": "수령자",
        "phone": "핸드폰",
        "zipcode": "우편번호",
        "address": "주소",
        "message": "배송메세지",
    },
}


def _base_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "REQM"


def _profile_file() -> Path:
    return _base_dir() / "output_formats.json"


def load_output_formats() -> list[dict[str, Any]]:
    custom: list[dict[str, Any]] = []
    try:
        data = json.loads(_profile_file().read_text(encoding="utf-8"))
        custom = [
            row for row in data
            if isinstance(row, dict) and row.get("id") and row.get("name")
            and row.get("template_path") and row.get("mapping")
        ]
    except (OSError, ValueError, TypeError):
        pass
    return [dict(DEFAULT_FORMAT), dict(B2C_PURCHASE_FORMAT), *custom]


def save_custom_output_format(name: str, source_path: str, header_row: int, mapping: dict[str, str]) -> dict[str, Any]:
    profile_id = str(uuid.uuid4())
    template_dir = _base_dir() / "output_templates"
    template_dir.mkdir(parents=True, exist_ok=True)
    target = template_dir / f"{profile_id}.xlsx"
    shutil.copy2(source_path, target)
    profile = {
        "id": profile_id,
        "name": name,
        "builtin": False,
        "template_path": str(target),
        "header_row": int(header_row),
        "mapping": mapping,
    }
    custom = [row for row in load_output_formats() if not row.get("builtin")]
    custom.append(profile)
    _write_custom(custom)
    return profile


def delete_output_format(profile_id: str) -> None:
    custom = [row for row in load_output_formats() if not row.get("builtin")]
    removed = next((row for row in custom if row.get("id") == profile_id), None)
    custom = [row for row in custom if row.get("id") != profile_id]
    _write_custom(custom)
    if removed:
        try:
            Path(str(removed.get("template_path", ""))).unlink(missing_ok=True)
        except OSError:
            pass


def _write_custom(formats: list[dict[str, Any]]) -> None:
    path = _profile_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(formats, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
