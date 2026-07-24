from __future__ import annotations

import hashlib
import json
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import QDate, QObject, QThread, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
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
from supabase import ClientOptions, create_client

from ecount_sales_core import (
    ConversionResult,
    ReferenceCatalog,
    VoucherLine,
    convert_orders,
    normalize_source,
    read_smartstore_orders,
    write_ecount_workbook,
)


SOURCE_DIR = Path(__file__).resolve().parent
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else SOURCE_DIR
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", SOURCE_DIR))
LOCAL_DATA_DIR = BUNDLE_DIR / "supabase" / "ecount_migration" / "data"
DB_LOG_PATH = APP_DIR / "db_connection.log"


def write_db_log(message: str) -> None:
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with DB_LOG_PATH.open("a", encoding="utf-8") as log:
            log.write(f"[{timestamp}] {message}\n")
    except Exception:
        pass


def load_config() -> dict[str, str]:
    for config_path in (APP_DIR / "config.json", APP_DIR.parent / "config.json", SOURCE_DIR / "config.json"):
        if config_path.exists():
            return json.loads(config_path.read_text(encoding="utf-8-sig"))
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


class SupabaseConnectWorker(QObject):
    status_changed = Signal(str)
    connected = Signal(object, object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, url: str, key: str, email: str, password: str) -> None:
        super().__init__()
        self.url = url
        self.key = key
        self.email = email
        self.password = password

    def run(self) -> None:
        try:
            write_db_log(f"인증 요청 시작: {self.email}")
            self.status_changed.emit("계정 로그인 확인 중...")
            client = create_client(
                self.url,
                self.key,
                options=ClientOptions(
                    postgrest_client_timeout=20,
                    storage_client_timeout=20,
                    function_client_timeout=20,
                ),
            )
            auth_response = client.auth.sign_in_with_password(
                {"email": self.email, "password": self.password}
            )
            if not auth_response.session:
                raise RuntimeError("로그인 세션을 받지 못했습니다.")
            write_db_log("계정 로그인 성공")
            self.status_changed.emit("로그인 완료 · 최신 DB 불러오는 중...")
            catalog = ReferenceCatalog(
                fetch_all(client, "ecount_item_reference"),
                fetch_all(client, "ecount_sales_channels"),
                fetch_all(client, "ecount_product_mappings"),
                fetch_all(client, "ecount_product_mapping_components"),
                fetch_all(client, "ecount_price_rules"),
                fetch_all(client, "ecount_price_rule_components"),
            )
            write_db_log("DB 테이블 조회 및 기준정보 구성 성공")
            self.connected.emit(client, catalog)
        except Exception as exc:
            write_db_log(f"연결 실패: {type(exc).__name__}: {exc}")
            self.failed.emit(str(exc))
        finally:
            self.password = ""
            self.finished.emit()


