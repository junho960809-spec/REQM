import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote


class EcountError(RuntimeError):
    pass


ALLOWED_TRANSFER_STATUSES = {"exact", "manual", "alias"}
TRANSFER_HISTORY_PATH = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "REQM" / "ecount_transfer_history.json"


def load_completed_transfer_requests() -> set[str]:
    try:
        data = json.loads(TRANSFER_HISTORY_PATH.read_text(encoding="utf-8"))
        return {str(value) for value in data if value}
    except (OSError, ValueError, TypeError):
        return set()


def save_completed_transfer_request(request_key: str) -> None:
    completed = list(load_completed_transfer_requests())
    if request_key not in completed:
        completed.append(request_key)
    TRANSFER_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRANSFER_HISTORY_PATH.write_text(
        json.dumps(completed[-1000:], ensure_ascii=False, indent=2), encoding="utf-8"
    )


def decimal_value(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        text = str(value or "").replace(",", "").strip()
        return Decimal(text) if text else default
    except InvalidOperation:
        return default


def parse_components(value: str) -> list[tuple[str, Decimal]]:
    components = []
    for part in str(value or "").split("+"):
        part = part.strip()
        if not part:
            continue
        match = re.match(r"^(.*?)\s*[×xX*]\s*([0-9]+(?:\.[0-9]+)?)$", part)
        if match:
            code, quantity = match.group(1).strip(), decimal_value(match.group(2))
        else:
            code, quantity = part, Decimal("1")
        if code and quantity > 0:
            components.append((code, quantity))
    return components


def collect_transfer_items(
    orders: list[dict[str, Any]], channel: str, items: list[dict[str, Any]] | None = None
) -> tuple[list[dict[str, str]], dict[str, int]]:
    names = {
        str(item.get("item_code", "")).strip(): str(item.get("standard_name", "")).strip()
        for item in (items or [])
    }
    totals: dict[str, Decimal] = defaultdict(Decimal)
    summary = {"selected": 0, "included": 0, "excluded": 0}
    for order in orders:
        if channel and str(order.get("channel", "")).strip() != channel:
            continue
        summary["selected"] += 1
        if order.get("status") not in ALLOWED_TRANSFER_STATUSES:
            summary["excluded"] += 1
            continue
        order_quantity = decimal_value(order.get("quantity"), Decimal("1"))
        components = parse_components(str(order.get("components", "")))
        if order_quantity <= 0 or not components:
            summary["excluded"] += 1
            continue
        for code, component_quantity in components:
            totals[code] += order_quantity * component_quantity
        summary["included"] += 1
    rows = [
        {"item_code": code, "item_name": names.get(code, ""), "quantity": format_quantity(quantity)}
        for code, quantity in sorted(totals.items())
        if quantity > 0
    ]
    return rows, summary


def format_quantity(value: Decimal) -> str:
    normalized = value.normalize()
    return str(int(normalized)) if normalized == normalized.to_integral() else format(normalized, "f")


def build_location_transfer_payload(
    transfer_date: str,
    employee_code: str,
    source_warehouse: str,
    target_warehouse: str,
    items: list[dict[str, str]],
    remarks: str = "",
) -> dict[str, list[dict[str, dict[str, str]]]]:
    rows = []
    for item in items:
        rows.append({"BulkDatas": {
            "IO_DATE": transfer_date,
            "UPLOAD_SER_NO": "1",
            "EMP_CD": employee_code,
            "WH_CD_F": source_warehouse,
            "WH_CD_T": target_warehouse,
            "U_MEMO1": "", "U_MEMO2": "", "U_MEMO3": "", "U_MEMO4": "", "U_MEMO5": "",
            "U_TXT1": "", "PJT_CD": "", "DOC_NO": "",
            "PROD_CD": item["item_code"], "PROD_DES": "", "SIZE_DES": "", "UQTY": "",
            "QTY": item["quantity"], "REMARKS": remarks,
            "P_REMARKS1": "", "P_REMARKS2": "", "P_REMARKS3": "",
            "P_AMT1": "", "P_AMT2": "",
        }})
    return {"LocationTranList": rows}


def transfer_request_key(channel: str, payload: dict[str, Any]) -> str:
    stable = json.dumps({"channel": channel, "payload": payload}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def parse_transfer_result(response: dict[str, Any]) -> dict[str, Any]:
    data = response.get("Data") or {}
    details = data.get("ResultDetails") or []
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except json.JSONDecodeError:
            details = []
    success = int(data.get("SuccessCnt") or 0)
    failed = int(data.get("FailCnt") or 0)
    if str(response.get("Status", "")) not in {"200", ""} or failed or not success:
        messages = []
        for row in details if isinstance(details, list) else []:
            messages.extend(str(error) for error in (row.get("Errors") or []))
        error = response.get("Error")
        if isinstance(error, dict):
            message = str(error.get("Message") or error.get("MessageDetail") or error)
        else:
            message = str(error or "")
        message = " / ".join(messages) or message or "이카운트 창고이동 처리에 실패했습니다."
        if "인증되지 않은 API" in message:
            message = (
                "창고이동 API 권한이 인증되지 않았습니다.\n\n"
                "1. 입력한 API 인증키가 현재 사용자 ID에서 발급된 키인지 확인하세요.\n"
                "2. 해당 사용자에게 재고Ⅱ > 창고이동 입력 권한이 있는지 확인하세요.\n"
                "3. 이카운트 Open API 설정에서 창고이동 API 사용 권한을 확인하세요.\n\n"
                f"이카운트 응답: {message}"
            )
        raise EcountError(message)
    return {"success_count": success, "fail_count": failed, "slip_numbers": data.get("SlipNos") or [], "raw": response}


class EcountClient:
    def __init__(
        self, company_code: str, user_id: str, api_key: str, zone: str = "", test_mode: bool = False
    ):
        self.company_code = company_code.strip()
        self.user_id = user_id.strip()
        self.api_key = api_key.strip()
        self.zone = zone.strip().upper()
        self.test_mode = bool(test_mode)

    @property
    def api_host_prefix(self) -> str:
        return "sboapi" if self.test_mode else "oapi"

    @staticmethod
    def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "REQM-ECOUNT/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise EcountError(f"이카운트 HTTP 오류 {exc.code}: {body[:500]}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise EcountError(f"이카운트 서버 연결 실패: {exc}") from exc

    def resolve_zone(self) -> str:
        if self.zone:
            return self.zone
        response = self._post_json(
            f"https://{self.api_host_prefix}.ecount.com/OAPI/V2/Zone", {"COM_CODE": self.company_code}
        )
        zone = str((response.get("Data") or {}).get("ZONE") or response.get("ZONE") or "").strip().upper()
        if not zone:
            raise EcountError("회사코드에 해당하는 이카운트 ZONE을 찾지 못했습니다.")
        self.zone = zone
        return zone

    @staticmethod
    def _find_session(value: Any) -> str:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).upper() == "SESSION_ID" and child:
                    return str(child)
                found = EcountClient._find_session(child)
                if found:
                    return found
        if isinstance(value, list):
            for child in value:
                found = EcountClient._find_session(child)
                if found:
                    return found
        return ""

    def login(self) -> str:
        zone = self.resolve_zone()
        response = self._post_json(
            f"https://{self.api_host_prefix}{zone}.ecount.com/OAPI/V2/OAPILogin",
            {"COM_CODE": self.company_code, "USER_ID": self.user_id, "API_CERT_KEY": self.api_key,
             "LAN_TYPE": "ko-KR", "ZONE": zone},
        )
        session_id = self._find_session(response)
        if not session_id:
            error = response.get("Error")
            if isinstance(error, dict):
                error = error.get("Message") or error.get("MessageDetail") or error
            raise EcountError(f"이카운트 Open API 로그인 실패: {error or '로그인 세션을 받지 못했습니다.'}")
        return session_id

    def save_location_transfer(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = self.login()
        encoded_session = quote(session_id, safe="")
        url = (
            f"https://{self.api_host_prefix}{self.zone}.ecount.com/OAPI/V2/Others/SaveLocationTran"
            f"?SESSION_ID={encoded_session}"
        )
        return parse_transfer_result(self._post_json(url, payload))
