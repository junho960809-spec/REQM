from __future__ import annotations

import os
import unittest
from datetime import datetime
from decimal import Decimal

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ecount_sales_app import ItemOrderDetailsDialog, SalesVoucherWindow, split_voucher_line_total
from ecount_sales_api_dialog import EcountSalesApiDialog
from ecount_sales_core import ConversionResult, ItemOrderDetail, SmartStoreOrder, VoucherLine


class SalesAppEditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_exact_total_split_keeps_requested_total(self) -> None:
        original = VoucherLine(
            "CUST", "", "ITEM", "품목", Decimal("3"), Decimal("100"), "300",
            source_orders=["ORDER-1"],
        )

        lines = split_voucher_line_total(original, Decimal("3"), Decimal("1000"), "100")

        self.assertEqual(sum((line.total for line in lines), Decimal("0")), Decimal("1000"))
        self.assertEqual({line.warehouse for line in lines}, {"100"})
        self.assertEqual(
            sorted((line.quantity, line.unit_price) for line in lines),
            [(Decimal("1"), Decimal("334")), (Decimal("2"), Decimal("333"))],
        )

    def test_order_detail_dialog_applies_amount_and_warehouse_by_buyer(self) -> None:
        detail = ItemOrderDetail(
            order_no="ORDER-1",
            product_order_no="PRODUCT-1",
            purchaser_name="홍길동",
            product_name="QP1000C",
            options="블랙",
            order_quantity=Decimal("1"),
            converted_quantity=Decimal("1"),
            unit_price=Decimal("26900"),
            total=Decimal("26900"),
            warehouse="300",
            source_type="셀메이트",
        )
        dialog = ItemOrderDetailsDialog("ITEM", "품목", [detail])
        self.assertEqual(dialog.table.item(0, 1).text(), "홍길동")
        dialog.table.item(0, 6).setText("25,900")
        dialog.table.item(0, 7).setText("100")

        dialog.accept()

        self.assertEqual(detail.total, Decimal("25900"))
        self.assertEqual(detail.warehouse, "100")

    def test_main_table_amount_and_warehouse_edit_updates_reconciliation(self) -> None:
        order = SmartStoreOrder(
            2, "ORDER-1", "PRODUCT-1", datetime(2026, 9, 9), "", "상품", "",
            Decimal("2"), Decimal("200"),
        )
        result = ConversionResult(
            orders=[order],
            lines=[VoucherLine(
                "CUST", "", "ITEM", "품목", Decimal("2"), Decimal("100"), "300",
                source_orders=["ORDER-1"],
            )],
            issues=[],
        )
        window = SalesVoucherWindow()
        window.current_result = result
        window._show_result(result)
        window.lines_table.item(0, 5).setText("190")
        window.lines_table.item(0, 6).setText("100")

        window.apply_main_table_edits()

        self.assertEqual(window.current_result.output_total, Decimal("190"))
        self.assertEqual(window.current_result.expected_output_total, Decimal("190"))
        self.assertEqual(window.current_result.amount_difference, Decimal("0"))
        self.assertEqual({line.warehouse for line in window.current_result.lines}, {"100"})
        window.close()

    def test_api_preview_shows_source_channel(self) -> None:
        line = VoucherLine(
            "CUST", "", "ITEM", "품목", Decimal("1"), Decimal("1000"), "300",
            source_orders=["ORDER-1"], source_channel="오늘의집",
        )
        dialog = EcountSalesApiDialog(
            [line], datetime(2026, 9, 9).date(), Decimal("1000"), "00109"
        )

        self.assertEqual(dialog.table.horizontalHeaderItem(2).text(), "판매처명")
        self.assertEqual(dialog.table.item(0, 2).text(), "오늘의집")
        self.assertEqual(dialog.table.horizontalHeaderItem(5).text(), "품목명")
        self.assertEqual(dialog.table.item(0, 5).text(), "품목")
        dialog.close()


if __name__ == "__main__":
    unittest.main()
