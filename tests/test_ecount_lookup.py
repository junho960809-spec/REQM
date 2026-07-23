import unittest

from main import EcountTransferDialog


class EcountLookupTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
