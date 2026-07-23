import json
import sys
import hashlib
import os
import subprocess
import uuid
import urllib.request
from urllib.parse import quote
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
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
    QAbstractItemView,
    QVBoxLayout,
    QWidget,
)
from supabase import Client, create_client

from excel_loader import COLUMN_ALIASES, load_orders, suggest_header_row
from matcher import ProductMatcher
from matcher import compact
from matcher import order_source_text
from shipping_export import export_wekep
from duty_free_loader import load_duty_free, match_barcodes
from catalog_import import compare_catalog, load_item_catalog
from location_store import load_locations, save_locations
from format_store import upsert_format
from direct_suggester import component_payload, components_text, suggest_direct_order


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
DEFAULT_CONFIG = {
    "supabase_url": "https://jcslohuraqclhryeqxoc.supabase.co",
    "supabase_publishable_key": "sb_publishable_dafbXHpLHVPDhsMwm_B5RA_LgCqlWeg",
}
ADMIN_USER_ID = "c7937d51-1a14-47aa-987e-6254c6c79014"
APP_VERSION = "1.0.17"
UPDATE_BASE_URL = "https://jcslohuraqclhryeqxoc.supabase.co/storage/v1/object/public/reqm-updates"
UPDATE_MANIFEST_URL = f"{UPDATE_BASE_URL}/manifest.json"


def remove_legacy_transfer_credentials() -> None:
    """Remove the encrypted API key left by the retired warehouse-transfer feature."""
    credential_path = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "REQM" / "credentials.json"
    try:
        credential_path.unlink(missing_ok=True)
    except OSError:
        pass


