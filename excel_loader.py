import csv
from pathlib import Path
from typing import Any

import openpyxl
import xlrd


COLUMN_ALIASES = {
    "channel": ["판매처명", "판매처", "쇼핑몰명"],
    "order_number": ["판매처주문번호", "주문번호", "쇼핑몰주문번호"],
    "serial_number": ["일련번호"],
    "recipient": ["수령인", "수취인", "받는분", "수령자"],
    "zipcode": ["수령자우편번호", "우편번호"],
    "address1": ["수령자주소", "주소"],
    "address2": ["수령자주소2", "상세주소"],
    "message": ["상세요구사항", "배송메세지", "배송메시지"],
    "phone": ["수령자휴대폰", "핸드폰", "휴대폰", "연락처"],
    "product_name": ["판매처상품명", "상품명", "품목명"],
    "option1": ["상품옵션", "옵션"],
    "option2": ["상품옵션2", "옵션2"],
    "option3": ["상품옵션3", "옵션3"],
    "quantity": ["주문수량", "수량"],
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
    raise ValueError("지원 형식은 .xls, .xlsx, .csv입니다.")


def _normalize_header(value: Any) -> str:
    return "".join(_clean(value).lower().split())


def _find_header(rows: list[list[Any]]) -> tuple[int, dict[str, int]]:
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
    return best_row, best_map


def load_orders(file_path: str) -> tuple[list[dict[str, str]], dict[str, int]]:
    path = Path(file_path)
    rows = _read_rows(path)
    if not rows:
        raise ValueError("파일에 데이터가 없습니다.")
    header_row, columns = _find_header(rows)
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
        manual_items = [
            _clean(row[index]) for index in range(extra_start, len(row))
            if _clean(row[index])
        ]
        matched_name = " / ".join(filter(None, [get("match1"), get("match2"), *manual_items]))
        orders.append(
            {
                "source_row": str(source_row),
                "order_number": order_number,
                "serial_number": get("serial_number"),
                "channel": get("channel"),
                "product_name": product_name,
                "options": options,
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
