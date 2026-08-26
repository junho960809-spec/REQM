from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from ecount_sales_api import (
    EcountSalesClient,
    build_sales_payload,
    parse_sales_result,
    payload_total,
    request_key,
)
from ecount_sales_core import VoucherLine


class EcountSalesApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lines = [
            VoucherLine("AC008712", "", "HQ", "본사품목", Decimal("2"), Decimal("11000"), "100"),
            VoucherLine("AC008712", "", "WK", "위킵품목", Decimal("3"), Decimal("8800"), "300"),
        ]

    def test_payload_separates_warehouses_into_slips(self) -> None:
        payload = build_sales_payload(self.lines, date(2026, 8, 26), "00109")
        rows = [row["BulkDatas"] for row in payload["SaleList"]]
        self.assertEqual({row["WH_CD"] for row in rows}, {"100", "300"})
        self.assertEqual(len({row["UPLOAD_SER_NO"] for row in rows}), 2)
        self.assertEqual(rows[0]["IO_DATE"], "20260826")
        self.assertEqual({row["EMP_CD"] for row in rows}, {"00109"})

    def test_payload_amount_matches_voucher_total_exactly(self) -> None:
        payload = build_sales_payload(self.lines, date(2026, 8, 26), "00109")
        self.assertEqual(payload_total(payload), Decimal("48400"))
        for row in payload["SaleList"]:
            data = row["BulkDatas"]
            self.assertEqual(
                Decimal(data["SUPPLY_AMT"]) + Decimal(data["VAT_AMT"]),
                Decimal(data["QTY"]) * Decimal(data["PRICE"]),
            )

    def test_custom_endpoint_list_key_and_encoded_session(self) -> None:
        client = EcountSalesClient("304293", "USER", "secret", "AB", endpoint="Sale/SaveSale")
        response = {"Status": "200", "Data": {"SuccessCnt": 2, "FailCnt": 0, "SlipNos": ["1", "2"]}}
        with patch.object(client, "login", return_value="session+value/="):
            with patch.object(client, "_post_json", return_value=response) as post:
                result = client.save_sales({"CustomSaleList": []})
        self.assertIn("/OAPI/V2/Sale/SaveSale?SESSION_ID=session%2Bvalue%2F%3D", post.call_args.args[0])
        self.assertEqual(result["slip_numbers"], ["1", "2"])

    def test_request_key_changes_when_environment_changes(self) -> None:
        payload = build_sales_payload(self.lines, date(2026, 8, 26), "00109")
        self.assertNotEqual(
            request_key(payload, "Sale/SaveSale", False),
            request_key(payload, "Sale/SaveSale", True),
        )

    def test_parse_success_response(self) -> None:
        result = parse_sales_result({
            "Status": "200",
            "Data": {"SuccessCnt": 2, "FailCnt": 0, "SlipNos": ["20260826-1", "20260826-2"]},
        })
        self.assertEqual(result["success_count"], 2)


if __name__ == "__main__":
    unittest.main()