class SetMappingDialog(QDialog):
    def __init__(
        self,
        order,
        items: dict[str, dict],
        existing_components: list[dict] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.order = order
        self.items = items
        self.setWindowTitle("확인 필요 품목 · 세트 DB 연결")
        self.resize(820, 430)

        layout = QVBoxLayout(self)
        title = QLabel(f"{order.product_name}\n옵션: {order.options or '(없음)'}")
        title.setStyleSheet("font-weight:700;color:#173F5F;")
        layout.addWidget(title)
        self.target_label = QLabel(f"세트 1개 기준 배분 대상 금액: {order.unit_total:,.0f}원")
        layout.addWidget(self.target_label)

        self.component_table = QTableWidget(0, 4)
        self.component_table.setHorizontalHeaderLabels(["DB 품목", "세트당 수량", "개당 금액", "배분 금액"])
        self.component_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3):
            self.component_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        layout.addWidget(self.component_table)

        controls = QHBoxLayout()
        add_button = QPushButton("구성품 추가")
        remove_button = QPushButton("선택 구성품 삭제")
        add_button.clicked.connect(self.add_component_row)
        remove_button.clicked.connect(self.remove_selected_component)
        controls.addWidget(add_button)
        controls.addWidget(remove_button)
        controls.addStretch(1)
        self.sum_label = QLabel()
        self.sum_label.setStyleSheet("font-weight:700;")
        controls.addWidget(self.sum_label)
        layout.addLayout(controls)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("Supabase에 세트 저장")
        buttons.accepted.connect(self.validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        seeded = existing_components or []
        if seeded:
            for component in seeded:
                self.add_component_row(
                    str(component.get("item_code") or ""),
                    Decimal(str(component.get("quantity") or 1)),
                    Decimal("0"),
                )
        else:
            self.add_component_row()
            self.add_component_row()
        self.update_total()

    def add_component_row(
        self,
        item_code: str = "",
        quantity: Decimal = Decimal("1"),
        unit_price: Decimal = Decimal("0"),
    ) -> None:
        if self.component_table.rowCount() >= 5:
            QMessageBox.information(self, "구성품 제한", "세트 구성품은 최대 5개까지 등록할 수 있습니다.")
            return
        row = self.component_table.rowCount()
        self.component_table.insertRow(row)
        combo = QComboBox()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.NoInsert)
        combo.addItem("품목코드 또는 품목명 검색", "")
        for code, item in sorted(self.items.items()):
            name = str(
                item.get("representative_name")
                or item.get("item_name")
                or item.get("standard_name")
                or code
            )
            combo.addItem(f"{code} | {name}", code)
        selected = combo.findData(item_code)
        if selected >= 0:
            combo.setCurrentIndex(selected)
        combo.currentIndexChanged.connect(self.update_total)
        self.component_table.setCellWidget(row, 0, combo)

        quantity_input = QSpinBox()
        quantity_input.setRange(1, 9999)
        quantity_input.setValue(max(1, int(quantity)))
        quantity_input.valueChanged.connect(self.update_total)
        self.component_table.setCellWidget(row, 1, quantity_input)

        price_input = QLineEdit(f"{unit_price:,.0f}")
        price_input.setAlignment(Qt.AlignRight)
        price_input.textChanged.connect(self.update_total)
        self.component_table.setCellWidget(row, 2, price_input)
        total_item = QTableWidgetItem("0")
        total_item.setFlags(total_item.flags() & ~Qt.ItemIsEditable)
        total_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.component_table.setItem(row, 3, total_item)
        self.update_total()

    def remove_selected_component(self) -> None:
        rows = sorted({index.row() for index in self.component_table.selectedIndexes()}, reverse=True)
        if not rows and self.component_table.currentRow() >= 0:
            rows = [self.component_table.currentRow()]
        for row in rows:
            self.component_table.removeRow(row)
        self.update_total()

    def _price_at(self, row: int) -> Decimal:
        widget = self.component_table.cellWidget(row, 2)
        try:
            return Decimal(widget.text().replace(",", "").strip() or "0")
        except Exception:
            return Decimal("-1")

    def update_total(self, *_args) -> None:
        total = Decimal("0")
        for row in range(self.component_table.rowCount()):
            quantity = Decimal(self.component_table.cellWidget(row, 1).value())
            price = self._price_at(row)
            allocated = quantity * max(price, Decimal("0"))
            total += allocated
            item = self.component_table.item(row, 3)
            if item is not None:
                item.setText(f"{allocated:,.0f}")
        difference = self.order.unit_total - total
        self.sum_label.setText(f"배분 합계 {total:,.0f}원 · 차이 {difference:,.0f}원")
        self.sum_label.setStyleSheet(
            "font-weight:700;color:#047857;" if difference == 0
            else "font-weight:700;color:#B91C1C;"
        )

    def components(self) -> list[dict]:
        result: list[dict] = []
        for row in range(self.component_table.rowCount()):
            combo = self.component_table.cellWidget(row, 0)
            item_code = str(combo.currentData() or "").strip()
            if not item_code:
                typed = combo.currentText().split("|", 1)[0].strip()
                if typed in self.items:
                    item_code = typed
            quantity = Decimal(self.component_table.cellWidget(row, 1).value())
            unit_price = self._price_at(row)
            result.append(
                {"item_code": item_code, "quantity": quantity, "unit_price": unit_price}
            )
        return result

    def validate_and_accept(self) -> None:
        components = self.components()
        if len(components) < 2:
            QMessageBox.warning(self, "세트 구성 확인", "세트 구성품을 2개 이상 입력해주세요.")
            return
        if any(row["item_code"] not in self.items for row in components):
            QMessageBox.warning(self, "품목 확인", "모든 구성품을 판매전표 DB 품목에서 선택해주세요.")
            return
        if any(row["unit_price"] < 0 or row["unit_price"] != row["unit_price"].to_integral_value() for row in components):
            QMessageBox.warning(self, "금액 확인", "구성품 금액은 0 이상의 원 단위 정수로 입력해주세요.")
            return
        allocated_total = sum(
            (row["quantity"] * row["unit_price"] for row in components),
            Decimal("0"),
        )
        if allocated_total != self.order.unit_total:
            QMessageBox.warning(
                self,
                "배분 금액 불일치",
                f"구성품 배분 합계가 {allocated_total:,.0f}원입니다.\n"
                f"세트 1개 금액 {self.order.unit_total:,.0f}원과 정확히 일치해야 합니다.",
            )
            return
        self.accept()


class SalesVoucherWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        write_db_log("프로그램 시작")
        self.setWindowTitle("REQM 판매전표 반자동화 - DB 로그인 수정본")
        self.resize(1280, 760)
        self.catalog: ReferenceCatalog | None = None
        self.supabase_client = None
        self.db_thread: QThread | None = None
        self.db_worker: SupabaseConnectWorker | None = None
        self.db_connecting = False
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
        self.connect_button = QPushButton("DB 로그인")
        self.connect_button.pressed.connect(self.connect_supabase)
        self.email.returnPressed.connect(self.connect_supabase)
        self.password.returnPressed.connect(self.connect_supabase)
        connection_layout.addWidget(self.email, 2)
        connection_layout.addWidget(self.password, 2)
        connection_layout.addWidget(self.connect_button)
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
            ("전표 배송비", self.summary_shipping, "#7C3AED"),
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
        self.issues_table.cellDoubleClicked.connect(self.open_set_mapping_dialog)
        self.issues_table.setToolTip("확인 필요 항목을 더블클릭하면 Supabase 세트 품목과 금액을 연결할 수 있습니다.")
        tabs.addTab(self.issues_table, "확인 필요")

        self.shipping_table.setHorizontalHeaderLabels(
            ["원본행", "배송비 묶음번호", "원배송비", "추가배송비", "할인액(참고)", "전표 배송비", "구분"]
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
        write_db_log("DB 로그인 버튼/Enter 입력 감지")
        try:
            self._start_supabase_connection()
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            write_db_log(f"로그인 시작 처리 예외: {detail}")
            self.db_connecting = False
            self.connect_button.setEnabled(True)
            self.connect_button.setText("DB 로그인")
            self.db_status.setText(f"로그인 시작 오류: {detail}")
            self.db_status.setStyleSheet("color:#B91C1C;font-weight:600;")

    def _start_supabase_connection(self) -> None:
        if self.db_connecting:
            write_db_log("이미 연결 작업이 실행 중이므로 중복 요청 무시")
            return
        if not self.email.text().strip() or not self.password.text():
            write_db_log("이메일 또는 비밀번호 미입력")
            QMessageBox.information(self, "로그인 정보", "Supabase 이메일과 비밀번호를 입력해주세요.")
            return
        config = load_config()
        url = config.get("supabase_url", "")
        key = config.get("supabase_publishable_key", "")
        if not url or not key:
            write_db_log("config.json의 URL 또는 publishable key 누락")
            QMessageBox.critical(self, "설정 오류", "config.json에 Supabase URL과 publishable key가 필요합니다.")
            return
        write_db_log(f"설정 확인 완료: {url}")
        self.connect_button.setEnabled(False)
        self.connect_button.setText("연결 중...")
        self.db_connecting = True
        self.db_status.setText("연결 준비 중...")
        self.db_status.setStyleSheet("color:#B45309;font-weight:600;")

        thread = QThread(self)
        worker = SupabaseConnectWorker(
            url,
            key,
            self.email.text().strip(),
            self.password.text(),
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.status_changed.connect(self._show_db_progress)
        worker.connected.connect(self._on_db_connected)
        worker.failed.connect(self._on_db_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_db_thread_finished)
        self.db_thread = thread
        self.db_worker = worker
        self.password.clear()
        thread.start()

    def _show_db_progress(self, message: str) -> None:
        write_db_log(f"진행 상태: {message}")
        self.db_status.setText(message)

    def _on_db_connected(self, client: object, catalog: object) -> None:
        self.supabase_client = client
        self.catalog = catalog
        write_db_log("프로그램에 Supabase DB 연결 적용 완료")
        self.db_status.setText("Supabase 최신 DB 연결 완료")
        self.db_status.setStyleSheet("color:#047857;font-weight:600;")

    def _on_db_failed(self, message: str) -> None:
        detail = message.strip() or "알 수 없는 오류"
        write_db_log(f"화면에 실패 표시: {detail}")
        self.db_status.setText(f"로그인 실패: {detail}")
        self.db_status.setToolTip(detail)
        self.db_status.setStyleSheet("color:#B91C1C;font-weight:600;")

    def _on_db_thread_finished(self) -> None:
        self.db_connecting = False
        self.connect_button.setEnabled(True)
        self.connect_button.setText("DB 로그인")
        self.db_thread = None
        self.db_worker = None

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
            editable_columns = {2, 3, 5}
            if line.needs_review:
                editable_columns.update({0, 1})
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column not in editable_columns:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                else:
                    item.setBackground(QColor("#FFF4CC"))
                if line.needs_review:
                    item.setBackground(QColor("#FDE68A"))
                    item.setToolTip(f"확인 필요: {line.review_reason}")
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
                    item_code=self.lines_table.item(row, 0).text().strip(),
                    item_name=self.lines_table.item(row, 1).text().strip(),
                    quantity=quantity,
                    unit_price=unit_price,
                    warehouse=warehouse,
                    source_count=original.source_count,
                    source_orders=original.source_orders,
                    is_shipping=original.is_shipping,
                    needs_review=original.needs_review,
                    review_reason=original.review_reason,
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

    def open_set_mapping_dialog(self, issue_row: int, _column: int = 0) -> None:
        if self.current_result is None or self.catalog is None:
            return
        if self.supabase_client is None:
            QMessageBox.information(
                self,
                "DB 연결 필요",
                "먼저 Supabase 관리자 계정으로 DB 로그인해주세요.",
            )
            return
        if issue_row < 0 or issue_row >= len(self.current_result.issues):
            return
        issue = self.current_result.issues[issue_row]
        order = next(
            (row for row in self.current_result.orders if row.source_row == issue.source_row),
            None,
        )
        if order is None:
            return
        existing_mapping = self.catalog.mappings.get(
            ("리큐엠_스마트스토어", order.normalized_source),
            {},
        )
        dialog = SetMappingDialog(
            order,
            self.catalog.items,
            existing_mapping.get("components", []),
            self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        components = dialog.components()
        allocated_total = sum(
            (row["quantity"] * row["unit_price"] for row in components),
            Decimal("0"),
        )
        mapping_key = str(existing_mapping.get("mapping_key") or "")
        if not mapping_key:
            mapping_key = hashlib.sha256(
                f"리큐엠_스마트스토어|{order.normalized_source}".encode("utf-8")
            ).hexdigest()
        price_rule_key = hashlib.sha256(
            (
                f"리큐엠_스마트스토어|{order.normalized_source}|"
                f"{order.unit_total:.2f}"
            ).encode("utf-8")
        ).hexdigest()
        source_text = f"{order.product_name}{order.options}"
        try:
            self.db_status.setText("세트 매핑을 Supabase에 저장 중...")
            self.supabase_client.table("ecount_product_mappings").upsert(
                {
                    "mapping_key": mapping_key,
                    "source_channel": "리큐엠_스마트스토어",
                    "source_product_text": source_text,
                    "normalized_source": order.normalized_source,
                    "mapping_type": "set",
                    "component_count": len(components),
                    "source_row": order.source_row,
                    "review_status": "confirmed",
                    "is_active": True,
                },
                on_conflict="mapping_key",
            ).execute()
            self.supabase_client.table("ecount_product_mapping_components").delete().eq(
                "mapping_key", mapping_key
            ).execute()
            self.supabase_client.table("ecount_product_mapping_components").insert(
                [
                    {
                        "mapping_key": mapping_key,
                        "sequence": sequence,
                        "item_code": component["item_code"],
                        "quantity": float(component["quantity"]),
                        "source_row": order.source_row,
                    }
                    for sequence, component in enumerate(components, start=1)
                ]
            ).execute()

            self.supabase_client.table("ecount_price_rules").upsert(
                {
                    "price_rule_key": price_rule_key,
                    "source_channel": "리큐엠_스마트스토어",
                    "source_product_name": order.product_name,
                    "source_options": order.options,
                    "normalized_source": order.normalized_source,
                    "total_unit_price": float(order.unit_total),
                    "item_type": "세트",
                    "main_product": components[0]["item_code"],
                    "set_name": order.options or order.product_name,
                    "component_count": len(components),
                    "allocated_total": float(allocated_total),
                    "allocation_variance": 0,
                    "source_row": order.source_row,
                    "review_status": "confirmed",
                    "is_active": True,
                },
                on_conflict="price_rule_key",
            ).execute()
            self.supabase_client.table("ecount_price_rule_components").delete().eq(
                "price_rule_key", price_rule_key
            ).execute()
            price_components = []
            for sequence, component in enumerate(components, start=1):
                item = self.catalog.items[component["item_code"]]
                alias = str(
                    item.get("representative_name")
                    or item.get("item_name")
                    or item.get("standard_name")
                    or component["item_code"]
                )
                price_components.append(
                    {
                        "price_rule_key": price_rule_key,
                        "sequence": sequence,
                        "component_alias": alias,
                        "normalized_component_alias": normalize_source(alias),
                        "item_code": component["item_code"],
                        "quantity": float(component["quantity"]),
                        "allocated_unit_price": float(component["unit_price"]),
                        "source_row": order.source_row,
                        "review_status": "confirmed",
                    }
                )
            self.supabase_client.table("ecount_price_rule_components").insert(
                price_components
            ).execute()
            self._reload_supabase_catalog()
            self.db_status.setText("세트 DB 저장 완료 · 주문 다시 분석")
            self.db_status.setStyleSheet("color:#047857;font-weight:600;")
            self.analyze()
        except Exception as exc:
            self.db_status.setText(f"세트 DB 저장 실패: {exc}")
            self.db_status.setStyleSheet("color:#B91C1C;font-weight:600;")
            QMessageBox.critical(self, "세트 DB 저장 실패", str(exc))

    def _reload_supabase_catalog(self) -> None:
        if self.supabase_client is None:
            return
        self.catalog = ReferenceCatalog(
            fetch_all(self.supabase_client, "ecount_item_reference"),
            fetch_all(self.supabase_client, "ecount_sales_channels"),
            fetch_all(self.supabase_client, "ecount_product_mappings"),
            fetch_all(self.supabase_client, "ecount_product_mapping_components"),
            fetch_all(self.supabase_client, "ecount_price_rules"),
            fetch_all(self.supabase_client, "ecount_price_rule_components"),
        )

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
            self._reload_supabase_catalog()
            self.analyze()
        except Exception as exc:
            QMessageBox.critical(self, "DB 추가 실패", str(exc))

    def export_excel(self) -> None:
        if self.current_result is None:
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
