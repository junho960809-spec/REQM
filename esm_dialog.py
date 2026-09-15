from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QStandardPaths, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QComboBox, QDateEdit, QTableWidget, QTableWidgetItem, QHeaderView,
    QFileDialog, QMessageBox, QLineEdit,
)

from esm_browser import EsmBrowserWorker
from esm_orders import DATE_TYPES, HEADERS, STATUSES, EsmSession, export_esm_original_format


class EsmHeader(QHeaderView):
    def paintSection(self, painter, rect, index):
        painter.save()
        painter.fillRect(rect, QColor("#BDD7EE" if index == 6 else "#FFCC00"))
        painter.setPen(QColor("#183153"))
        painter.drawText(rect.adjusted(5, 0, -5, 0), Qt.AlignCenter, HEADERS[index])
        painter.restore()


class EsmSourceDialog(QDialog):
    def __init__(self, start, end, parent=None, session=None):
        super().__init__(parent)
        self.setWindowTitle("ESM 주문 수집 및 취합")
        self.resize(1180, 720)
        self.session = session
        self.worker = None
        self.busy = False
        self.logged_in = False
        self.pending_result = None
        root = Path(QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)) / "REQM" / "ESM"
        layout = QVBoxLayout(self)
        title = QLabel("ESM 주문 수집 및 취합")
        title.setStyleSheet("font-size:20px;font-weight:700;color:#183153")
        layout.addWidget(title)
        layout.addWidget(QLabel("스타배송 · 출고/배송 관리 · A/G 전체"))
        login_row = QHBoxLayout()
        self.login_button = QPushButton("ESM 로그인")
        self.login_button.clicked.connect(self.login)
        self.login_status = QLabel("Chrome을 우선 사용하고, 실행할 수 없을 때만 Edge를 사용합니다.")
        login_row.addWidget(self.login_button)
        login_row.addWidget(self.login_status, 1)
        layout.addLayout(login_row)
        controls = QGridLayout()
        self.date_type = QComboBox()
        self.date_type.addItems(DATE_TYPES)
        self.start_date = QDateEdit(start)
        self.end_date = QDateEdit(end)
        for field in (self.start_date, self.end_date):
            field.setCalendarPopup(True)
            field.setDisplayFormat("yyyy-MM-dd")
        self.collect_button = QPushButton("수집 시작")
        self.collect_button.setStyleSheet("background:#2563EB;color:white;padding:8px 18px;font-weight:700")
        self.collect_button.clicked.connect(self.collect)
        self.stop_button = QPushButton("수집 중지")
        self.stop_button.clicked.connect(self.cancel_collection)
        for column, widget in enumerate((QLabel("조회 기준"), self.date_type, QLabel("조회 기간"), self.start_date,
                                         QLabel("~"), self.end_date, self.collect_button, self.stop_button)):
            controls.addWidget(widget, 0, column)
        layout.addLayout(controls)
        layout.addWidget(QLabel("수집 대상: " + " · ".join(STATUSES)))
        layout.addWidget(QLabel("배송상태의 ‘전체’는 제외하고, 세부조건은 ‘전체’로 조회합니다."))
        folder_row = QHBoxLayout()
        self.root_path = QLineEdit(str(root))
        self.folder_button = QPushButton("보관 위치 선택")
        self.folder_button.clicked.connect(self.choose_folder)
        folder_row.addWidget(QLabel("원본 보관 위치"))
        folder_row.addWidget(self.root_path, 1)
        folder_row.addWidget(self.folder_button)
        layout.addLayout(folder_row)
        tools = QHBoxLayout()
        self.local_button = QPushButton("내려받은 ESM 원본 가져오기")
        self.local_button.clicked.connect(self.import_files)
        self.history_button = QPushButton("이전 수집 열기")
        self.history_button.clicked.connect(self.open_history)
        tools.addWidget(self.local_button)
        tools.addWidget(self.history_button)
        tools.addStretch()
        layout.addLayout(tools)
        self.progress = QLabel("날짜를 확인하고 수집을 시작하세요. 수집할 때마다 새 보관 폴더가 만들어집니다.")
        self.progress.setWordWrap(True)
        self.progress.setStyleSheet("background:#EFF6FF;padding:8px;color:#1E40AF")
        layout.addWidget(self.progress)
        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeader(EsmHeader(Qt.Horizontal, self.table))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for i in range(len(HEADERS)):
            self.table.horizontalHeaderItem(i).setBackground(QColor("#BDD7EE" if i == 6 else "#FFCC00"))
        layout.addWidget(self.table, 1)
        layout.addWidget(QLabel("개당 금액 = (판매금액 − 판매자쿠폰할인) ÷ 수량 · 배송비는 이번 ESM 전표에 포함하지 않습니다."))
        layout.addWidget(QLabel("원본에는 구매자 정보가 포함될 수 있습니다. 보관 폴더 접근과 파일 공유에 유의해주세요."))
        footer = QHBoxLayout()
        self.open_button = QPushButton("원본 폴더 열기")
        self.open_button.clicked.connect(self.open_originals)
        self.zip_button = QPushButton("원본 전체 ZIP 저장")
        self.zip_button.clicked.connect(self.save_zip)
        self.excel_button = QPushButton("ESM 원본양식 통합 저장")
        self.excel_button.clicked.connect(self.save_excel)
        self.import_button = QPushButton("판매전표로 가져오기")
        self.import_button.clicked.connect(self.use_orders)
        self.import_button.setStyleSheet("background:#2563EB;color:white;padding:8px;font-weight:700")
        close = QPushButton("닫기")
        close.clicked.connect(self.reject)
        for button in (self.open_button, self.zip_button, self.excel_button, self.import_button, close):
            footer.addWidget(button)
        layout.addLayout(footer)
        if session:
            self.show_session(session)
        self.refresh_buttons()

    def refresh_buttons(self):
        complete = bool(self.session and self.session.manifest.get("state") == "완료")
        for field in (self.date_type, self.start_date, self.end_date, self.root_path, self.folder_button,
                      self.local_button, self.history_button):
            field.setEnabled(not self.busy)
        self.login_button.setEnabled(not self.worker)
        self.collect_button.setEnabled(self.logged_in and not self.busy)
        self.stop_button.setEnabled(self.busy)
        self.open_button.setEnabled(bool(self.session))
        self.zip_button.setEnabled(bool(self.session) and not self.busy)
        self.excel_button.setEnabled(complete and not self.busy)
        self.import_button.setEnabled(complete and not self.busy and bool(self.session.manifest.get("order_count")))

    def login(self):
        if self.worker:
            return
        self.worker = EsmBrowserWorker(self)
        self.worker.status_changed.connect(self.progress.setText)
        self.worker.ready.connect(self.set_ready)
        self.worker.session_started.connect(self.new_session)
        self.worker.collected.connect(self.show_session)
        self.worker.failed.connect(self.failure)
        self.worker.collecting_changed.connect(self.set_busy)
        self.worker.finished.connect(self.browser_finished)
        self.worker.start()
        self.login_status.setText("브라우저를 여는 중...")
        self.refresh_buttons()

    def set_ready(self, ready):
        self.logged_in = ready
        self.login_status.setText("ESM 로그인 확인" if ready else "ESM 로그인 필요")
        self.refresh_buttons()

    def set_busy(self, value):
        self.busy = value
        self.refresh_buttons()

    def cancel_collection(self):
        if self.worker:
            self.worker.cancelled.set()
            self.progress.setText("진행 중인 작업을 정리하고 중지합니다. 내려받은 원본은 보관됩니다.")

    def collect(self):
        start, end = self.start_date.date().toPython(), self.end_date.date().toPython()
        if start > end:
            QMessageBox.warning(self, "조회 기간", "종료일이 시작일보다 빠릅니다.")
            return
        if not self.root_path.text().strip():
            QMessageBox.warning(self, "보관 위치", "원본 보관 위치를 선택해주세요.")
            return
        self.set_busy(True)
        self.worker.collect(Path(self.root_path.text()), self.date_type.currentText(), start, end)

    def new_session(self, session):
        self.session = session
        self.table.setRowCount(0)
        self.refresh_buttons()

    def show_session(self, session):
        try:
            orders, duplicates = session.orders()
        except Exception as exc:
            self.session = session
            self.table.setRowCount(0)
            session.manifest["state"] = "검증 실패"
            self.failure(str(exc))
            return
        self.session = session
        self.table.setRowCount(min(500, len(orders)))
        for r, order in enumerate(orders[:500]):
            values = (order.account, order.product, f"{order.quantity:,.0f}", order.options,
                      f"{order.sales:,}", f"{order.coupon:,}", f"{order.unit:,.8f}".rstrip("0").rstrip("."))
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                if c in (2, 4, 5, 6):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(r, c, item)
        self.progress.setText(
            f"{session.manifest['state']} · {len(orders):,}행 · 중복 제외 {duplicates:,}행 · 미리보기 최대 500행, Excel은 전체 저장\n"
            f"{session.manifest['created_at']} / {session.manifest['mode']} / "
            f"{session.manifest['date_type']} {session.manifest['start_date']} ~ {session.manifest['end_date']}\n"
            f"보관: {session.folder}"
        )
        self.refresh_buttons()

    def failure(self, message):
        self.progress.setText(message)
        self.refresh_buttons()
        if self.pending_result is None:
            QMessageBox.warning(self, "ESM 확인 필요", message)

    def choose_folder(self):
        selected = QFileDialog.getExistingDirectory(self, "원본 보관 위치", self.root_path.text())
        if selected:
            self.root_path.setText(selected)

    def import_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "ESM 원본 Excel 선택 (파일 안의 전체 주문을 취합합니다)", "", "Excel (*.xls *.xlsx)")
        if not files:
            return
        try:
            session = EsmSession.create(Path(self.root_path.text()), self.date_type.currentText(),
                                        self.start_date.date().toPython(), self.end_date.date().toPython(), "파일 가져오기 · 기간 필터 미적용")
            self.new_session(session)
            for number, filename in enumerate(files, 1):
                session.archive(Path(filename), f"가져온원본{number}")
            session.finish()
            self.show_session(session)
        except Exception as exc:
            if self.session:
                self.session.manifest["state"] = "실패"
                self.session.save()
            self.failure(str(exc))

    def open_history(self):
        filename, _ = QFileDialog.getOpenFileName(self, "이전 수집기록 선택", self.root_path.text(), "수집 기록 (수집기록.json)")
        if filename:
            try:
                self.show_session(EsmSession.open(Path(filename).parent))
            except Exception as exc:
                self.failure(str(exc))

    def open_originals(self):
        if self.session:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.session.folder / "원본")))

    def save_zip(self):
        filename, _ = QFileDialog.getSaveFileName(self, "ESM 원본 전체 ZIP 저장", "ESM_원본.zip", "ZIP (*.zip)")
        if filename:
            try:
                self.session.export_zip(Path(filename))
                self.progress.setText(f"원본 ZIP 저장 완료: {filename}")
            except Exception as exc:
                self.failure(str(exc))

    def save_excel(self):
        filename, _ = QFileDialog.getSaveFileName(self, "ESM 원본양식 통합 저장", "ESM_원본양식_통합.xlsx", "Excel (*.xlsx)")
        if filename:
            try:
                export_esm_original_format(self.session, Path(filename))
                self.progress.setText(f"ESM 원본양식 통합 저장 완료: {filename}")
            except Exception as exc:
                self.failure(str(exc))

    def use_orders(self):
        try:
            if not self.session or self.session.manifest["state"] != "완료":
                raise ValueError("수집을 완료한 후 가져올 수 있습니다.")
            self.session.orders()
            self.accept()
        except Exception as exc:
            self.failure(str(exc))

    def done(self, result):
        # 브라우저 작업 중 창을 파괴하면 QThread가 중단되므로 정상 종료까지 기다린다.
        if self.worker and self.worker.isRunning():
            self.pending_result = result
            self.worker.stop()
            self.setEnabled(False)
            self.progress.setText("브라우저 작업을 정리하는 중입니다. 잠시 기다려주세요.")
            return
        super().done(result)

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            event.ignore()
            self.reject()
        else:
            super().closeEvent(event)

    def browser_finished(self):
        self.worker = None
        self.logged_in = False
        self.busy = False
        self.refresh_buttons()
        if self.pending_result is not None:
            super().done(self.pending_result)
