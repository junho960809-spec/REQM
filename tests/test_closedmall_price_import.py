import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from closedmall_price_import import canonical_channel, read_closedmall_price_summary


class ClosedMallPriceImportTests(unittest.TestCase):
    def test_channel_aliases_are_canonicalized(self):
        self.assertEqual(canonical_channel("주식회사 무신사"), "무신사")
        self.assertEqual(canonical_channel("삼성카드 주식회사 복지몰"), "삼성카드 복지몰")

    def test_summary_marks_integer_price_and_excludes_shipping(self):
        wb = Workbook()
        ws = wb.active
        ws.append(["기간"])
        ws.append(["거래처별", "품목별", "거래처코드", "수량", "공급가액", "부가세", "합계"])
        ws.append(["주식회사 무신사", "QP2000C_블랙", "100", 2, 60000, 6000, 66000])
        ws.append(["주식회사 무신사", "택배운송비", "100", 2, 5455, 545, 6000])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "summary.xlsx"
            wb.save(path)
            rows = read_closedmall_price_summary(path)
        self.assertEqual(rows[0].unit_price, 33000)
        self.assertEqual(rows[0].status, "매칭 필요")
        self.assertEqual(rows[1].status, "배송비 제외")


if __name__ == "__main__":
    unittest.main()
