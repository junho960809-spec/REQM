from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from openpyxl import load_workbook

from ecount_sales_core import (
    ReferenceCatalog,
    SmartStoreOrder,
    convert_orders,
    write_ecount_workbook,
)


class SalesCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = ReferenceCatalog(
            items=[
                {"item_code": "MAIN", "representative_name": "본품"},
                {"item_code": "OPTION", "representative_name": "옵션품목"},
            ],
            channels=[
                {"source_name": "리큐엠_스마트스토어", "ecount_customer_code": "AC008712", "ecount_customer_name": "샵N", "is_active": True}
            ],
            mappings=[
                {"mapping_key": "set-1", "source_channel": "리큐엠_스마트스토어", "normalized_source": "상품세트옵션핑크", "mapping_type": "set", "review_status": "confirmed", "is_active": True}
            ],
            mapping_components=[
                {"mapping_key": "set-1", "sequence": 1, "item_code": "MAIN", "quantity": 1},
                {"mapping_key": "set-1", "sequence": 2, "item_code": "OPTION", "quantity": 1},
            ],
            price_rules=[
                {"price_rule_key": "price-1", "source_channel": "리큐엠_스마트스토어", "source_product_name": "상품 세트", "source_options": "옵션 핑크", "normalized_source": "상품세트옵션핑크", "total_unit_price": 35400, "review_status": "confirmed", "is_active": True}
            ],
            price_components=[
                {"price_rule_key": "price-1", "sequence": 1, "item_code": "MAIN", "quantity": 1, "allocated_unit_price": 28500},
                {"price_rule_key": "price-1", "sequence": 2, "item_code": "OPTION", "quantity": 1, "allocated_unit_price": 6900},
            ],
        )

    def test_set_discount_is_absorbed_by_main_product(self) -> None:
        order = SmartStoreOrder(
            source_row=2,
            order_no="ORDER-1",
            product_order_no="PRODUCT-1",
            paid_at=datetime(2026, 7, 21, 10, 0),
            status="구매확정",
            product_name="상품 세트",
            options="옵션 핑크",
            quantity=Decimal("1"),
            item_total=Decimal("34900"),
        )
        result = convert_orders([order], self.catalog)
        by_code = {line.item_code: line for line in result.lines}
        self.assertEqual(by_code["MAIN"].unit_price, Decimal("28000.00"))
        self.assertEqual(by_code["OPTION"].unit_price, Decimal("6900.00"))
        self.assertEqual(result.input_total, result.output_total)
        self.assertEqual(result.issues, [])

    def test_export_uses_voucher_date_and_tax_formulas(self) -> None:
        order = SmartStoreOrder(
            source_row=2,
            order_no="ORDER-1",
            product_order_no="PRODUCT-1",
            paid_at=datetime(2026, 7, 21, 10, 0),
            status="구매확정",
            product_name="상품 세트",
            options="옵션 핑크",
            quantity=Decimal("1"),
            item_total=Decimal("34900"),
        )
        result = convert_orders([order], self.catalog)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "output.xlsx"
            write_ecount_workbook(output, result, date(2026, 7, 22))
            workbook = load_workbook(output, data_only=False)
            sheet = workbook["이카운트 웹입력"]
            self.assertEqual(sheet["A2"].value, 20260722)
            self.assertEqual(sheet["E2"].value, "00109")
            self.assertEqual(sheet["F2"].value, "300")
            self.assertEqual(sheet["S2"].value, "=Q2/1.1*P2")
            self.assertEqual(sheet["T2"].value, "=Q2*P2-S2")
            workbook.close()


if __name__ == "__main__":
    unittest.main()
