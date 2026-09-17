from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


MARKET_URLS = {
    "옥션": "https://www.esmplus.com/Member/SignIn/LogOn",
    "지마켓": "https://www.esmplus.com/Member/SignIn/LogOn",
}


def find_browser(preference: str = "자동 선택") -> tuple[str, str] | None:
    candidates = []
    chrome = shutil.which("chrome") or next(
        (str(p) for p in (
            Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ) if p.is_file()), ""
    )
    edge = shutil.which("msedge") or next(
        (str(p) for p in (
            Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
        ) if p.is_file()), ""
    )
    if preference in ("자동 선택", "Chrome"):
        candidates.append(("Chrome", chrome))
    if preference in ("자동 선택", "Edge IE 모드"):
        candidates.append(("Edge IE 모드", edge))
    return next(((name, path) for name, path in candidates if path), None)


def launch_marketplace(
    market: str, preference: str, profile_root: Path,
) -> tuple[str, subprocess.Popen]:
    browser = find_browser(preference)
    if browser is None:
        raise RuntimeError("Chrome 또는 Microsoft Edge를 찾을 수 없습니다.")
    browser_name, executable = browser
    profile = profile_root / market
    profile.mkdir(parents=True, exist_ok=True)
    arguments = [executable, f"--user-data-dir={profile}", "--no-first-run"]
    if browser_name == "Edge IE 모드":
        arguments.append("--ie-mode-test")
    arguments.append(MARKET_URLS[market])
    return browser_name, subprocess.Popen(arguments)


def load_browser_settings(path: Path) -> dict:
    defaults = {"옥션": "자동 선택", "지마켓": "자동 선택"}
    try:
        stored = json.loads(path.read_text(encoding="utf-8-sig"))
        return defaults | {key: value for key, value in stored.items() if key in defaults}
    except Exception:
        return defaults


def save_browser_settings(path: Path, settings: dict) -> None:
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
