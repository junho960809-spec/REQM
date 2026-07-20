import re
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any


def normalize(value: str) -> str:
    value = (value or "").lower().replace("mah", "mah")
    value = re.sub(r"[\[\](){}:,_/\\+\-]", " ", value)
    return " ".join(value.split())


def compact(value: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", normalize(value))


class ProductMatcher:
    def __init__(self, items: list[dict[str, Any]], products: list[dict[str, Any]], components: list[dict[str, Any]], aliases: list[dict[str, Any]] | None = None):
        self.items = [row for row in items if row.get("is_active", True)]
        self.products = [row for row in products if row.get("is_active", True)]
        self.components_by_product: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in components:
            self.components_by_product[str(row.get("registered_product_id", ""))].append(row)
        self.items_by_exact: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in self.items:
            self.items_by_exact[compact(str(item.get("standard_name", "")))].append(item)
        self.products_by_exact: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for product in self.products:
            key = compact(str(product.get("normalized_name") or product.get("original_name", "")))
            self.products_by_exact[key].append(product)
        self.aliases = {
            (str(row.get("source_channel", "")), str(row.get("normalized_source", ""))): row
            for row in (aliases or []) if row.get("is_active", True)
        }

    @staticmethod
    def _components_text(rows: list[dict[str, Any]]) -> str:
        return " + ".join(
            f"{row.get('item_code', '')}×{row.get('quantity', 1)}" for row in sorted(rows, key=lambda x: int(x.get("sequence", 0)))
        )

    def _match_item_part(self, part: str) -> tuple[str, list[dict[str, Any]], str]:
        key = compact(part)
        exact = self.items_by_exact.get(key, [])
        if len(exact) == 1:
            return "exact", exact, "품목명 정확 일치"
        if len(exact) > 1:
            return "ambiguous", exact, "동일 품목명이 여러 코드에 존재"

        source = normalize(part)
        source_tokens = set(source.split())
        candidates: list[tuple[float, dict[str, Any]]] = []
        for item in self.items:
            name = normalize(str(item.get("standard_name", "")))
            model = normalize(str(item.get("model", "")))
            if model and model not in source and compact(model) not in key:
                continue
            score = SequenceMatcher(None, compact(source), compact(name)).ratio()
            name_tokens = set(name.split())
            if source_tokens and name_tokens:
                score = max(score, len(source_tokens & name_tokens) / len(source_tokens | name_tokens))
            if score >= 0.58:
                candidates.append((score, item))
        candidates.sort(key=lambda x: x[0], reverse=True)
        if not candidates:
            return "missing", [], "DB에서 품목을 찾지 못함"
        top_score = candidates[0][0]
        close = [item for score, item in candidates if top_score - score <= 0.04]
        forms = {str(item.get("form", "")) for item in close if item.get("form")}
        if len(close) > 1 and ("기본형" in forms or "핸디형" in forms):
            return "ambiguous", close[:5], "기본형/핸디형 구분 필요"
        if len(close) > 1:
            return "ambiguous", close[:5], "유사 후보가 여러 개"
        return "similar", [candidates[0][1]], f"유사 품목 일치 {top_score:.0%}"

    def match(self, order: dict[str, str]) -> dict[str, str]:
        alias_key = compact(" ".join(filter(None, [order.get("product_name", ""), order.get("options", "")])))
        alias = self.aliases.get((order.get("channel", ""), alias_key)) or self.aliases.get(("", alias_key))
        if alias:
            components = alias.get("components") or []
            return {
                "status": "alias",
                "matched_product": " / ".join(str(row.get("standard_name", row.get("item_code", ""))) for row in components),
                "components": " + ".join(f"{row.get('item_code', '')}×{row.get('quantity', 1)}" for row in components),
                "reason": "저장된 사용자 별칭 적용",
            }
        matched_name = order.get("matched_name", "").strip()
        if matched_name:
            parts = [part.strip() for part in matched_name.split(" / ") if part.strip()]
            found: list[dict[str, Any]] = []
            levels: list[str] = []
            notes: list[str] = []
            for part in parts:
                level, candidates, note = self._match_item_part(part)
                levels.append(level)
                notes.append(f"{part}: {note}")
                if candidates:
                    found.append(candidates[0])
            if "missing" in levels:
                status = "missing"
            elif "ambiguous" in levels:
                status = "ambiguous"
            elif "similar" in levels:
                status = "similar"
            else:
                status = "exact"
            return {
                "status": status,
                "matched_product": matched_name,
                "components": " + ".join(str(item.get("item_code", "")) for item in found),
                "reason": " | ".join(notes),
            }

        source = " ".join(filter(None, [order.get("product_name", ""), order.get("options", "")]))
        key = compact(source)
        exact = self.products_by_exact.get(key, [])
        if len(exact) == 1:
            product = exact[0]
            components = self.components_by_product.get(str(product.get("registered_product_id", "")), [])
            return {
                "status": "exact",
                "matched_product": str(product.get("original_name", "")),
                "components": self._components_text(components),
                "reason": "등록상품명 정확 일치",
            }

        scored: list[tuple[float, dict[str, Any]]] = []
        for product in self.products:
            target = str(product.get("normalized_name") or product.get("original_name", ""))
            score = SequenceMatcher(None, key, compact(target)).ratio()
            if score >= 0.68:
                scored.append((score, product))
        scored.sort(key=lambda x: x[0], reverse=True)
        if not scored:
            return {"status": "missing", "matched_product": "", "components": "", "reason": "등록상품 DB에 없음"}
        top_score = scored[0][0]
        close = [product for score, product in scored if top_score - score <= 0.025]
        if len(close) > 1:
            return {
                "status": "ambiguous",
                "matched_product": " / ".join(str(p.get("original_name", "")) for p in close[:3]),
                "components": "",
                "reason": "유사 등록상품 후보가 여러 개",
            }
        product = scored[0][1]
        components = self.components_by_product.get(str(product.get("registered_product_id", "")), [])
        return {
            "status": "similar",
            "matched_product": str(product.get("original_name", "")),
            "components": self._components_text(components),
            "reason": f"유사 등록상품 일치 {top_score:.0%}",
        }
