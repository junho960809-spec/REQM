from __future__ import annotations

import unittest

from ecount_transfer import EcountClient, parse_location_transfer_result


class EcountTransferResultTests(unittest.TestCase):
    def test_direct_execution_compatible_host_and_encoded_session(self):
        client = EcountClient("123456", "tester", "secret", "AB", "sboapi")
        self.assertEqual(client._base_url(), "https://sboapiAB.ecount.com/OAPI/V2")

    def test_reads_standard_success_response(self):
        parsed = parse_location_transfer_result({
            "Status": "200",
            "Data": {
                "SuccessCnt": 2,
                "FailCnt": 0,
                "SlipNos": ["20260723-1"],
                "ResultDetails": [],
            },
        })
        self.assertEqual(parsed["success"], 2)
        self.assertEqual(parsed["fail"], 0)
        self.assertEqual(parsed["slips"], ["20260723-1"])

    def test_reads_nested_result_and_error(self):
        parsed = parse_location_transfer_result({
            "Status": 200,
            "Data": {
                "Result": {
                    "SuccessCnt": 0,
                    "FailCnt": 1,
                    "ResultDetails": [{"TotalError": "등록되지 않은 품목코드입니다."}],
                }
            },
        })
        self.assertEqual(parsed["fail"], 1)
        self.assertIn("등록되지 않은 품목코드입니다.", parsed["details"])

    def test_surfaces_top_level_ecount_error(self):
        parsed = parse_location_transfer_result({
            "Status": "500",
            "Error": {"Message": "API 입력 권한이 없습니다."},
        })
        self.assertFalse(parsed["recognized"])
        self.assertIn("API 입력 권한이 없습니다.", parsed["details"])


if __name__ == "__main__":
    unittest.main()
