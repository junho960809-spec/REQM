from __future__ import annotations

import csv
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
    QCompleter,
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
    combine_order_sources,
    build_item_order_details,
    ConversionResult,
    ReferenceCatalog,
    SmartStoreOrder,
    VoucherLine,
    convert_orders,
    detect_smartstore_order_period,
    normalize_source,
    read_purchase_confirmed_orders,
    read_sellmate_orders,
    read_smartstore_orders_range,
    write_ecount_workbook,
    write_ecount_lines_workbook,
)
from ecount_sales_api_dialog import EcountSalesApiDialog


SOURCE_DIR = Path(__file__).resolve().parent
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else SOURCE_DIR
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", SOURCE_DIR))
LOCAL_DATA_DIR = BUNDLE_DIR / "supabase" / "ecount_migration" / "data"
DB_LOG_PATH = APP_DIR / "db_connection.log"
SPECIAL_ITEMS_PATH = APP_DIR / "special_warehouse_items.json"
DELETED_ITEMS_PATH = APP_DIR / "deleted_db_items.json"


def search_text_matches(keyword: str, searchable: str) -> bool:
    """공백·하이픈·밑줄 차이를 무시하고 코드와 주문번호를 검색한다."""
    raw_keyword = (keyword or "").strip().casefold()
    if not raw_keyword:
        return True
    raw_searchable = (searchable or "").casefold()
    if raw_keyword in raw_searchable:
        return True
    normalized_keyword = normalize_source(raw_keyword)
    return bool(normalized_keyword and normalized_keyword in normalize_source(raw_searchable))


