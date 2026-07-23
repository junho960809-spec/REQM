import unittest
from unittest.mock import patch

from excel_loader import load_orders
from matcher import ProductMatcher


class OrderPdfImportTests(unittest.TestCase):
    def test_pdf_table_columns_are_loaded(self):
        rows = [
            ["주문번호", "수령인", "품목코드", "품목명", "수량", "주소", "연락처"],
            ["ORDER-1", "홍길동", "ITEM-01", "테스트 상품", "2", "서울시", "010-0000-0000"],
        ]
        with patch("excel_loader._read_rows", return_value=rows):
            orders, columns = load_orders("sample.pdf")

        self.assertEqual(columns["item_code"], 2)
        self.assertEqual(orders[0]["source_item_code"], "ITEM-01")
        self.assertEqual(orders[0]["product_name"], "테스트 상품")
        self.assertEqual(orders[0]["quantity"], "2")
        self.assertEqual(orders[0]["recipient"], "홍길동")

    def test_source_item_code_matches_db_before_name(self):
        matcher = ProductMatcher(
            [{"item_code": "ITEM-01", "standard_name": "DB 표준 상품", "is_active": True}],
            [],
            [],
        )
        result = matcher.match(
            {"source_item_code": "item-01", "product_name": "판매처의 다른 상품명", "options": ""}
        )

        self.assertEqual(result["status"], "exact")
        self.assertEqual(result["components"], "ITEM-01")
        self.assertEqual(result["matched_product"], "DB 표준 상품")


if __name__ == "__main__":
    unittest.main()
