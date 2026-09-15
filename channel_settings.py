"""판매처별 배송비 처리 규칙의 로컬 저장과 조회."""
from __future__ import annotations

import json
import os
from pathlib import Path


SETTINGS_PATH = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "REQM" / "channel_shipping_rules.json"
DEFAULT_RULE = {"method": "separate", "source": "custom10", "default_fee": 0,
                "island": "already_included", "active": True}
DEFAULT_RULES = {
    "스마트스토어": {**DEFAULT_RULE, "source": "shipping_total"},
    "오늘의집": {**DEFAULT_RULE, "method": "subtract"},
    "11번가": {**DEFAULT_RULE, "source": "amount_minus_unit"},
    "지마켓": {**DEFAULT_RULE, "method": "exclude", "source": "none"},
    "옥션": {**DEFAULT_RULE, "method": "exclude", "source": "none"},
}


def load_shipping_rules(path: Path = SETTINGS_PATH) -> dict[str, dict]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(loaded, dict):
            loaded = {}
    except (OSError, ValueError, TypeError):
        loaded = {}
    rules = {name: dict(value) for name, value in DEFAULT_RULES.items()}
    for name, value in loaded.items():
        if isinstance(value, dict):
            rules[str(name)] = {**DEFAULT_RULE, **value}
    return rules


def save_shipping_rules(rules: dict[str, dict], path: Path = SETTINGS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def shipping_rule_for(channel: str, rules: dict[str, dict] | None = None) -> dict:
    current = rules if rules is not None else load_shipping_rules()
    target = "".join(str(channel or "").split()).casefold()
    for name, rule in current.items():
        if "".join(str(name).split()).casefold() == target:
            return {**DEFAULT_RULE, **rule}
    return dict(DEFAULT_RULE)
