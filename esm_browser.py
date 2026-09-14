"""ESM 화면의 조회/엑셀 다운로드만 실행하는 브라우저 작업자."""
from __future__ import annotations

import queue
import re
import tempfile
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

from PySide6.QtCore import QThread, Signal

from esm_orders import EsmSession, STATUSES

LOGIN_URL = "https://signin.esmplus.com/login"
ORDERS_URL = "https://www.esmplus.com/Escrow/SmartDelivery/SmartDeliveryRequestManagement?menuCode=TDM366"
ESM_PROFILE_ROOT = Path.home() / ".reqm" / "esm-browser"


def browser_channels() -> tuple[str, str]:
    """Use Chrome first and keep Edge only as the compatibility fallback."""
    return "chrome", "msedge"


def date_windows(start: date, end: date):
    while start <= end:
        stop = min(start + timedelta(days=30), end)
        yield start, stop
        start = stop + timedelta(days=1)


def set_calendar(page, selector: str, value: date):
    field = page.locator(selector)
    if field.input_value() == value.isoformat():
        return
    page.locator(selector + " + img").click()
    calendar = page.locator("#ui-datepicker-div")
    calendar.locator(".ui-datepicker-year").select_option(str(value.year))
    calendar.locator(".ui-datepicker-month").select_option(str(value.month - 1))
    calendar.get_by_role("link", name=str(value.day), exact=True).click()
    if field.input_value() != value.isoformat():
        raise ValueError("ESM 조회 날짜가 적용되지 않았습니다.")


