import csv
import re
from pathlib import Path
from typing import Any

import openpyxl
import xlrd

from format_store import load_formats


COLUMN_ALIASES = {
    "channel": ["판매처명", "판매처", "쇼핑몰명", "mall"],
    "order_number": ["판매처주문번호", "주문번호", "쇼핑몰주문번호", "품목별 주문번호", "배송번호"],
    "serial_number": ["일련번호"],
    "recipient": ["수령인", "수취인", "받는분", "수령자", "수령인명", "인수자"],
    "zipcode": ["수령자우편번호", "우편번호", "수령인 우편번호(XXXXXX)"],
    "address1": ["수령자주소", "주소", "수령인 주소1", "인수자 주소"],
    "address2": ["수령자주소2", "상세주소", "수령인 주소2"],
    "message": ["상세요구사항", "배송메세지", "배송메시지", "배송메시지2(한줄로)", "고객메시지"],
    "phone": ["수령자휴대폰", "핸드폰", "휴대폰", "연락처", "수령인핸드폰번호", "수령인 핸드폰", "인수자 HP"],
    "product_name": ["판매처상품명", "상품명", "품목명", "주문상품명(기간할인 제목+버전)"],
    "item_code": ["품목코드", "상품코드", "제품코드", "재고코드", "협력사상품코드", "SKU", "PROD_CD", "ITEM CODE"],
    "option1": ["상품옵션", "옵션", "속성명"],
    "option2": ["상품옵션2", "옵션2"],
    "option3": ["상품옵션3", "옵션3"],
    "quantity": ["주문수량", "수량", "주문품목 수량", "대상 수량"],
    "model": ["모델명", "모델"],
    "match1": ["재고매칭(1)옵션내용", "재고매칭1", "매칭상품1"],
    "match2": ["재고매칭(2)옵션내용", "재고매칭2", "매칭상품2"],
}

REQUIRED = {"order_number", "recipient", "product_name", "quantity"}


def _clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _read_rows(path: Path) -> list[list[Any]]:
    suffix = path.suffix.lower()
    if suffix == ".xls":
        book = xlrd.open_workbook(path)
        sheet = book.sheet_by_index(0)
        return [[sheet.cell_value(r, c) for c in range(sheet.ncols)] for r in range(sheet.nrows)]
    if suffix == ".xlsx":
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
        sheet = book.active
        return [list(row) for row in sheet.iter_rows(values_only=True)]
    if suffix == ".csv":
        for encoding in ("utf-8-sig", "cp949"):
            try:
                with path.open("r", encoding=encoding, newline="") as handle:
                    return list(csv.reader(handle))
            except UnicodeDecodeError:
                continue
    if suffix == ".pdf":
        try:
            import pdfplumber
        except ImportError as exc:
            raise RuntimeError("PDF 분석 모듈이 없습니다. 최신 프로그램으로 업데이트해 주세요.") from exc
        rows: list[list[Any]] = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables() or []
                for table in tables:
                    rows.extend([list(row) for row in table if row])
                if not tables:
                    for line in (page.extract_text() or "").splitlines():
                        cells = [part.strip() for part in re.split(r"\s{2,}|\t", line) if part.strip()]
                        if cells:
                            rows.append(cells)
        if not rows:
            raise ValueError("PDF에서 표 또는 텍스트를 추출하지 못했습니다. 이미지 PDF라면 OCR 처리가 필요합니다.")
        return rows
    raise ValueError("지원 형식은 .xls, .xlsx, .csv, .pdf입니다.")


def _normalize_header(value: Any) -> str:
    return "".join(_clean(value).lower().split())


def _profile_columns(rows: list[list[Any]], profile: dict[str, Any]) -> tuple[int, dict[str, int]] | None:
    mapping = profile.get("mapping") or {}
    for row_index, row in enumerate(rows[:50]):
        headers = {_normalize_header(value): index for index, value in enumerate(row) if _clean(value)}
        columns = {
            key: headers[_normalize_header(header)]
            for key, header in mapping.items()
            if header and _normalize_header(header) in headers
        }
        if REQUIRED.issubset(columns):
            return row_index, columns
    return None


