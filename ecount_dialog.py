from typing import Any

from PySide6.QtCore import QDate, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QDialog, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from ecount_client import (
    EcountClient, build_location_transfer_payload, collect_transfer_items,
    save_completed_transfer_request, transfer_request_key,
)
from ecount_user_store import delete_ecount_user, load_ecount_users, upsert_ecount_user
from ecount_credential_store import delete_api_key, load_api_key, save_api_key


class EcountUserManagerDialog(QDialog):
    def __init__(self, current_user_id: str = "", parent=None):
        super().__init__(parent)
        self.selected_profile = None
        self.current_user_id = current_user_id.strip()
        self.setWindowTitle("이카운트 사용자 ID 관리")
        self.resize(620, 460)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["사용자 ID", "담당자코드", "표시이름"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

        self.user_id = QLineEdit()
        self.user_id.setPlaceholderText("예: JUNHO191")
        self.employee_code = QLineEdit()
        self.employee_code.setPlaceholderText("예: 00210")
        self.display_name = QLineEdit()
        self.display_name.setPlaceholderText("선택사항")
        form = QFormLayout()
        form.addRow("이카운트 사용자 ID", self.user_id)
        form.addRow("담당자코드", self.employee_code)
        form.addRow("표시이름", self.display_name)

        self.save_button = QPushButton("등록 / 수정")
        self.delete_button = QPushButton("삭제")
        self.select_button = QPushButton("선택하여 입력")
        self.close_button = QPushButton("닫기")
        buttons = QHBoxLayout()
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.delete_button)
        buttons.addStretch(1)
        buttons.addWidget(self.select_button)
        buttons.addWidget(self.close_button)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("자주 사용하는 이카운트 ID와 담당자코드를 저장합니다."))
        layout.addWidget(self.table, 1)
        layout.addLayout(form)
        layout.addLayout(buttons)

        self.table.itemSelectionChanged.connect(self.fill_selected_row)
        self.table.cellDoubleClicked.connect(lambda _row, _column: self.choose_selected())
        self.save_button.clicked.connect(self.save_profile)
        self.delete_button.clicked.connect(self.delete_profile)
        self.select_button.clicked.connect(self.choose_selected)
        self.close_button.clicked.connect(self.reject)
        self.refresh_table()

    def refresh_table(self, selected_user_id: str = "") -> None:
        self.profiles = load_ecount_users()
        self.table.setRowCount(len(self.profiles))
        selected_row = -1
        target = (selected_user_id or self.current_user_id).casefold()
        for row_index, profile in enumerate(self.profiles):
            for column, key in enumerate(("user_id", "employee_code", "display_name")):
                self.table.setItem(row_index, column, QTableWidgetItem(profile[key]))
            if profile["user_id"].casefold() == target:
                selected_row = row_index
        if selected_row >= 0:
            self.table.selectRow(selected_row)

    def fill_selected_row(self) -> None:
        row = self.table.currentRow()
        if not (0 <= row < len(self.profiles)):
            return
        profile = self.profiles[row]
        self.user_id.setText(profile["user_id"])
        self.employee_code.setText(profile["employee_code"])
        self.display_name.setText(profile["display_name"])

    def save_profile(self) -> None:
        try:
            profile = upsert_ecount_user({
                "user_id": self.user_id.text(),
                "employee_code": self.employee_code.text(),
                "display_name": self.display_name.text(),
            })
        except Exception as exc:
            QMessageBox.warning(self, "입력 확인", str(exc))
            return
        self.current_user_id = profile["user_id"]
        self.refresh_table(profile["user_id"])

    def delete_profile(self) -> None:
        user_id = self.user_id.text().strip()
        if not user_id:
            QMessageBox.warning(self, "삭제할 사용자", "삭제할 사용자 ID를 선택하세요.")
            return
        answer = QMessageBox.question(
            self, "사용자 삭제", f"{user_id} 사용자 정보를 삭제하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        delete_ecount_user(user_id)
        self.user_id.clear()
        self.employee_code.clear()
        self.display_name.clear()
        self.current_user_id = ""
        self.refresh_table()

    def choose_selected(self) -> None:
        row = self.table.currentRow()
        if not (0 <= row < len(self.profiles)):
            QMessageBox.warning(self, "사용자 선택", "입력할 사용자를 선택하세요.")
            return
        self.selected_profile = self.profiles[row]
        self.accept()


class TransferWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, credentials: dict[str, Any], payload: dict[str, Any]):
        super().__init__()
        self.credentials = credentials
        self.payload = payload

    def run(self) -> None:
        try:
            client = EcountClient(**self.credentials)
            self.succeeded.emit(client.save_location_transfer(self.payload))
        except Exception as exc:
            self.failed.emit(str(exc))


