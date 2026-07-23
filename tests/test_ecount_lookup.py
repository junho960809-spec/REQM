import unittest

from PySide6.QtWidgets import QApplication

from main import EcountTransferDialog


class EcountLookupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.employees = [
            {"employee_code": "E001", "employee_name": "홍 길동"},
            {"employee_code": "E002", "employee_name": "김철수"},
            {"employee_code": "E003", "employee_name": "김철수"},
        ]
        self.warehouses = [
            {"warehouse_code": "W001", "warehouse_name": "본사 창고"},
            {"warehouse_code": "W002", "warehouse_name": "제2창고"},
        ]

    def test_employee_name_resolves_ignoring_spaces(self):
        code = EcountTransferDialog._code_from_input(
            "홍길동", self.employees, "employee_code", "employee_name"
        )
        self.assertEqual(code, "E001")

    def test_warehouse_name_resolves_to_code(self):
        code = EcountTransferDialog._code_from_input(
            "본사창고", self.warehouses, "warehouse_code", "warehouse_name"
        )
        self.assertEqual(code, "W001")

    def test_code_is_case_insensitive(self):
        code = EcountTransferDialog._code_from_input(
            "w002", self.warehouses, "warehouse_code", "warehouse_name"
        )
        self.assertEqual(code, "W002")

    def test_duplicate_name_requires_selection(self):
        code = EcountTransferDialog._code_from_input(
            "김철수", self.employees, "employee_code", "employee_name"
        )
        self.assertEqual(code, "")

    def test_unknown_value_is_not_sent_as_code(self):
        code = EcountTransferDialog._code_from_input(
            "없는창고", self.warehouses, "warehouse_code", "warehouse_name"
        )
        self.assertEqual(code, "")

    def test_missing_code_is_resolved_from_item_name(self):
        dialog = EcountTransferDialog({
            "items": [
                {"item_code": "ITEM-01", "standard_name": "테스트 상품 블랙", "is_active": True},
            ],
            "products": [],
            "components": [],
            "employees": [],
            "warehouses": [],
            "app_role": "viewer",
        })
        dialog.ecount_items = [
            {"item_code": "ITEM-01", "standard_name": "테스트 상품 블랙", "is_active": True},
        ]
        rows = dialog.resolve_transfer_item_codes([
            {"item_code": "", "item_name": "테스트 상품 블랙", "quantity": 3},
        ])
        dialog.close()
        self.assertEqual(rows[0]["item_code"], "ITEM-01")
        self.assertEqual(rows[0]["quantity"], 3)

    def test_unknown_item_name_is_blocked(self):
        dialog = EcountTransferDialog({
            "items": [],
            "products": [],
            "components": [],
            "employees": [],
            "warehouses": [],
            "app_role": "viewer",
        })
        dialog.ecount_items = [
            {"item_code": "ITEM-01", "standard_name": "다른 품목", "is_active": True},
        ]
        with self.assertRaisesRegex(ValueError, "이카운트 코드를 확정하지 못했습니다"):
            dialog.resolve_transfer_item_codes([
                {"item_code": "", "item_name": "없는 품목", "quantity": 1},
            ])
        dialog.close()

    def test_qp1000c_black_resolves_to_ecount_qp1000c1_code(self):
        dialog = EcountTransferDialog({
            "items": [],
            "products": [],
            "components": [],
            "employees": [],
            "warehouses": [],
            "app_role": "viewer",
        })
        rows = dialog.resolve_transfer_item_codes([
            {
                "item_code": "",
                "item_name": "10000mAh 보조배터리 QP1000C - 블랙",
                "quantity": 2,
            },
        ])
        dialog.close()
        self.assertEqual(rows[0]["item_code"], "[RQM]-QP1000C1-BK")
        self.assertEqual(rows[0]["item_name"], "[리큐엠] 보조배터리 QP1000C1 블랙")


if __name__ == "__main__":
    unittest.main()