def _find_header(rows: list[list[Any]], profile: dict[str, Any] | None = None) -> tuple[int, dict[str, int], str]:
    if profile:
        found = _profile_columns(rows, profile)
        if not found:
            raise ValueError(f"저장된 '{profile.get('name', '')}' 양식의 필수 열을 찾지 못했습니다.")
        return found[0], found[1], str(profile.get("name", "사용자 양식"))

    for saved in load_formats():
        found = _profile_columns(rows, saved)
        if found:
            saved_headers = {_normalize_header(value) for value in rows[found[0]] if _clean(value)}
            built_in_direct = (
                {"배송번호", "품목별주문번호", "주문상품명(기간할인제목+버전)"}.issubset(saved_headers)
                or {"인수자", "인수자hp", "속성명", "협력사상품코드"}.issubset(saved_headers)
            )
            if built_in_direct:
                break
            return found[0], found[1], str(saved.get("name", "사용자 양식"))

    alias_lookup = {
        _normalize_header(alias): key
        for key, aliases in COLUMN_ALIASES.items()
        for alias in aliases
    }
    best_row = -1
    best_map: dict[str, int] = {}
    # 상단 안내문이나 병합행이 늘어나는 변형 양식도 찾을 수 있도록 넉넉히 탐색한다.
    for row_index, row in enumerate(rows[:50]):
        current: dict[str, int] = {}
        for col_index, value in enumerate(row):
            key = alias_lookup.get(_normalize_header(value))
            if key and key not in current:
                current[key] = col_index
        if len(current) > len(best_map):
            best_row, best_map = row_index, current
    missing = REQUIRED - set(best_map)
    if best_row < 0 or missing:
        labels = ", ".join(sorted(missing))
        raise ValueError(f"필수 열을 찾지 못했습니다: {labels}")
    header_values = {_normalize_header(value) for value in rows[best_row] if _clean(value)}
    if {"배송번호", "품목별주문번호", "주문상품명(기간할인제목+버전)"}.issubset(header_values):
        format_name = "판매처 직접파일 · 쌤몰"
        # 배송 묶음번호보다 각 품목을 고유하게 식별하는 품목별 주문번호를 사용한다.
        best_map["order_number"] = next(
            index for index, value in enumerate(rows[best_row])
            if _normalize_header(value) == "품목별주문번호"
        )
    elif {"인수자", "인수자hp", "속성명", "협력사상품코드"}.issubset(header_values):
        format_name = "판매처 직접파일 · 현대홈쇼핑"
    elif {"mall", "수령인명", "업체명", "모델명"}.issubset(header_values):
        format_name = "판매처 직접파일 · 이알아이"
    elif "재고매칭1옵션내용" in header_values or "재고매칭1" in header_values:
        format_name = "셀메이트 파일"
    else:
        format_name = "일반 주문 파일"
    return best_row, best_map, format_name


def suggest_header_row(file_path: str) -> tuple[int, list[str]]:
    rows = _read_rows(Path(file_path))
    if not rows:
        raise ValueError("파일에 데이터가 없습니다.")
    candidates = []
    for index, row in enumerate(rows[:50]):
        headers = [_clean(value) for value in row]
        text_count = sum(bool(value) for value in headers)
        known_count = sum(
            _normalize_header(value) in {
                _normalize_header(alias) for aliases in COLUMN_ALIASES.values() for alias in aliases
            }
            for value in headers if value
        )
        candidates.append((known_count, text_count, -index, index, headers))
    _, _, _, row_index, headers = max(candidates)
    return row_index, headers


def load_orders(file_path: str, profile: dict[str, Any] | None = None) -> tuple[list[dict[str, str]], dict[str, int]]:
    path = Path(file_path)
    rows = _read_rows(path)
    if not rows:
        raise ValueError("파일에 데이터가 없습니다.")
    header_row, columns, format_name = _find_header(rows, profile)
    # 셀메이트 파일은 마지막 표준 열 뒤의 제목 없는 열에 작업자가 품목을 수기로
    # 추가하는 경우가 있다. 인식한 가장 오른쪽 열 이후를 모두 보조 품목 영역으로 본다.
    extra_start = max(columns.values()) + 1
    orders: list[dict[str, str]] = []
    for source_row, row in enumerate(rows[header_row + 1 :], start=header_row + 2):
        def get(key: str) -> str:
            index = columns.get(key)
            return _clean(row[index]) if index is not None and index < len(row) else ""

        order_number = get("order_number")
        product_name = get("product_name")
        if not order_number and not product_name:
            continue
        options = " / ".join(filter(None, [get("option1"), get("option2"), get("option3")]))
        address = " ".join(filter(None, [get("address1"), get("address2")]))
        manual_items = []
        if "match1" in columns or "match2" in columns:
            manual_items = [
                _clean(row[index]) for index in range(extra_start, len(row))
                if _clean(row[index])
            ]
        matched_name = " / ".join(filter(None, [get("match1"), get("match2"), *manual_items]))
        channel = get("channel")
        if format_name == "판매처 직접파일 · 이알아이":
            channel = "이알아이"
        elif format_name == "판매처 직접파일 · 쌤몰":
            channel = "쌤몰"
        elif format_name == "판매처 직접파일 · 현대홈쇼핑":
            channel = "현대홈쇼핑"
        model = get("model")
        if not model and format_name.startswith("판매처 직접파일"):
            # 판매처 파일에 모델 열이 없을 때 상품명 안의 영문+숫자 모델을 사용한다.
            matches = re.findall(r"\b[A-Z]{1,6}[-_]?[A-Z0-9]{2,}\b", product_name.upper())
            model = matches[-1] if matches else ""
        orders.append(
            {
                "source_row": str(source_row),
                "order_number": order_number,
                "serial_number": get("serial_number"),
                "channel": channel,
                "source_format": format_name,
                "product_name": product_name,
                "source_item_code": get("item_code"),
                "options": options,
                "model": model,
                "quantity": get("quantity"),
                "recipient": get("recipient"),
                "phone": get("phone"),
                "zipcode": get("zipcode"),
                "address": address,
                "message": get("message"),
                "matched_name": matched_name,
                "manual_items": " / ".join(manual_items),
                "manual_input_detected": bool(manual_items),
            }
        )
    return orders, columns
