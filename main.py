import json
import sys
import hashlib
from pathlib import Path

from PySide6.QtCore import QDate, Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDateEdit,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from supabase import Client, create_client

from excel_loader import load_orders
from matcher import ProductMatcher
from matcher import compact
from shipping_export import export_wekep
from duty_free_loader import load_duty_free, match_barcodes
from ecount_transfer import EcountClient, aggregate_transfer_rows, match_transfer_rows, read_transfer_file


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
DEFAULT_CONFIG = {
    "supabase_url": "https://jcslohuraqclhryeqxoc.supabase.co",
    "supabase_publishable_key": "sb_publishable_dafbXHpLHVPDhsMwm_B5RA_LgCqlWeg",
    "ecount_com_code": "304293",
    "ecount_user_id": "ororamobile",
    "ecount_zone": "AB",
}
ADMIN_USER_ID = "c7937d51-1a14-47aa-987e-6254c6c79014"


class ItemManagerDialog(QDialog):
    """관리자 전용 표준 품목 관리 화면."""
    def __init__(self, client: Client, items: list[dict], parent=None):
        super().__init__(parent)
        self.client, self.items = client, items
        self.setWindowTitle("DB 품목 관리 · 관리자")
        self.resize(900, 560)
        self.search = QLineEdit()
        self.search.setPlaceholderText("품목코드 또는 품목명 검색")
        self.grid = QTableWidget(0, 6)
        self.grid.setHorizontalHeaderLabels(["품목코드", "표준 품목명", "모델", "색상", "형태", "사용"])
        self.grid.horizontalHeader().setStretchLastSection(True)
        add_btn, edit_btn, active_btn = QPushButton("신규 품목"), QPushButton("선택 수정"), QPushButton("사용/중지 전환")
        buttons = QHBoxLayout()
        for button in (add_btn, edit_btn, active_btn): buttons.addWidget(button)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("표준 품목(items) 관리 — 변경 내용은 Supabase에 즉시 저장됩니다."))
        layout.addWidget(self.search); layout.addWidget(self.grid); layout.addLayout(buttons)
        self.search.textChanged.connect(self.refresh)
        add_btn.clicked.connect(self.add_item); edit_btn.clicked.connect(self.edit_item); active_btn.clicked.connect(self.toggle_active)
        self.refresh()

    def refresh(self) -> None:
        word = self.search.text().strip().lower()
        rows = [r for r in self.items if word in f"{r.get('item_code','')} {r.get('standard_name','')}".lower()]
        self.grid.setRowCount(len(rows))
        self.grid_rows = rows
        for i, row in enumerate(rows):
            values = [row.get("item_code", ""), row.get("standard_name", ""), row.get("model", ""), row.get("color", ""), row.get("form", ""), "사용" if row.get("is_active", True) else "중지"]
            for j, value in enumerate(values): self.grid.setItem(i, j, QTableWidgetItem(str(value or "")))

    def selected(self) -> dict | None:
        row = self.grid.currentRow()
        return self.grid_rows[row] if 0 <= row < len(self.grid_rows) else None

    def ask_fields(self, original=None) -> dict | None:
        original = original or {}
        result = {}
        for key, label in (("item_code", "품목코드"), ("standard_name", "표준 품목명"), ("model", "모델"), ("color", "색상"), ("form", "형태")):
            value, ok = QInputDialog.getText(self, "품목 정보", label, text=str(original.get(key, "") or ""))
            if not ok: return None
            result[key] = value.strip()
        if not result["item_code"] or not result["standard_name"]:
            QMessageBox.warning(self, "필수값", "품목코드와 표준 품목명은 필수입니다."); return None
        result["is_active"] = original.get("is_active", True)
        result["review_status"] = original.get("review_status", "confirmed")
        return result

    def add_item(self) -> None:
        data = self.ask_fields()
        if data:
            try: self.client.table("items").insert(data).execute(); self.items.append(data); self.refresh()
            except Exception as exc: QMessageBox.critical(self, "저장 실패", str(exc))

    def edit_item(self) -> None:
        row = self.selected()
        if not row: QMessageBox.information(self, "선택", "수정할 품목을 선택하세요."); return
        data = self.ask_fields(row)
        if data:
            try:
                self.client.table("items").update(data).eq("item_code", row["item_code"]).execute(); row.update(data); self.refresh()
            except Exception as exc: QMessageBox.critical(self, "수정 실패", str(exc))

    def toggle_active(self) -> None:
        row = self.selected()
        if not row: return
        value = not row.get("is_active", True)
        try: self.client.table("items").update({"is_active": value}).eq("item_code", row["item_code"]).execute(); row["is_active"] = value; self.refresh()
        except Exception as exc: QMessageBox.critical(self, "변경 실패", str(exc))


