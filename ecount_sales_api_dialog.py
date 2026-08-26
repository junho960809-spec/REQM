from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ecount_sales_api import (
    EcountSalesClient,
    build_sales_payload,
    load_api_key,
    load_history,
    load_settings,
    payload_total,
    request_key,
    save_api_key,
    save_history,
    save_settings,
)
from ecount_sales_core import VoucherLine


class SalesApiWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, credentials: dict[str, Any], payload: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.credentials = credentials
        self.payload = payload

    def run(self) -> None:
        try:
            client = EcountSalesClient(**self.credentials)
            result = client.save_sales(self.payload) if self.payload is not None else {"session": client.login()}
            self.succeeded.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class EcountSalesApiDialog(QDialog):
    def __init__(
        self,
        lines: list[VoucherLine],
        voucher_date: date,
        expected_total: Decimal,
        manager_code: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.lines = list(lines)
        self.voucher_date = voucher_date
        self.expected_total = expected_total
        self.config = load_settings()
        if manager_code:
            self.config["employee_code"] = manager_code
        self.worker: SalesApiWorker | None = None
        self.payload: dict[str, Any] | None = None
        self.current_request_key = ""
        self.login_verified = False
        self.setWindowTitle("이카운트 판매전표 API 입력")
        self.resize(1080, 720)

        self.company_code = QLineEdit(str(self.config.get("company_code", "")))
        self.user_id = QLineEdit(str(self.config.get("user_id", "")))
        self.api_key = QLineEdit(load_api_key(self.user_id.text()))
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("이카운트 Open API 인증키")
        self.zone = QLineEdit(str(self.config.get("zone", "")))
        self.zone.setPlaceholderText("비워두면 회사코드로 자동 확인")
        self.employee_code = QLineEdit(str(self.config.get("employee_code", manager_code)))
        self.endpoint = QLineEdit(str(self.config.get("endpoint", "Sale/SaveSale")))
        self.endpoint.setPlaceholderText("예: Sale/SaveSale")
        self.list_key = QLineEdit(str(self.config.get("list_key", "SaleList")))
        self.remarks = QLineEdit(str(self.config.get("remarks", "REQM 판매전표 API 입력")))
        self.test_mode = QCheckBox("테스트 API 키 사용")
        self.test_mode.setChecked(bool(self.config.get("test_mode", False)))

        key_row = QHBoxLayout()
        key_row.addWidget(self.api_key, 1)
        self.key_save_button = QPushButton("API 키 암호화 저장")
        key_row.addWidget(self.key_save_button)

        form = QFormLayout()
        form.addRow("회사코드", self.company_code)
        form.addRow("사용자 ID", self.user_id)
        form.addRow("API 인증키", key_row)
        form.addRow("ZONE", self.zone)
        form.addRow("담당자코드", self.employee_code)
        form.addRow("판매 API 경로", self.endpoint)
        form.addRow("요청 목록 키", self.list_key)
        form.addRow("적요", self.remarks)
        form.addRow("API 환경", self.test_mode)

        warehouse_totals: dict[str, Decimal] = {}
        warehouse_counts: dict[str, int] = {}
        for line in self.lines:
            warehouse = str(line.warehouse)
            warehouse_totals[warehouse] = warehouse_totals.get(warehouse, Decimal("0")) + line.total
            warehouse_counts[warehouse] = warehouse_counts.get(warehouse, 0) + 1
        parts = [
            f"창고 {warehouse}: {warehouse_counts[warehouse]:,}행 / {warehouse_totals[warehouse]:,.0f}원"
            for warehouse in sorted(warehouse_totals)
        ]
        self.summary = QLabel(
            f"전표일자 {voucher_date:%Y-%m-%d} · 전체 {len(lines):,}행 · "
            f"총액 {expected_total:,.0f}원\n" + "   |   ".join(parts)
        )
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("font-weight:700;color:#0F766E;padding:8px;")

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["전표묶음", "창고", "거래처코드", "품목코드", "수량", "단가", "금액"]
        )
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        for column in range(4, 7):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self._fill_preview()

        self.settings_button = QPushButton("설정 저장")
        self.login_button = QPushButton("API 로그인 테스트")
        self.preview_button = QPushButton("전송 데이터 다시 검증")
        self.submit_button = QPushButton("이카운트 판매전표 실제 입력")
        self.submit_button.setObjectName("primary")
        self.submit_button.setEnabled(False)
        self.close_button = QPushButton("닫기")
        actions = QHBoxLayout()
        actions.addWidget(self.settings_button)
        actions.addWidget(self.login_button)
        actions.addWidget(self.preview_button)
        actions.addStretch(1)
        actions.addWidget(self.submit_button)
        actions.addWidget(self.close_button)

        self.status = QLabel("전송 전 API 설정과 요청 금액을 확인해주세요.")
        self.status.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.summary)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.status)
        layout.addLayout(actions)

        self.user_id.editingFinished.connect(self._load_saved_key)
        for field in (self.company_code, self.user_id, self.api_key, self.zone):
            field.textChanged.connect(self._invalidate_login)
        self.test_mode.toggled.connect(self._invalidate_login)
        self.key_save_button.clicked.connect(self._save_key)
        self.settings_button.clicked.connect(self._save_settings)
        self.login_button.clicked.connect(self._test_login)
        self.preview_button.clicked.connect(self._validate_payload)
        self.submit_button.clicked.connect(self._submit)
        self.close_button.clicked.connect(self.reject)

    def _fill_preview(self) -> None:
        serials = {warehouse: index for index, warehouse in enumerate(sorted({str(row.warehouse) for row in self.lines}), 1)}
        ordered = sorted(self.lines, key=lambda row: (str(row.warehouse), row.item_code, row.unit_price))
        self.table.setRowCount(len(ordered))
        for row_index, line in enumerate(ordered):
            values = [
                serials[str(line.warehouse)],
                line.warehouse,
                line.customer_code,
                line.item_code,
                f"{line.quantity:f}",
                f"{line.unit_price:,.0f}",
                f"{line.total:,.0f}",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column >= 4:
                    item.setTextAlignment(2 | 128)
                self.table.setItem(row_index, column, item)

    def _settings(self) -> dict[str, Any]:
        return {
            "company_code": self.company_code.text().strip(),
            "user_id": self.user_id.text().strip(),
            "zone": self.zone.text().strip().upper(),
            "employee_code": self.employee_code.text().strip(),
            "endpoint": self.endpoint.text().strip().strip("/"),
            "list_key": self.list_key.text().strip(),
            "remarks": self.remarks.text().strip(),
            "test_mode": self.test_mode.isChecked(),
        }

    def _credentials(self) -> dict[str, Any] | None:
        settings = self._settings()
        required = {
            "회사코드": settings["company_code"],
            "사용자 ID": settings["user_id"],
            "API 인증키": self.api_key.text().strip(),
            "담당자코드": settings["employee_code"],
            "판매 API 경로": settings["endpoint"],
            "요청 목록 키": settings["list_key"],
        }
        missing = [label for label, value in required.items() if not value]
        if missing:
            QMessageBox.warning(self, "API 설정 확인", "다음 값을 입력하세요: " + ", ".join(missing))
            return None
        return {
            "company_code": settings["company_code"],
            "user_id": settings["user_id"],
            "api_key": self.api_key.text().strip(),
            "zone": settings["zone"],
            "test_mode": settings["test_mode"],
            "endpoint": settings["endpoint"],
        }

    def _load_saved_key(self) -> None:
        saved = load_api_key(self.user_id.text())
        if saved:
            self.api_key.setText(saved)

    def _invalidate_login(self, _value: object = None) -> None:
        self.login_verified = False
        if self.worker is None or not self.worker.isRunning():
            self.submit_button.setEnabled(False)

    def _save_key(self) -> None:
        try:
            save_api_key(self.user_id.text(), self.api_key.text())
        except Exception as exc:
            QMessageBox.warning(self, "API 키 저장 실패", str(exc))
            return
        QMessageBox.information(self, "API 키 저장", "현재 Windows 사용자 전용으로 암호화 저장했습니다.")

    def _save_settings(self) -> None:
        save_settings(self._settings())
        self.status.setText("판매전표 API 설정을 저장했습니다.")

    def _validate_payload(self) -> bool:
        try:
            self.payload = build_sales_payload(
                self.lines,
                self.voucher_date,
                self.employee_code.text().strip(),
                self.list_key.text().strip(),
                self.remarks.text().strip(),
            )
            api_total = payload_total(self.payload)
        except Exception as exc:
            QMessageBox.warning(self, "전송 데이터 오류", str(exc))
            return False
        if api_total != self.expected_total:
            QMessageBox.critical(
                self,
                "전송 금액 불일치",
                f"프로그램 전표 총액 {self.expected_total:,.0f}원과 API 요청 총액 "
                f"{api_total:,.0f}원이 일치하지 않아 전송을 차단했습니다.",
            )
            return False
        self.status.setText(f"검증 완료 · API 요청 총액 {api_total:,.0f}원 · 창고별 전표 2개 이하")
        return True

    def _set_running(self, running: bool) -> None:
        for button in (
            self.settings_button,
            self.login_button,
            self.preview_button,
            self.submit_button,
            self.close_button,
            self.key_save_button,
        ):
            button.setEnabled(not running)
        self.submit_button.setEnabled(not running and self.login_verified)
        self.setWindowTitle("이카운트 판매전표 전송 중..." if running else "이카운트 판매전표 API 입력")

    def _test_login(self) -> None:
        credentials = self._credentials()
        if credentials is None:
            return
        self._set_running(True)
        self.status.setText("이카운트 Open API 로그인 확인 중...")
        self.worker = SalesApiWorker(credentials)
        self.worker.succeeded.connect(self._login_succeeded)
        self.worker.failed.connect(self._failed)
        self.worker.start()

    def _login_succeeded(self, _result: object) -> None:
        self.login_verified = True
        self._set_running(False)
        self.status.setText("API 로그인 성공 · 실제 판매전표는 아직 전송하지 않았습니다.")
        QMessageBox.information(self, "로그인 성공", "이카운트 Open API 로그인이 정상 작동합니다.")

    def _submit(self) -> None:
        if not self.login_verified:
            QMessageBox.warning(self, "API 로그인 필요", "실제 입력 전에 API 로그인 테스트를 완료해주세요.")
            return
        credentials = self._credentials()
        if credentials is None or not self._validate_payload() or self.payload is None:
            return
        self._save_settings()
        self.current_request_key = request_key(
            self.payload, self.endpoint.text().strip(), self.test_mode.isChecked()
        )
        completed = {str(row.get("request_key")) for row in load_history() if row.get("status") == "success"}
        if self.current_request_key in completed:
            QMessageBox.critical(
                self,
                "중복 전송 차단",
                "동일한 날짜·창고·품목·수량·금액의 판매전표가 이미 성공 처리되었습니다.",
            )
            return
        first = QMessageBox.question(
            self,
            "판매전표 입력 확인",
            f"본사·위킵 창고별 판매전표를 이카운트에 입력합니다.\n"
            f"총 {len(self.lines):,}행 · {self.expected_total:,.0f}원\n\n계속할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if first != QMessageBox.StandardButton.Yes:
            return
        second = QMessageBox.warning(
            self,
            "최종 실행 확인",
            "실행하면 이카운트 판매·재고·미수금에 실제 반영될 수 있습니다. 정말 입력하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if second != QMessageBox.StandardButton.Yes:
            return
        self._set_running(True)
        self.status.setText("이카운트 판매전표 전송 중...")
        self.worker = SalesApiWorker(credentials, self.payload)
        self.worker.succeeded.connect(self._submit_succeeded)
        self.worker.failed.connect(self._failed)
        self.worker.start()

    def _submit_succeeded(self, result: dict[str, Any]) -> None:
        self._set_running(False)
        slips = [str(value) for value in result.get("slip_numbers", [])]
        save_history({
            "request_key": self.current_request_key,
            "status": "success",
            "sent_at": datetime.now().isoformat(timespec="seconds"),
            "voucher_date": self.voucher_date.isoformat(),
            "amount": str(self.expected_total),
            "line_count": len(self.lines),
            "slip_numbers": slips,
            "endpoint": self.endpoint.text().strip(),
            "test_mode": self.test_mode.isChecked(),
        })
        slip_text = ", ".join(slips) or "이카운트에서 확인 필요"
        self.status.setText(f"전송 성공 · 전표번호 {slip_text}")
        QMessageBox.information(
            self,
            "판매전표 입력 완료",
            f"이카운트 판매전표 입력이 완료되었습니다.\n"
            f"성공 {result.get('success_count', 0):,}건 · 전표번호 {slip_text}",
        )

    def _failed(self, message: str) -> None:
        self._set_running(False)
        self.status.setText("API 처리 실패 · 아래 오류를 확인해주세요.")
        QMessageBox.critical(self, "이카운트 API 오류", message)

    def reject(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        super().reject()
