from __future__ import annotations

import hashlib
import json
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QDateEdit,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from supabase import create_client

from ecount_sales_core import (
    ConversionResult,
    ReferenceCatalog,
    VoucherLine,
    convert_orders,
    read_smartstore_orders,
    write_ecount_workbook,
)


SOURCE_DIR = Path(__file__).resolve().parent
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else SOURCE_DIR
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", SOURCE_DIR))
LOCAL_DATA_DIR = BUNDLE_DIR / "supabase" / "ecount_migration" / "data"


def load_config() -> dict[str, str]:
    for config_path in (APP_DIR / "config.json", APP_DIR.parent / "config.json", SOURCE_DIR / "config.json"):
        if config_path.exists():
            return json.loads(config_path.read_text(encoding="utf-8"))
    return {}


def fetch_all(client, table_name: str) -> list[dict]:
    rows: list[dict] = []
    start = 0
    while True:
        page = client.table(table_name).select("*").range(start, start + 999).execute().data or []
        rows.extend(page)
        if len(page) < 1000:
            return rows
        start += 1000


class SalesVoucherWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("REQM 판매전표 반자동화 - 테스트")
        self.resize(1280, 760)
        self.catalog: ReferenceCatalog | None = None
        self.supabase_client = None
        self.current_result: ConversionResult | None = None
        self.file_path = QLineEdit()
        self.file_path.setPlaceholderText("스마트스토어에서 내려받은 원본 Excel을 선택하세요")
        self.email = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.db_status = QLabel("DB 준비 중")
        self.order_date = QDateEdit(QDate.currentDate().addDays(-1))
        self.voucher_date = QDateEdit(QDate.currentDate())
        self.order_date.setCalendarPopup(True)
        self.voucher_date.setCalendarPopup(True)
        self.order_date.setFixedWidth(112)
        self.voucher_date.setFixedWidth(112)
        self.manager_code = QLineEdit("00109")
        self.manager_code.setFixedWidth(self.manager_code.fontMetrics().horizontalAdvance("000000") + 24)
        self.default_warehouse = QSpinBox()
        self.default_warehouse.setRange(1, 9999)
        self.default_warehouse.setValue(300)
        self.default_warehouse.setFixedWidth(self.default_warehouse.fontMetrics().horizontalAdvance("00000") + 30)
        self.summary_orders = QLabel("0")
        self.summary_lines = QLabel("0")
        self.summary_issues = QLabel("0")
        self.summary_total = QLabel("0원")
        self.summary_shipping = QLabel("0원")
        self.summary_difference = QLabel("0원")
        self.lines_table = QTableWidget(0, 7)
        self.issues_table = QTableWidget(0, 6)
        self.shipping_table = QTableWidget(0, 7)
        self.export_button = QPushButton("이카운트 Excel 저장")
        self.export_button.setEnabled(False)
        self._build_ui()
        self._load_local_catalog()

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(14, 8, 14, 10)
        layout.setSpacing(7)

        title = QLabel("판매전표 반자동화")
        title.setObjectName("title")
        subtitle = QLabel("원본 주문을 자동 매칭하고, 예외만 확인한 뒤 이카운트 입력자료를 만듭니다.")
        subtitle.setObjectName("subtitle")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        connection = QGroupBox("Supabase 최신 DB 연결")
        connection_layout = QHBoxLayout(connection)
        connection_layout.setContentsMargins(10, 5, 10, 7)
        connection_layout.setSpacing(6)
        self.email.setPlaceholderText("이메일")
        self.password.setPlaceholderText("비밀번호 (저장하지 않음)")
        connect_button = QPushButton("DB 연결")
        connect_button.clicked.connect(self.connect_supabase)
        connection_layout.addWidget(self.email, 2)
        connection_layout.addWidget(self.password, 2)
        connection_layout.addWidget(connect_button)
        connection_layout.addWidget(self.db_status, 2)
        connection.setMaximumHeight(72)
        layout.addWidget(connection)

        options = QGroupBox("변환 설정")
        options_layout = QGridLayout(options)
        options_layout.setContentsMargins(10, 5, 10, 7)
        options_layout.setHorizontalSpacing(6)
        options_layout.setVerticalSpacing(5)
        browse_button = QPushButton("원본 선택")
        browse_button.clicked.connect(self.choose_file)
        analyze_button = QPushButton("분석 및 자동 매칭")
        analyze_button.setObjectName("primary")
        analyze_button.clicked.connect(self.analyze)
        options_layout.addWidget(QLabel("원본 파일"), 0, 0)
        options_layout.addWidget(self.file_path, 0, 1, 1, 7)
        options_layout.addWidget(browse_button, 0, 8)
        options_layout.addWidget(QLabel("주문 대상일"), 1, 0)
        options_layout.addWidget(self.order_date, 1, 1)
        options_layout.addWidget(QLabel("전표 일자"), 1, 2)
        options_layout.addWidget(self.voucher_date, 1, 3)
        options_layout.addWidget(QLabel("담당자"), 1, 4)
        options_layout.addWidget(self.manager_code, 1, 5)
        options_layout.addWidget(QLabel("기본 창고"), 1, 6)
        options_layout.addWidget(self.default_warehouse, 1, 7)
        options_layout.addWidget(analyze_button, 1, 8)
        options_layout.setColumnStretch(1, 1)
        options_layout.setColumnStretch(3, 1)
        options.setMaximumHeight(108)
        layout.addWidget(options)

        cards = QHBoxLayout()
        for label, widget, color in (
            ("대상 주문행", self.summary_orders, "#1D4ED8"),
            ("전표 품목행", self.summary_lines, "#047857"),
            ("확인 필요", self.summary_issues, "#B45309"),
            ("전표 총액", self.summary_total, "#0F172A"),
            ("실제 배송비", self.summary_shipping, "#7C3AED"),
            ("금액 차이", self.summary_difference, "#B91C1C"),
        ):
            card = QFrame()
            card.setObjectName("card")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(10, 5, 10, 5)
            card_layout.setSpacing(1)
            caption = QLabel(label)
            caption.setObjectName("caption")
            widget.setObjectName("metric")
            widget.setStyleSheet(f"color:{color};")
            card_layout.addWidget(caption)
            card_layout.addWidget(widget)
            cards.addWidget(card)
        layout.addLayout(cards)

        tabs = QTabWidget()
        self.lines_table.setHorizontalHeaderLabels(["품목코드", "품목명", "수량", "단가", "금액", "창고", "원본건수"])
        self.lines_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.lines_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for column in range(2, 7):
            self.lines_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.lines_table.setAlternatingRowColors(True)
        self.lines_table.setSortingEnabled(False)
        tabs.addTab(self.lines_table, "자동 변환 결과")

        self.issues_table.setHorizontalHeaderLabels(["원본행", "주문번호", "상품명", "옵션", "금액", "확인 사유"])
        self.issues_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.issues_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.issues_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.issues_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.issues_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.issues_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        tabs.addTab(self.issues_table, "확인 필요")

        self.shipping_table.setHorizontalHeaderLabels(
            ["원본행", "배송비 묶음번호", "원배송비", "추가배송비", "할인액", "실제 배송비", "구분"]
        )
        self.shipping_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.shipping_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for column in range(2, 7):
            self.shipping_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.shipping_table.setAlternatingRowColors(True)
        tabs.addTab(self.shipping_table, "배송비 검수")
        layout.addWidget(tabs, 1)

        bottom = QHBoxLayout()
        guide = QLabel("노란색 셀은 수정 가능 · 배송비 조정건은 배송비 검수 탭에 주황색으로 표시됩니다.")
        bottom.addWidget(guide)
        remove_issue_button = QPushButton("선택 항목 분석에서 삭제")
        remove_issue_button.clicked.connect(self.remove_selected_issues)
        bottom.addWidget(remove_issue_button)
        add_db_button = QPushButton("DB에 단품 바로 추가")
        add_db_button.clicked.connect(self.add_selected_issue_to_db)
        bottom.addWidget(add_db_button)
        bottom.addStretch()
        self.export_button.clicked.connect(self.export_excel)
        bottom.addWidget(self.export_button)
        layout.addLayout(bottom)
        self.setCentralWidget(root)
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background:#F5F7FB; color:#182230; font-family:'Malgun Gothic'; font-size:10pt; }
            QLabel#title { font-size:18pt; font-weight:700; color:#102A43; }
            QLabel#subtitle { color:#627D98; margin-bottom:0; }
            QGroupBox { background:white; border:1px solid #D9E2EC; border-radius:7px; margin-top:7px; padding:6px; font-weight:600; }
            QGroupBox::title { subcontrol-origin:margin; left:10px; padding:0 4px; }
            QLineEdit, QDateEdit, QSpinBox { background:white; border:1px solid #BCCCDC; border-radius:5px; padding:5px; }
            QPushButton { background:#E6EEF7; border:0; border-radius:6px; padding:7px 11px; font-weight:600; }
            QPushButton:hover { background:#D6E4F0; }
            QPushButton#primary, QPushButton[text='이카운트 Excel 저장'] { background:#087E8B; color:white; }
            QPushButton#primary:hover, QPushButton[text='이카운트 Excel 저장']:hover { background:#066A74; }
            QPushButton:disabled { background:#CBD5E1; color:#64748B; }
            QFrame#card { background:white; border:1px solid #D9E2EC; border-radius:8px; }
            QLabel#caption { color:#627D98; }
            QLabel#metric { font-size:18pt; font-weight:700; }
            QTableWidget { background:white; alternate-background-color:#F8FAFC; border:1px solid #D9E2EC; gridline-color:#E7EDF3; }
            QHeaderView::section { background:#173F5F; color:white; padding:8px; border:0; font-weight:600; }
            QTabWidget::pane { border:1px solid #D9E2EC; background:white; }
            QTabBar::tab { background:#E8EEF5; padding:9px 18px; }
            QTabBar::tab:selected { background:#173F5F; color:white; }
            QScrollBar:vertical { width:20px; background:#E8EEF5; }
            QScrollBar:horizontal { height:20px; background:#E8EEF5; }
            QScrollBar::handle { background:#7892A8; border-radius:7px; min-height:42px; min-width:42px; }
            QScrollBar::handle:hover { background:#526D82; }
            QMessageBox, QInputDialog { font-size:9pt; }
            """
        )

    def _load_local_catalog(self) -> None:
        try:
            self.catalog = ReferenceCatalog.from_csv_dir(LOCAL_DATA_DIR)
            self.db_status.setText("로컬 기준 DB 사용 중 (Supabase 연결 가능)")
            self.db_status.setStyleSheet("color:#047857;")
        except Exception as exc:
            self.db_status.setText(f"DB 준비 실패: {exc}")
            self.db_status.setStyleSheet("color:#B91C1C;")

    def connect_supabase(self) -> None:
        if not self.email.text().strip() or not self.password.text():
            QMessageBox.information(self, "로그인 정보", "Supabase 이메일과 비밀번호를 입력해주세요.")
            return
        config = load_config()
        url = config.get("supabase_url", "")
        key = config.get("supabase_publishable_key", "")
        if not url or not key:
            QMessageBox.critical(self, "설정 오류", "config.json에 Supabase URL과 publishable key가 필요합니다.")
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            client = create_client(url, key)
            client.auth.sign_in_with_password({"email": self.email.text().strip(), "password": self.password.text()})
            self.supabase_client = client
            self.catalog = ReferenceCatalog(
                fetch_all(client, "ecount_item_reference"),
                fetch_all(client, "ecount_sales_channels"),
                fetch_all(client, "ecount_product_mappings"),
                fetch_all(client, "ecount_product_mapping_components"),
                fetch_all(client, "ecount_price_rules"),
                fetch_all(client, "ecount_price_rule_components"),
            )
            self.password.clear()
            self.db_status.setText("Supabase 최신 DB 연결 완료")
            self.db_status.setStyleSheet("color:#047857;font-weight:600;")
        except Exception as exc:
            QMessageBox.critical(self, "DB 연결 실패", str(exc))
        finally:
            QApplication.restoreOverrideCursor()

    def choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "스마트스토어 원본 선택", str(Path.home()), "Excel 파일 (*.xlsx *.xlsm)")
        if path:
            self.file_path.setText(path)

    def analyze(self) -> None:
        if self.catalog is None:
            QMessageBox.warning(self, "DB 없음", "기준 DB를 준비하거나 Supabase에 연결해주세요.")
            return
        source = Path(self.file_path.text().strip())
        if not source.exists():
            QMessageBox.information(self, "원본 파일", "스마트스토어 원본 Excel 파일을 선택해주세요.")
            return
        order_date = self.order_date.date().toPython()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            orders = read_smartstore_orders(source, order_date)
            if not orders:
                QMessageBox.information(self, "대상 없음", f"{order_date:%Y-%m-%d} 결제 주문을 찾지 못했습니다.")
                return
            self.current_result = convert_orders(orders, self.catalog, default_warehouse=str(self.default_warehouse.value()))
            self._show_result(self.current_result)
            self.export_button.setEnabled(bool(self.current_result.lines))
        except Exception as exc:
            QMessageBox.critical(self, "분석 실패", str(exc))
        finally:
            QApplication.restoreOverrideCursor()

    def _show_result(self, result: ConversionResult) -> None:
        self.summary_orders.setText(f"{len(result.orders):,}")
        self.summary_lines.setText(f"{len(result.lines):,}")
        self.summary_issues.setText(f"{len(result.issues):,}")
        self.summary_total.setText(f"{result.output_total:,.0f}원")
        self.summary_shipping.setText(f"{result.shipping_total:,.0f}원")
        self.summary_difference.setText(f"{result.amount_difference:,.0f}원")
        self.summary_difference.setStyleSheet(
            "color:#047857;" if result.is_reconciled else "color:#B91C1C;font-weight:700;"
        )
        self.lines_table.setSortingEnabled(False)
        self.lines_table.setRowCount(len(result.lines))
        editable_columns = {2, 3, 5}
        for row_index, line in enumerate(result.lines):
            values = [
                line.item_code,
                line.item_name,
                f"{line.quantity:,.0f}",
                f"{line.unit_price:,.0f}",
                f"{line.total:,.0f}",
                line.warehouse,
                str(line.source_count),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column not in editable_columns:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                else:
                    item.setBackground(QColor("#FFF4CC"))
                if column in {2, 3, 4, 6}:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.lines_table.setItem(row_index, column, item)
        self.lines_table.setSortingEnabled(False)

        self.issues_table.setRowCount(len(result.issues))
        for row_index, issue in enumerate(result.issues):
            values = [issue.source_row, issue.order_no, issue.product_name, issue.options, f"{issue.amount:,.0f}", issue.reason]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if column == 5:
                    item.setBackground(QColor("#FDE68A"))
                self.issues_table.setItem(row_index, column, item)

        self.shipping_table.setRowCount(len(result.shipping_charges))
        for row_index, charge in enumerate(result.shipping_charges):
            values = [
                charge.source_row,
                charge.bundle_key,
                f"{charge.shipping_total:,.0f}",
                f"{charge.extra_shipping:,.0f}",
                f"{charge.shipping_discount:,.0f}",
                f"{charge.effective_amount:,.0f}",
                "조정 확인" if charge.is_adjusted else "일반",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if column >= 2:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if charge.is_adjusted:
                    item.setBackground(QColor("#FED7AA"))
                self.shipping_table.setItem(row_index, column, item)

    def _apply_table_edits(self) -> None:
        assert self.current_result is not None
        updated: list[VoucherLine] = []
        for row in range(self.lines_table.rowCount()):
            original = self.current_result.lines[row]
            quantity = Decimal(self.lines_table.item(row, 2).text().replace(",", ""))
            unit_price = Decimal(self.lines_table.item(row, 3).text().replace(",", ""))
            warehouse = self.lines_table.item(row, 5).text().strip()
            if quantity <= 0:
                raise ValueError(f"{row + 1}행 수량은 0보다 커야 합니다.")
            if unit_price < 0 or unit_price != unit_price.to_integral_value():
                raise ValueError(f"{row + 1}행 단가는 0 이상의 원 단위 정수여야 합니다.")
            if not warehouse:
                raise ValueError(f"{row + 1}행 창고가 비어 있습니다.")
            updated.append(
                VoucherLine(
                    customer_code=original.customer_code,
                    customer_name=original.customer_name,
                    item_code=original.item_code,
                    item_name=original.item_name,
                    quantity=quantity,
                    unit_price=unit_price,
                    warehouse=warehouse,
                    source_count=original.source_count,
                    source_orders=original.source_orders,
                    is_shipping=original.is_shipping,
                )
            )
        self.current_result.lines = updated

    def remove_selected_issues(self) -> None:
        if self.current_result is None or self.catalog is None:
            return
        selected_rows = sorted({index.row() for index in self.issues_table.selectedIndexes()})
        if not selected_rows:
            QMessageBox.information(self, "선택 필요", "분석에서 삭제할 확인 필요 항목을 선택해주세요.")
            return
        source_rows = {self.current_result.issues[row].source_row for row in selected_rows}
        answer = QMessageBox.question(
            self,
            "분석 항목 삭제",
            f"선택한 {len(source_rows)}개 주문행을 이번 분석에서 제외할까요?\n원본 파일과 DB는 삭제되지 않습니다.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        remaining = [order for order in self.current_result.orders if order.source_row not in source_rows]
        self.current_result = convert_orders(
            remaining,
            self.catalog,
            default_warehouse=str(self.default_warehouse.value()),
        )
        self._show_result(self.current_result)
        self.export_button.setEnabled(bool(self.current_result.lines))

    def add_selected_issue_to_db(self) -> None:
        if self.current_result is None or self.catalog is None:
            return
        selected_rows = sorted({index.row() for index in self.issues_table.selectedIndexes()})
        if len(selected_rows) != 1:
            QMessageBox.information(self, "선택 필요", "DB에 추가할 확인 필요 항목 한 개를 선택해주세요.")
            return
        if self.supabase_client is None:
            QMessageBox.information(self, "DB 연결 필요", "Supabase 최신 DB에 연결한 뒤 다시 시도해주세요.")
            return
        issue = self.current_result.issues[selected_rows[0]]
        if issue.reason != "상품/옵션 조합이 DB에 없습니다.":
            QMessageBox.information(self, "단품 추가 불가", "DB 미등록 상품/옵션 항목만 단품으로 바로 추가할 수 있습니다.")
            return
        order = next((row for row in self.current_result.orders if row.source_row == issue.source_row), None)
        if order is None:
            return
        item_code, ok = QInputDialog.getText(
            self,
            "DB에 단품 바로 추가",
            f"{order.product_name}\n{order.options}\n\n연결할 이카운트 품목코드:",
        )
        item_code = item_code.strip()
        if not ok or not item_code:
            return
        if item_code not in self.catalog.items:
            QMessageBox.warning(self, "품목코드 확인", "판매전표 DB에 존재하는 품목코드를 입력해주세요.")
            return
        source_text = f"{order.product_name}{order.options}"
        mapping_key = hashlib.sha256(
            f"리큐엠_스마트스토어|{order.normalized_source}".encode("utf-8")
        ).hexdigest()
        answer = QMessageBox.question(
            self,
            "DB 추가 확인",
            f"선택 상품을 단품 {item_code}로 등록하고 즉시 다시 분석할까요?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.supabase_client.table("ecount_product_mappings").upsert(
                {
                    "mapping_key": mapping_key,
                    "source_channel": "리큐엠_스마트스토어",
                    "source_product_text": source_text,
                    "normalized_source": order.normalized_source,
                    "mapping_type": "single",
                    "component_count": 1,
                    "source_row": order.source_row,
                    "review_status": "confirmed",
                    "is_active": True,
                },
                on_conflict="mapping_key",
            ).execute()
            self.supabase_client.table("ecount_product_mapping_components").upsert(
                {
                    "mapping_key": mapping_key,
                    "sequence": 1,
                    "item_code": item_code,
                    "quantity": 1,
                    "source_row": order.source_row,
                },
                on_conflict="mapping_key,sequence",
            ).execute()
            self.catalog = ReferenceCatalog(
                fetch_all(self.supabase_client, "ecount_item_reference"),
                fetch_all(self.supabase_client, "ecount_sales_channels"),
                fetch_all(self.supabase_client, "ecount_product_mappings"),
                fetch_all(self.supabase_client, "ecount_product_mapping_components"),
                fetch_all(self.supabase_client, "ecount_price_rules"),
                fetch_all(self.supabase_client, "ecount_price_rule_components"),
            )
            self.analyze()
        except Exception as exc:
            QMessageBox.critical(self, "DB 추가 실패", str(exc))

    def export_excel(self) -> None:
        if self.current_result is None:
            return
        if self.current_result.issues:
            QMessageBox.warning(
                self,
                "저장 차단",
                f"확인 필요한 주문이 {len(self.current_result.issues)}건 있습니다.\n"
                "DB에 추가하거나 분석에서 명시적으로 삭제한 뒤 저장해주세요.",
            )
            return
        try:
            self._apply_table_edits()
        except Exception as exc:
            QMessageBox.warning(self, "수정값 오류", f"수량·단가·창고 값을 확인해주세요.\n{exc}")
            return
        if not self.current_result.is_reconciled:
            QMessageBox.critical(
                self,
                "금액 검수 실패",
                f"검수 기준금액과 전표금액이 {self.current_result.amount_difference:,.0f}원 차이납니다.\n"
                "금액 차이가 0원이 아니면 저장할 수 없습니다.",
            )
            return
        order_day = self.order_date.date().toString("yyyyMMdd")
        suggested = Path(self.file_path.text()).with_name(f"네이버_이카운트_판매전표_{order_day}.xlsx")
        path, _ = QFileDialog.getSaveFileName(self, "이카운트 Excel 저장", str(suggested), "Excel 파일 (*.xlsx)")
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        try:
            write_ecount_workbook(path, self.current_result, self.voucher_date.date().toPython(), self.manager_code.text().strip() or "00109")
            QMessageBox.information(self, "저장 완료", f"이카운트 입력자료를 저장했습니다.\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "저장 실패", str(exc))


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = SalesVoucherWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
