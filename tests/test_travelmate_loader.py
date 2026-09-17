import unittest
from unittest.mock import patch

from duty_free_loader import load_duty_free


class TravelmateLoaderTests(unittest.TestCase):
    def test_purchase_order_uses_order_quantity_and_store_destination(self):
        rows = [
            ["발 주 일 : 26.07.23", None, None, None],
            ["NO", "TM 상품코드", "TM 상품명", "발주수량", "매장명"],
            [1, "TM-001", "[리큐엠] 테스트 상품", 10, "1청사"],
            [2, "TM-002", "[리큐엠] 미발주 상품", None, "1청사"],
            ["＊ 인천공항 1청사", None, "인천시 중구 공항로 271, 인천공항 1터미널 3층 트래블메이트 김성겸매니저", "010-6500-3014"],
        ]
        with patch("duty_free_loader.openpyxl.load_workbook") as load_workbook:
            sheet = load_workbook.return_value.active
            sheet.iter_rows.return_value = iter([tuple(row) for row in rows])
            result = load_duty_free("sample.xlsx")

        self.assertIsNotNone(result)
        orders, detected_type = result
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["quantity"], "10")
        self.assertEqual(orders[0]["source_item_code"], "TM-001")
        self.assertEqual(orders[0]["matched_name"], "테스트 상품")
        self.assertEqual(orders[0]["recipient"], "김성겸매니저")
        self.assertIn("공항로 271", orders[0]["address"])
        self.assertEqual(orders[0]["phone"], "010-6500-3014")
        self.assertTrue(orders[0]["embedded_destination"])
        self.assertIn("트래블메이트", detected_type)


if __name__ == "__main__":
    unittest.main()