def version_key(value: str) -> tuple[int, ...]:
    parts = []
    for part in str(value or "0").split("."):
        digits = "".join(character for character in part if character.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


class UpdateCheckWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def run(self) -> None:
        try:
            request = urllib.request.Request(UPDATE_MANIFEST_URL, headers={"User-Agent": "REQM-Updater"})
            with urllib.request.urlopen(request, timeout=15) as response:
                manifest = json.loads(response.read().decode("utf-8"))
            for key in ("version", "file", "sha256"):
                if not manifest.get(key):
                    raise ValueError(f"업데이트 정보에 {key} 값이 없습니다.")
            if not manifest.get("chunks") and not manifest.get("file"):
                raise ValueError("업데이트 파일 정보가 없습니다.")
            self.succeeded.emit(manifest)
        except Exception as exc:
            self.failed.emit(str(exc))


class UpdateDownloadWorker(QThread):
    succeeded = Signal(str, object)
    failed = Signal(str)

    def __init__(self, manifest: dict):
        super().__init__()
        self.manifest = manifest

    def run(self) -> None:
        try:
            update_dir = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "REQM" / "updates"
            update_dir.mkdir(parents=True, exist_ok=True)
            file_name = Path(str(self.manifest["file"])).name
            target = update_dir / f"{self.manifest['version']}_{file_name}"
            digest = hashlib.sha256()
            remote_parts = self.manifest.get("chunks") or [file_name]
            with target.open("wb") as output:
                for remote_name in remote_parts:
                    safe_name = Path(str(remote_name)).name
                    url = f"{UPDATE_BASE_URL}/{quote(safe_name)}"
                    request = urllib.request.Request(url, headers={"User-Agent": "REQM-Updater"})
                    with urllib.request.urlopen(request, timeout=60) as response:
                        while True:
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                            digest.update(chunk)
            expected = str(self.manifest["sha256"]).strip().lower()
            if digest.hexdigest().lower() != expected:
                target.unlink(missing_ok=True)
                raise ValueError("다운로드 파일의 보안 해시가 일치하지 않습니다.")
            self.succeeded.emit(str(target), self.manifest)
        except Exception as exc:
            self.failed.emit(str(exc))


class ItemManagerDialog(QDialog):
    """관리자 전용 표준 품목 관리 화면."""
    def __init__(self, client: Client, items: list[dict], barcodes: list[dict], parent=None):
        super().__init__(parent)
        self.client, self.items, self.barcodes = client, items, barcodes
        self.setWindowTitle("DB 품목 관리 · 관리자")
        self.resize(900, 560)
        self.search = QLineEdit()
        self.search.setPlaceholderText("품목코드 또는 품목명 검색")
        self.grid = QTableWidget(0, 6)
        self.grid.setHorizontalHeaderLabels(["품목코드", "표준 품목명", "모델", "색상", "형태", "사용"])
        self.grid.horizontalHeader().setStretchLastSection(True)
        self.grid.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        import_btn = QPushButton("엑셀 품목 가져오기")
        import_btn.setObjectName("primaryButton")
        add_btn, edit_btn, active_btn = QPushButton("신규 품목"), QPushButton("선택 수정"), QPushButton("사용/중지 전환")
        delete_btn = QPushButton("삭제")
        buttons = QHBoxLayout()
        for button in (import_btn, add_btn, edit_btn, active_btn, delete_btn): buttons.addWidget(button)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("표준 품목(items) 관리 — 변경 내용은 Supabase에 즉시 저장됩니다."))
        layout.addWidget(self.search); layout.addWidget(self.grid); layout.addLayout(buttons)
        self.search.textChanged.connect(self.refresh)
        import_btn.clicked.connect(self.import_catalog)
        add_btn.clicked.connect(self.add_item); edit_btn.clicked.connect(self.edit_item); active_btn.clicked.connect(self.toggle_active)
        delete_btn.clicked.connect(self.delete_items)
        self.refresh()

    def import_catalog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "품목코드·품목명·바코드 파일 선택",
            "",
            "Excel 파일 (*.xlsx *.xlsm)",
        )
        if not path:
            return
        try:
            records = load_item_catalog(path)
            result = compare_catalog(records, self.items, self.barcodes)
        except Exception as exc:
            QMessageBox.critical(self, "파일 분석 실패", str(exc))
            return

        conflicts = result["barcode_conflicts"]
        conflict_note = ""
        if conflicts:
            examples = ", ".join(
                f"{row['barcode']} ({row['item_code']}↔{row['db_item_code']})"
                for row in conflicts[:3]
            )
            conflict_note = f"\n바코드 충돌 {len(conflicts):,}개: {examples}\n충돌 항목은 등록하지 않습니다."
        message = (
            f"파일 품목: {len(records):,}개\n"
            f"신규 품목: {len(result['new_items']):,}개\n"
            f"DB에 이미 있는 품목: {len(result['existing_items']):,}개\n"
            f"이름이 다른 기존 품목: {len(result['renamed_items']):,}개 (DB 이름 유지)\n"
            f"신규 바코드: {len(result['new_barcodes']):,}개\n"
            f"DB에 이미 있는 바코드: {len(result['existing_barcodes']):,}개"
            f"{conflict_note}\n\n신규 품목과 충돌 없는 신규 바코드만 DB에 등록할까요?"
        )
        answer = QMessageBox.question(
            self,
            "가져오기 미리보기",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        item_payload = [
            {
                "item_code": row["item_code"],
                "standard_name": row["standard_name"],
                "model": "",
                "color": "",
                "form": "",
                "is_active": True,
                "review_status": "confirmed",
            }
            for row in result["new_items"]
        ]
        barcode_payload = [
            {"item_code": row["item_code"], "barcode": row["barcode"], "is_active": True}
            for row in result["new_barcodes"]
        ]
        try:
            if item_payload:
                response = self.client.table("items").insert(item_payload).execute()
                self.items.extend(response.data or item_payload)
            if barcode_payload:
                response = self.client.table("item_barcodes").insert(barcode_payload).execute()
                self.barcodes.extend(response.data or barcode_payload)
            self.refresh()
            QMessageBox.information(
                self,
                "가져오기 완료",
                f"신규 품목 {len(item_payload):,}개와 신규 바코드 {len(barcode_payload):,}개를 등록했습니다.",
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "DB 등록 실패",
                "일부 품목이 먼저 등록되었을 수 있습니다. DB를 다시 불러온 뒤 재시도하세요.\n\n" + str(exc),
            )

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

    def delete_items(self) -> None:
        selected_indexes = self.grid.selectionModel().selectedRows()
        selected_rows = sorted({index.row() for index in selected_indexes})
        candidates = [self.grid_rows[index] for index in selected_rows if 0 <= index < len(self.grid_rows)]
        if not candidates:
            QMessageBox.information(self, "삭제할 품목 선택", "삭제할 품목을 선택하세요.\n여러 품목은 Ctrl 또는 Shift를 누른 채 선택할 수 있습니다.")
            return
        lines = "\n".join(
            f"• {row.get('item_code', '')} | {row.get('standard_name', '')}"
            for row in candidates[:20]
        )
        if len(candidates) > 20:
            lines += f"\n• 외 {len(candidates) - 20:,}개"
        answer = QMessageBox.question(
            self,
            "DB 품목 삭제 확인",
            f"선택한 {len(candidates):,}개 품목과 연결 바코드·상품 구성 정보를 DB에서 삭제합니다.\n\n"
            f"{lines}\n\n삭제 후 복구할 수 없습니다. 계속할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        codes = [str(row.get("item_code", "")) for row in candidates if row.get("item_code")]
        try:
            aliases = self.client.table("item_aliases").select("source_channel,normalized_source,components").execute().data or []
            related_aliases = [
                alias for alias in aliases
                if alias.get("normalized_source") and any(
                    str(component.get("item_code", "")) in codes
                    for component in (alias.get("components") or [])
                )
            ]
            for alias in related_aliases:
                self.client.table("item_aliases").delete().eq(
                    "source_channel", str(alias.get("source_channel", ""))
                ).eq(
                    "normalized_source", str(alias.get("normalized_source", ""))
                ).execute()
            self.client.table("item_barcodes").delete().in_("item_code", codes).execute()
            self.client.table("product_components").delete().in_("item_code", codes).execute()
            self.client.table("items").delete().in_("item_code", codes).execute()
            self.items[:] = [row for row in self.items if str(row.get("item_code", "")) not in codes]
            self.barcodes[:] = [row for row in self.barcodes if str(row.get("item_code", "")) not in codes]
            self.refresh()
            QMessageBox.information(self, "삭제 완료", f"선택한 DB 품목 {len(codes):,}개를 삭제했습니다.")
        except Exception as exc:
            QMessageBox.critical(self, "DB 삭제 실패", f"삭제 도중 오류가 발생했습니다. DB를 다시 불러와 확인하세요.\n\n{exc}")


class DutyLocationDialog(QDialog):
    """Local reusable duty-free shipping destination address book."""
    FIELD_LABELS = (
        ("name", "출고지 이름"),
        ("channel", "면세점 구분"),
        ("recipient", "수령인/담당자"),
        ("phone", "연락처"),
        ("zipcode", "우편번호"),
        ("address", "주소"),
        ("message", "배송 메모"),
    )

    def __init__(self, locations: list[dict[str, str]], parent=None):
        super().__init__(parent)
        self.locations = [dict(row) for row in locations]
        self.current_id = ""
        self.setWindowTitle("면세점 출고지 정보 관리")
        self.resize(780, 520)
        self.list_widget = QListWidget()
        self.fields: dict[str, QLineEdit] = {}
        form = QFormLayout()
        for key, label in self.FIELD_LABELS:
            edit = QLineEdit()
            if key == "channel":
                edit.setPlaceholderText("예: 롯데면세점, 시티면세점 T2 606매장")
            elif key == "address":
                edit.setPlaceholderText("기본 주소와 상세 주소를 함께 입력")
            self.fields[key] = edit
            form.addRow(label, edit)

        new_button = QPushButton("새 출고지")
        save_button = QPushButton("저장")
        save_button.setObjectName("primaryButton")
        delete_button = QPushButton("삭제")
        close_button = QPushButton("완료")
        buttons = QHBoxLayout()
        for button in (new_button, save_button, delete_button, close_button):
            buttons.addWidget(button)

        right = QVBoxLayout()
        right.addLayout(form)
        right.addStretch(1)
        right.addLayout(buttons)
        body = QHBoxLayout()
        body.addWidget(self.list_widget, 2)
        body.addLayout(right, 3)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("자주 사용하는 면세점·터미널·매장별 출고지를 등록하세요."))
        layout.addLayout(body)

        self.list_widget.currentRowChanged.connect(self.load_selected)
        new_button.clicked.connect(self.new_location)
        save_button.clicked.connect(self.save_current)
        delete_button.clicked.connect(self.delete_current)
        close_button.clicked.connect(self.accept)
        self.refresh_list()
        if self.locations:
            self.list_widget.setCurrentRow(0)

    def refresh_list(self, selected_id: str = "") -> None:
        self.list_widget.clear()
        selected_row = -1
        for index, row in enumerate(self.locations):
            label = row.get("name", "")
            if row.get("channel"):
                label += f" · {row['channel']}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, row.get("id", ""))
            self.list_widget.addItem(item)
            if selected_id and row.get("id") == selected_id:
                selected_row = index
        if selected_row >= 0:
            self.list_widget.setCurrentRow(selected_row)

    def load_selected(self, index: int) -> None:
        if not (0 <= index < len(self.locations)):
            return
        row = self.locations[index]
        self.current_id = row.get("id", "")
        for key, edit in self.fields.items():
            edit.setText(row.get(key, ""))

    def new_location(self) -> None:
        self.current_id = ""
        self.list_widget.clearSelection()
        self.list_widget.setCurrentRow(-1)
        for edit in self.fields.values():
            edit.clear()
        self.fields["name"].setFocus()

    def save_current(self) -> None:
        data = {key: edit.text().strip() for key, edit in self.fields.items()}
        if not data["name"] or not data["address"]:
            QMessageBox.warning(self, "필수 정보", "출고지 이름과 주소는 반드시 입력하세요.")
            return
        data["id"] = self.current_id or str(uuid.uuid4())
        index = next((i for i, row in enumerate(self.locations) if row.get("id") == data["id"]), -1)
        if index >= 0:
            self.locations[index] = data
        else:
            self.locations.append(data)
        self.current_id = data["id"]
        save_locations(self.locations)
        self.refresh_list(self.current_id)
        QMessageBox.information(self, "저장 완료", f"'{data['name']}' 출고지를 저장했습니다.")

    def delete_current(self) -> None:
        index = next((i for i, row in enumerate(self.locations) if row.get("id") == self.current_id), -1)
        if index < 0:
            return
        answer = QMessageBox.question(
            self, "출고지 삭제", f"'{self.locations[index].get('name', '')}' 출고지를 삭제할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.locations.pop(index)
        self.current_id = ""
        save_locations(self.locations)
        self.refresh_list()
        self.new_location()


class AccountSettingsDialog(QDialog):
    """로그인 입력란을 메인 화면 대신 작은 설정 창에서 관리한다."""
    def __init__(self, email: str, password: str, logged_in: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("설정 · 로그인 계정")
        self.setMinimumWidth(430)
        self.email_edit = QLineEdit(email)
        self.password_edit = QLineEdit(password)
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        status = QLabel("현재 로그인된 계정입니다." if logged_in else "로그인에 사용할 계정을 입력하세요.")
        status.setWordWrap(True)
        form = QFormLayout()
        form.addRow("이메일", self.email_edit)
        form.addRow("비밀번호", self.password_edit)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("적용")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(status)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def credentials(self) -> tuple[str, str]:
        return self.email_edit.text().strip(), self.password_edit.text()


class FileFormatDialog(QDialog):
    """One-time mapping wizard for an unknown seller spreadsheet format."""
    FIELD_LABELS = (
        ("order_number", "주문번호 *"),
        ("product_name", "상품명 *"),
        ("quantity", "수량 *"),
        ("recipient", "수령인 *"),
        ("option1", "상품옵션"),
        ("option2", "추가 옵션"),
        ("model", "모델명"),
        ("channel", "판매처"),
        ("phone", "연락처"),
        ("zipcode", "우편번호"),
        ("address1", "주소"),
        ("address2", "상세주소"),
        ("message", "배송메세지"),
        ("serial_number", "일련번호"),
    )
    REQUIRED_KEYS = {"order_number", "product_name", "quantity", "recipient"}

    def __init__(self, file_path: str, parent=None):
        super().__init__(parent)
        self.file_path = file_path
        self.profile: dict = {}
        row_index, headers = suggest_header_row(file_path)
        self.header_row = row_index
        self.headers = [header for header in headers if header]
        self.setWindowTitle("새 주문 파일 양식 등록")
        self.resize(650, 650)
        self.name_edit = QLineEdit(Path(file_path).stem)
        self.combos: dict[str, QComboBox] = {}
        form = QFormLayout()
        form.addRow("양식 이름 *", self.name_edit)
        for key, label in self.FIELD_LABELS:
            combo = QComboBox()
            combo.addItem("(사용 안 함)", "")
            for header in self.headers:
                combo.addItem(header, header)
            aliases = {"".join(alias.lower().split()) for alias in COLUMN_ALIASES.get(key, [])}
            selected = next(
                (index for index, header in enumerate(self.headers, start=1)
                 if "".join(header.lower().split()) in aliases),
                0,
            )
            combo.setCurrentIndex(selected)
            self.combos[key] = combo
            form.addRow(label, combo)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("양식 저장 후 불러오기")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        intro = QLabel(
            f"처음 보는 주문 파일입니다. {self.header_row + 1}행을 제목 행으로 분석했습니다.\n"
            "각 항목에 해당하는 원본 열을 한 번만 확인하면 다음 파일부터 자동 인식합니다."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def accept(self) -> None:
        name = self.name_edit.text().strip()
        mapping = {key: str(combo.currentData() or "") for key, combo in self.combos.items()}
        missing = [key for key in self.REQUIRED_KEYS if not mapping.get(key)]
        if not name or missing:
            QMessageBox.warning(self, "필수 연결", "양식 이름과 별표(*) 항목을 모두 연결하세요.")
            return
        self.profile = {
            "id": str(uuid.uuid4()),
            "name": f"판매처 직접파일 · {name}",
            "mapping": mapping,
        }
        upsert_format(self.profile)
        super().accept()


class DirectSuggestionDialog(QDialog):
    def __init__(self, orders: list[dict], items: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("판매상품 자동 추천 · 일괄 확인")
        self.resize(1180, 650)
        self.entries: list[dict] = []
        seen: set[str] = set()
        for order in orders:
            if order.get("status") not in {"missing", "ambiguous"}:
                continue
            key = compact(order_source_text(order))
            if not key or key in seen:
                continue
            seen.add(key)
            self.entries.append({"key": key, "order": order, "suggestion": suggest_direct_order(order, items)})

        auto_count = sum(entry["suggestion"]["status"] == "auto" for entry in self.entries)
        review_count = len(self.entries) - auto_count
        info = QLabel(
            f"고유 미등록 조합 {len(self.entries):,}개 · 자동확정 가능 {auto_count:,}개 · 확인 필요 {review_count:,}개\n"
            "모델·색상·옵션 후보가 하나로 확실한 항목만 일괄 확정됩니다. 확인 필요 항목은 저장하지 않습니다."
        )
        info.setWordWrap(True)
        self.table = QTableWidget(len(self.entries), 5)
        self.table.setHorizontalHeaderLabels(["판정", "모델", "판매처 상품·옵션", "추천 DB 품목", "추천 근거"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        for row_index, entry in enumerate(self.entries):
            order, suggestion = entry["order"], entry["suggestion"]
            values = [
                "자동확정" if suggestion["status"] == "auto" else "확인 필요",
                order.get("model", ""),
                " / ".join(filter(None, [order.get("product_name", ""), order.get("options", "")])),
                components_text(suggestion["components"]),
                suggestion["reason"],
            ]
            color = QColor("#d9ead3") if suggestion["status"] == "auto" else QColor("#fce5cd")
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setBackground(color)
                self.table.setItem(row_index, column, item)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(f"자동 추천 {auto_count:,}개 일괄 확정")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("나중에 확인")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(auto_count > 0)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addWidget(self.table)
        layout.addWidget(buttons)

    def confirmed_entries(self) -> list[dict]:
        return [entry for entry in self.entries if entry["suggestion"]["status"] == "auto"]

    def review_entries(self) -> list[dict]:
        return [entry for entry in self.entries if entry["suggestion"]["status"] != "auto"]


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
        self.candidates.itemDoubleClicked.connect(lambda *_: self.add_item())
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
                 "duty_locations": duty_locations, "aliases": aliases, "client": client,
                 "auth_user_id": str(auth_result.user.id), "app_role": app_role},
            )
        except Exception as exc:
            self.failed.emit(str(exc))


class FileDropZone(QFrame):
    filesDropped = Signal(list)

    def __init__(self, parent=None, label_text: str = "엑셀 또는 PDF 파일을 여기에 드래그 앤 드롭하세요", allowed_suffixes: set[str] | None = None):
        super().__init__(parent)
        self.allowed_suffixes = allowed_suffixes or {".xls", ".xlsx", ".pdf"}
        self.setAcceptDrops(True)
        self.setMinimumHeight(76)
        self.setStyleSheet(
            "QFrame { border: 2px dashed #48bdb7; border-radius: 20px; "
            "background: #e9f8f6; color: #172321; }"
        )
        layout = QVBoxLayout(self)
        label = QLabel(label_text)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)

    def dragEnterEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls()]
        if paths and all(Path(path).suffix.lower() in self.allowed_suffixes for path in paths):
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls()]
        self.filesDropped.emit(paths)
        event.acceptProposedAction()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.worker = None
        self.update_worker = None
        self.matcher = None
        self.supabase_client = None
        self.catalog: dict = {}
        self.current_mode = "parcel"
        self.current_orders: list[dict[str, str]] = []
        self.duty_locations = load_locations()
        self.selected_location_name = ""
        self.is_admin = False
        self.setWindowTitle("REQM 출고 관리")
        self.resize(1420, 860)
        self.setStyleSheet("""
            QMainWindow, QWidget#mainContainer { background: #f7f7f3; color: #151515; font-family: '맑은 고딕'; font-size: 13px; }
            QLabel#appTitle { color: #111111; font-size: 30px; font-weight: 900; letter-spacing: -1px; }
            QLabel#appSubtitle { color: #6f716f; font-size: 13px; padding-left: 3px; }
            QLabel#versionLabel { color: #a3a5a2; font-size: 10px; padding: 0 0 4px 4px; }
            QLabel#sectionTitle { color: #111111; font-size: 18px; font-weight: 850; padding: 7px 2px; }
            QLabel#statusCard { background: #e3f6f3; color: #244945; border: none; border-radius: 17px; padding: 13px 17px; }
            QFrame#loginCard, QFrame#fileCard, QFrame#locationCard { background: #ffffff; border: 1px solid #e3e3df; border-radius: 20px; }
            QFrame#fileCard { background: #f0f0ed; border: none; }
            QFrame#locationCard { background: #f7efe2; border: none; }
            QLineEdit, QComboBox { background: #ffffff; color: #171717; border: 1px solid #d8d8d3; border-radius: 13px; padding: 10px 13px; selection-background-color: #5bcac2; selection-color: #111111; }
            QLineEdit:focus, QComboBox:focus { border: 1px solid #38aaa3; }
            QPushButton { background: #ffffff; color: #151515; border: 1px solid #cacac5; border-radius: 15px; padding: 10px 16px; font-weight: 700; }
            QPushButton:hover { background: #e9f8f6; border-color: #48bdb7; }
            QPushButton:pressed { background: #d6f0ed; }
            QPushButton:disabled { background: #e9e9e5; color: #a6a7a4; border-color: #e1e1dc; }
            QPushButton#primaryButton { background: #121212; color: #ffffff; border: none; padding: 13px 22px; font-size: 14px; }
            QPushButton#primaryButton:hover { background: #2f6662; }
            QPushButton#fileButton { background: #121212; color: #ffffff; border: none; padding: 8px 14px; font-size: 13px; }
            QPushButton#fileButton:hover { background: #2f6662; }
            QPushButton#exportButton { background: #121212; color: #ffffff; border: none; padding: 14px 28px; font-size: 16px; }
            QPushButton#exportButton:hover { background: #2f6662; }
            QPushButton#adminButton { background: #ffffff; color: #151515; border-color: #cfcfca; padding: 9px 16px; }
            QTableWidget { background: #ffffff; alternate-background-color: #fafaf7; border: 1px solid #deded9; border-radius: 16px; gridline-color: #ecece8; selection-background-color: #d9f3f0; selection-color: #111111; }
            QHeaderView::section { background: #ecece8; color: #1b1b1b; border: none; border-right: 1px solid #dadad5; border-bottom: 1px solid #d6d6d1; padding: 11px; font-weight: 800; }
            QScrollBar:vertical { background: #ecece8; width: 22px; margin: 2px; border-radius: 10px; }
            QScrollBar::handle:vertical { background: #8fc9c4; min-height: 48px; border-radius: 9px; margin: 2px; }
            QScrollBar::handle:vertical:hover { background: #55aaa4; }
            QScrollBar:horizontal { background: #ecece8; height: 22px; margin: 2px; border-radius: 10px; }
            QScrollBar::handle:horizontal { background: #8fc9c4; min-width: 48px; border-radius: 9px; margin: 2px; }
            QScrollBar::handle:horizontal:hover { background: #55aaa4; }
            QScrollBar::add-line, QScrollBar::sub-line { width: 0px; height: 0px; }
            QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
        """)

        title = QLabel("REQM  출고 관리")
        title.setObjectName("appTitle")
        version_label = QLabel(f"v{APP_VERSION}")
        version_label.setObjectName("versionLabel")
        subtitle = QLabel("주문 파일을 자동 분석하고 정확한 출고 데이터로 변환합니다")
        subtitle.setObjectName("appSubtitle")
        self.email = QLineEdit()
        self.email.setPlaceholderText("프로그램 계정 이메일")
        self.email.setText("")
        self.password = QLineEdit()
        self.password.setPlaceholderText("비밀번호")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.login_button = QPushButton("로그인")
        self.login_button.setObjectName("primaryButton")
        self.login_button.setFixedWidth(84)
        self.b2c_button = QPushButton("B2C 엑셀 파일 (셀메이트)")
        self.b2c_button.setEnabled(False)
        self.b2b_button = QPushButton("B2B 엑셀 파일 (면세점)")
        self.b2b_button.setEnabled(False)
        self.auto_button = QPushButton("📁  출고 작업 파일 선택")
        self.auto_button.setObjectName("fileButton")
        self.auto_button.setFixedHeight(34)
        self.auto_button.setMaximumWidth(175)
        self.auto_button.setEnabled(False)
        self.db_button = QPushButton("▣  DB 관리")
        self.db_button.setObjectName("adminButton")
        self.db_button.setMaximumWidth(155)
        self.db_button.setEnabled(False)
        self.settings_button = QPushButton("설정")
        self.settings_button.setObjectName("adminButton")
        self.settings_button.setMaximumWidth(100)
        self.update_button = QPushButton("업데이트")
        self.update_button.setObjectName("adminButton")
        self.update_button.setMaximumWidth(105)
        self.export_button = QPushButton("택배 출고용 변환")
        self.export_button.setObjectName("exportButton")
        self.export_button.setEnabled(False)
        self.status = QLabel("공개용 API 키 설정 후 연결을 확인하세요.")
        self.status.setObjectName("statusCard")
        self.status.setWordWrap(True)

        self.table = QTableWidget()
        headers = [
            "상태", "DB 대조 상품", "출고 품목코드", "판정 이유", "원본행", "원본 품목코드", "주문번호",
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
        layout.setContentsMargins(24, 14, 24, 16)
        layout.setSpacing(8)
        login_row = QHBoxLayout()
        login_row.setContentsMargins(14, 12, 14, 12)
        login_row.setSpacing(10)
        self.email.setFixedWidth(300)
        self.password.setFixedWidth(250)
        login_row.addWidget(self.email)
        login_row.addWidget(self.password)
        login_row.addWidget(self.login_button)
        self.header_row = QHBoxLayout()
        title_line = QHBoxLayout()
        title_line.setSpacing(2)
        title_line.addWidget(title)
        title_line.addWidget(version_label, 0, Qt.AlignmentFlag.AlignBottom)
        title_block = QWidget()
        title_block.setLayout(title_line)
        self.header_row.addWidget(title_block)
        self.header_row.addStretch(1)
        self.header_row.addWidget(self.db_button)
        self.header_row.addWidget(self.update_button)
        self.header_row.addWidget(self.settings_button)
        layout.addLayout(self.header_row)
        layout.addWidget(subtitle)
        self.login_row = login_row
        self.login_card = QFrame()
        self.login_card.setObjectName("loginCard")
        self.login_card.setLayout(login_row)
        self.login_card.setMaximumWidth(690)
        file_card = QFrame()
        file_card.setObjectName("fileCard")
        file_layout = QVBoxLayout(file_card)
        file_layout.setContentsMargins(12, 8, 12, 9)
        file_layout.setSpacing(5)
        file_label = QLabel("출고 파일 입력  ·  Excel / CSV / PDF")
        file_label.setObjectName("appSubtitle")
        file_label.setWordWrap(True)
        file_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.order_drop_zone = FileDropZone(
            label_text="📄\n드래그 앤 드롭",
            allowed_suffixes={".xls", ".xlsx", ".csv", ".pdf"},
        )
        self.order_drop_zone.setFixedSize(92, 92)
        self.order_drop_zone.filesDropped.connect(self.load_dropped_order_files)
        file_layout.addWidget(file_label)
        file_layout.addWidget(self.auto_button, 0, Qt.AlignmentFlag.AlignCenter)
        file_layout.addWidget(self.order_drop_zone, 0, Qt.AlignmentFlag.AlignCenter)
        file_card.setFixedWidth(215)
        top_work_row = QHBoxLayout()
        top_work_row.setSpacing(18)
        top_work_row.addWidget(self.login_card, 0, Qt.AlignmentFlag.AlignTop)
        top_work_row.addStretch(1)
        top_work_row.addWidget(file_card, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        layout.addLayout(top_work_row)
        layout.addWidget(self.status)
        location_row = QHBoxLayout()
        location_row.setContentsMargins(12, 8, 12, 8)
        location_row.setSpacing(8)
        self.location_combo = QComboBox()
        self.location_combo.setPlaceholderText("면세점 출고지를 선택하세요")
        self.location_combo.setFixedWidth(340)
        self.location_manage_button = QPushButton("출고지 정보 관리")
        self.location_apply_button = QPushButton("선택 출고지 적용")
        self.location_apply_button.setEnabled(False)
        location_row.addWidget(QLabel("📍  면세점 출고지"))
        location_row.addWidget(self.location_combo)
        location_row.addWidget(self.location_manage_button)
        location_row.addWidget(self.location_apply_button)
        location_card = QFrame()
        location_card.setObjectName("locationCard")
        location_card.setLayout(location_row)
        location_card.setMaximumWidth(850)
        analysis_title = QLabel("▥  분석 결과")
        analysis_title.setObjectName("sectionTitle")
        analysis_header_row = QHBoxLayout()
        analysis_header_row.setSpacing(14)
        analysis_header_row.addWidget(analysis_title)
        analysis_header_row.addStretch(1)
        analysis_header_row.addWidget(location_card, 0, Qt.AlignmentFlag.AlignRight)
        layout.addLayout(analysis_header_row)
        layout.addWidget(self.table, 1)
        export_row = QHBoxLayout()
        export_row.addStretch(1)
        export_row.addWidget(self.export_button)
        layout.addLayout(export_row)
        container = QWidget()
        container.setObjectName("mainContainer")
        container.setLayout(layout)
        self.setCentralWidget(container)
        self.login_button.clicked.connect(self.login)
        self.settings_button.clicked.connect(self.open_account_settings)
        self.update_button.clicked.connect(self.check_for_updates)
        self.email.returnPressed.connect(self.login)
        self.password.returnPressed.connect(self.login)
        self.b2c_button.clicked.connect(self.select_b2c_file)
        self.b2b_button.clicked.connect(self.select_b2b_file)
        self.auto_button.clicked.connect(lambda: self.select_file("auto"))
        self.db_button.clicked.connect(self.open_db_manager)
        self.export_button.clicked.connect(self.export_file)
        self.location_manage_button.clicked.connect(self.manage_locations)
        self.location_apply_button.clicked.connect(self.apply_location)
        self.table.cellDoubleClicked.connect(self.edit_match)
        self.refresh_location_combo()

    def refresh_location_combo(self, preferred_channel: str = "") -> None:
        selected_id = self.location_combo.currentData() if hasattr(self, "location_combo") else ""
        self.duty_locations = load_locations()
        self.location_combo.clear()
        selected_index = -1
        for index, row in enumerate(self.duty_locations):
            label = row.get("name", "")
            self.location_combo.addItem(label, row.get("id", ""))
            if row.get("id") == selected_id:
                selected_index = index
            elif selected_index < 0 and preferred_channel and preferred_channel in row.get("channel", ""):
                selected_index = index
        if selected_index >= 0:
            self.location_combo.setCurrentIndex(selected_index)
        elif self.location_combo.count():
            self.location_combo.setCurrentIndex(0)

    def manage_locations(self) -> None:
        dialog = DutyLocationDialog(self.duty_locations, self)
        dialog.exec()
        self.refresh_location_combo()

    def apply_location(self) -> None:
        location_id = self.location_combo.currentData()
        location = next((row for row in self.duty_locations if row.get("id") == location_id), None)
        if not location:
            QMessageBox.warning(self, "출고지 선택", "적용할 면세점 출고지를 선택하세요.")
            return
        if not self.current_orders or self.current_mode != "duty_free":
            QMessageBox.information(self, "면세점 파일", "먼저 면세점 출고 파일을 불러오세요.")
            return
        for order in self.current_orders:
            order["recipient"] = location.get("recipient", "")
            order["phone"] = location.get("phone", "")
            order["zipcode"] = location.get("zipcode", "")
            order["address"] = location.get("address", "")
            if location.get("message"):
                order["message"] = location["message"]
        self.selected_location_name = location.get("name", "")
        self.populate_table(self.current_orders)
        self.export_button.setEnabled(True)
        self.status.setText(f"면세점 출고지 적용 완료: {self.selected_location_name} · {len(self.current_orders):,}행")

    def login(self) -> None:
        if self.supabase_client is not None:
            self.logout()
            return
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
        self.supabase_client = catalog["client"]
        self.catalog = catalog
        self.is_admin = catalog.get("app_role") == "admin"
        self.db_button.setEnabled(self.is_admin)
        self.matcher = ProductMatcher(catalog["items"], catalog["products"], catalog["components"], catalog["aliases"])
        self.login_row.removeWidget(self.login_button)
        self.login_card.hide()
        self.login_button.setText("로그아웃")
        self.login_button.setObjectName("adminButton")
        self.login_button.setMaximumWidth(90)
        self.header_row.addWidget(self.login_button)
        self.login_button.style().unpolish(self.login_button)
        self.login_button.style().polish(self.login_button)
        self.status.setText(
            f"DB 준비 완료: 품목 {count:,}개 · 등록상품 {len(catalog['products']):,}개 · "
            f"구성품 {len(catalog['components']):,}개 · 권한: {'관리자' if self.is_admin else '일반 사용자(조회 전용)'}"
        )

    def open_account_settings(self) -> None:
        dialog = AccountSettingsDialog(
            self.email.text(), self.password.text(), self.supabase_client is not None, self
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        email, password = dialog.credentials()
        self.email.setText(email)
        self.password.setText(password)
        if self.supabase_client is not None:
            self.status.setText("계정 입력값을 변경했습니다. 변경된 계정은 다음 로그인부터 사용됩니다.")

    def logout(self) -> None:
        try:
            self.supabase_client.auth.sign_out()
        except Exception:
            pass
        self.supabase_client = None
        self.matcher = None
        self.catalog = {}
        self.is_admin = False
        self.current_orders = []
        self.table.setRowCount(0)
        self.db_button.setEnabled(False)
        self.auto_button.setEnabled(False)
        self.b2c_button.setEnabled(False)
        self.b2b_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.header_row.removeWidget(self.login_button)
        self.login_row.addWidget(self.login_button)
        self.login_button.setText("로그인")
        self.login_button.setObjectName("primaryButton")
        self.login_button.setMaximumWidth(100)
        self.login_button.style().unpolish(self.login_button)
        self.login_button.style().polish(self.login_button)
        self.login_card.show()
        self.status.setText("로그아웃되었습니다. 다시 로그인해 주세요.")

    def check_for_updates(self) -> None:
        if self.update_worker and self.update_worker.isRunning():
            return
        self.update_button.setEnabled(False)
        self.status.setText(f"업데이트 확인 중... 현재 버전 {APP_VERSION}")
        self.update_worker = UpdateCheckWorker()
        self.update_worker.succeeded.connect(self.on_update_checked)
        self.update_worker.failed.connect(self.on_update_failed)
        self.update_worker.start()

    def on_update_checked(self, manifest: dict) -> None:
        self.update_button.setEnabled(True)
        latest = str(manifest.get("version", "0"))
        if version_key(latest) <= version_key(APP_VERSION):
            self.status.setText(f"최신 버전입니다. 현재 {APP_VERSION} · 배포 {latest}")
            QMessageBox.information(self, "업데이트", f"현재 최신 버전 {APP_VERSION}을 사용 중입니다.")
            return
        notes = str(manifest.get("notes", "새 기능과 오류 수정이 포함되어 있습니다."))
        answer = QMessageBox.question(
            self,
            "새 업데이트 발견",
            f"새 버전 {latest}이 있습니다. (현재 {APP_VERSION})\n\n{notes}\n\n지금 다운로드할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.status.setText(f"업데이트 보류 · 현재 버전 {APP_VERSION}")
            return
        self.update_button.setEnabled(False)
        self.status.setText(f"새 버전 {latest} 다운로드 및 보안 검사 중...")
        self.update_worker = UpdateDownloadWorker(manifest)
        self.update_worker.succeeded.connect(self.install_downloaded_update)
        self.update_worker.failed.connect(self.on_update_failed)
        self.update_worker.start()

    def on_update_failed(self, message: str) -> None:
        self.update_button.setEnabled(True)
        self.status.setText("업데이트 확인 실패 · 현재 버전은 그대로 사용할 수 있습니다.")
        QMessageBox.warning(self, "업데이트 실패", message)

    def install_downloaded_update(self, downloaded_path: str, manifest: dict) -> None:
        self.update_button.setEnabled(True)
        if not getattr(sys, "frozen", False):
            QMessageBox.information(self, "개발 실행", f"다운로드와 검증은 완료됐습니다.\n{downloaded_path}")
            return
        current_exe = Path(sys.executable).resolve()
        source_exe = Path(downloaded_path).resolve()
        update_dir = source_exe.parent
        script_path = update_dir / "apply_reqm_update.ps1"
        def ps_quote(path: Path) -> str:
            return str(path).replace("'", "''")
        script = (
            f"$target = '{ps_quote(current_exe)}'\n"
            f"$source = '{ps_quote(source_exe)}'\n"
            f"$pidToWait = {os.getpid()}\n"
            "$log = Join-Path (Split-Path -Parent $source) 'update.log'\n"
            "Set-Content -LiteralPath $log -Value ('Update started: ' + (Get-Date)) -Encoding UTF8\n"
            "Wait-Process -Id $pidToWait -ErrorAction SilentlyContinue\n"
            "$copied = $false\n"
            "for ($attempt = 1; $attempt -le 60; $attempt++) {\n"
            "    try {\n"
            "        Copy-Item -LiteralPath $source -Destination $target -Force -ErrorAction Stop\n"
            "        $sourceHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $source).Hash\n"
            "        $targetHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $target).Hash\n"
            "        if ($sourceHash -ne $targetHash) { throw 'Copied file hash mismatch' }\n"
            "        $copied = $true\n"
            "        Add-Content -LiteralPath $log -Value ('Copy succeeded: attempt ' + $attempt) -Encoding UTF8\n"
            "        break\n"
            "    } catch {\n"
            "        Add-Content -LiteralPath $log -Value ('Copy retry ' + $attempt + ': ' + $_.Exception.Message) -Encoding UTF8\n"
            "        Start-Sleep -Seconds 1\n"
            "    }\n"
            "}\n"
            "if (-not $copied) {\n"
            "    Add-Content -LiteralPath $log -Value 'Update failed: target remained locked.' -Encoding UTF8\n"
            "    exit 1\n"
            "}\n"
            "$env:PYINSTALLER_RESET_ENVIRONMENT = '1'\n"
            "Get-ChildItem Env: | Where-Object { $_.Name -like '_PYI_*' } | ForEach-Object { Remove-Item ('Env:' + $_.Name) -ErrorAction SilentlyContinue }\n"
            "Start-Sleep -Seconds 2\n"
            "Start-Process -FilePath $target -WorkingDirectory (Split-Path -Parent $target)\n"
            "Remove-Item -LiteralPath $source -Force -ErrorAction SilentlyContinue\n"
            "Add-Content -LiteralPath $log -Value ('Restart requested: ' + (Get-Date)) -Encoding UTF8\n"
        )
        script_path.write_text(script, encoding="utf-8-sig")
        answer = QMessageBox.question(
            self,
            "업데이트 준비 완료",
            f"버전 {manifest.get('version')} 다운로드와 보안 검사가 완료됐습니다.\n"
            "프로그램을 종료하고 업데이트한 뒤 자동으로 다시 실행할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.status.setText("업데이트 파일 준비 완료 · 업데이트 버튼을 다시 눌러 적용할 수 있습니다.")
            return
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(script_path)],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        QApplication.instance().quit()

    def open_db_manager(self) -> None:
        if not self.is_admin:
            QMessageBox.warning(self, "권한 없음", "관리자에게 DB 수정 권한을 요청하세요.")
            return
        ItemManagerDialog(self.supabase_client, self.catalog["items"], self.catalog["barcodes"], self).exec()
        self.reload_catalog_after_db_change()

    def reload_catalog_after_db_change(self) -> None:
        """관리 화면에서 변경된 Supabase 데이터를 즉시 다시 불러온다."""
        def fetch_all(table: str) -> list[dict]:
            rows: list[dict] = []
            start = 0
            while True:
                page = self.supabase_client.table(table).select("*").range(start, start + 999).execute().data or []
                rows.extend(page)
                if len(page) < 1000:
                    return rows
                start += 1000

        try:
            for key, table in (
                ("items", "items"),
                ("products", "registered_products"),
                ("components", "product_components"),
                ("barcodes", "item_barcodes"),
                ("aliases", "item_aliases"),
            ):
                self.catalog[key] = fetch_all(table)
            self.matcher = ProductMatcher(
                self.catalog["items"], self.catalog["products"],
                self.catalog["components"], self.catalog["aliases"],
            )
            rematched = self.rematch_deleted_items()
            self.status.setText(
                f"DB 변경사항 새로고침 완료: 품목 {len(self.catalog['items']):,}개 · "
                f"등록상품 {len(self.catalog['products']):,}개 · 별칭 {len(self.catalog['aliases']):,}개 · "
                f"삭제 품목 매칭 재검사 {rematched:,}행"
            )
        except Exception as exc:
            QMessageBox.warning(
                self, "DB 새로고침 실패",
                f"DB 변경은 저장됐지만 프로그램에 다시 불러오지 못했습니다.\n프로그램을 재실행해 주세요.\n\n{exc}",
            )

    def rematch_deleted_items(self) -> int:
        if not self.current_orders or self.current_mode != "parcel":
            return 0
        active_codes = {
            str(item.get("item_code", ""))
            for item in self.catalog.get("items", []) if item.get("is_active", True)
        }
        changed = 0
        for order in self.current_orders:
            codes = [
                part.strip().split("×", 1)[0].strip()
                for part in str(order.get("components", "") or "").split("+")
                if part.strip()
            ]
            deleted_codes = [code for code in codes if code and code not in active_codes]
            if not deleted_codes:
                continue
            result = self.matcher.match(order)
            result["status"] = "ambiguous" if result.get("status") != "missing" else "missing"
            result["reason"] = (
                f"삭제된 DB 품목({', '.join(deleted_codes)}) 매칭 제거 · 더블클릭하여 재연결 | "
                + str(result.get("reason", ""))
            )
            order.update(result)
            changed += 1
        if changed:
            self.populate_table(self.current_orders)
        return changed

    def on_failure(self, message: str) -> None:
        self.login_button.setEnabled(True)
        self.status.setText("연결 실패")
        QMessageBox.critical(self, "Supabase 연결 실패", message)

    def select_b2c_file(self) -> None:
        self.select_file("b2c")

    def select_b2b_file(self) -> None:
        self.select_file("b2b")

    def select_file(self, expected_type: str) -> None:
        title = "출고 파일 자동 판별" if expected_type == "auto" else ("B2C 셀메이트 주문 파일 선택" if expected_type == "b2c" else "B2B 면세점 출고 요청 파일 선택")
        path, _ = QFileDialog.getOpenFileName(
            self,
            title,
            "",
            "출고 파일 (*.xls *.xlsx *.csv *.pdf)",
        )
        if not path:
            return
        self.load_order_file(path, expected_type)

    def load_dropped_order_files(self, paths: list[str]) -> None:
        if not paths:
            return
        if len(paths) > 1:
            QMessageBox.information(self, "파일 한 개씩 처리", "출고 파일은 한 번에 한 개씩 분석합니다. 첫 번째 파일을 불러옵니다.")
        self.load_order_file(paths[0], "auto")

    def load_order_file(self, path: str, expected_type: str = "auto") -> None:
        try:
            if self.matcher is None:
                raise RuntimeError("먼저 Supabase에 로그인해 DB를 불러오세요.")
            duty_result = load_duty_free(path)
            if duty_result:
                if expected_type not in {"b2b", "auto"}:
                    raise ValueError("면세점 B2B 파일로 감지됐습니다. B2B 엑셀 파일 버튼을 사용하세요.")
                orders, detected_type = duty_result
                if all(order.get("match_method") == "name_or_code" for order in orders):
                    for order in orders:
                        order.update(self.matcher.match(order))
                else:
                    match_barcodes(orders, self.catalog.get("barcodes", []), self.catalog.get("items", []))
                columns = {"duty_free": 1}
                self.current_mode = "duty_free"
                embedded_destination = bool(orders) and all(
                    order.get("embedded_destination") and order.get("recipient") and order.get("address")
                    for order in orders
                )
                self.selected_location_name = detected_type if embedded_destination else ""
                self.refresh_location_combo(detected_type)
                self.location_apply_button.setEnabled(True)
                self.export_button.setText(
                    "매장 출고용 변환" if detected_type.startswith("트래블메이트") else "면세점 출고용 변환"
                )
                self.export_button.setEnabled(embedded_destination)
            else:
                if expected_type not in {"b2c", "auto"}:
                    raise ValueError("면세점 B2B 양식을 찾지 못했습니다. B2C 파일이라면 B2C 엑셀 파일 버튼을 사용하세요.")
                try:
                    orders, columns = load_orders(path)
                except ValueError as original_error:
                    if "필수 열" not in str(original_error) and "양식" not in str(original_error):
                        raise
                    format_dialog = FileFormatDialog(path, self)
                    if format_dialog.exec() != QDialog.DialogCode.Accepted:
                        raise original_error
                    orders, columns = load_orders(path, format_dialog.profile)
                for order in orders:
                    order.update(self.matcher.match(order))
                    if Path(path).suffix.lower() == ".pdf":
                        missing_shipping = [
                            label for key, label in (("order_number", "주문번호"), ("recipient", "수령인"))
                            if not str(order.get(key, "")).strip()
                        ]
                        if missing_shipping:
                            order["status"] = "missing"
                            order["reason"] = (
                                "PDF 필수 출고 정보 누락: " + ", ".join(missing_shipping)
                                + " · 원본 PDF 표의 열 제목을 확인하세요 | " + order.get("reason", "")
                            )
                    if order.get("manual_input_detected"):
                        order["status"] = "similar"
                        order["reason"] = "재고매칭 표준 열 뒤 수기 추가 품목 감지 · 검토 필요 | " + order.get("reason", "")
                detected_type = orders[0].get("source_format", "일반 택배") if orders else "일반 택배"
                self.current_mode = "parcel"
                self.location_apply_button.setEnabled(False)
                self.export_button.setText("택배 출고용 변환")
                self.export_button.setEnabled(True)
            self.mark_duplicates(orders)
            if detected_type.startswith("판매처 직접파일"):
                suggestion_dialog = DirectSuggestionDialog(orders, self.catalog.get("items", []), self)
                if suggestion_dialog.entries and suggestion_dialog.exec() == QDialog.DialogCode.Accepted:
                    self.apply_direct_suggestions(
                        orders,
                        suggestion_dialog.confirmed_entries(),
                        suggestion_dialog.review_entries(),
                    )
        except Exception as exc:
            QMessageBox.critical(self, "파일 분석 실패", str(exc))
            return
        self.current_orders = orders
        self.populate_table(self.current_orders)
        counts = {key: sum(1 for row in orders if row.get("status") == key) for key in ("exact", "similar", "ambiguous", "missing", "barcode_error")}
        self.status.setText(
            f"{detected_type} 분석 완료 {len(orders):,}행 · 정확 {counts['exact']:,} · 유사 {counts['similar']:,} · "
            f"확인필요 {counts['ambiguous']:,} · 미등록 {counts['missing']:,} · "
            f"바코드오류 {counts['barcode_error']:,} · {len(columns)}개 열 인식"
        )

    def apply_direct_suggestions(self, orders: list[dict], confirmed: list[dict], reviews: list[dict]) -> None:
        confirmed_by_key = {entry["key"]: entry["suggestion"] for entry in confirmed}
        review_by_key = {entry["key"]: entry["suggestion"] for entry in reviews}
        payloads = []
        for entry in confirmed:
            order, suggestion = entry["order"], entry["suggestion"]
            payloads.append(
                {
                    "source_channel": order.get("channel", ""),
                    "source_product_name": order.get("product_name", ""),
                    "source_options": order.get("options", ""),
                    "normalized_source": entry["key"],
                    "components": component_payload(suggestion["components"]),
                    "is_active": True,
                }
            )
        for order in orders:
            if order.get("status") == "duplicate":
                continue
            key = compact(order_source_text(order))
            suggestion = confirmed_by_key.get(key)
            if suggestion:
                components = suggestion["components"]
                order.update(
                    {
                        "status": "alias",
                        "matched_product": " / ".join(str(item.get("standard_name", "")) for item in components),
                        "components": components_text(components),
                        "reason": "모델·옵션 자동 추천 일괄 확정",
                    }
                )
            elif key in review_by_key:
                suggestion = review_by_key[key]
                order.update(
                    {
                        "status": "ambiguous",
                        "matched_product": " / ".join(str(item.get("standard_name", "")) for item in suggestion["components"]),
                        "components": components_text(suggestion["components"]),
                        "reason": "자동 추천 확인 필요 · " + suggestion["reason"],
                    }
                )
        if not payloads:
            return
        if not self.is_admin:
            QMessageBox.warning(self, "DB 저장 안 함", "자동 추천은 현재 파일에 적용했지만 관리자 권한이 없어 다음 파일용 연결 규칙은 저장하지 못했습니다.")
            return
        try:
            self.supabase_client.table("item_aliases").upsert(
                payloads, on_conflict="source_channel,normalized_source"
            ).execute()
            for payload in payloads:
                self.matcher.aliases[(payload["source_channel"], payload["normalized_source"])] = payload
            QMessageBox.information(self, "일괄 확정 완료", f"자동 추천 {len(payloads):,}개를 적용하고 다음 파일용 DB 연결 규칙으로 저장했습니다.")
        except Exception as exc:
            QMessageBox.warning(self, "별칭 일괄 저장 실패", f"현재 파일에는 적용했지만 DB 저장에 실패했습니다.\n{exc}")

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
            "status", "matched_product", "components", "reason", "source_row", "source_item_code", "order_number",
            "channel", "product_name", "options", "quantity", "recipient", "phone", "zipcode",
            "address", "matched_name",
        ]
        labels = {"exact": "정확", "similar": "유사", "ambiguous": "확인필요", "missing": "미등록", "barcode_error": "바코드오류", "manual": "수동확정", "alias": "별칭적용", "duplicate": "중복출고"}
        colors = {
            "exact": QColor("#d9ead3"),
            "similar": QColor("#fff2cc"),
            "ambiguous": QColor("#fce5cd"),
            "missing": QColor("#f4cccc"),
            "barcode_error": QColor("#e06666"),
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
                and candidate.get("model") == order.get("model")
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
                source_key = compact(order_source_text(order))
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
        if not self.current_orders:
            QMessageBox.warning(self, "저장할 데이터 없음", "먼저 주문 파일을 불러오세요.")
            return
        if self.current_mode == "duty_free" and not self.selected_location_name:
            QMessageBox.warning(self, "출고지 미적용", "면세점 출고지를 선택하고 '선택 출고지 적용'을 눌러주세요.")
            return
        unresolved = [row for row in self.current_orders if row.get("status") in {"missing", "ambiguous", "duplicate", "barcode_error"}]
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
            "면세점 출고 파일 저장" if self.current_mode == "duty_free" else "위킵 택배 출고 파일 저장",
            "면세점_출고.xlsx" if self.current_mode == "duty_free" else "위킵_택배출고.xlsx",
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
            history = [{"duplicate_key": self.duplicate_key(row), "order_number": row.get("order_number", ""), "sales_channel": row.get("channel", ""), "recipient": row.get("recipient", ""), "phone": row.get("phone", ""), "address": row.get("address", ""), "product_name": row.get("product_name", ""), "options": row.get("options", ""), "quantity": row.get("quantity", ""), "source_type": "duty_free" if self.current_mode == "duty_free" else "b2c"} for row in self.current_orders if row.get("order_number")]
            if history:
                self.supabase_client.table("shipment_history").upsert(history, on_conflict="duplicate_key").execute()
        except Exception as exc:
            QMessageBox.warning(self, "이력 저장 안내", f"Excel은 저장됐지만 중복 방지 이력을 Supabase에 기록하지 못했습니다.\n관리자용 SQL 적용 여부를 확인하세요.\n{exc}")
        QMessageBox.information(self, "저장 완료", f"위킵 출고 파일을 저장했습니다.\n{file_path}")

if __name__ == "__main__":
    remove_legacy_transfer_credentials()
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
