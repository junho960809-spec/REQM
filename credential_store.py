from __future__ import annotations

import base64
import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path


CRYPTPROTECT_UI_FORBIDDEN = 0x01
DESCRIPTION = "REQM ECOUNT API credential"


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def credential_file() -> Path:
    root = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "REQM"
    root.mkdir(parents=True, exist_ok=True)
    return root / "credentials.json"


def _crypt32():
    if os.name != "nt":
        raise RuntimeError("API 인증키 암호화 저장은 Windows에서만 지원합니다.")
    crypt32 = ctypes.windll.crypt32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    return crypt32


def _input_blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    blob = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    return blob, buffer


def _protect(value: str) -> str:
    raw = value.encode("utf-8")
    source, source_buffer = _input_blob(raw)
    encrypted = DATA_BLOB()
    if not _crypt32().CryptProtectData(
        ctypes.byref(source), DESCRIPTION, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(encrypted),
    ):
        raise ctypes.WinError()
    try:
        return base64.b64encode(ctypes.string_at(encrypted.pbData, encrypted.cbData)).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(encrypted.pbData)
        del source_buffer


def _unprotect(value: str) -> str:
    source, source_buffer = _input_blob(base64.b64decode(value))
    decrypted = DATA_BLOB()
    description = wintypes.LPWSTR()
    if not _crypt32().CryptUnprotectData(
        ctypes.byref(source), ctypes.byref(description), None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(decrypted),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(decrypted.pbData, decrypted.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(decrypted.pbData)
        if description:
            ctypes.windll.kernel32.LocalFree(description)
        del source_buffer


def load_ecount_api_key() -> str:
    path = credential_file()
    if not path.exists():
        return ""
    payload = json.loads(path.read_text(encoding="utf-8"))
    encrypted = str(payload.get("ecount_api_key", ""))
    return _unprotect(encrypted) if encrypted else ""


def save_ecount_api_key(api_key: str) -> None:
    value = api_key.strip()
    if not value:
        raise ValueError("저장할 API 인증키가 없습니다.")
    path = credential_file()
    temporary = path.with_suffix(".tmp")
    payload = {"version": 1, "ecount_api_key": _protect(value)}
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def delete_ecount_api_key() -> None:
    path = credential_file()
    if path.exists():
        path.unlink()