class CorrectionDialog(QDialog):
    def __init__(self, order: dict[str, str], items: list[dict], parent=None):
        super().__init__(parent)
        self.items = items
        self.selected: list[tuple[dict, int]] = []
        self.setWindowTitle("출고 품목 수동 수정")
        self.resize(850, 560)

        source = QLabel(
            f"원본 상품: {order.get('product_name', '')}\n"
            f"옵션: {order.get('options', '')}\n"
            f"현재 재고매칭: {order.get('matched_name', '')}"
        )
        source.setWordWrap(True)
        self.search = QLineEdit()
        self.search.setPlaceholderText("품목코드, 품목명, 모델, 색상 검색")
        self.candidates = QListWidget()
        self.chosen = QListWidget()
        self.scope = QComboBox()
        self.scope.addItem("이 행만 수정", "row")
        self.scope.addItem("현재 파일의 같은 상품 모두 수정", "same")
        self.scope.addItem("같은 상품 전체 수정 + Supabase 별칭 저장", "database")
        add_button = QPushButton("선택 품목 추가 →")
        remove_button = QPushButton("선택 구성품 제거")

        lists = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(QLabel("DB 품목 검색 결과"))
        left.addWidget(self.candidates)
        left.addWidget(add_button)
        right = QVBoxLayout()
        right.addWidget(QLabel("적용할 출고 구성품"))
        right.addWidget(self.chosen)
        right.addWidget(remove_button)
        lists.addLayout(left)
        lists.addLayout(right)

        form = QFormLayout()
        form.addRow("적용 범위", self.scope)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout = QVBoxLayout(self)
        layout.addWidget(source)
        layout.addWidget(self.search)
        layout.addLayout(lists)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self.search.textChanged.connect(self.refresh_candidates)
        add_button.clicked.connect(self.add_item)
        remove_button.clicked.connect(self.remove_item)
        buttons.accepted.connect(self.validate_and_accept)
        buttons.rejected.connect(self.reject)
        self.refresh_candidates()

    def refresh_candidates(self) -> None:
        keyword = self.search.text().strip().lower()
        self.candidates.clear()
        shown = 0
        for item in self.items:
            text = " | ".join(
                str(item.get(key, "")) for key in ("item_code", "standard_name", "model", "color", "form")
            )
            if keyword and keyword not in text.lower():
                continue
            widget_item = QListWidgetItem(text)
            widget_item.setData(256, item)
            self.candidates.addItem(widget_item)
            shown += 1
            if shown >= 300:
                break

    def add_item(self) -> None:
        current = self.candidates.currentItem()
        if current is None:
            QMessageBox.information(self, "품목 선택", "추가할 품목을 먼저 선택하세요.")
            return
        item = current.data(256)
        quantity, ok = QInputDialog.getInt(self, "구성 수량", "이 품목의 세트 구성 수량", 1, 1, 999)
        if not ok:
            return
        self.selected.append((item, quantity))
        self.refresh_chosen()

    def remove_item(self) -> None:
        row = self.chosen.currentRow()
        if row >= 0:
            self.selected.pop(row)
            self.refresh_chosen()

    def refresh_chosen(self) -> None:
        self.chosen.clear()
        for item, quantity in self.selected:
            self.chosen.addItem(f"{item.get('item_code', '')} × {quantity} | {item.get('standard_name', '')}")

    def validate_and_accept(self) -> None:
        if not self.selected:
            QMessageBox.warning(self, "구성품 확인", "출고할 품목을 한 개 이상 추가하세요.")
            return
        self.accept()

    def result_data(self) -> tuple[str, str, str, list[dict]]:
        names = " / ".join(str(item.get("standard_name", "")) for item, _ in self.selected)
        components = " + ".join(f"{item.get('item_code', '')}×{qty}" for item, qty in self.selected)
        component_data = [
            {"item_code": item.get("item_code", ""), "standard_name": item.get("standard_name", ""), "quantity": qty}
            for item, qty in self.selected
        ]
        return names, components, str(self.scope.currentData()), component_data


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return DEFAULT_CONFIG
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


class LoginWorker(QThread):
    succeeded = Signal(int, object)
    failed = Signal(str)

    def __init__(self, email: str, password: str):
        super().__init__()
        self.email = email
        self.password = password

    def run(self) -> None:
        try:
            config = load_config()
            client: Client = create_client(
                config["supabase_url"], config["supabase_publishable_key"]
            )
            auth_result = client.auth.sign_in_with_password(
                {"email": self.email, "password": self.password}
            )
            def fetch_all(table: str) -> list[dict]:
                rows: list[dict] = []
                start = 0
                page_size = 1000
                while True:
                    response = client.table(table).select("*").range(start, start + page_size - 1).execute()
                    page = response.data or []
                    rows.extend(page)
                    if len(page) < page_size:
                        return rows
                    start += page_size

            items = fetch_all("items")
            products = fetch_all("registered_products")
            components = fetch_all("product_components")
            barcodes = fetch_all("item_barcodes")
            duty_locations = fetch_all("duty_free_locations")
            try:
                employees = fetch_all("ecount_employees")
            except Exception:
                employees = []
            try:
                warehouses = fetch_all("ecount_warehouses")
            except Exception:
                warehouses = []
            try:
                aliases = fetch_all("item_aliases")
            except Exception:
                aliases = []
            try:
                role_rows = fetch_all("app_user_roles")
                app_role = next((r.get("role") for r in role_rows if str(r.get("user_id")) == str(auth_result.user.id)), "viewer")
            except Exception:
                app_role = "admin" if str(auth_result.user.id) == ADMIN_USER_ID else "viewer"
            self.succeeded.emit(
                len(items),
                {"items": items, "products": products, "components": components, "barcodes": barcodes,
                 "duty_locations": duty_locations, "aliases": aliases, "employees": employees,
                 "warehouses": warehouses, "client": client,
                 "auth_user_id": str(auth_result.user.id), "app_role": app_role},
            )
        except Exception as exc:
            self.failed.emit(str(exc))