class EcountTransferDialog(QDialog):
    def __init__(
        self, orders: list[dict], catalog_items: list[dict], config: dict,
        completed_requests: set[str] | None = None, parent=None,
    ):
        super().__init__(parent)
        self.orders = orders
        self.catalog_items = catalog_items
        self.config = config or {}
        self.completed_requests = completed_requests if completed_requests is not None else set()
        self.worker = None
        self.payload = None
        self.request_key = ""
        self.setWindowTitle("이카운트 창고이동")
        self.resize(860, 680)

        channels = sorted({str(row.get("channel", "")).strip() for row in orders if str(row.get("channel", "")).strip()})
        self.transfer_scope = ", ".join(channels) or "전체 주문"
        self.transfer_date = QDateEdit()
        self.transfer_date.setCalendarPopup(True)
        self.transfer_date.setDate(QDate.currentDate())
        self.company_code = QLineEdit(str(self.config.get("company_code", "")))
        self.company_code.setText("304293")
        self.company_code.setReadOnly(True)
        self.user_id = QLineEdit(str(self.config.get("user_id", "")))
        self.user_id.hide()
        self.employee = QLineEdit(str(self.config.get("employee_code", "")))
        self.employee.hide()
        self.user_display = QLineEdit()
        self.user_display.setReadOnly(True)
        self.user_display.setPlaceholderText("사용자를 선택하세요")
        self.user_manage_button = QPushButton("사용자 불러오기 / 관리")
        user_row = QWidget()
        user_layout = QHBoxLayout(user_row)
        user_layout.setContentsMargins(0, 0, 0, 0)
        user_layout.addWidget(self.user_display, 1)
        user_layout.addWidget(self.user_manage_button)
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("ID별 Windows 암호화 저장 가능")
        self.api_key_save_button = QPushButton("키 저장")
        self.api_key_delete_button = QPushButton("삭제")
        self.test_mode = QCheckBox("테스트키")
        self.test_mode.setChecked(bool(self.config.get("test_mode", False)))
        key_row = QWidget()
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.addWidget(self.api_key, 1)
        key_layout.addWidget(self.test_mode)
        key_layout.addWidget(self.api_key_save_button)
        key_layout.addWidget(self.api_key_delete_button)
        self.zone = QLineEdit(str(self.config.get("zone", "")))
        self.zone.setText("AB")
        self.zone.setReadOnly(True)
        self.source = QLineEdit(str(self.config.get("source_warehouse", "100")))
        self.target = QLineEdit(str(self.config.get("target_warehouse", "300")))
        warehouse_row = QWidget()
        warehouse_layout = QHBoxLayout(warehouse_row)
        warehouse_layout.setContentsMargins(0, 0, 0, 0)
        warehouse_layout.addWidget(self.source)
        warehouse_layout.addWidget(QLabel("→"))
        warehouse_layout.addWidget(self.target)
        base_remarks = str(self.config.get("remarks", "REQM 출고 창고이동"))
        remarks = f"{base_remarks} · {self.transfer_scope}" if self.transfer_scope not in base_remarks else base_remarks
        self.remarks = QLineEdit(remarks)

        form = QFormLayout()
        for label, widget in (
            ("이동일자", self.transfer_date), ("사용자 ID / 담당자", user_row),
            ("API 인증키", key_row), ("출발창고 → 도착창고", warehouse_row), ("적요", self.remarks),
        ):
            form.addRow(label, widget)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["품목코드", "품목명", "이동수량"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.preview_button = QPushButton("전체 주문 다시 집계")
        self.submit_button = QPushButton("이카운트 창고이동 실행")
        self.cancel_button = QPushButton("닫기")
        buttons = QHBoxLayout()
        buttons.addWidget(self.preview_button)
        buttons.addStretch(1)
        buttons.addWidget(self.submit_button)
        buttons.addWidget(self.cancel_button)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.summary)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)
        self.user_manage_button.clicked.connect(self.open_user_manager)
        self.api_key_save_button.clicked.connect(self.save_current_api_key)
        self.api_key_delete_button.clicked.connect(self.delete_current_api_key)
        self.preview_button.clicked.connect(self.refresh_preview)
        self.submit_button.clicked.connect(self.submit)
        self.cancel_button.clicked.connect(self.reject)
        self.update_user_display()
        self.apply_saved_user()
        self.refresh_preview()

    def open_user_manager(self) -> None:
        dialog = EcountUserManagerDialog(self.user_id.text(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.selected_profile:
            return
        self.user_id.setText(dialog.selected_profile["user_id"])
        self.employee.setText(dialog.selected_profile["employee_code"])
        self.update_user_display()
        self.load_saved_api_key()

    def apply_saved_user(self) -> None:
        user_id = self.user_id.text().strip().casefold()
        profile = next(
            (row for row in load_ecount_users() if row["user_id"].casefold() == user_id), None
        )
        if profile:
            self.user_id.setText(profile["user_id"])
            self.employee.setText(profile["employee_code"])
            self.update_user_display()
            self.load_saved_api_key()

    def update_user_display(self) -> None:
        user_id = self.user_id.text().strip()
        employee_code = self.employee.text().strip()
        self.user_display.setText(f"{user_id} / {employee_code}" if user_id and employee_code else "")

    def load_saved_api_key(self) -> None:
        saved = load_api_key(self.user_id.text())
        self.api_key.setText(saved)
        self.api_key.setPlaceholderText(
            "Windows 암호화 저장된 인증키 자동 적용" if saved else "인증키 입력 후 '키 저장'"
        )

    def save_current_api_key(self) -> None:
        try:
            save_api_key(self.user_id.text(), self.api_key.text())
        except Exception as exc:
            QMessageBox.warning(self, "인증키 저장 실패", str(exc))
            return
        QMessageBox.information(
            self, "인증키 저장", "현재 Windows 사용자 전용 암호화 저장소에 인증키를 저장했습니다."
        )

    def delete_current_api_key(self) -> None:
        if not self.user_id.text().strip():
            QMessageBox.warning(self, "사용자 선택", "먼저 이카운트 사용자를 선택하세요.")
            return
        delete_api_key(self.user_id.text())
        self.api_key.clear()
        self.api_key.setPlaceholderText("인증키 입력 후 '키 저장'")

    def refresh_preview(self) -> None:
        self.items, counts = collect_transfer_items(self.orders, "", self.catalog_items)
        self.summary.setText(
            f"선택 주문 {counts['selected']:,}행 · 이동 포함 {counts['included']:,}행 · "
            f"확인 필요/중복 등 제외 {counts['excluded']:,}행 · 집계 품목 {len(self.items):,}개"
        )
        self.table.setRowCount(len(self.items))
        for row_index, row in enumerate(self.items):
            for column, key in enumerate(("item_code", "item_name", "quantity")):
                self.table.setItem(row_index, column, QTableWidgetItem(row[key]))
        self.submit_button.setEnabled(bool(self.items))

    def _required_values(self) -> bool:
        fields = {
            "회사코드": self.company_code.text(), "이카운트 사용자 ID": self.user_id.text(),
            "API 인증키": self.api_key.text(), "담당자코드": self.employee.text(),
            "출발창고": self.source.text(), "도착창고": self.target.text(),
        }
        missing = [label for label, value in fields.items() if not value.strip()]
        if missing:
            QMessageBox.warning(self, "입력 확인", "다음 값을 입력하세요: " + ", ".join(missing))
            return False
        if self.source.text().strip() == self.target.text().strip():
            QMessageBox.warning(self, "창고 확인", "출발창고와 도착창고는 달라야 합니다.")
            return False
        return True

    def submit(self) -> None:
        self.refresh_preview()
        if not self.items or not self._required_values():
            return
        self.payload = build_location_transfer_payload(
            self.transfer_date.date().toString("yyyyMMdd"), self.employee.text().strip(),
            self.source.text().strip(), self.target.text().strip(), self.items, self.remarks.text().strip(),
        )
        self.request_key = transfer_request_key(self.transfer_scope, self.payload)
        if self.request_key in self.completed_requests:
            QMessageBox.warning(
                self, "중복 실행 차단",
                "같은 실행 중 동일한 판매처·날짜·창고·품목 수량으로 이미 창고이동을 완료했습니다.",
            )
            return
        total = sum(float(row["quantity"]) for row in self.items)
        first = QMessageBox.question(
            self, "창고이동 확인",
            f"{len(self.items):,}개 품목, 총 {total:g}개를\n"
            f"창고 {self.source.text()} → {self.target.text()}로 이동합니다. 계속할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if first != QMessageBox.StandardButton.Yes:
            return
        second = QMessageBox.warning(
            self, "최종 실행 확인", "실행하면 이카운트 재고에 실제 반영됩니다. 정말 실행하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel,
        )
        if second != QMessageBox.StandardButton.Yes:
            return
        self._set_running(True)
        credentials = {
            "company_code": self.company_code.text(), "user_id": self.user_id.text(),
            "api_key": self.api_key.text(), "zone": self.zone.text(), "test_mode": self.test_mode.isChecked(),
        }
        self.worker = TransferWorker(credentials, self.payload)
        self.worker.succeeded.connect(self.on_success)
        self.worker.failed.connect(self.on_failure)
        self.worker.start()

    def _set_running(self, running: bool) -> None:
        self.submit_button.setEnabled(not running and bool(getattr(self, "items", [])))
        self.preview_button.setEnabled(not running)
        self.cancel_button.setEnabled(not running)
        self.user_manage_button.setEnabled(not running)
        self.api_key_save_button.setEnabled(not running)
        self.api_key_delete_button.setEnabled(not running)
        self.setWindowTitle("이카운트 창고이동 전송 중..." if running else "이카운트 창고이동")

    def on_success(self, result: dict[str, Any]) -> None:
        self._set_running(False)
        self.completed_requests.add(self.request_key)
        try:
            save_completed_transfer_request(self.request_key)
        except OSError:
            pass
        slip_numbers = ", ".join(str(value) for value in result.get("slip_numbers", [])) or "확인 필요"
        QMessageBox.information(
            self, "창고이동 완료",
            f"이카운트 창고이동이 완료됐습니다.\n성공 {result.get('success_count', 0):,}건 · 전표번호 {slip_numbers}",
        )
        self.accept()

    def on_failure(self, message: str) -> None:
        self._set_running(False)
        QMessageBox.critical(self, "창고이동 실패", message)

    def reject(self) -> None:
        if self.worker and self.worker.isRunning():
            return
        super().reject()
