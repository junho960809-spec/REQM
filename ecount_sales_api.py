from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import urllib.error
import urllib.request
from ctypes import wintypes
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ecount_sales_core import VoucherLine


class EcountSalesError(RuntimeError):
    pass


LOCAL_REQM_DIR = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "REQM"
SETTINGS_PATH = LOCAL_REQM_DIR / "ecount_sales_api_settings.json"
CREDENTIAL_STORE_PATH = LOCAL_REQM_DIR / "ecount_api_keys.json"
HISTORY_PATH = LOCAL_REQM_DIR / "ecount_sales_history.json"
CRYPTPROTECT_UI_FORBIDDEN = 0x01
DEFAULT_ENDPOINT = "Sale/SaveSale"
DEFAULT_LIST_KEY = "SaleList"


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _input_blob(data: bytes) -> tuple[DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    return DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def protect_secret(value: str) -> str:
    input_blob, buffer = _input_blob(value.encode("utf-8"))
    output_blob = DataBlob()
    result = ctypes.windll.crypt32.CryptProtectData(
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
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)


def unprotect_secret(value: str) -> str:
    input_blob, buffer = _input_blob(base64.b64decode(value.encode("ascii")))
    output_blob = DataBlob()
    result = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(input_blob), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output_blob),
    )
    _ = buffer
    if not result:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return default


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_settings(path: Path = SETTINGS_PATH) -> dict[str, Any]:
    defaults = {
        "company_code": "",
        "user_id": "",
        "zone": "",
        "employee_code": "00109",
        "endpoint": DEFAULT_ENDPOINT,
        "list_key": DEFAULT_LIST_KEY,
        "test_mode": False,
        "remarks": "REQM 판매전표 API 입력",
    }
    loaded = _read_json(path, {})
    if isinstance(loaded, dict):
        defaults.update({key: value for key, value in loaded.items() if key in defaults})
    return defaults


def save_settings(settings: dict[str, Any], path: Path = SETTINGS_PATH) -> None:
    safe = {key: value for key, value in settings.items() if key != "api_key"}
    _write_json(path, safe)


def save_api_key(user_id: str, api_key: str, path: Path = CREDENTIAL_STORE_PATH) -> None:
    user_key = str(user_id or "").strip().casefold()
    secret = str(api_key or "").strip()
    if not user_key or not secret:
        raise ValueError("이카운트 사용자 ID와 API 인증키를 입력하세요.")
    store = _read_json(path, {})
    if not isinstance(store, dict):
        store = {}
    store[user_key] = protect_secret(secret)
    _write_json(path, store)


def load_api_key(user_id: str, path: Path = CREDENTIAL_STORE_PATH) -> str:
    store = _read_json(path, {})
    encrypted = store.get(str(user_id or "").strip().casefold(), "") if isinstance(store, dict) else ""
    if not encrypted:
        return ""
    try:
        return unprotect_secret(str(encrypted))
    except (OSError, ValueError, ctypes.Error):
        return ""