class LookupLineEdit(QLineEdit):
    lookupRequested = Signal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.lookupRequested.emit()
            return
        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.lookupRequested.emit()
        super().mouseDoubleClickEvent(event)


class LookupDialog(QDialog):
    def __init__(self, title: str, rows: list[dict], code_key: str, name_key: str, parent=None):
        super().__init__(parent)
        self.rows, self.code_key, self.name_key = rows, code_key, name_key
        self.filtered_rows = rows
        self.setWindowTitle(title)
        self.resize(560, 480)
        self.search = QLineEdit()
        self.search.setPlaceholderText("코드 또는 이름 검색")
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["코드", "이름"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout = QVBoxLayout(self)
        layout.addWidget(self.search)
        layout.addWidget(self.table)
        layout.addWidget(buttons)
        self.search.textChanged.connect(self.refresh)
        self.table.cellDoubleClicked.connect(lambda *_: self.accept_selected())
        buttons.accepted.connect(self.accept_selected)
        buttons.rejected.connect(self.reject)
        self.refresh()

    def refresh(self):
        word = self.search.text().strip().lower()
        self.filtered_rows = [row for row in self.rows if word in f"{row.get(self.code_key, '')} {row.get(self.name_key, '')}".lower()]
        self.table.setRowCount(len(self.filtered_rows))
        for index, row in enumerate(self.filtered_rows):
            self.table.setItem(index, 0, QTableWidgetItem(str(row.get(self.code_key, ""))))
            self.table.setItem(index, 1, QTableWidgetItem(str(row.get(self.name_key, ""))))
        if self.filtered_rows:
            self.table.selectRow(0)

    def accept_selected(self):
        if 0 <= self.table.currentRow() < len(self.filtered_rows):
            self.accept()

    def selected_text(self) -> str:
        if not (0 <= self.table.currentRow() < len(self.filtered_rows)):
            return ""
        row = self.filtered_rows[self.table.currentRow()]
        return f"{row.get(self.code_key, '')} | {row.get(self.name_key, '')}"


class FileDropZone(QFrame):
    filesDropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setMinimumHeight(76)
        self.setStyleSheet("QFrame { border: 2px dashed #9bbce5; border-radius: 10px; background: #f7fbff; }")
        layout = QVBoxLayout(self)
        label = QLabel("엑셀 또는 PDF 파일을 여기에 드래그 앤 드롭하세요")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)

    def dragEnterEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls()]
        if paths and all(Path(path).suffix.lower() in {".xls", ".xlsx", ".pdf"} for path in paths):
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls()]
        self.filesDropped.emit(paths)
        event.acceptProposedAction()