def load_code_set(path: Path) -> set[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return {str(value).strip() for value in data if str(value).strip()}
    except Exception:
        return set()


def save_code_set(path: Path, values: set[str]) -> None:
    path.write_text(
        json.dumps(sorted(values), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def catalog_without_items(catalog: ReferenceCatalog, deleted_codes: set[str]) -> ReferenceCatalog:
    if not deleted_codes:
        return catalog
    deleted = {code.casefold() for code in deleted_codes}
    items = [
        row for row in catalog.item_rows
        if str(row.get("item_code") or "").casefold() not in deleted
    ]
    invalid_mapping_keys = {
        str(row.get("mapping_key") or "") for row in catalog.mapping_component_rows
        if str(row.get("item_code") or "").casefold() in deleted
    }
    mapping_components = [
        row for row in catalog.mapping_component_rows
        if str(row.get("mapping_key") or "") not in invalid_mapping_keys
    ]
    valid_mapping_keys = {str(row.get("mapping_key") or "") for row in mapping_components}
    mappings = [
        row for row in catalog.mapping_rows
        if str(row.get("mapping_key") or "") in valid_mapping_keys
    ]
    invalid_rule_keys = {
        str(row.get("price_rule_key") or "") for row in catalog.price_component_rows
        if str(row.get("item_code") or "").casefold() in deleted
    }
    price_components = [
        row for row in catalog.price_component_rows
        if str(row.get("price_rule_key") or "") not in invalid_rule_keys
    ]
    valid_rule_keys = {str(row.get("price_rule_key") or "") for row in price_components}
    price_rules = [
        row for row in catalog.price_rule_rows
        if str(row.get("price_rule_key") or "") in valid_rule_keys
    ]
    return ReferenceCatalog(
        items,
        catalog.channel_rows,
        mappings,
        mapping_components,
        price_rules,
        price_components,
    )


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


def merge_reference_rows(
    local_rows: list[dict],
    remote_rows: list[dict],
    *key_fields: str,
) -> list[dict]:
    """내장 DB를 기본으로 두고 같은 키의 Supabase 행이 있으면 원격 값을 우선한다."""
    merged: dict[tuple[str, ...], dict] = {}
    for row in [*local_rows, *remote_rows]:
        key = tuple(str(row.get(field, "")) for field in key_fields)
        if any(key):
            merged[key] = row
    return list(merged.values())


def read_local_reference_table(table_name: str) -> list[dict]:
    path = LOCAL_DATA_DIR / f"{table_name}.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_merged_reference_data(client) -> tuple[list[dict], ...]:
    specs = (
        ("ecount_item_reference", ("item_code",)),
        ("ecount_sales_channels", ("source_name",)),
        ("ecount_product_mappings", ("mapping_key",)),
        ("ecount_product_mapping_components", ("mapping_key", "sequence")),
        ("ecount_price_rules", ("price_rule_key",)),
        ("ecount_price_rule_components", ("price_rule_key", "sequence")),
    )
    return tuple(
        merge_reference_rows(
            read_local_reference_table(table_name),
            fetch_all(client, table_name),
            *key_fields,
        )
        for table_name, key_fields in specs
    )


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
            items, channels, mappings, mapping_components, price_rules, price_components = (
                load_merged_reference_data(client)
            )
            catalog = ReferenceCatalog(
                items,
                channels,
                mappings,
                mapping_components,
                price_rules,
                price_components,
            )
            write_db_log("Supabase 원격 DB와 내장 판매전표 DB 통합 성공")
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
        component_guide = QLabel(
            "행사·공동구매처럼 세트로 분류됐지만 실제 품목이 하나라면 구성품 1개만 남겨 저장할 수 있습니다."
        )
        component_guide.setStyleSheet("color:#526D82;")
        layout.addWidget(component_guide)

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
                    Decimal(
                        str(
                            component.get("allocated_unit_price")
                            or component.get("unit_price")
                            or 0
                        )
                    ),
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
        completion_labels = []
        for code, item in sorted(self.items.items()):
            name = str(
                item.get("representative_name")
                or item.get("item_name")
                or item.get("standard_name")
                or code
            )
            label = f"{code} | {name}"
            combo.addItem(label, code)
            completion_labels.append(label)
        completer = QCompleter(completion_labels, combo)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        combo.setCompleter(completer)
        combo.setToolTip("품목코드 또는 품목명의 일부를 입력하면 검색 결과가 표시됩니다.")
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
        if len(components) < 1:
            QMessageBox.warning(self, "세트 구성 확인", "세트 구성품을 1개 이상 입력해주세요.")
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


class ItemEditDialog(QDialog):
    def __init__(self, item_code: str = "", item_name: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("판매전표 품목 등록" if not item_code else "판매전표 품목 수정")
        form = QFormLayout(self)
        self.code_input = QLineEdit(item_code)
        self.name_input = QLineEdit(item_name)
        self.code_input.setReadOnly(bool(item_code))
        form.addRow("품목코드", self.code_input)
        form.addRow("품목명", self.name_input)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("저장")
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _validate(self) -> None:
        if not self.code_input.text().strip() or not self.name_input.text().strip():
            QMessageBox.warning(self, "필수값", "품목코드와 품목명을 모두 입력해주세요.")
            return
        self.accept()

    def values(self) -> tuple[str, str]:
        return self.code_input.text().strip(), self.name_input.text().strip()


class RuleIdentityDialog(QDialog):
    def __init__(
        self,
        product_name: str = "",
        options: str = "",
        total_unit_price: Decimal = Decimal("0"),
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("상품 매칭·가격 규칙")
        form = QFormLayout(self)
        self.product_input = QLineEdit(product_name)
        self.options_input = QLineEdit(options)
        self.amount_input = QLineEdit(f"{total_unit_price:,.0f}")
        self.amount_input.setAlignment(Qt.AlignRight)
        form.addRow("쇼핑몰 상품명", self.product_input)
        form.addRow("옵션", self.options_input)
        form.addRow("세트 1개 총금액", self.amount_input)
        guide = QLabel("같은 상품명·옵션·총금액 규칙은 중복 저장할 수 없습니다.")
        guide.setStyleSheet("color:#526D82;")
        form.addRow(guide)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("구성품·가격 입력")
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _validate(self) -> None:
        if not self.product_input.text().strip():
            QMessageBox.warning(self, "필수값", "쇼핑몰 상품명을 입력해주세요.")
            return
        try:
            amount = Decimal(self.amount_input.text().replace(",", "").strip())
        except Exception:
            QMessageBox.warning(self, "금액 확인", "총금액을 숫자로 입력해주세요.")
            return
        if amount < 0 or amount != amount.to_integral_value():
            QMessageBox.warning(self, "금액 확인", "총금액은 0 이상의 원 단위 정수여야 합니다.")
            return
        self.accept()

    def values(self) -> tuple[str, str, Decimal]:
        return (
            self.product_input.text().strip(),
            self.options_input.text().strip(),
            Decimal(self.amount_input.text().replace(",", "").strip()),
        )


class NumericTableWidgetItem(QTableWidgetItem):
    def __init__(self, value: Decimal | int | float, suffix: str = "") -> None:
        numeric = Decimal(str(value))
        super().__init__(f"{numeric:,.0f}{suffix}")
        self.numeric_value = numeric

    def __lt__(self, other: QTableWidgetItem) -> bool:
        if isinstance(other, NumericTableWidgetItem):
            return self.numeric_value < other.numeric_value
        return super().__lt__(other)


class ItemChecklistDialog(QDialog):
    def __init__(
        self,
        title: str,
        items: list[dict],
        checked_codes: set[str],
        action_text: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(620, 560)
        layout = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText("품목코드 또는 품목명 검색")
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["품목코드", "품목명"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setSortingEnabled(False)
        for row_data in sorted(items, key=lambda row: str(row.get("item_code") or "").casefold()):
            code = str(row_data.get("item_code") or "")
            name = str(row_data.get("representative_name") or row_data.get("item_name") or "")
            row = self.table.rowCount()
            self.table.insertRow(row)
            code_item = QTableWidgetItem(code)
            code_item.setFlags(code_item.flags() | Qt.ItemIsUserCheckable)
            code_item.setCheckState(Qt.Checked if code in checked_codes else Qt.Unchecked)
            name_item = QTableWidgetItem(name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, 0, code_item)
            self.table.setItem(row, 1, name_item)
        self.table.setSortingEnabled(True)
        layout.addWidget(self.table)
        self.search.textChanged.connect(self._filter)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText(action_text)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _filter(self, text: str) -> None:
        keyword = (text or "").strip().casefold()
        for row in range(self.table.rowCount()):
            searchable = " ".join(
                self.table.item(row, column).text()
                for column in range(2)
                if self.table.item(row, column) is not None
            ).casefold()
            self.table.setRowHidden(row, bool(keyword and keyword not in searchable))

    def checked_codes(self) -> set[str]:
        return {
            self.table.item(row, 0).text()
            for row in range(self.table.rowCount())
            if self.table.item(row, 0).checkState() == Qt.Checked
        }


class WarehouseLinesDialog(QDialog):
    def __init__(
        self,
        lines: list[VoucherLine],
        voucher_date: date,
        manager_code: str,
        warehouse_code: str,
        warehouse_label: str,
        allow_release: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.lines = lines
        self.removed_codes: set[str] = set()
        self.voucher_date = voucher_date
        self.manager_code = manager_code
        self.warehouse_code = warehouse_code
        self.warehouse_label = warehouse_label
        self.allow_release = allow_release
        self.setWindowTitle(f"{warehouse_label} 출고")
        self.resize(1050, 560)
        layout = QVBoxLayout(self)
        total_quantity = sum((line.quantity for line in lines), Decimal("0"))
        total_amount = sum((line.total for line in lines), Decimal("0"))
        summary = QLabel(
            f"창고 {warehouse_code} · 품목행 {len(lines):,}개 · 수량 {total_quantity:,.0f}개 · "
            f"금액 {total_amount:,.0f}원 "
            "(전체 전표 총액에는 그대로 포함)"
        )
        summary.setStyleSheet("font-weight:700;color:#0F766E;")
        layout.addWidget(summary)
        self.search = QLineEdit()
        self.search.setPlaceholderText("품목코드·품목명·주문번호 검색")
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["품목코드", "품목명", "수량", "단가", "금액", "주문건수", "주문번호"]
        )
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for column in range(2, 6):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.Stretch)
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(lines))
        for row, line in enumerate(lines):
            order_numbers = list(dict.fromkeys(line.source_orders))
            values = [
                line.item_code,
                line.item_name,
                line.quantity,
                line.unit_price,
                line.total,
                len(order_numbers),
                ", ".join(order_numbers),
            ]
            for column, value in enumerate(values):
                item = (
                    NumericTableWidgetItem(value)
                    if column in {2, 3, 4, 5}
                    else QTableWidgetItem(str(value))
                )
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if column >= 2:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(row, column, item)
        self.table.setSortingEnabled(True)
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.table)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        if allow_release:
            remove_button = buttons.addButton("선택 품목 본사출고 해제", QDialogButtonBox.ActionRole)
            remove_button.clicked.connect(self._remove_selected)
        export_button = buttons.addButton(f"{warehouse_label} Excel 저장", QDialogButtonBox.ActionRole)
        export_button.clicked.connect(self._export)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _remove_selected(self) -> None:
        selected_rows = sorted({index.row() for index in self.table.selectedIndexes()})
        if not selected_rows and self.table.currentRow() >= 0:
            selected_rows = [self.table.currentRow()]
        codes = {
            self.table.item(row, 0).text()
            for row in selected_rows
            if self.table.item(row, 0) is not None
        }
        if not codes:
            QMessageBox.information(self, "선택 필요", "본사출고에서 해제할 품목을 선택해주세요.")
            return
        answer = QMessageBox.question(
            self,
            "본사출고 지정 해제",
            f"{len(codes):,}개 품목을 본사출고 대상에서 해제하고 기본창고로 되돌릴까요?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.removed_codes.update(codes)
        self.lines = [line for line in self.lines if line.item_code not in codes]
        for row in range(self.table.rowCount() - 1, -1, -1):
            item = self.table.item(row, 0)
            if item is not None and item.text() in codes:
                self.table.removeRow(row)

    def _filter(self, text: str) -> None:
        keyword = (text or "").strip()
        for row in range(self.table.rowCount()):
            searchable = " ".join(
                self.table.item(row, column).text()
                for column in range(self.table.columnCount())
                if self.table.item(row, column) is not None
            )
            self.table.setRowHidden(row, not search_text_matches(keyword, searchable))

    def _export(self) -> None:
        if not self.lines:
            QMessageBox.information(self, "대상 없음", f"저장할 {self.warehouse_label} 품목이 없습니다.")
            return
        suggested = f"{self.warehouse_label}_이카운트_판매전표_{self.voucher_date:%Y%m%d}.xlsx"
        path, _ = QFileDialog.getSaveFileName(
            self, f"{self.warehouse_label} Excel 저장", suggested, "Excel 파일 (*.xlsx)"
        )
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        try:
            write_ecount_lines_workbook(
                path, self.lines, self.voucher_date, self.manager_code, self.warehouse_label
            )
            QMessageBox.information(
                self, "저장 완료", f"{self.warehouse_label} 품목만 저장했습니다.\n{path}"
            )
        except Exception as exc:
            QMessageBox.critical(self, "저장 실패", str(exc))


def mask_person_name(name: str) -> str:
    text = (name or "").strip()
    if not text:
        return "-"
    if len(text) == 1:
        return "*"
    if len(text) == 2:
        return f"{text[0]}*"
    return f"{text[0]}{'*' * (len(text) - 2)}{text[-1]}"


class ItemOrderDetailsDialog(QDialog):
    def __init__(self, item_code: str, item_name: str, details: list, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("품목 주문 상세")
        self.resize(1080, 620)
        layout = QVBoxLayout(self)
        order_count = len({(row.order_no, row.product_order_no) for row in details})
        total_quantity = sum((row.converted_quantity for row in details), Decimal("0"))
        summary = QLabel(
            f"품목코드  {item_code}    |    품목명  {item_name}    |    "
            f"주문건수  {order_count:,}건    |    합계 수량  {total_quantity:,.0f}개"
        )
        summary.setStyleSheet(
            "background:#F0F7FF;border:1px solid #BFDBFE;border-radius:7px;"
            "padding:12px;font-size:11pt;font-weight:700;color:#173F5F;"
        )
        layout.addWidget(summary)
        search = QLineEdit()
        search.setPlaceholderText("주문번호, 주문자명, 상품명, 옵션 검색")
        search.setClearButtonEnabled(True)
        layout.addWidget(search)
        table = QTableWidget(0, 7)
        table.setHorizontalHeaderLabels(
            ["주문번호", "주문자명", "원본 상품명", "옵션", "주문 수량", "환산 수량", "출고창고"]
        )
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        for column in range(4, 7):
            table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        table.setAlternatingRowColors(True)
        table.setSortingEnabled(False)
        table.setRowCount(len(details))
        for row_index, detail in enumerate(details):
            values = [
                detail.order_no,
                mask_person_name(detail.purchaser_name),
                detail.product_name,
                detail.options,
                detail.order_quantity,
                detail.converted_quantity,
                detail.warehouse,
            ]
            for column, value in enumerate(values):
                item = NumericTableWidgetItem(value) if column in {4, 5} else QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if column >= 4:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                table.setItem(row_index, column, item)
        table.setSortingEnabled(True)
        layout.addWidget(table)

        def apply_filter(text: str) -> None:
            keyword = (text or "").strip().casefold()
            for row in range(table.rowCount()):
                searchable = " ".join(
                    table.item(row, column).text()
                    for column in range(4)
                    if table.item(row, column) is not None
                ).casefold()
                table.setRowHidden(row, bool(keyword and keyword not in searchable))

        search.textChanged.connect(apply_filter)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class SmartStoreSourceDialog(QDialog):
    def __init__(
        self,
        source_path: str,
        confirmed_path: str,
        start_date: QDate,
        end_date: QDate,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("스마트스토어 전표 입력")
        self.resize(720, 230)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.source_path = QLineEdit(source_path)
        self.source_path.setReadOnly(True)
        self.confirmed_path = QLineEdit(confirmed_path)
        self.confirmed_path.setReadOnly(True)
        self.start_date = QDateEdit(start_date)
        self.end_date = QDateEdit(end_date)
        for widget in (self.start_date, self.end_date):
            widget.setCalendarPopup(True)

        source_row = QWidget()
        source_layout = QHBoxLayout(source_row)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_layout.addWidget(self.source_path, 1)
        source_button = QPushButton("원본 선택")
        source_button.clicked.connect(self._choose_source)
        source_layout.addWidget(source_button)
        form.addRow("스마트스토어 원본", source_row)

        confirmed_row = QWidget()
        confirmed_layout = QHBoxLayout(confirmed_row)
        confirmed_layout.setContentsMargins(0, 0, 0, 0)
        confirmed_layout.addWidget(self.confirmed_path, 1)
        confirmed_button = QPushButton("구매확정 선택")
        confirmed_button.clicked.connect(self._choose_confirmed)
        confirmed_layout.addWidget(confirmed_button)
        clear_button = QPushButton("해제")
        clear_button.clicked.connect(self.confirmed_path.clear)
        confirmed_layout.addWidget(clear_button)
        form.addRow("구매확정 파일", confirmed_row)

        dates = QWidget()
        dates_layout = QHBoxLayout(dates)
        dates_layout.setContentsMargins(0, 0, 0, 0)
        dates_layout.addWidget(self.start_date)
        dates_layout.addWidget(QLabel("~"))
        dates_layout.addWidget(self.end_date)
        dates_layout.addStretch()
        form.addRow("주문 기간", dates)
        layout.addLayout(form)
        note = QLabel("원본을 선택하면 결제일 기준 주문 기간을 자동으로 확인합니다.")
        note.setStyleSheet("color:#526D82;")
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("이 입력 사용")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _choose_source(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "스마트스토어 원본 선택", str(Path.home()), "Excel 파일 (*.xlsx *.xlsm)"
        )
        if not path:
            return
        try:
            start, end = detect_smartstore_order_period(path)
        except Exception as exc:
            QMessageBox.warning(self, "파일 형식 확인", str(exc))
            return
        self.source_path.setText(path)
        self.start_date.setDate(QDate(start.year, start.month, start.day))
        self.end_date.setDate(QDate(end.year, end.month, end.day))

    def _choose_confirmed(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "스마트스토어 구매확정 파일 선택", str(Path.home()), "Excel 파일 (*.xlsx *.xlsm)"
        )
        if path:
            self.confirmed_path.setText(path)


class SellmateSourceDialog(QDialog):
    def __init__(self, source_path: str, voucher_date: QDate, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("폐쇄몰·외부 판매처 전표 입력")
        self.resize(720, 190)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.source_path = QLineEdit(source_path)
        self.source_path.setReadOnly(True)
        source_row = QWidget()
        source_layout = QHBoxLayout(source_row)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_layout.addWidget(self.source_path, 1)
        source_button = QPushButton("셀메이트 원본 선택")
        source_button.clicked.connect(self._choose_source)
        source_layout.addWidget(source_button)
        form.addRow("셀메이트 파일", source_row)
        self.voucher_date = QDateEdit(voucher_date)
        self.voucher_date.setCalendarPopup(True)
        form.addRow("전표 일자", self.voucher_date)
        layout.addLayout(form)
        note = QLabel("파일의 판매처명을 기준으로 거래처코드를 자동 적용합니다. 주문일 열이 없어 전표 일자는 직접 지정합니다.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#526D82;")
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("이 입력 사용")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _choose_source(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "셀메이트 원본 선택", str(Path.home()), "Excel 파일 (*.xlsx *.xlsm)"
        )
        if path:
            try:
                read_sellmate_orders(path, self.voucher_date.date().toPython())
            except Exception as exc:
                QMessageBox.warning(self, "파일 형식 확인", str(exc))
                return
            self.source_path.setText(path)


class SalesVoucherWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        write_db_log("프로그램 시작")
        self.setWindowTitle("REQM 판매전표 반자동화 - DB 로그인 수정본")
        self.resize(1280, 760)
        self.catalog: ReferenceCatalog | None = None
        self.base_catalog: ReferenceCatalog | None = None
        self.special_item_codes = load_code_set(SPECIAL_ITEMS_PATH)
        self.deleted_item_codes = load_code_set(DELETED_ITEMS_PATH)
        self.supabase_client = None
        self.db_thread: QThread | None = None
        self.db_worker: SupabaseConnectWorker | None = None
        self.db_connecting = False
        self.current_result: ConversionResult | None = None
        self.source_mode = "smartstore"
        self.file_path = QLineEdit()
        self.file_path.setPlaceholderText("위의 입력 유형 버튼에서 원본 Excel을 선택하세요")
        self.file_path.setReadOnly(True)
        self.confirmed_file_path = QLineEdit()
        self.confirmed_file_path.setPlaceholderText("구매확정 Excel을 선택하세요 (선택 사항)")
        self.confirmed_file_path.setReadOnly(True)
        self.source_mode_label = QLabel("스마트스토어 입력 대기")
        self.email = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.db_status = QLabel("DB 준비 중")
        self.order_date = QDateEdit(QDate.currentDate().addDays(-1))
        self.order_end_date = QDateEdit(QDate.currentDate().addDays(-1))
        self.voucher_date = QDateEdit(QDate.currentDate())
        self.order_date.setCalendarPopup(True)
        self.order_end_date.setCalendarPopup(True)
        self.voucher_date.setCalendarPopup(True)
        self.order_date.setFixedWidth(112)
        self.order_end_date.setFixedWidth(112)
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
        self.summary_initial_total = QLabel("0원")
        self.summary_final_total = QLabel("0원")
        self.summary_total = QLabel("0원")
        self.summary_shipping = QLabel("0원")
        self.summary_difference = QLabel("0원")
        self.lines_table = QTableWidget(0, 7)
        self.pivot_table = QTableWidget(0, 5)
        self.issues_table = QTableWidget(0, 6)
        self.shipping_table = QTableWidget(0, 7)
        self.db_item_search = QLineEdit()
        self.db_rule_search = QLineEdit()
        self.result_filter_column = QComboBox()
        self.db_item_filter_column = QComboBox()
        self.db_rule_filter_column = QComboBox()
        self.table_filter_controls: list[tuple[QTableWidget, QComboBox, QLineEdit]] = []
        self.db_items_table = QTableWidget(0, 3)
        self.db_rules_table = QTableWidget(0, 7)
        self.export_button = QPushButton("이카운트 Excel 저장")
        self.export_button.setEnabled(False)
        self.ecount_api_button = QPushButton("이카운트 판매전표 입력")
        self.ecount_api_button.setEnabled(False)
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
        browse_button = QPushButton("스마트스토어 전표")
        browse_button.clicked.connect(self.choose_file)
        sellmate_button = QPushButton("폐쇄몰·외부 판매처 전표")
        sellmate_button.clicked.connect(self.choose_sellmate_file)
        analyze_button = QPushButton("분석 및 자동 매칭")
        analyze_button.setObjectName("primary")
        analyze_button.clicked.connect(self.analyze)
        options_layout.addWidget(QLabel("입력 유형"), 0, 0)
        options_layout.addWidget(browse_button, 0, 1, 1, 2)
        options_layout.addWidget(sellmate_button, 0, 3, 1, 3)
        options_layout.addWidget(self.source_mode_label, 0, 6, 1, 5)
        options_layout.addWidget(QLabel("선택 파일"), 1, 0)
        options_layout.addWidget(self.file_path, 1, 1, 1, 10)
        options_layout.addWidget(QLabel("추가 파일"), 2, 0)
        options_layout.addWidget(self.confirmed_file_path, 2, 1, 1, 4)
        options_layout.addWidget(QLabel("전표 일자"), 2, 5)
        options_layout.addWidget(self.voucher_date, 2, 6)
        options_layout.addWidget(QLabel("담당자"), 2, 7)
        options_layout.addWidget(self.manager_code, 2, 8)
        options_layout.addWidget(QLabel("기본 창고"), 2, 9)
        options_layout.addWidget(self.default_warehouse, 2, 10)
        options_layout.addWidget(analyze_button, 3, 1, 1, 10)
        options_layout.setColumnStretch(1, 1)
        options_layout.setColumnStretch(3, 1)
        options.setMaximumHeight(172)
        layout.addWidget(options)

        cards = QHBoxLayout()
        for label, widget, color in (
            ("대상 주문행", self.summary_orders, "#1D4ED8"),
            ("전표 품목행", self.summary_lines, "#047857"),
            ("확인 필요", self.summary_issues, "#B45309"),
            ("합산 최초 상품금액", self.summary_initial_total, "#334155"),
            ("합산 최종 상품금액", self.summary_final_total, "#0F766E"),
            ("전표 총액(배송비 포함)", self.summary_total, "#0F172A"),
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
        self.lines_table.setSortingEnabled(True)
        self.lines_table.horizontalHeader().setToolTip(
            "품목코드 또는 품목명 제목을 클릭하면 오름차순·내림차순으로 정렬됩니다."
        )
        self.lines_table.cellDoubleClicked.connect(self.open_result_order_details)
        result_tab = QWidget()
        result_layout = QVBoxLayout(result_tab)
        result_layout.setContentsMargins(5, 5, 5, 5)
        result_layout.setSpacing(5)
        result_search_layout = QHBoxLayout()
        result_search_layout.addWidget(QLabel("결과 검색"))
        self.result_filter_column.addItems(
            ["전체 열", "품목코드", "품목명", "수량", "단가", "금액", "창고", "주문건수", "주문번호"]
        )
        self.result_filter_column.currentIndexChanged.connect(
            lambda: self.filter_result_lines(self.result_search.text())
        )
        result_search_layout.addWidget(self.result_filter_column)
        self.result_search = QLineEdit()
        self.result_search.setPlaceholderText("품목코드·품목명·주문번호의 일부를 입력하세요")
        self.result_search.setClearButtonEnabled(True)
        self.result_search.textChanged.connect(self.filter_result_lines)
        result_search_layout.addWidget(self.result_search, 1)
        self.result_filter_count = QLabel("전체 표시")
        self.result_filter_count.setStyleSheet("color:#526D82;")
        result_search_layout.addWidget(self.result_filter_count)
        result_layout.addLayout(result_search_layout)
        result_layout.addWidget(self.lines_table, 1)
        tabs.addTab(result_tab, "자동 변환 결과")

        self.pivot_table.setHorizontalHeaderLabels(["품목코드", "품목명", "합계 수량", "평균 단가", "합계 금액"])
        self.pivot_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.pivot_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for column in range(2, 5):
            self.pivot_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.pivot_table.setAlternatingRowColors(True)
        self.pivot_table.setSortingEnabled(True)
        self.pivot_table.cellDoubleClicked.connect(self.open_pivot_order_details)
        pivot_tab = QWidget()
        pivot_layout = QVBoxLayout(pivot_tab)
        pivot_layout.setContentsMargins(5, 5, 5, 5)
        self._add_column_filter(pivot_layout, self.pivot_table, ["품목코드", "품목명", "합계 수량", "평균 단가", "합계 금액"])
        pivot_layout.addWidget(self.pivot_table)
        tabs.addTab(pivot_tab, "품목 집계")

        self.issues_table.setColumnCount(7)
        self.issues_table.setHorizontalHeaderLabels(["입력파일", "원본행", "주문번호", "상품명", "옵션", "금액", "확인 사유"])
        self.issues_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.issues_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.issues_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.issues_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.issues_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.issues_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.issues_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.Stretch)
        self.issues_table.cellDoubleClicked.connect(self.open_set_mapping_dialog)
        self.issues_table.setToolTip("확인 필요 항목을 더블클릭하면 Supabase 세트 품목과 금액을 연결할 수 있습니다.")
        issues_tab = QWidget()
        issues_layout = QVBoxLayout(issues_tab)
        issues_layout.setContentsMargins(5, 5, 5, 5)
        self._add_column_filter(
            issues_layout,
            self.issues_table,
            ["입력파일", "원본행", "주문번호", "상품명", "옵션", "금액", "확인 사유"],
        )
        issues_layout.addWidget(self.issues_table)
        tabs.addTab(issues_tab, "확인 필요")

        self.shipping_table.setHorizontalHeaderLabels(
            ["원본행", "배송비 묶음번호", "원배송비", "추가배송비", "할인액(참고)", "전표 배송비", "구분"]
        )
        self.shipping_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.shipping_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for column in range(2, 7):
            self.shipping_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.shipping_table.setAlternatingRowColors(True)
        shipping_tab = QWidget()
        shipping_layout = QVBoxLayout(shipping_tab)
        shipping_layout.setContentsMargins(5, 5, 5, 5)
        self._add_column_filter(
            shipping_layout,
            self.shipping_table,
            ["원본행", "배송비 묶음번호", "원배송비", "추가배송비", "할인액", "전표 배송비", "구분"],
        )
        shipping_layout.addWidget(self.shipping_table)
        tabs.addTab(shipping_tab, "배송비 검수")

        db_tab = QWidget()
        db_layout = QVBoxLayout(db_tab)
        db_layout.setContentsMargins(5, 5, 5, 5)
        db_guide = QLabel(
            "DB 데이터는 누구나 조회할 수 있으며, 등록·수정은 Supabase 관리자 로그인 후 가능합니다."
        )
        db_guide.setStyleSheet("color:#526D82;")
        db_layout.addWidget(db_guide)
        db_subtabs = QTabWidget()

        item_tab = QWidget()
        item_layout = QVBoxLayout(item_tab)
        item_controls = QHBoxLayout()
        self.db_item_search.setPlaceholderText("품목코드 또는 품목명 검색")
        self.db_item_search.setClearButtonEnabled(True)
        self.db_item_search.textChanged.connect(self.filter_db_items)
        self.db_item_filter_column.addItems(["전체 열", "품목코드", "품목명", "상태"])
        self.db_item_filter_column.currentIndexChanged.connect(
            lambda: self.filter_db_items(self.db_item_search.text())
        )
        item_controls.addWidget(self.db_item_filter_column)
        item_controls.addWidget(self.db_item_search, 1)
        add_item_button = QPushButton("품목 등록")
        edit_item_button = QPushButton("선택 품목 수정")
        delete_item_button = QPushButton("선택 품목 로컬 삭제")
        deleted_items_button = QPushButton("삭제 품목·복구")
        add_item_button.clicked.connect(self.add_db_item)
        edit_item_button.clicked.connect(self.edit_db_item)
        delete_item_button.clicked.connect(self.delete_local_db_items)
        deleted_items_button.clicked.connect(self.open_deleted_items)
        item_controls.addWidget(add_item_button)
        item_controls.addWidget(edit_item_button)
        item_controls.addWidget(delete_item_button)
        item_controls.addWidget(deleted_items_button)
        item_layout.addLayout(item_controls)
        self.db_items_table.setHorizontalHeaderLabels(["품목코드", "품목명", "상태"])
        self.db_items_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.db_items_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.db_items_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.db_items_table.setSortingEnabled(True)
        self.db_items_table.setAlternatingRowColors(True)
        self.db_items_table.doubleClicked.connect(self.edit_db_item)
        item_layout.addWidget(self.db_items_table)
        db_subtabs.addTab(item_tab, "품목")

        rule_tab = QWidget()
        rule_layout = QVBoxLayout(rule_tab)
        rule_controls = QHBoxLayout()
        self.db_rule_search.setPlaceholderText("상품명·옵션·품목코드·품목명 검색")
        self.db_rule_search.setClearButtonEnabled(True)
        self.db_rule_search.textChanged.connect(self.filter_db_rules)
        self.db_rule_filter_column.addItems(
            ["전체 열", "상품명", "옵션", "총금액", "구분", "구성품·배분금액", "상태"]
        )
        self.db_rule_filter_column.currentIndexChanged.connect(
            lambda: self.filter_db_rules(self.db_rule_search.text())
        )
        rule_controls.addWidget(self.db_rule_filter_column)
        rule_controls.addWidget(self.db_rule_search, 1)
        add_rule_button = QPushButton("매칭·가격 등록")
        edit_rule_button = QPushButton("선택 규칙 수정")
        add_rule_button.clicked.connect(self.add_db_rule)
        edit_rule_button.clicked.connect(self.edit_db_rule)
        rule_controls.addWidget(add_rule_button)
        rule_controls.addWidget(edit_rule_button)
        rule_layout.addLayout(rule_controls)
        self.db_rules_table.setHorizontalHeaderLabels(
            ["상품명", "옵션", "총금액", "구분", "구성품·배분금액", "상태", "규칙키"]
        )
        self.db_rules_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.db_rules_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.db_rules_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.db_rules_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.db_rules_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.db_rules_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.db_rules_table.setColumnHidden(6, True)
        self.db_rules_table.setSortingEnabled(True)
        self.db_rules_table.setAlternatingRowColors(True)
        self.db_rules_table.doubleClicked.connect(self.edit_db_rule)
        rule_layout.addWidget(self.db_rules_table)
        db_subtabs.addTab(rule_tab, "상품 매칭·가격")

        db_layout.addWidget(db_subtabs)
        tabs.addTab(db_tab, "DB 관리")
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
        special_select_button = QPushButton("본사출고 품목 지정·수정")
        headquarters_view_button = QPushButton("본사창고 출고")
        wekeep_view_button = QPushButton("위킵창고 출고")
        special_select_button.clicked.connect(self.select_special_items)
        headquarters_view_button.clicked.connect(self.open_headquarters_lines)
        wekeep_view_button.clicked.connect(self.open_wekeep_lines)
        bottom.addWidget(special_select_button)
        bottom.addWidget(headquarters_view_button)
        bottom.addWidget(wekeep_view_button)
        bottom.addStretch()
        self.ecount_api_button.clicked.connect(self.open_ecount_sales_api)
        bottom.addWidget(self.ecount_api_button)
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

    def _add_column_filter(
        self,
        layout: QVBoxLayout,
        table: QTableWidget,
        headers: list[str],
    ) -> None:
        controls = QHBoxLayout()
        controls.addWidget(QLabel("열 필터"))
        column = QComboBox()
        column.addItem("전체 열")
        column.addItems(headers)
        keyword = QLineEdit()
        keyword.setPlaceholderText("선택한 열에서 찾을 값을 입력하세요")
        keyword.setClearButtonEnabled(True)
        controls.addWidget(column)
        controls.addWidget(keyword, 1)
        keyword.textChanged.connect(lambda: self._filter_table(table, column, keyword))
        column.currentIndexChanged.connect(lambda: self._filter_table(table, column, keyword))
        layout.addLayout(controls)
        self.table_filter_controls.append((table, column, keyword))

    def _filter_table(
        self,
        table: QTableWidget,
        column_selector: QComboBox,
        keyword_input: QLineEdit,
    ) -> None:
        keyword = keyword_input.text().strip().casefold()
        selected = column_selector.currentIndex() - 1
        for row in range(table.rowCount()):
            columns = range(table.columnCount()) if selected < 0 else (selected,)
            searchable = " ".join(
                table.item(row, column).text()
                for column in columns
                if table.item(row, column) is not None
            ).casefold()
            table.setRowHidden(row, bool(keyword and keyword not in searchable))

    def _reapply_table_filters(self, table: QTableWidget) -> None:
        for target, column, keyword in self.table_filter_controls:
            if target is table:
                self._filter_table(target, column, keyword)

    def _load_local_catalog(self) -> None:
        try:
            self.base_catalog = ReferenceCatalog.from_csv_dir(LOCAL_DATA_DIR)
            self.catalog = catalog_without_items(self.base_catalog, self.deleted_item_codes)
            self.db_status.setText("로컬 기준 DB 사용 중 (Supabase 연결 가능)")
            self.db_status.setStyleSheet("color:#047857;")
            self._refresh_db_management()
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
        self.base_catalog = catalog
        self.catalog = catalog_without_items(catalog, self.deleted_item_codes)
        self._refresh_db_management()
        write_db_log("프로그램에 Supabase DB 연결 적용 완료")
        self.db_status.setText("Supabase + 내장 최신 DB 연결 완료")
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
        dialog = SmartStoreSourceDialog(
            self.file_path.text() if self.source_mode == "smartstore" else "",
            self.confirmed_file_path.text() if self.source_mode == "smartstore" else "",
            self.order_date.date(),
            self.order_end_date.date(),
            self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        if not dialog.source_path.text().strip():
            QMessageBox.information(self, "원본 파일", "스마트스토어 원본 Excel 파일을 선택해주세요.")
            return
        self.source_mode = "smartstore"
        self.file_path.setText(dialog.source_path.text())
        self.confirmed_file_path.setText(dialog.confirmed_path.text())
        self.order_date.setDate(dialog.start_date.date())
        self.order_end_date.setDate(dialog.end_date.date())
        self.source_mode_label.setText(
            f"스마트스토어 · {dialog.start_date.date().toString('yyyy-MM-dd')} ~ "
            f"{dialog.end_date.date().toString('yyyy-MM-dd')}"
        )
        self.source_mode_label.setStyleSheet("color:#1D4ED8;font-weight:700;")

    def choose_sellmate_file(self) -> None:
        dialog = SellmateSourceDialog(
            self.file_path.text() if self.source_mode == "sellmate" else "",
            self.voucher_date.date(),
            self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        if not dialog.source_path.text().strip():
            QMessageBox.information(self, "원본 파일", "셀메이트 원본 Excel 파일을 선택해주세요.")
            return
        self.source_mode = "sellmate"
        self.file_path.setText(dialog.source_path.text())
        self.confirmed_file_path.clear()
        self.voucher_date.setDate(dialog.voucher_date.date())
        self.source_mode_label.setText(
            f"폐쇄몰·외부 판매처 · 전표일 {dialog.voucher_date.date().toString('yyyy-MM-dd')}"
        )
        self.source_mode_label.setStyleSheet("color:#7C3AED;font-weight:700;")

    def choose_confirmed_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "스마트스토어 구매확정 파일 선택",
            str(Path.home()),
            "Excel 파일 (*.xlsx *.xlsm)",
        )
        if path:
            self.confirmed_file_path.setText(path)

    def analyze(self) -> None:
        if self.catalog is None:
            QMessageBox.warning(self, "DB 없음", "기준 DB를 준비하거나 Supabase에 연결해주세요.")
            return
        source = Path(self.file_path.text().strip())
        if not source.exists():
            QMessageBox.information(self, "원본 파일", "입력 유형 버튼에서 원본 Excel 파일을 선택해주세요.")
            return
        start_date = self.order_date.date().toPython()
        end_date = self.order_end_date.date().toPython()
        if end_date < start_date:
            QMessageBox.warning(self, "주문 기간", "주문 종료일은 시작일보다 빠를 수 없습니다.")
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            if self.source_mode == "sellmate":
                original_orders = read_sellmate_orders(source, self.voucher_date.date().toPython())
            else:
                original_orders = read_smartstore_orders_range(source, start_date, end_date)
            if not original_orders:
                QMessageBox.information(
                    self,
                    "대상 없음",
                    "셀메이트 주문을 찾지 못했습니다."
                    if self.source_mode == "sellmate"
                    else f"{start_date:%Y-%m-%d} ~ {end_date:%Y-%m-%d} 결제 주문을 찾지 못했습니다.",
                )
                return
            confirmed_orders = []
            confirmed_path_text = (
                self.confirmed_file_path.text().strip()
                if self.source_mode == "smartstore"
                else ""
            )
            if confirmed_path_text:
                confirmed_source = Path(confirmed_path_text)
                if not confirmed_source.exists():
                    raise ValueError("선택한 구매확정 Excel 파일을 찾을 수 없습니다.")
                confirmed_orders = read_purchase_confirmed_orders(confirmed_source)
            orders = combine_order_sources(original_orders, confirmed_orders)
            self.current_result = convert_orders(orders, self.catalog, default_warehouse=str(self.default_warehouse.value()))
            self._apply_special_warehouses()
            self._show_result(self.current_result)
            self.export_button.setEnabled(bool(self.current_result.lines))
            self.ecount_api_button.setEnabled(bool(self.current_result.lines))
        except Exception as exc:
            QMessageBox.critical(self, "분석 실패", str(exc))
        finally:
            QApplication.restoreOverrideCursor()

    def _show_result(self, result: ConversionResult) -> None:
        self.summary_orders.setText(f"{len(result.orders):,}")
        self.summary_lines.setText(f"{len(result.lines):,}")
        self.summary_issues.setText(f"{len(result.issues):,}")
        self.summary_initial_total.setText(f"{result.initial_item_total:,.0f}원")
        self.summary_final_total.setText(f"{result.final_item_total:,.0f}원")
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
                item = (
                    NumericTableWidgetItem(Decimal(value.replace(",", "")))
                    if column in {2, 3, 4, 6}
                    else QTableWidgetItem(value)
                )
                item.setData(Qt.UserRole, row_index)
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
        self.lines_table.setSortingEnabled(True)
        self.filter_result_lines(self.result_search.text())
        self._show_pivot_result(result)

        self.issues_table.setRowCount(len(result.issues))
        for row_index, issue in enumerate(result.issues):
            values = [
                issue.source_type,
                issue.source_row,
                issue.order_no,
                issue.product_name,
                issue.options,
                f"{issue.amount:,.0f}",
                issue.reason,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if column == 6:
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
        self._reapply_table_filters(self.issues_table)
        self._reapply_table_filters(self.shipping_table)

    def filter_result_lines(self, text: str) -> None:
        keyword = (text or "").strip()
        selected = self.result_filter_column.currentIndex() - 1
        order_number_filter = selected == self.lines_table.columnCount()
        visible = 0
        for row in range(self.lines_table.rowCount()):
            order_numbers = ""
            index_item = self.lines_table.item(row, 0)
            original_index = index_item.data(Qt.UserRole) if index_item is not None else None
            if (
                self.current_result is not None
                and original_index is not None
                and 0 <= int(original_index) < len(self.current_result.lines)
            ):
                order_numbers = " ".join(self.current_result.lines[int(original_index)].source_orders)

            if order_number_filter:
                searchable = order_numbers
            else:
                columns = range(self.lines_table.columnCount()) if selected < 0 else (selected,)
                searchable = " ".join(
                    self.lines_table.item(row, column).text()
                    for column in columns
                    if self.lines_table.item(row, column) is not None
                )
                if selected < 0:
                    searchable = f"{searchable} {order_numbers}"
            show = search_text_matches(keyword, searchable)
            self.lines_table.setRowHidden(row, not show)
            if show:
                visible += 1
        if keyword:
            self.result_filter_count.setText(f"{visible:,}/{self.lines_table.rowCount():,}행")
        else:
            self.result_filter_count.setText(f"전체 {self.lines_table.rowCount():,}행")

    def _show_pivot_result(self, result: ConversionResult) -> None:
        grouped: dict[tuple[str, str], dict[str, Decimal]] = {}
        for line in result.lines:
            key = (line.item_code, line.item_name)
            values = grouped.setdefault(key, {"quantity": Decimal("0"), "amount": Decimal("0")})
            values["quantity"] += line.quantity
            values["amount"] += line.total
        self.pivot_table.setSortingEnabled(False)
        self.pivot_table.setRowCount(len(grouped))
        for row, ((item_code, item_name), values) in enumerate(sorted(grouped.items())):
            quantity = values["quantity"]
            amount = values["amount"]
            average = amount / quantity if quantity else Decimal("0")
            texts = [
                item_code,
                item_name,
                f"{quantity:,.0f}",
                f"{average:,.0f}",
                f"{amount:,.0f}",
            ]
            for column, text in enumerate(texts):
                item = (
                    NumericTableWidgetItem([quantity, average, amount][column - 2])
                    if column >= 2
                    else QTableWidgetItem(text)
                )
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if column >= 2:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.pivot_table.setItem(row, column, item)
        self.pivot_table.setSortingEnabled(True)
        self._reapply_table_filters(self.pivot_table)

    def open_result_order_details(self, table_row: int, _column: int = 0) -> None:
        if self.current_result is None or self.catalog is None:
            return
        index_item = self.lines_table.item(table_row, 0)
        if index_item is None:
            return
        original_index = index_item.data(Qt.UserRole)
        if original_index is None or not (0 <= int(original_index) < len(self.current_result.lines)):
            return
        line = self.current_result.lines[int(original_index)]
        self._open_item_order_details(
            line.item_code,
            line.item_name,
            line.unit_price,
            set(line.source_orders),
        )

    def open_pivot_order_details(self, table_row: int, _column: int = 0) -> None:
        code_item = self.pivot_table.item(table_row, 0)
        name_item = self.pivot_table.item(table_row, 1)
        if code_item is None or name_item is None:
            return
        source_orders = {
            order_no
            for line in self.current_result.lines
            if line.item_code == code_item.text()
            for order_no in line.source_orders
        } if self.current_result is not None else set()
        self._open_item_order_details(code_item.text(), name_item.text(), None, source_orders)

    def _open_item_order_details(
        self,
        item_code: str,
        item_name: str,
        unit_price: Decimal | None,
        source_order_nos: set[str] | None = None,
    ) -> None:
        if self.current_result is None or self.catalog is None:
            return
        if item_code == "택배운송비":
            QMessageBox.information(
                self,
                "배송비 상세",
                "배송비 주문별 정보는 배송비 검수 탭에서 묶음번호 기준으로 확인해주세요.",
            )
            return
        relevant_orders = (
            [order for order in self.current_result.orders if order.order_no in source_order_nos]
            if source_order_nos
            else self.current_result.orders
        )
        details = build_item_order_details(
            relevant_orders,
            self.catalog,
            item_code,
            unit_price,
            str(self.default_warehouse.value()),
        )
        for detail in details:
            if item_code in self.special_item_codes or "QM4100" in f"{item_code} {item_name}".upper():
                detail.warehouse = "100"
        if not details:
            QMessageBox.information(self, "주문 상세", "연결된 원본 주문을 찾지 못했습니다.")
            return
        ItemOrderDetailsDialog(item_code, item_name, details, self).exec()

    def _refresh_db_management(self) -> None:
        if self.catalog is None:
            return
        item_rows = sorted(
            self.catalog.item_rows,
            key=lambda row: str(row.get("item_code") or "").casefold(),
        )
        self.db_items_table.setSortingEnabled(False)
        self.db_items_table.setRowCount(len(item_rows))
        for row_index, row in enumerate(item_rows):
            values = [
                str(row.get("item_code") or ""),
                str(
                    row.get("representative_name")
                    or row.get("item_name")
                    or row.get("standard_name")
                    or ""
                ),
                str(row.get("review_status") or "confirmed"),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.db_items_table.setItem(row_index, column, item)
        self.db_items_table.setSortingEnabled(True)

        price_components: dict[str, list[dict]] = {}
        for component in self.catalog.price_component_rows:
            price_components.setdefault(str(component.get("price_rule_key") or ""), []).append(component)
        rule_rows = sorted(
            self.catalog.price_rule_rows,
            key=lambda row: (
                str(row.get("source_product_name") or "").casefold(),
                Decimal(str(row.get("total_unit_price") or 0)),
            ),
        )
        self.db_rules_table.setSortingEnabled(False)
        self.db_rules_table.setRowCount(len(rule_rows))
        for row_index, row in enumerate(rule_rows):
            rule_key = str(row.get("price_rule_key") or "")
            components = sorted(
                price_components.get(rule_key, []),
                key=lambda component: int(component.get("sequence") or 0),
            )
            component_text = " / ".join(
                f"{component.get('item_code', '')} × {Decimal(str(component.get('quantity') or 1)):g}"
                f" @ {Decimal(str(component.get('allocated_unit_price') or 0)):,.0f}원"
                for component in components
            )
            values = [
                str(row.get("source_product_name") or ""),
                str(row.get("source_options") or ""),
                f"{Decimal(str(row.get('total_unit_price') or 0)):,.0f}",
                str(row.get("item_type") or ""),
                component_text,
                str(row.get("review_status") or "confirmed"),
                rule_key,
            ]
            for column, value in enumerate(values):
                item = (
                    NumericTableWidgetItem(Decimal(str(row.get("total_unit_price") or 0)))
                    if column == 2
                    else QTableWidgetItem(value)
                )
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if column == 2:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.db_rules_table.setItem(row_index, column, item)
        self.db_rules_table.setSortingEnabled(True)

        self.filter_db_items(self.db_item_search.text())
        self.filter_db_rules(self.db_rule_search.text())

    def filter_db_items(self, text: str) -> None:
        keyword = (text or "").strip().casefold()
        selected = self.db_item_filter_column.currentIndex() - 1
        for row in range(self.db_items_table.rowCount()):
            columns = range(self.db_items_table.columnCount()) if selected < 0 else (selected,)
            searchable = " ".join(
                self.db_items_table.item(row, column).text()
                for column in columns
                if self.db_items_table.item(row, column) is not None
            ).casefold()
            self.db_items_table.setRowHidden(row, bool(keyword and keyword not in searchable))

    def filter_db_rules(self, text: str) -> None:
        keyword = (text or "").strip().casefold()
        selected = self.db_rule_filter_column.currentIndex() - 1
        for row in range(self.db_rules_table.rowCount()):
            columns = range(self.db_rules_table.columnCount()) if selected < 0 else (selected,)
            searchable = " ".join(
                self.db_rules_table.item(row, column).text()
                for column in columns
                if self.db_rules_table.item(row, column) is not None
            ).casefold()
            self.db_rules_table.setRowHidden(row, bool(keyword and keyword not in searchable))

    def _require_supabase(self) -> bool:
        if self.supabase_client is not None:
            return True
        QMessageBox.information(
            self,
            "DB 로그인 필요",
            "조회는 내장 DB로 가능하지만 등록·수정은 Supabase 관리자 로그인 후 가능합니다.",
        )
        return False

    def _apply_special_warehouses(self) -> None:
        if self.current_result is None:
            return
        for line in self.current_result.lines:
            is_qm4100 = "QM4100" in f"{line.item_code} {line.item_name}".upper()
            line.warehouse = (
                "100"
                if line.item_code in self.special_item_codes or is_qm4100
                else str(self.default_warehouse.value())
            )

    def select_special_items(self) -> None:
        source_catalog = self.base_catalog or self.catalog
        if source_catalog is None:
            return
        available_rows = [
            row for row in source_catalog.item_rows
            if str(row.get("item_code") or "") not in self.deleted_item_codes
        ]
        dialog = ItemChecklistDialog(
            "본사출고 특수 품목 지정",
            available_rows,
            self.special_item_codes,
            "본사출고 품목 적용",
            self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        self.special_item_codes = dialog.checked_codes()
        try:
            save_code_set(SPECIAL_ITEMS_PATH, self.special_item_codes)
        except Exception as exc:
            QMessageBox.critical(self, "설정 저장 실패", str(exc))
            return
        self._apply_special_warehouses()
        if self.current_result is not None:
            self._show_result(self.current_result)
        QMessageBox.information(
            self,
            "본사출고 품목 적용",
            f"{len(self.special_item_codes):,}개 품목을 본사창고(100) 출고 대상으로 지정했습니다.",
        )

    def open_headquarters_lines(self) -> None:
        if self.current_result is None:
            QMessageBox.information(self, "분석 필요", "판매처 원본 파일을 먼저 분석해주세요.")
            return
        lines = [
            line for line in self.current_result.lines
            if str(line.warehouse) == "100"
        ]
        dialog = WarehouseLinesDialog(
            lines,
            self.voucher_date.date().toPython(),
            self.manager_code.text().strip() or "00109",
            "100",
            "본사창고",
            True,
            self,
        )
        dialog.exec()
        if dialog.removed_codes:
            self.special_item_codes.difference_update(dialog.removed_codes)
            try:
                save_code_set(SPECIAL_ITEMS_PATH, self.special_item_codes)
            except Exception as exc:
                QMessageBox.critical(self, "설정 저장 실패", str(exc))
                return
            self._apply_special_warehouses()
            self._show_result(self.current_result)
            self.db_status.setText(
                f"본사출고 품목 {len(dialog.removed_codes):,}개 해제 · 기본창고 복원"
            )
            self.db_status.setStyleSheet("color:#047857;font-weight:600;")

    def open_wekeep_lines(self) -> None:
        if self.current_result is None:
            QMessageBox.information(self, "분석 필요", "판매처 원본 파일을 먼저 분석해주세요.")
            return
        warehouse_code = str(self.default_warehouse.value())
        lines = [
            line for line in self.current_result.lines
            if str(line.warehouse) == warehouse_code
        ]
        WarehouseLinesDialog(
            lines,
            self.voucher_date.date().toPython(),
            self.manager_code.text().strip() or "00109",
            warehouse_code,
            "위킵창고",
            False,
            self,
        ).exec()

    def delete_local_db_items(self) -> None:
        source_catalog = self.base_catalog or self.catalog
        if source_catalog is None:
            return
        selected_rows = sorted({index.row() for index in self.db_items_table.selectedIndexes()})
        if not selected_rows and self.db_items_table.currentRow() >= 0:
            selected_rows = [self.db_items_table.currentRow()]
        codes = {
            self.db_items_table.item(row, 0).text()
            for row in selected_rows
            if self.db_items_table.item(row, 0) is not None
        }
        if not codes:
            QMessageBox.information(self, "선택 필요", "로컬 DB에서 숨길 품목을 선택해주세요.")
            return
        answer = QMessageBox.question(
            self,
            "로컬 품목 삭제",
            f"{len(codes):,}개 품목을 이 프로그램의 DB에서 삭제할까요?\n"
            "Supabase 원본 데이터는 삭제되지 않으며 언제든 복구할 수 있습니다.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.deleted_item_codes.update(codes)
        save_code_set(DELETED_ITEMS_PATH, self.deleted_item_codes)
        self.special_item_codes.difference_update(codes)
        save_code_set(SPECIAL_ITEMS_PATH, self.special_item_codes)
        self.catalog = catalog_without_items(source_catalog, self.deleted_item_codes)
        self._refresh_db_management()
        self.db_status.setText(f"로컬 DB 품목 {len(codes):,}개 삭제 · Supabase 유지")
        self.db_status.setStyleSheet("color:#B45309;font-weight:600;")

    def open_deleted_items(self) -> None:
        source_catalog = self.base_catalog or self.catalog
        if source_catalog is None:
            return
        deleted_rows = [
            row for row in source_catalog.item_rows
            if str(row.get("item_code") or "") in self.deleted_item_codes
        ]
        dialog = ItemChecklistDialog(
            "삭제 DB 관리 - 삭제 품목",
            deleted_rows,
            set(),
            "선택 품목 복구",
            self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        restored = dialog.checked_codes()
        if not restored:
            QMessageBox.information(self, "복구 선택", "복구할 품목의 체크박스를 선택해주세요.")
            return
        self.deleted_item_codes.difference_update(restored)
        save_code_set(DELETED_ITEMS_PATH, self.deleted_item_codes)
        self.catalog = catalog_without_items(source_catalog, self.deleted_item_codes)
        self._refresh_db_management()
        self.db_status.setText(f"로컬 DB 품목 {len(restored):,}개 복구 완료")
        self.db_status.setStyleSheet("color:#047857;font-weight:600;")

    def add_db_item(self) -> None:
        if not self._require_supabase() or self.catalog is None:
            return
        dialog = ItemEditDialog(parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        code, name = dialog.values()
        if code.casefold() in {str(key).casefold() for key in self.catalog.items}:
            QMessageBox.warning(self, "중복 품목", f"품목코드 {code}는 이미 등록되어 있습니다.")
            return
        normalized_name = normalize_source(name)
        duplicates = [
            row for row in self.catalog.item_rows
            if normalize_source(str(row.get("representative_name") or row.get("item_name") or "")) == normalized_name
        ]
        if duplicates:
            QMessageBox.warning(
                self,
                "중복 품목명",
                f"같은 품목명이 이미 {duplicates[0].get('item_code', '')} 품목으로 등록되어 있습니다.",
            )
            return
        self._upsert_db_item(code, name)

    def edit_db_item(self, *_args) -> None:
        if not self._require_supabase() or self.catalog is None:
            return
        row = self.db_items_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "선택 필요", "수정할 품목을 선택해주세요.")
            return
        code = self.db_items_table.item(row, 0).text()
        name = self.db_items_table.item(row, 1).text()
        dialog = ItemEditDialog(code, name, self)
        if dialog.exec() != QDialog.Accepted:
            return
        _, new_name = dialog.values()
        normalized_name = normalize_source(new_name)
        duplicate = next(
            (
                item for item in self.catalog.item_rows
                if str(item.get("item_code") or "") != code
                and normalize_source(str(item.get("representative_name") or item.get("item_name") or "")) == normalized_name
            ),
            None,
        )
        if duplicate:
            QMessageBox.warning(
                self, "중복 품목명",
                f"같은 품목명이 이미 {duplicate.get('item_code', '')} 품목으로 등록되어 있습니다.",
            )
            return
        self._upsert_db_item(code, new_name)

    def _upsert_db_item(self, code: str, name: str) -> None:
        try:
            self.supabase_client.table("ecount_item_reference").upsert(
                {
                    "item_code": code,
                    "representative_name": name,
                    "review_status": "confirmed",
                    "is_active": True,
                },
                on_conflict="item_code",
            ).execute()
            self._reload_supabase_catalog()
            self.db_status.setText(f"품목 저장 완료: {code}")
        except Exception as exc:
            QMessageBox.critical(self, "품목 저장 실패", str(exc))

    def _rule_duplicate(
        self, product_name: str, options: str, amount: Decimal, exclude_key: str = ""
    ) -> dict | None:
        if self.catalog is None:
            return None
        normalized = normalize_source(f"{product_name}{options}")
        return next(
            (
                row for row in self.catalog.price_rule_rows
                if str(row.get("price_rule_key") or "") != exclude_key
                and str(row.get("source_channel") or "") == "리큐엠_스마트스토어"
                and str(row.get("normalized_source") or "") == normalized
                and Decimal(str(row.get("total_unit_price") or 0)) == amount
            ),
            None,
        )

    def add_db_rule(self) -> None:
        if not self._require_supabase() or self.catalog is None:
            return
        identity = RuleIdentityDialog(parent=self)
        if identity.exec() != QDialog.Accepted:
            return
        product, options, amount = identity.values()
        if self._rule_duplicate(product, options, amount):
            QMessageBox.warning(self, "중복 규칙", "같은 상품명·옵션·총금액 규칙이 이미 등록되어 있습니다.")
            return
        self._edit_rule_components(product, options, amount)

    def edit_db_rule(self, *_args) -> None:
        if not self._require_supabase() or self.catalog is None:
            return
        row_index = self.db_rules_table.currentRow()
        if row_index < 0:
            QMessageBox.information(self, "선택 필요", "수정할 매칭·가격 규칙을 선택해주세요.")
            return
        rule_key = self.db_rules_table.item(row_index, 6).text()
        rule = next(
            (row for row in self.catalog.price_rule_rows if str(row.get("price_rule_key") or "") == rule_key),
            None,
        )
        if rule is None:
            QMessageBox.warning(self, "규칙 확인", "선택한 규칙을 찾을 수 없습니다.")
            return
        identity = RuleIdentityDialog(
            str(rule.get("source_product_name") or ""),
            str(rule.get("source_options") or ""),
            Decimal(str(rule.get("total_unit_price") or 0)),
            self,
        )
        if identity.exec() != QDialog.Accepted:
            return
        product, options, amount = identity.values()
        if self._rule_duplicate(product, options, amount, rule_key):
            QMessageBox.warning(self, "중복 규칙", "같은 상품명·옵션·총금액 규칙이 이미 등록되어 있습니다.")
            return
        existing = [
            component for component in self.catalog.price_component_rows
            if str(component.get("price_rule_key") or "") == rule_key
        ]
        self._edit_rule_components(product, options, amount, rule_key, existing)

    def _edit_rule_components(
        self,
        product: str,
        options: str,
        amount: Decimal,
        price_rule_key: str = "",
        existing_components: list[dict] | None = None,
    ) -> None:
        normalized = normalize_source(f"{product}{options}")
        order = SmartStoreOrder(
            source_row=0,
            order_no="DB-MANUAL",
            product_order_no="DB-MANUAL",
            paid_at=datetime.now(),
            status="DB관리",
            product_name=product,
            options=options,
            quantity=Decimal("1"),
            item_total=amount,
        )
        dialog = SetMappingDialog(order, self.catalog.items, existing_components, self)
        if dialog.exec() != QDialog.Accepted:
            return
        components = dialog.components()
        mapping = next(
            (
                row for row in self.catalog.mapping_rows
                if str(row.get("source_channel") or "") == "리큐엠_스마트스토어"
                and str(row.get("normalized_source") or "") == normalized
            ),
            None,
        )
        mapping_key = (
            str(mapping.get("mapping_key") or "") if mapping
            else hashlib.sha256(f"manual|리큐엠_스마트스토어|{normalized}".encode("utf-8")).hexdigest()
        )
        rule_key = price_rule_key or hashlib.sha256(
            f"manual|리큐엠_스마트스토어|{normalized}|{amount}".encode("utf-8")
        ).hexdigest()
        try:
            self.supabase_client.table("ecount_product_mappings").upsert(
                {
                    "mapping_key": mapping_key,
                    "source_channel": "리큐엠_스마트스토어",
                    "source_product_text": f"{product}{options}",
                    "normalized_source": normalized,
                    "mapping_type": "set",
                    "component_count": len(components),
                    "source_row": 0,
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
                        "source_row": 0,
                    }
                    for sequence, component in enumerate(components, 1)
                ]
            ).execute()
            self.supabase_client.table("ecount_price_rules").upsert(
                {
                    "price_rule_key": rule_key,
                    "source_channel": "리큐엠_스마트스토어",
                    "source_product_name": product,
                    "source_options": options,
                    "normalized_source": normalized,
                    "total_unit_price": float(amount),
                    "item_type": "세트",
                    "main_product": components[0]["item_code"],
                    "set_name": options or product,
                    "component_count": len(components),
                    "allocated_total": float(amount),
                    "allocation_variance": 0,
                    "source_row": 0,
                    "review_status": "confirmed",
                    "is_active": True,
                },
                on_conflict="price_rule_key",
            ).execute()
            self.supabase_client.table("ecount_price_rule_components").delete().eq(
                "price_rule_key", rule_key
            ).execute()
            self.supabase_client.table("ecount_price_rule_components").insert(
                [
                    {
                        "price_rule_key": rule_key,
                        "sequence": sequence,
                        "component_alias": str(
                            self.catalog.items[component["item_code"]].get("representative_name")
                            or component["item_code"]
                        ),
                        "normalized_component_alias": normalize_source(
                            str(
                                self.catalog.items[component["item_code"]].get("representative_name")
                                or component["item_code"]
                            )
                        ),
                        "item_code": component["item_code"],
                        "quantity": float(component["quantity"]),
                        "allocated_unit_price": float(component["unit_price"]),
                        "source_row": 0,
                        "review_status": "confirmed",
                    }
                    for sequence, component in enumerate(components, 1)
                ]
            ).execute()
            self._reload_supabase_catalog()
            self.db_status.setText("상품 매칭·가격 규칙 저장 완료")
            self.db_status.setStyleSheet("color:#047857;font-weight:600;")
        except Exception as exc:
            QMessageBox.critical(self, "규칙 저장 실패", str(exc))

    def _apply_table_edits(self) -> None:
        assert self.current_result is not None
        updated: dict[int, VoucherLine] = {}
        for row in range(self.lines_table.rowCount()):
            index_item = self.lines_table.item(row, 0)
            original_index = int(index_item.data(Qt.UserRole))
            original = self.current_result.lines[original_index]
            quantity = Decimal(self.lines_table.item(row, 2).text().replace(",", ""))
            unit_price = Decimal(self.lines_table.item(row, 3).text().replace(",", ""))
            warehouse = self.lines_table.item(row, 5).text().strip()
            if quantity <= 0:
                raise ValueError(f"{row + 1}행 수량은 0보다 커야 합니다.")
            if unit_price < 0 or unit_price != unit_price.to_integral_value():
                raise ValueError(f"{row + 1}행 단가는 0 이상의 원 단위 정수여야 합니다.")
            if not warehouse:
                raise ValueError(f"{row + 1}행 창고가 비어 있습니다.")
            updated[original_index] = VoucherLine(
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
        self.current_result.lines = [updated[index] for index in range(len(self.current_result.lines))]

    def remove_selected_issues(self) -> None:
        if self.current_result is None or self.catalog is None:
            return
        selected_rows = sorted({index.row() for index in self.issues_table.selectedIndexes()})
        if not selected_rows:
            QMessageBox.information(self, "선택 필요", "분석에서 삭제할 확인 필요 항목을 선택해주세요.")
            return
        source_keys = {
            (self.current_result.issues[row].source_type, self.current_result.issues[row].source_row)
            for row in selected_rows
        }
        answer = QMessageBox.question(
            self,
            "분석 항목 삭제",
            f"선택한 {len(source_keys)}개 주문행을 이번 분석에서 제외할까요?\n입력 파일과 DB는 삭제되지 않습니다.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        remaining = [
            order
            for order in self.current_result.orders
            if (order.source_type, order.source_row) not in source_keys
        ]
        self.current_result = convert_orders(
            remaining,
            self.catalog,
            default_warehouse=str(self.default_warehouse.value()),
        )
        self._show_result(self.current_result)
        self.export_button.setEnabled(bool(self.current_result.lines))
        self.ecount_api_button.setEnabled(bool(self.current_result.lines))

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
            (
                row
                for row in self.current_result.orders
                if row.source_row == issue.source_row and row.source_type == issue.source_type
            ),
            None,
        )
        if order is None:
            return
        existing_mapping = self.catalog.mappings.get(
            (order.source_channel, order.normalized_source),
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
                f"{order.source_channel}|{order.normalized_source}".encode("utf-8")
            ).hexdigest()
        price_rule_key = hashlib.sha256(
            (
                f"{order.source_channel}|{order.normalized_source}|"
                f"{order.unit_total:.2f}"
            ).encode("utf-8")
        ).hexdigest()
        source_text = f"{order.product_name}{order.options}"
        try:
            self.db_status.setText("세트 매핑을 Supabase에 저장 중...")
            self.supabase_client.table("ecount_product_mappings").upsert(
                {
                    "mapping_key": mapping_key,
                    "source_channel": order.source_channel,
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
                    "source_channel": order.source_channel,
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
        self.base_catalog = ReferenceCatalog(*load_merged_reference_data(self.supabase_client))
        self.catalog = catalog_without_items(self.base_catalog, self.deleted_item_codes)
        self._refresh_db_management()

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
        order = next(
            (
                row
                for row in self.current_result.orders
                if row.source_row == issue.source_row and row.source_type == issue.source_type
            ),
            None,
        )
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
            f"{order.source_channel}|{order.normalized_source}".encode("utf-8")
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
                    "source_channel": order.source_channel,
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

    def open_ecount_sales_api(self) -> None:
        if self.current_result is None or not self.current_result.lines:
            QMessageBox.information(self, "분석 필요", "판매처 원본 파일을 먼저 분석해주세요.")
            return
        try:
            self._apply_table_edits()
        except Exception as exc:
            QMessageBox.warning(self, "수정값 오류", f"수량·단가·창고 값을 확인해주세요.\n{exc}")
            return
        if not self.current_result.is_reconciled:
            QMessageBox.critical(
                self,
                "API 전송 차단",
                f"금액 차이 {self.current_result.amount_difference:,.0f}원이 있어 이카운트에 전송할 수 없습니다.",
            )
            return
        review_lines = [line for line in self.current_result.lines if line.needs_review]
        if self.current_result.issues or review_lines:
            QMessageBox.critical(
                self,
                "API 전송 차단",
                f"확인 필요 항목 {max(len(self.current_result.issues), len(review_lines)):,}건을 먼저 처리해주세요.",
            )
            return
        invalid_lines = [
            line for line in self.current_result.lines
            if line.quantity <= 0 or line.unit_price < 0 or not str(line.warehouse).strip()
        ]
        if invalid_lines:
            QMessageBox.critical(
                self,
                "API 전송 차단",
                f"수량·단가·창고 값이 올바르지 않은 전표 행이 {len(invalid_lines):,}개 있습니다.",
            )
            return
        EcountSalesApiDialog(
            self.current_result.lines,
            self.voucher_date.date().toPython(),
            self.current_result.output_total,
            self.manager_code.text().strip() or "00109",
            self,
        ).exec()

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
        if self.source_mode == "sellmate":
            period = self.voucher_date.date().toString("yyyyMMdd")
        else:
            start_day = self.order_date.date().toString("yyyyMMdd")
            end_day = self.order_end_date.date().toString("yyyyMMdd")
            period = start_day if start_day == end_day else f"{start_day}~{end_day}"
        prefix = "셀메이트" if self.source_mode == "sellmate" else "네이버"
        suggested = Path(self.file_path.text()).with_name(f"{prefix}_이카운트_판매전표_{period}.xlsx")
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