class EsmBrowserWorker(QThread):
    status_changed = Signal(str)
    ready = Signal(bool)
    session_started = Signal(object)
    collected = Signal(object)
    failed = Signal(str)
    collecting_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.commands = queue.Queue()
        self.cancelled = threading.Event()
        self.stopping = threading.Event()

    def collect(self, root, date_type, start, end):
        self.cancelled.clear()
        self.commands.put((root, date_type, start, end))

    def stop(self):
        self.cancelled.set()
        self.stopping.set()

    def check_cancel(self):
        if self.cancelled.is_set() or self.stopping.is_set():
            raise ValueError("수집을 중지했습니다. 이미 내려받은 원본은 보관됩니다.")

    def run(self):
        # Playwright 객체는 생성한 스레드 안에서만 사용한다. 프로그램은 비밀번호를
        # 저장하지 않고 브라우저 전용 프로필의 로그인 쿠키만 재사용한다.
        from playwright.sync_api import sync_playwright
        try:
            with sync_playwright() as pw:
                context = None
                selected_channel = ""
                for channel in browser_channels():
                    try:
                        profile = ESM_PROFILE_ROOT / channel
                        profile.mkdir(parents=True, exist_ok=True)
                        context = pw.chromium.launch_persistent_context(
                            str(profile), channel=channel, headless=False, accept_downloads=True,
                        )
                        selected_channel = channel
                        break
                    except Exception:
                        continue
                if context is None:
                    raise ValueError("Microsoft Edge 또는 Google Chrome을 설치한 후 다시 실행해주세요.")
                pages = context.pages
                page = pages[0] if pages else context.new_page()
                page.set_default_timeout(15000)
                page.goto(LOGIN_URL, wait_until="domcontentloaded")
                browser_label = "Chrome" if selected_channel == "chrome" else "Edge"
                self.status_changed.emit(
                    f"{browser_label}에서 ESM 로그인을 확인해주세요. 로그인 세션은 이 PC에서 재사용됩니다."
                )
                previous = False
                while not self.stopping.is_set():
                    if page.is_closed():
                        break
                    url = urlparse(page.url)
                    logged_in = url.hostname == "www.esmplus.com" and url.path.startswith(("/Home", "/Escrow"))
                    if logged_in != previous:
                        previous = logged_in
                        self.ready.emit(logged_in)
                        self.status_changed.emit("ESM 로그인 확인 · 조회 기간을 선택하고 수집을 시작하세요." if logged_in else "ESM 로그인이 필요합니다.")
                    try:
                        request = self.commands.get_nowait()
                    except queue.Empty:
                        page.wait_for_timeout(250)
                        continue
                    if not logged_in:
                        self.failed.emit("ESM 로그인 후 수집을 시작해주세요.")
                        self.collecting_changed.emit(False)
                        continue
                    self.collecting_changed.emit(True)
                    session = None
                    try:
                        session = EsmSession.create(*request)
                        self.session_started.emit(session)
                        self._collect(page, session, request[2], request[3])
                        self.collected.emit(session)
                    except Exception as exc:
                        if session:
                            session.manifest["state"] = "중지" if self.cancelled.is_set() else "실패"
                            session.save()
                        # 브라우저 예외에는 요청 주소/데이터가 포함될 수 있어 그대로 기록하지 않는다.
                        message = str(exc) if isinstance(exc, ValueError) else "ESM 화면 응답 또는 다운로드를 확인하지 못했습니다. 열린 브라우저를 확인하고 다시 수집해주세요."
                        self.failed.emit(message)
                    finally:
                        self.collecting_changed.emit(False)
                context.close()
        except Exception as exc:
            self.failed.emit(str(exc) if isinstance(exc, ValueError) else "ESM 브라우저를 열지 못했습니다. Chrome/Edge 설치와 네트워크 연결을 확인해주세요.")
        finally:
            self.ready.emit(False)

    def _collect(self, page, session, start, end):
        windows = list(date_windows(start, end))
        state_totals = {s: 0 for s in STATUSES}
        for part, (first, last) in enumerate(windows, 1):
            for status in STATUSES:
                self.check_cancel()
                self.status_changed.emit(f"{part}/{len(windows)} 구간 · {first} ~ {last} · {status} 조회 중")
                # 매 조회마다 빈 결과 화면에서 시작하여 이전 건수를 읽지 않는다.
                page.goto(ORDERS_URL, wait_until="domcontentloaded")
                page.locator("#searchAccount").select_option(label="A/G 전체")
                page.locator("#searchDateType").select_option(label=session.manifest["date_type"])
                set_calendar(page, "#searchEDT", last)
                set_calendar(page, "#searchSDT", first)
                page.locator("#searchDeliveryType").select_option(label=status)
                sub = page.locator("#searchOrderType")
                if sub.is_visible() and sub.locator("option", has_text=re.compile("^전체$")).count():
                    sub.select_option(label="전체")
                page.locator("#searchKeyword").fill("")
                page.locator("#btnSearch").click()
                page.locator("#spanSearchData").wait_for(state="visible", timeout=60000)
                description = page.locator("#spanSearchData").inner_text()
                if status not in description or first.isoformat() not in description or last.isoformat() not in description:
                    raise ValueError("ESM 검색 결과의 조회 조건이 요청과 다릅니다.")
                text = page.locator("#totalClaimCount").inner_text()
                match = re.search(r"\(([\d,]+)\)", text)
                if not match:
                    raise ValueError("ESM 조회 건수를 확인하지 못했습니다.")
                count = int(match.group(1).replace(",", ""))
                if count:
                    self._download(page, session, status, count)
                state_totals[status] += count
                session.manifest["statuses"][status] = {"state": "완료" if part == len(windows) else "진행 중", "count": state_totals[status]}
                session.save()
                self.status_changed.emit(f"{status}: {count:,}건 원본 확인 완료" if count else f"{status}: 0건 (원본 파일 없음)")
        self.check_cancel()
        session.finish()

    def _download(self, page, session, status, count):
        downloads = []
        listener = lambda download: downloads.append(download)
        page.on("download", listener)
        try:
            page.locator("#excelDown").click()
            deadline = time.monotonic() + 90
            notice_confirmed = False
            while not downloads and time.monotonic() < deadline:
                self.check_cancel()
                if not notice_confirmed and page.get_by_text("개인정보 다운로드 영역 안내", exact=True).is_visible():
                    page.get_by_role("button", name="확인", exact=True).click()
                    notice_confirmed = True
                page.wait_for_timeout(200)
            if not downloads:
                raise ValueError("원본 다운로드를 확인하지 못했습니다. ESM 브라우저의 안내를 확인한 뒤 다시 수집해주세요.")
            download = downloads[0]
            with tempfile.TemporaryDirectory(prefix="reqm-esm-") as temp:
                path = Path(temp) / "download.xls"
                download.save_as(path)
                if download.failure():
                    raise ValueError("ESM 파일 다운로드가 실패했습니다.")
                session.archive(path, status, count, download.suggested_filename)
        finally:
            page.remove_listener("download", listener)
