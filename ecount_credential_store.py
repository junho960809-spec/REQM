import base64
import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path


CREDENTIAL_STORE_PATH = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "REQM" / "ecount_api_keys.json"
CRYPTPROTECT_UI_FORBIDDEN = 0x01


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _input_blob(data: bytes) -> tuple[DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    blob = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    return blob, buffer


def protect_secret(value: str) -> str:
    data = value.encode("utf-8")
    input_blob, buffer = _input_blob(data)
    output_blob = DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    result = crypt32.CryptProtectData(
        ctypes.byref(input_blob), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output_blob),
    )
    _ = buffer
    if not result:
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        return base64.b64encode(encrypted).decode("ascii")
    finally:
        kernel32.LocalFree(output_blob.pbData)


def unprotect_secret(value: str) -> str:
    encrypted = base64.b64decode(value.encode("ascii"))
    input_blob, buffer = _input_blob(encrypted)
    output_blob = DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    result = crypt32.CryptUnprotectData(
        ctypes.byref(input_blob), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output_blob),
    )
    _ = buffer
    if not result:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(output_blob.pbData)


def _load_store(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(key): str(value) for key, value in data.items() if key and value}
    except (OSError, ValueError, TypeError):
        return {}


def _save_store(data: dict[str, str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def save_api_key(user_id: str, api_key: str, path: Path = CREDENTIAL_STORE_PATH) -> None:
    user_key = str(user_id or "").strip().casefold()
    secret = str(api_key or "").strip()
    if not user_key or not secret:
        raise ValueError("사용자 ID와 API 인증키를 입력하세요.")
    store = _load_store(path)
    store[user_key] = protect_secret(secret)
    _save_store(store, path)


def load_api_key(user_id: str, path: Path = CREDENTIAL_STORE_PATH) -> str:
    encrypted = _load_store(path).get(str(user_id or "").strip().casefold(), "")
    if not encrypted:
        return ""
    try:
        return unprotect_secret(encrypted)
    except (OSError, ValueError, ctypes.Error):
        return ""


def delete_api_key(user_id: str, path: Path = CREDENTIAL_STORE_PATH) -> None:
    store = _load_store(path)
    store.pop(str(user_id or "").strip().casefold(), None)
    _save_store(store, path)