class EcountTransferDialog(QDialog):
    STATUS_LABELS = {"exact": "정확", "similar": "유사", "ambiguous": "확인필요", "missing": "미등록"}
    STATUS_COLORS = {"exact": QColor("#d9ead3"), "similar": QColor("#fff2cc"), "ambiguous": QColor("#fce5cd"), "missing": QColor("#f4cccc")}

    def __init__(self, catalog: dict, parent=None):
        super().__init__(parent)
        self.catalog = catalog
        self.rows = []
        self.setWindowTitle("이카운트 창고이동 보드")
        self.resize(1120, 720)

        self.io_date = QDateEdit(QDate.currentDate())
        self.io_date.setCalendarPopup(True)
        self.io_date.setDisplayFormat("yyyy-MM-dd")
        self.io_date.lineEdit().setReadOnly(True)
        self.employee = LookupLineEdit(); self.employee.setPlaceholderText("엔터/더블클릭으로 담당자 선택 또는 코드 직접 입력")
        self.from_warehouse = LookupLineEdit(); self.from_warehouse.setPlaceholderText("엔터/더블클릭으로 보내는 창고 선택 또는 코드 직접 입력")
        self.to_warehouse = LookupLineEdit(); self.to_warehouse.setPlaceholderText("엔터/더블클릭으로 받는 창고 선택 또는 코드 직접 입력")
        self.remarks = QLineEdit("출고 프로그램 창고이동")
        self.employee.lookupRequested.connect(self.choose_employee)
        self.from_warehouse.lookupRequested.connect(lambda: self.choose_warehouse(self.from_warehouse, "보내는 창고 선택"))
        self.to_warehouse.lookupRequested.connect(lambda: self.choose_warehouse(self.to_warehouse, "받는 창고 선택"))
        self.employee.textChanged.connect(lambda: self.complete_code(self.employee, self.catalog.get("employees", []), "employee_code", "employee_name"))
        self.from_warehouse.textChanged.connect(lambda: self.complete_code(self.from_warehouse, self.catalog.get("warehouses", []), "warehouse_code", "warehouse_name"))
        self.to_warehouse.textChanged.connect(lambda: self.complete_code(self.to_warehouse, self.catalog.get("warehouses", []), "warehouse_code", "warehouse_name"))

        form = QFormLayout()
        form.addRow("일자", self.io_date)
        form.addRow("담당자", self.employee)
        form.addRow("보내는 창고", self.from_warehouse)
        form.addRow("받는 창고", self.to_warehouse)
        form.addRow("적요", self.remarks)

        self.drop_zone = FileDropZone()
        self.drop_zone.filesDropped.connect(self.load_files)
        find_button = QPushButton("파일 찾기")
        find_button.clicked.connect(self.find_files)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["상태", "입력 품목코드", "입력 품목명", "적용 품목코드", "적용 품목명", "수량", "판정 이유"])
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.DoubleClicked | QTableWidget.EditTrigger.EditKeyPressed)
        self.table.cellDoubleClicked.connect(self.edit_transfer_item)

        config = load_config()
        self.api_values = {
            "com_code": str(config.get("ecount_com_code", "304293")),
            "user_id": str(config.get("ecount_user_id", "ororamobile")),
            "zone": str(config.get("ecount_zone", "AB")),
            "api_key": "",
        }
        info_button = QPushButton("정보")
        info_button.setMaximumWidth(64)
        info_button.clicked.connect(self.show_api_info)
        edit_item_button = QPushButton("선택 행 품목 수정")
        edit_item_button.clicked.connect(lambda: self.edit_transfer_item(self.table.currentRow(), 3))

        submit_button = QPushButton("이카운트 창고이동 등록")
        submit_button.setObjectName("exportButton")
        submit_button.clicked.connect(self.submit_transfer)
        close_button = QPushButton("닫기"); close_button.clicked.connect(self.reject)
        button_row = QHBoxLayout(); button_row.addStretch(1); button_row.addWidget(submit_button); button_row.addWidget(close_button)

        layout = QVBoxLayout(self)
        title = QLabel("이카운트 창고이동")
        title.setStyleSheet("font-size: 21px; font-weight: 800; color: #172b4d;")
        title_row = QHBoxLayout(); title_row.addWidget(title); title_row.addStretch(1); title_row.addWidget(info_button)
        layout.addLayout(title_row); layout.addLayout(form); layout.addWidget(self.drop_zone); layout.addWidget(find_button)
        layout.addWidget(self.table, 1); layout.addWidget(edit_item_button); layout.addLayout(button_row)

    def choose_employee(self):
        dialog = LookupDialog("담당자 선택", self.catalog.get("employees", []), "employee_code", "employee_name", self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.employee.setText(dialog.selected_text())

    def choose_warehouse(self, target: QLineEdit, title: str):
        dialog = LookupDialog(title, self.catalog.get("warehouses", []), "warehouse_code", "warehouse_name", self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            target.setText(dialog.selected_text())

    @staticmethod
    def complete_code(target: QLineEdit, rows: list[dict], code_key: str, name_key: str):
        text = target.text().strip()
        if not text or "|" in text:
            return
        matched = next((row for row in rows if str(row.get(code_key, "")).strip() == text), None)
        if matched:
            target.blockSignals(True)
            target.setText(f"{matched.get(code_key, '')} | {matched.get(name_key, '')}")
            target.setCursorPosition(len(target.text()))
            target.blockSignals(False)

    def show_api_info(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("이카운트 API 정보")
        dialog.setFixedWidth(430)
        com_code = QLineEdit(self.api_values["com_code"])
        user_id = QLineEdit(self.api_values["user_id"])
        zone = QLineEdit(self.api_values["zone"])
        api_key = QLineEdit(self.api_values["api_key"])
        api_key.setEchoMode(QLineEdit.EchoMode.Password)
        api_key.setPlaceholderText("API 인증키 (프로그램 종료 시 삭제)")
        form = QFormLayout()
        form.addRow("로그인 코드", com_code)
        form.addRow("이카운트 ID", user_id)
        form.addRow("ZONE", zone)
        form.addRow("API 인증키", api_key)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        layout = QVBoxLayout(dialog); layout.addLayout(form); layout.addWidget(buttons)
        buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.api_values.update({"com_code": com_code.text().strip(), "user_id": user_id.text().strip(), "zone": zone.text().strip(), "api_key": api_key.text().strip()})

    def edit_transfer_item(self, row_index: int, column_index: int):
        if row_index < 0 or column_index not in {3, 4}:
            return
        dialog = LookupDialog("적용 품목 선택", self.catalog.get("items", []), "item_code", "standard_name", self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected = dialog.filtered_rows[dialog.table.currentRow()]
        self.rows[row_index]["item_code"] = str(selected.get("item_code", ""))
        self.rows[row_index]["item_name"] = str(selected.get("standard_name", ""))
        self.rows[row_index]["status"] = "similar"
        self.rows[row_index]["reason"] = "사용자 품목 수정"
        self.populate_table()
        self.table.selectRow(row_index)

    def find_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "창고이동 품목 파일 선택", "", "Excel/PDF (*.xls *.xlsx *.pdf)")
        if paths:
            self.load_files(paths)

    def load_files(self, paths: list[str]):
        try:
            raw_rows = []
            for path in paths:
                raw_rows.extend(read_transfer_file(path))
            matched = match_transfer_rows(raw_rows, self.catalog.get("items", []), self.catalog.get("products", []), self.catalog.get("components", []))
            self.rows = aggregate_transfer_rows(matched)
            self.populate_table()
        except Exception as exc:
            QMessageBox.critical(self, "파일 분석 실패", str(exc))

    def populate_table(self):
        self.table.setRowCount(len(self.rows))
        keys = ["status", "source_code", "source_name", "item_code", "item_name", "quantity", "reason"]
        for row_index, row in enumerate(self.rows):
            color = self.STATUS_COLORS.get(row.get("status"), QColor("white"))
            for col_index, key in enumerate(keys):
                value = self.STATUS_LABELS.get(row.get(key), row.get(key, "")) if key == "status" else row.get(key, "")
                if key == "quantity":
                    number = float(value or 0); value = str(int(number)) if number.is_integer() else str(number)
                item = QTableWidgetItem(str(value))
                item.setBackground(color)
                if key in {"status", "reason"}:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row_index, col_index, item)

    @staticmethod
    def _code_from_input(value: str, rows: list[dict], code_key: str, name_key: str) -> str:
        text = value.strip()
        if "|" in text:
            return text.split("|", 1)[0].strip()
        for row in rows:
            if text in {str(row.get(code_key, "")).strip(), str(row.get(name_key, "")).strip()}:
                return str(row.get(code_key, "")).strip()
        return text

    def rows_from_table(self) -> list[dict]:
        result = []
        for row_index in range(self.table.rowCount()):
            code = self.table.item(row_index, 3).text().strip() if self.table.item(row_index, 3) else ""
            name = self.table.item(row_index, 4).text().strip() if self.table.item(row_index, 4) else ""
            qty_text = self.table.item(row_index, 5).text().strip() if self.table.item(row_index, 5) else ""
            try:
                quantity = float(qty_text.replace(",", ""))
            except ValueError:
                raise ValueError(f"{row_index + 1}행 수량을 확인해 주세요.")
            if not code or not name or quantity <= 0:
                raise ValueError(f"{row_index + 1}행의 품목코드·품목명·수량을 확인해 주세요.")
            result.append({"item_code": code, "item_name": name, "quantity": quantity})
        return result

    def submit_transfer(self):
        if not self.rows:
            QMessageBox.warning(self, "품목 없음", "먼저 엑셀 또는 PDF 파일을 입력해 주세요."); return
        employees, warehouses = self.catalog.get("employees", []), self.catalog.get("warehouses", [])
        employee_code = self._code_from_input(self.employee.text(), employees, "employee_code", "employee_name")
        from_code = self._code_from_input(self.from_warehouse.text(), warehouses, "warehouse_code", "warehouse_name")
        to_code = self._code_from_input(self.to_warehouse.text(), warehouses, "warehouse_code", "warehouse_name")
        if not employee_code or not from_code or not to_code:
            QMessageBox.warning(self, "필수값", "담당자, 보내는 창고, 받는 창고를 입력해 주세요."); return
        if from_code == to_code:
            QMessageBox.warning(self, "창고 확인", "보내는 창고와 받는 창고는 달라야 합니다."); return
        if not self.api_values.get("api_key", "").strip():
            QMessageBox.warning(self, "API 인증키", "오른쪽 위 정보 버튼을 눌러 이카운트 API 인증키를 입력해 주세요."); return
        try:
            rows = self.rows_from_table()
        except Exception as exc:
            QMessageBox.warning(self, "품목 확인", str(exc)); return
        answer = QMessageBox.question(self, "실제 창고이동 등록", f"{len(rows)}개 품목을 이카운트에 실제 등록합니다.\n{from_code} → {to_code}\n계속할까요?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            client = EcountClient(self.api_values["com_code"], self.api_values["user_id"], self.api_values["api_key"], self.api_values["zone"])
            session_id = client.login()
            result = client.save_location_transfer(session_id, rows, self.io_date.date().toString("yyyyMMdd"), employee_code, from_code, to_code, self.remarks.text().strip())
            data = result.get("Data") or {}
            success, fail = int(data.get("SuccessCnt") or 0), int(data.get("FailCnt") or 0)
            slips = ", ".join(str(value) for value in (data.get("SlipNos") or [])) or "없음"
            details = "\n".join(str(row.get("TotalError", "")) for row in (data.get("ResultDetails") or []))
            if result.get("Status") == "200" and fail == 0 and success > 0:
                QMessageBox.information(self, "등록 완료", f"성공 {success}건 / 실패 {fail}건\n전표번호: {slips}")
            else:
                QMessageBox.warning(self, "등록 결과 확인", f"성공 {success}건 / 실패 {fail}건\n전표번호: {slips}\n{details}")
        except Exception as exc:
            QMessageBox.critical(self, "이카운트 등록 실패", str(exc))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.worker = None
        self.matcher = None
        self.supabase_client = None
        self.catalog: dict = {}
        self.current_mode = "parcel"
        self.current_orders: list[dict[str, str]] = []
        self.is_admin = False
        self.setWindowTitle("리큐엠 출고용 데모버전 1.0.4")
        self.resize(1280, 720)
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #f4f7fb; color: #243247; font-family: '맑은 고딕'; font-size: 13px; }
            QLabel#appTitle { color: #172b4d; font-size: 25px; font-weight: 800; }
            QLabel#appSubtitle { color: #6b7a90; font-size: 12px; }
            QLabel#statusCard { background: #eaf4ff; color: #24527a; border: 1px solid #cfe5fa; border-radius: 9px; padding: 10px 12px; }
            QLineEdit { background: white; border: 1px solid #d6deea; border-radius: 8px; padding: 8px 10px; selection-background-color: #3977d5; }
            QLineEdit:focus { border: 1px solid #3977d5; }
            QPushButton { background: white; color: #29405e; border: 1px solid #d4deeb; border-radius: 8px; padding: 9px 14px; font-weight: 600; }
            QPushButton:hover { background: #edf5ff; border-color: #9bbce5; }
            QPushButton:pressed { background: #dfeeff; }
            QPushButton:disabled { background: #edf0f4; color: #9aa6b5; border-color: #e1e5eb; }
            QPushButton#primaryButton { background: #2563b8; color: white; border: none; padding: 11px 18px; }
            QPushButton#primaryButton:hover { background: #1f559e; }
            QPushButton#exportButton { background: #17a589; color: white; border: none; padding: 10px 16px; }
            QPushButton#exportButton:hover { background: #138a73; }
            QPushButton#adminButton { background: #fff8e8; color: #8a6116; border-color: #ead49f; padding: 5px 9px; font-size: 11px; }
            QTableWidget { background: white; alternate-background-color: #f8fafc; border: 1px solid #dce4ee; border-radius: 9px; gridline-color: #e7edf4; selection-background-color: #dbeafe; selection-color: #172b4d; }
            QHeaderView::section { background: #eaf0f7; color: #344b67; border: none; border-right: 1px solid #d7e0eb; border-bottom: 1px solid #cbd6e3; padding: 8px; font-weight: 700; }
        """)

        title = QLabel("리큐엠 출고 관리")
        title.setObjectName("appTitle")
        subtitle = QLabel("주문 파일을 자동 분석하고 정확한 출고 데이터로 변환합니다")
        subtitle.setObjectName("appSubtitle")
        self.email = QLineEdit()
        self.email.setPlaceholderText("프로그램 계정 이메일")
        self.email.setText("jonho1@naver.com")
        self.password = QLineEdit()
        self.password.setPlaceholderText("비밀번호")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.login_button = QPushButton("로그인하고 품목 DB 확인")
        self.login_button.setObjectName("primaryButton")
        self.b2c_button = QPushButton("B2C 엑셀 파일 (셀메이트)")
        self.b2c_button.setEnabled(False)
        self.b2b_button = QPushButton("B2B 엑셀 파일 (면세점)")
        self.b2b_button.setEnabled(False)
        self.auto_button = QPushButton("출고 엑셀 파일 선택 (자동 판별)")
        self.auto_button.setObjectName("primaryButton")
        self.auto_button.setEnabled(False)
        self.db_button = QPushButton("DB 관리 (관리자 전용)")
        self.db_button.setObjectName("adminButton")
        self.db_button.setMaximumWidth(155)
        self.db_button.setEnabled(False)
        self.transfer_button = QPushButton("창고이동 보드")
        self.transfer_button.setEnabled(False)
        self.export_button = QPushButton("택배 출고용 변환")
        self.export_button.setObjectName("exportButton")
        self.export_button.setEnabled(False)
        self.status = QLabel("공개용 API 키 설정 후 연결을 확인하세요.")
        self.status.setObjectName("statusCard")
        self.status.setWordWrap(True)

        self.table = QTableWidget()
        headers = [
            "상태", "DB 대조 상품", "출고 품목코드", "판정 이유", "원본행", "주문번호",
            "판매처", "상품명", "옵션", "수량", "수령인", "연락처", "우편번호", "주소", "재고매칭",
        ]
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)

        layout = QVBoxLayout()
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(9)
        login_row = QHBoxLayout()
        login_row.addWidget(self.email)
        login_row.addWidget(self.password)
        login_row.addWidget(self.login_button)
        header_row = QHBoxLayout()
        header_row.addWidget(title)
        header_row.addStretch(1)
        header_row.addWidget(self.db_button)
        header_row.addWidget(self.transfer_button)
        layout.addLayout(header_row)
        layout.addWidget(subtitle)
        layout.addLayout(login_row)
        action_row = QHBoxLayout()
        action_row.addWidget(self.auto_button)
        layout.addLayout(action_row)
        layout.addWidget(self.export_button)
        for widget in (self.status, self.table):
            layout.addWidget(widget)
        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)
        self.login_button.clicked.connect(self.login)
        self.b2c_button.clicked.connect(self.select_b2c_file)
        self.b2b_button.clicked.connect(self.select_b2b_file)
        self.auto_button.clicked.connect(lambda: self.select_file("auto"))
        self.db_button.clicked.connect(self.open_db_manager)
        self.transfer_button.clicked.connect(self.open_transfer_board)
        self.export_button.clicked.connect(self.export_file)
        self.table.cellDoubleClicked.connect(self.edit_match)

    def login(self) -> None:
        if not self.email.text().strip() or not self.password.text():
            QMessageBox.warning(self, "입력 확인", "이메일과 비밀번호를 입력하세요.")
            return
        self.login_button.setEnabled(False)
        self.status.setText("로그인 및 품목 DB 조회 중...")
        self.worker = LoginWorker(self.email.text().strip(), self.password.text())
        self.worker.succeeded.connect(self.on_success)
        self.worker.failed.connect(self.on_failure)
        self.worker.start()

    def on_success(self, count: int, catalog: dict) -> None:
        self.login_button.setEnabled(True)
        self.b2c_button.setEnabled(True)
        self.b2b_button.setEnabled(True)
        self.auto_button.setEnabled(True)
        self.transfer_button.setEnabled(True)
        self.supabase_client = catalog["client"]
        self.catalog = catalog
        self.is_admin = catalog.get("app_role") == "admin"
        self.db_button.setEnabled(self.is_admin)
        self.matcher = ProductMatcher(catalog["items"], catalog["products"], catalog["components"], catalog["aliases"])
        self.status.setText(
            f"DB 준비 완료: 품목 {count:,}개 · 등록상품 {len(catalog['products']):,}개 · "
            f"구성품 {len(catalog['components']):,}개 · 권한: {'관리자' if self.is_admin else '일반 사용자(조회 전용)'}"
        )

    def open_db_manager(self) -> None:
        if not self.is_admin:
            QMessageBox.warning(self, "권한 없음", "관리자에게 DB 수정 권한을 요청하세요.")
            return
        ItemManagerDialog(self.supabase_client, self.catalog["items"], self).exec()
        self.matcher = ProductMatcher(self.catalog["items"], self.catalog["products"], self.catalog["components"], self.catalog["aliases"])

    def open_transfer_board(self) -> None:
        if not self.catalog:
            QMessageBox.warning(self, "로그인 필요", "먼저 로그인해 DB 정보를 불러와 주세요.")
            return
        EcountTransferDialog(self.catalog, self).exec()

    def on_failure(self, message: str) -> None:
        self.login_button.setEnabled(True)
        self.status.setText("연결 실패")
        QMessageBox.critical(self, "Supabase 연결 실패", message)

    def select_b2c_file(self) -> None:
        self.select_file("b2c")

    def select_b2b_file(self) -> None:
        self.select_file("b2b")

    def select_file(self, expected_type: str) -> None:
        title = "출고 Excel 파일 자동 판별" if expected_type == "auto" else ("B2C 셀메이트 주문 파일 선택" if expected_type == "b2c" else "B2B 면세점 출고 요청 파일 선택")
        path, _ = QFileDialog.getOpenFileName(
            self,
            title,
            "",
            "Excel/CSV 파일 (*.xls *.xlsx *.csv)",
        )
        if not path:
            return
        try:
            if self.matcher is None:
                raise RuntimeError("먼저 Supabase에 로그인해 DB를 불러오세요.")
            duty_result = load_duty_free(path)
            if duty_result:
                if expected_type not in {"b2b", "auto"}:
                    raise ValueError("면세점 B2B 파일로 감지됐습니다. B2B 엑셀 파일 버튼을 사용하세요.")
                orders, detected_type = duty_result
                match_barcodes(orders, self.catalog.get("barcodes", []), self.catalog.get("items", []))
                columns = {"duty_free": 1}
                self.current_mode = "duty_free"
                self.export_button.setEnabled(False)
            else:
                if expected_type not in {"b2c", "auto"}:
                    raise ValueError("면세점 B2B 양식을 찾지 못했습니다. B2C 파일이라면 B2C 엑셀 파일 버튼을 사용하세요.")
                orders, columns = load_orders(path)
                for order in orders:
                    order.update(self.matcher.match(order))
                    if order.get("manual_input_detected"):
                        order["status"] = "similar"
                        order["reason"] = "재고매칭 표준 열 뒤 수기 추가 품목 감지 · 검토 필요 | " + order.get("reason", "")
                detected_type = "일반 택배"
                self.current_mode = "parcel"
                self.export_button.setEnabled(True)
            self.mark_duplicates(orders)
        except Exception as exc:
            QMessageBox.critical(self, "파일 분석 실패", str(exc))
            return
        self.current_orders = orders
        self.populate_table(self.current_orders)
        counts = {key: sum(1 for row in orders if row.get("status") == key) for key in ("exact", "similar", "ambiguous", "missing")}
        self.status.setText(
            f"{detected_type} 분석 완료 {len(orders):,}행 · 정확 {counts['exact']:,} · 유사 {counts['similar']:,} · "
            f"확인필요 {counts['ambiguous']:,} · 미등록 {counts['missing']:,} · {len(columns)}개 열 인식"
        )

    def mark_duplicates(self, orders: list[dict[str, str]]) -> None:
        """합포장은 허용하고, 동일 주문의 동일 상품 행만 중복으로 표시한다."""
        seen, shipped = set(), set()
        try:
            numbers = [r.get("order_number", "") for r in orders if r.get("order_number")]
            if numbers:
                response = self.supabase_client.table("shipment_history").select("duplicate_key").in_("order_number", list(set(numbers))).execute()
                shipped = {str(r.get("duplicate_key", "")) for r in (response.data or [])}
        except Exception:
            pass  # 마이그레이션 전에도 파일 내부 중복 검사는 동작한다.
        for row in orders:
            key = self.duplicate_key(row)
            if key and (key in seen or key in shipped):
                row["status"] = "duplicate"
                row["reason"] = "동일 주문·수령정보·상품 행이 현재 파일에서 반복됨" if key in seen else "동일 출고 행이 이전 출고 이력에 있음"
            if key: seen.add(key)

    @staticmethod
    def duplicate_key(row: dict[str, str]) -> str:
        # 수령인 이름만 같아서는 중복이 아니다. 합포장 내 서로 다른 상품도 각각 정상 행이다.
        fields = ("order_number", "recipient", "phone", "zipcode", "address", "product_name", "options", "quantity")
        normalized = "|".join(compact(str(row.get(field, ""))) for field in fields)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if row.get("order_number") else ""

    def populate_table(self, orders: list[dict[str, str]]) -> None:
        keys = [
            "status", "matched_product", "components", "reason", "source_row", "order_number",
            "channel", "product_name", "options", "quantity", "recipient", "phone", "zipcode",
            "address", "matched_name",
        ]
        labels = {"exact": "정확", "similar": "유사", "ambiguous": "확인필요", "missing": "미등록", "manual": "수동확정", "alias": "별칭적용", "duplicate": "중복출고"}
        colors = {
            "exact": QColor("#d9ead3"),
            "similar": QColor("#fff2cc"),
            "ambiguous": QColor("#fce5cd"),
            "missing": QColor("#f4cccc"),
            "manual": QColor("#cfe2f3"),
            "alias": QColor("#d9d2e9"),
            "duplicate": QColor("#ea9999"),
        }
        self.table.setRowCount(len(orders))
        for row_index, order in enumerate(orders):
            for col_index, key in enumerate(keys):
                value = labels.get(order.get(key, ""), order.get(key, "")) if key == "status" else order.get(key, "")
                item = QTableWidgetItem(value)
                item.setBackground(colors.get(order.get("status", ""), QColor("white")))
                self.table.setItem(row_index, col_index, item)

    def edit_match(self, row_index: int, _column_index: int) -> None:
        if self.matcher is None or not (0 <= row_index < len(self.current_orders)):
            return
        order = self.current_orders[row_index]
        dialog = CorrectionDialog(order, self.matcher.items, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        matched_product, components, scope, component_data = dialog.result_data()
        targets = [row_index]
        if scope in {"same", "database"}:
            targets = [
                index for index, candidate in enumerate(self.current_orders)
                if candidate.get("product_name") == order.get("product_name")
                and candidate.get("options") == order.get("options")
            ]
        for index in targets:
            self.current_orders[index].update(
                {
                    "status": "manual",
                    "matched_product": matched_product,
                    "components": components,
                    "reason": "사용자 수동 확정",
                }
            )
        self.populate_table(self.current_orders)
        if scope == "database" and not self.is_admin:
            QMessageBox.warning(self, "권한 없음", "현재 파일에는 적용했지만 DB 별칭 저장은 관리자만 할 수 있습니다.")
            self.status.setText(f"수동 수정 완료: {len(targets)}개 행에 적용 (DB 저장 안 함)")
            return
        if scope == "database":
            try:
                source_key = compact(" ".join(filter(None, [order.get("product_name", ""), order.get("options", "")])))
                payload = {
                    "source_channel": order.get("channel", ""),
                    "source_product_name": order.get("product_name", ""),
                    "source_options": order.get("options", ""),
                    "normalized_source": source_key,
                    "components": component_data,
                    "is_active": True,
                }
                self.supabase_client.table("item_aliases").upsert(
                    payload, on_conflict="source_channel,normalized_source"
                ).execute()
                self.matcher.aliases[(order.get("channel", ""), source_key)] = payload
                self.status.setText(f"수동 수정 및 별칭 저장 완료: {len(targets)}개 행에 적용")
            except Exception as exc:
                QMessageBox.warning(self, "별칭 저장 실패", f"현재 파일 수정은 적용됐지만 DB 저장에 실패했습니다.\n{exc}")
        else:
            self.status.setText(f"수동 수정 완료: {len(targets)}개 행에 적용")

    def export_file(self) -> None:
        if self.current_mode != "parcel":
            QMessageBox.information(self, "면세점 파일", "면세점 전용 출력 기능은 다음 단계에서 제공됩니다.")
            return
        if not self.current_orders:
            QMessageBox.warning(self, "저장할 데이터 없음", "먼저 주문 파일을 불러오세요.")
            return
        unresolved = [row for row in self.current_orders if row.get("status") in {"missing", "ambiguous", "duplicate"}]
        if unresolved:
            answer = QMessageBox.question(
                self,
                "오류 항목 포함 변환",
                f"미등록·확인 필요·중복 출고 항목이 {len(unresolved)}개 남아 있습니다.\n"
                "오류 항목은 품목코드가 비어 있거나 중복될 수 있습니다.\n그래도 택배 출고용 파일로 변환하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "위킵 택배 출고 파일 저장",
            "위킵_택배출고.xlsx",
            "Excel 파일 (*.xlsx)",
        )
        if not file_path:
            return
        if not file_path.lower().endswith(".xlsx"):
            file_path += ".xlsx"
        try:
            export_wekep(self.current_orders, file_path)
        except Exception as exc:
            QMessageBox.critical(self, "Excel 저장 실패", str(exc))
            return
        try:
            history = [{"duplicate_key": self.duplicate_key(row), "order_number": row.get("order_number", ""), "sales_channel": row.get("channel", ""), "recipient": row.get("recipient", ""), "phone": row.get("phone", ""), "address": row.get("address", ""), "product_name": row.get("product_name", ""), "options": row.get("options", ""), "quantity": row.get("quantity", ""), "source_type": "b2c"} for row in self.current_orders if row.get("order_number")]
            if history:
                self.supabase_client.table("shipment_history").upsert(history, on_conflict="duplicate_key").execute()
        except Exception as exc:
            QMessageBox.warning(self, "이력 저장 안내", f"Excel은 저장됐지만 중복 방지 이력을 Supabase에 기록하지 못했습니다.\n관리자용 SQL 적용 여부를 확인하세요.\n{exc}")
        QMessageBox.information(self, "저장 완료", f"위킵 출고 파일을 저장했습니다.\n{file_path}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
