from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime
from decimal import Decimal

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QAbstractItemView, QTabWidget

from pathlib import Path

from ecount_sales_app import (
    ItemOrderDetailsDialog, MarketplaceSettingsDialog, SalesLoginDialog, SalesVoucherWindow,
    clear_saved_login, load_saved_login, save_login, show_authenticated_window,
    split_voucher_line_total,
)
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

    def test_saved_login_is_encrypted_and_can_be_removed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "login.json"
            save_login("worker@example.com", "secret-password", path)
            self.assertNotIn("secret-password", path.read_text(encoding="utf-8"))
            self.assertEqual(load_saved_login(path), ("worker@example.com", "secret-password"))
            clear_saved_login(path)
            self.assertFalse(path.exists())

    def test_login_dialog_precedes_work_screen_and_supports_remember(self) -> None:
        dialog = SalesLoginDialog(auto_login=False)
        self.assertEqual(dialog.windowTitle(), "REQM 판매전표 로그인")
        self.assertEqual(dialog.remember.text(), "로그인 정보 저장")
        dialog.close()

    def test_authenticated_transition_shows_main_window_before_catalog_apply(self) -> None:
        events = []

        class FakeApp:
            def setQuitOnLastWindowClosed(self, enabled):
                events.append(("quit_on_close", enabled))

        class FakeWindow:
            def show(self): events.append("show")
            def showNormal(self): events.append("show_normal")
            def raise_(self): events.append("raise")
            def activateWindow(self): events.append("activate")

        class FakeLogin:
            client = object()
            catalog = object()

        with patch("ecount_sales_app.QTimer.singleShot") as single_shot:
            show_authenticated_window(FakeApp(), FakeWindow(), FakeLogin())

        self.assertEqual(events[:2], ["show", "show_normal"])
        self.assertIn(("quit_on_close", True), events)
        single_shot.assert_called_once()

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

    def test_result_can_be_filtered_by_sales_channel(self) -> None:
        result = ConversionResult(orders=[], lines=[
            VoucherLine("A", "", "ITEM-A", "품목A", Decimal("1"), Decimal("100"), "300", source_channel="오늘의집"),
            VoucherLine("B", "", "ITEM-B", "품목B", Decimal("1"), Decimal("200"), "300", source_channel="11번가"),
        ], issues=[])
        window = SalesVoucherWindow()
        window.current_result = result
        window._show_result(result)
        window.result_channel_filter.setCurrentText("오늘의집")
        visibility = {
            window.lines_table.item(row, 2).text(): not window.lines_table.isRowHidden(row)
            for row in range(window.lines_table.rowCount())
        }
        self.assertTrue(visibility["오늘의집"])
        self.assertFalse(visibility["11번가"])
        window.close()

    def test_main_screen_uses_compact_workflow_navigation(self) -> None:
        window = SalesVoucherWindow()
        self.assertEqual([window.input_type.itemText(index) for index in range(window.input_type.count())],
                         ["스마트스토어", "폐쇄몰·외부 판매처", "ESM 옥션·지마켓"])
        self.assertFalse(window.main_tabs.isTabVisible(window.pivot_tab_index))
        self.assertFalse(window.main_tabs.isTabVisible(window.db_tab_index))
        self.assertEqual(window.main_tabs.tabText(window.result_tab_index), "전표 결과")
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

    def test_marketplace_settings_groups_configuration_tabs(self) -> None:
        window = SalesVoucherWindow()
        dialog = MarketplaceSettingsDialog(window)
        tabs = dialog.findChild(QTabWidget)
        self.assertEqual([tabs.tabText(index) for index in range(tabs.count())],
                         ["배송비 규칙", "로그인·수집", "이카운트 연결", "판매처 기본정보"])
        self.assertEqual(dialog.SOURCE_LABELS["custom10"], "셀메이트 기준")
        self.assertEqual(dialog.rule_table.selectionMode(), QAbstractItemView.ExtendedSelection)
        self.assertEqual(dialog.channel_table.selectionMode(), QAbstractItemView.ExtendedSelection)
        dialog.close()
        window.close()


if __name__ == "__main__":
    unittest.main()