def build_sales_payload(
    lines: list[VoucherLine],
    voucher_date: date,
    employee_code: str,
    list_key: str = DEFAULT_LIST_KEY,
    remarks: str = "",
) -> dict[str, list[dict[str, dict[str, str]]]]:
    if not lines:
        raise ValueError("전송할 판매전표 품목이 없습니다.")
    voucher_serials = {
        key: str(index)
        for index, key in enumerate(sorted({
            (str(line.warehouse), str(line.customer_code)) for line in lines
        }), start=1)
    }
    rows: list[dict[str, dict[str, str]]] = []
    for line in sorted(
        lines,
        key=lambda row: (str(row.warehouse), str(row.customer_code), row.item_code, row.unit_price),
    ):
        total = line.total.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        supply = (total / Decimal("1.1")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        vat = total - supply
        rows.append({"BulkDatas": {
            "IO_DATE": voucher_date.strftime("%Y%m%d"),
            "UPLOAD_SER_NO": voucher_serials[(str(line.warehouse), str(line.customer_code))],
            "CUST": str(line.customer_code),
            "CUST_DES": "",
            "EMP_CD": employee_code,
            "WH_CD": str(line.warehouse),
            "IO_TYPE": "",
            "EXCHANGE_TYPE": "",
            "EXCHANGE_RATE": "",
            "PROD_CD": str(line.item_code),
            "PROD_DES": "",
            "SIZE_DES": "",
            "UQTY": "",
            "QTY": format(line.quantity, "f"),
            "PRICE": format(line.unit_price.quantize(Decimal("1"), rounding=ROUND_HALF_UP), "f"),
            "USER_PRICE_VAT": "",
            "SUPPLY_AMT": format(supply, "f"),
            "SUPPLY_AMT_F": "",
            "VAT_AMT": format(vat, "f"),
            "REMARKS": remarks,
        }})
    return {list_key.strip() or DEFAULT_LIST_KEY: rows}


def payload_total(payload: dict[str, Any]) -> Decimal:
    rows = next((value for value in payload.values() if isinstance(value, list)), [])
    return sum(
        (
            Decimal(str((row.get("BulkDatas") or {}).get("SUPPLY_AMT") or "0"))
            + Decimal(str((row.get("BulkDatas") or {}).get("VAT_AMT") or "0"))
            for row in rows
        ),
        Decimal("0"),
    )


def request_key(payload: dict[str, Any], endpoint: str, test_mode: bool) -> str:
    stable = json.dumps(
        {"payload": payload, "endpoint": endpoint, "test_mode": test_mode},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def load_history(path: Path = HISTORY_PATH) -> list[dict[str, Any]]:
    value = _read_json(path, [])
    return value if isinstance(value, list) else []


def save_history(record: dict[str, Any], path: Path = HISTORY_PATH) -> None:
    history = load_history(path)
    history.append(record)
    _write_json(path, history[-2000:])


def parse_sales_result(response: dict[str, Any]) -> dict[str, Any]:
    data = response.get("Data") or {}
    success = int(data.get("SuccessCnt") or 0)
    failed = int(data.get("FailCnt") or 0)
    status = str(response.get("Status") or "")
    if status not in {"", "200"} or failed or not success:
        messages: list[str] = []
        details = data.get("ResultDetails") or []
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except ValueError:
                details = []
        for detail in details if isinstance(details, list) else []:
            messages.extend(str(error) for error in (detail.get("Errors") or []))
        error = response.get("Error") or response.get("Errors") or ""
        if isinstance(error, dict):
            error = error.get("Message") or error.get("MessageDetail") or error
        message = " / ".join(messages) or str(error) or "이카운트 판매전표 입력에 실패했습니다."
        raise EcountSalesError(message)
    return {
        "success_count": success,
        "fail_count": failed,
        "slip_numbers": data.get("SlipNos") or [],
        "raw": response,
    }


class EcountSalesClient:
    def __init__(
        self,
        company_code: str,
        user_id: str,
        api_key: str,
        zone: str = "",
        test_mode: bool = False,
        endpoint: str = DEFAULT_ENDPOINT,
    ) -> None:
        self.company_code = company_code.strip()
        self.user_id = user_id.strip()
        self.api_key = api_key.strip()
        self.zone = zone.strip().upper()
        self.test_mode = bool(test_mode)
        self.endpoint = endpoint.strip().strip("/") or DEFAULT_ENDPOINT

    @property
    def api_host_prefix(self) -> str:
        return "sboapi" if self.test_mode else "oapi"

    @staticmethod
    def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "REQM-SALES/1.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8-sig"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise EcountSalesError(f"이카운트 HTTP 오류 {exc.code}: {body[:500]}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise EcountSalesError(f"이카운트 서버 연결 실패: {exc}") from exc

    def resolve_zone(self) -> str:
        if self.zone:
            return self.zone
        response = self._post_json(
            f"https://{self.api_host_prefix}.ecount.com/OAPI/V2/Zone",
            {"COM_CODE": self.company_code},
        )
        self.zone = str((response.get("Data") or {}).get("ZONE") or response.get("ZONE") or "").strip().upper()
        if not self.zone:
            raise EcountSalesError("회사코드에 해당하는 이카운트 ZONE을 찾지 못했습니다.")
        return self.zone

    @staticmethod
    def _find_session(value: Any) -> str:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).upper() == "SESSION_ID" and child:
                    return str(child)
                found = EcountSalesClient._find_session(child)
                if found:
                    return found
        if isinstance(value, list):
            for child in value:
                found = EcountSalesClient._find_session(child)
                if found:
                    return found
        return ""

    def login(self) -> str:
        zone = self.resolve_zone()
        response = self._post_json(
            f"https://{self.api_host_prefix}{zone}.ecount.com/OAPI/V2/OAPILogin",
            {
                "COM_CODE": self.company_code,
                "USER_ID": self.user_id,
                "API_CERT_KEY": self.api_key,
                "LAN_TYPE": "ko-KR",
                "ZONE": zone,
            },
        )
        session = self._find_session(response)
        if not session:
            error = response.get("Error") or "로그인 세션을 받지 못했습니다."
            if isinstance(error, dict):
                error = error.get("Message") or error.get("MessageDetail") or error
            raise EcountSalesError(f"이카운트 Open API 로그인 실패: {error}")
        return session

    def save_sales(self, payload: dict[str, Any]) -> dict[str, Any]:
        session = quote(self.login(), safe="")
        url = (
            f"https://{self.api_host_prefix}{self.zone}.ecount.com/OAPI/V2/{self.endpoint}"
            f"?SESSION_ID={session}"
        )
        return parse_sales_result(self._post_json(url, payload))
