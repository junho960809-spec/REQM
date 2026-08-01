import unittest
from unittest.mock import patch

from ecount_client import EcountClient, EcountError, build_location_transfer_payload, collect_transfer_items, parse_transfer_result


class EcountTransferTests(unittest.TestCase):
    def test_collects_selected_channel_and_multiplies_set_quantity(self):
        orders = [
            {"channel": "스마트스토어", "status": "exact", "quantity": "3", "components": "A×2 + B×1"},
            {"channel": "스마트스토어", "status": "manual", "quantity": "1", "components": "A"},
            {"channel": "스마트스토어", "status": "similar", "quantity": "9", "components": "C"},
            {"channel": "쿠팡", "status": "exact", "quantity": "5", "components": "A"},
        ]
        rows, summary = collect_transfer_items(orders, "스마트스토어", [{"item_code": "A", "standard_name": "에이"}])
        self.assertEqual(rows, [
            {"item_code": "A", "item_name": "에이", "quantity": "7"},
            {"item_code": "B", "item_name": "", "quantity": "3"},
        ])
        self.assertEqual(summary, {"selected": 3, "included": 2, "excluded": 1})

    def test_collects_all_channels_when_channel_is_empty(self):
        orders = [
            {"channel": "스마트스토어", "status": "exact", "quantity": "2", "components": "A"},
            {"channel": "쿠팡", "status": "exact", "quantity": "3", "components": "A"},
        ]
        rows, summary = collect_transfer_items(orders, "")
        self.assertEqual(rows[0]["quantity"], "5")
        self.assertEqual(summary["selected"], 2)

    def test_payload_groups_all_items_in_one_upload_serial(self):
        payload = build_location_transfer_payload(
            "20260801", "00210", "100", "300",
            [{"item_code": "A", "quantity": "2"}, {"item_code": "B", "quantity": "3"}], "테스트",
        )
        rows = [row["BulkDatas"] for row in payload["LocationTranList"]]
        self.assertEqual({row["UPLOAD_SER_NO"] for row in rows}, {"1"})
        self.assertEqual([row["PROD_CD"] for row in rows], ["A", "B"])
        self.assertEqual(rows[0]["WH_CD_F"], "100")
        self.assertEqual(rows[0]["WH_CD_T"], "300")

    def test_parses_string_result_details(self):
        result = parse_transfer_result({
            "Status": "200",
            "Data": {"SuccessCnt": 1, "FailCnt": 0, "ResultDetails": '[{"IsSuccess":true}]', "SlipNos": ["20260801-1"]},
        })
        self.assertEqual(result["slip_numbers"], ["20260801-1"])

    def test_encodes_session_id_in_transfer_url(self):
        client = EcountClient("304293", "JUNHO191", "secret", "AB")
        response = {"Status": "200", "Data": {"SuccessCnt": 1, "FailCnt": 0, "SlipNos": ["1"]}}
        with patch.object(client, "login", return_value="session+value/="):
            with patch.object(client, "_post_json", return_value=response) as post:
                client.save_location_transfer({"LocationTranList": []})
        self.assertIn("SESSION_ID=session%2Bvalue%2F%3D", post.call_args.args[0])

    def test_uses_sboapi_for_test_key(self):
        client = EcountClient("304293", "JUNHO191", "test-secret", "AB", test_mode=True)
        response = {"Status": "200", "Data": {"SuccessCnt": 1, "FailCnt": 0, "SlipNos": ["1"]}}
        with patch.object(client, "login", return_value="session"):
            with patch.object(client, "_post_json", return_value=response) as post:
                client.save_location_transfer({"LocationTranList": []})
        self.assertTrue(post.call_args.args[0].startswith("https://sboapiAB.ecount.com/"))

    def test_uses_oapi_for_production_key(self):
        client = EcountClient("304293", "JUNHO191", "live-secret", "AB", test_mode=False)
        self.assertEqual(client.api_host_prefix, "oapi")

    def test_explains_unauthorized_transfer_permission(self):
        with self.assertRaises(EcountError) as caught:
            parse_transfer_result({
                "Status": "500", "Error": {"Code": 0, "Message": "인증되지 않은 API입니다."}
            })
        self.assertIn("현재 사용자 ID에서 발급된 키", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
