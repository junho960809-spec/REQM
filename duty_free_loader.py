from pathlib import Path
import re
from typing import Any

import openpyxl


def clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def norm(value: Any) -> str:
    return "".join(clean(value).lower().split()).replace(".", "")


COLOR_WORDS = {
    "블랙", "화이트", "핑크", "그레이", "그린", "블루", "레드", "옐로우", "오렌지",
    "퍼플", "베이지", "민트", "라벤더", "라밴더", "버터", "캐롯", "캐럿", "샌드",
    "코발트블루", "세이지민트", "올리브", "브라운", "네이비",
}


def _model_tokens(value: Any) -> set[str]:
    text = clean(value).lower()
    return {
        re.sub(r"[^a-z0-9]", "", token)
        for token in re.findall(r"[a-z]{1,8}[-_ ]?\d+[a-z0-9]*", text)
        if len(re.sub(r"[^a-z0-9]", "", token)) >= 4
    }


def _colors(value: Any) -> set[str]:
    text = norm(value).replace("블루투스", "")
    aliases = {"라밴더": "라벤더", "캐럿": "캐롯"}
    return {aliases.get(color, color) for color in COLOR_WORDS if color in text}


def barcode_name_error(product_name: str, item: dict) -> str:
    """Return a reason when a barcode's DB item clearly conflicts with the source product."""
    db_text = " ".join(
        clean(item.get(key, ""))
        for key in ("standard_name", "model", "color", "form")
    )
    source_norm, db_norm = norm(product_name), norm(db_text)

    source_models = _model_tokens(product_name)
    db_models = _model_tokens(db_text)
    model_overlap = any(
        source.startswith(db) or db.startswith(source)
        for source in source_models for db in db_models
    )
    if source_models and db_models and not model_overlap:
        return f"상품 모델 불일치: 파일 {', '.join(sorted(source_models))} / DB {', '.join(sorted(db_models))}"

    source_colors = _colors(product_name)
    db_colors = _colors(db_text)
    if source_colors and db_colors and source_colors.isdisjoint(db_colors):
        return f"상품 색상 불일치: 파일 {', '.join(sorted(source_colors))} / DB {', '.join(sorted(db_colors))}"

    source_is_set = "세트" in source_norm or "+" in clean(product_name)
    db_is_set = "세트" in db_norm or str(item.get("item_code", "")).upper().startswith("SET-")
    if source_is_set and not db_is_set:
        return "파일은 세트 상품이지만 바코드는 DB 단품에 연결됨"
    return ""


def load_duty_free(file_path: str) -> tuple[list[dict[str, str]], str] | None:
    path = Path(file_path)
    if path.suffix.lower() != ".xlsx":
        return None
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = book.active
    rows = [list(row) for row in sheet.iter_rows(values_only=True)]
    file_text = " ".join(clean(value) for row in rows[:8] for value in row if value)

    for header_index, row in enumerate(rows[:15]):
        headers = {norm(value): index for index, value in enumerate(row) if value is not None}
        city_barcode = headers.get("바코드")
        city_product = headers.get("상품명")
        city_qty = headers.get("수량")
        if city_barcode is not None and city_product is not None and city_qty is not None and "box" in headers:
            store_code = "403" if "403" in file_text or "403" in path.name else "606" if "606" in file_text or "606" in path.name else ""
            store_name = f"T2 {store_code}매장" if store_code else "시티면세점 매장"
            result = []
            for source_row, data in enumerate(rows[header_index + 1 :], start=header_index + 2):
                barcode = clean(data[city_barcode]) if city_barcode < len(data) else ""
                product = clean(data[city_product]) if city_product < len(data) else ""
                if not barcode and not product:
                    continue
                get = lambda name: clean(data[headers[name]]) if name in headers and headers[name] < len(data) else ""
                result.append({
                    "source_row": str(source_row), "order_number": store_name, "channel": "시티면세점",
                    "product_name": product, "options": "", "quantity": clean(data[city_qty]),
                    "recipient": get("수령인"), "phone": get("연락처"), "zipcode": "",
                    "address": get("주소"), "message": f"BOX {get('box')}" if get("box") else "",
                    "matched_name": barcode, "barcode": barcode, "store_code": store_code,
                })
            return result, f"시티면세점 {store_name}"

        ref_col = next((index for key, index in headers.items() if key in {"refno", "바코드"}), None)
        product_col = headers.get("상품명")
        qty_col = headers.get("수량")
        code_col = headers.get("skuno") if "skuno" in headers else headers.get("상품코드")
        if ref_col is not None and product_col is not None and qty_col is not None:
            source_text = file_text + " " + path.name
            duty_name = (
                "롯데면세점" if "롯데" in source_text else
                "현대면세점" if "현대" in source_text else
                "신라면세점" if "신라" in source_text else
                "신세계면세점" if "신세계" in source_text else
                "시티면세점" if "시티" in source_text or "넥서스코프" in source_text else
                "면세점(종류 확인 필요)"
            )
            result = []
            for source_row, data in enumerate(rows[header_index + 1 :], start=header_index + 2):
                barcode = clean(data[ref_col]) if ref_col < len(data) else ""
                product = clean(data[product_col]) if product_col < len(data) else ""
                if not barcode and not product:
                    continue
                external_code = clean(data[code_col]) if code_col is not None and code_col < len(data) else ""
                result.append({
                    "source_row": str(source_row), "order_number": external_code, "channel": duty_name,
                    "product_name": product, "options": "", "quantity": clean(data[qty_col]),
                    "recipient": "", "phone": "", "zipcode": "", "address": "", "message": "",
                    "matched_name": barcode, "barcode": barcode, "external_code": external_code,
                })
            return result, duty_name
    return None


def match_barcodes(rows: list[dict[str, str]], barcodes: list[dict], items: list[dict]) -> None:
    barcode_map = {str(row.get("barcode", "")): str(row.get("item_code", "")) for row in barcodes if row.get("is_active", True)}
    item_map = {str(row.get("item_code", "")): row for row in items}
    for row in rows:
        item_code = barcode_map.get(row.get("barcode", ""), "")
        item = item_map.get(item_code)
        if item:
            mismatch = barcode_name_error(row.get("product_name", ""), item)
            if mismatch:
                row.update({
                    "status": "barcode_error", "matched_product": str(item.get("standard_name", "")),
                    "components": item_code, "reason": "바코드-상품 불일치 · " + mismatch,
                })
            else:
                row.update({
                    "status": "exact", "matched_product": str(item.get("standard_name", "")),
                    "components": item_code, "reason": "면세점 바코드 정확 일치",
                })
        else:
            row.update({
                "status": "barcode_error", "matched_product": "", "components": "",
                "reason": "바코드 오류 · DB에 등록되지 않은 바코드",
            })
