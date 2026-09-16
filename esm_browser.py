"""ESM 화면의 조회/엑셀 다운로드만 실행하는 브라우저 작업자."""
from __future__ import annotations

import queue
import re
import os
import shutil
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

ERROR_GUIDES = {
    "BROWSER_START": (
        "Chrome 실행 오류",
        "Chrome을 시작하지 못했습니다. 실행 중인 ESM Chrome 창을 닫고 다시 시도하거나 PC를 재부팅해주세요.",
    ),
    "BROWSER_NOT_FOUND": (
        "Chrome 설치 확인",
        "Google Chrome을 찾지 못했습니다. Chrome을 설치하거나 설치 경로를 확인해주세요.",
    ),
    "PROFILE_LOCKED": (
        "ESM 전용 Chrome 사용 중",
        "ESM 로그인용 Chrome이 이미 실행 중입니다. 기존 ESM Chrome 창을 모두 닫고 다시 시도해주세요.",
    ),
    "NETWORK": (
        "ESM 네트워크 연결 오류",
        "ESM 로그인 페이지에 연결하지 못했습니다. 인터넷·VPN·방화벽 상태를 확인한 뒤 다시 시도해주세요.",
    ),
    "LOGIN_CREDENTIALS": (
        "ESM 아이디·비밀번호 확인",
        "아이디 또는 비밀번호가 맞지 않습니다. 최근 비밀번호를 변경했다면 새 비밀번호로 직접 로그인해주세요.",
    ),
    "ACCOUNT_RESTRICTED": (
        "ESM 계정 상태 확인",
        "로그인 제한·휴면·계정 잠금 상태일 수 있습니다. ESM 안내에 따라 본인인증 또는 계정 복구를 완료해주세요.",
    ),
    "LOGIN_DELAYED": (
        "ESM 로그인이 완료되지 않음",
        "Chrome 로그인 화면이 계속 열려 있습니다. 비밀번호 변경, 추가 인증, 보안 문자 또는 계정 안내를 확인해주세요.",
    ),
    "SESSION_EXPIRED": (
        "ESM 로그인 세션 만료",
        "저장된 로그인 세션이 만료됐습니다. ESM 로그인을 다시 완료한 뒤 수집을 시작해주세요.",
    ),
    "SCREEN_CHANGED": (
        "ESM 화면 확인 필요",
        "ESM 화면 구조가 변경됐거나 조회 요소를 찾지 못했습니다. 열린 화면과 공지사항을 확인해주세요.",
    ),
    "DOWNLOAD": (
        "ESM 다운로드 확인 필요",
        "엑셀 다운로드가 완료되지 않았습니다. 개인정보 다운로드 안내창과 브라우저 다운로드 권한을 확인해주세요.",
    ),
}


def user_error(code: str, detail: str = "") -> str:
    title, guide = ERROR_GUIDES[code]
    suffix = f"\n\n상세: {detail}" if detail else ""
    return f"[ESM-{code}] {title}\n{guide}{suffix}"


def classify_browser_error(exc: Exception, stage: str = "browser") -> str:
    detail = str(exc)
    lowered = detail.casefold()
    if any(word in lowered for word in ("processsingleton", "user data directory is already in use", "profile in use")):
        return user_error("PROFILE_LOCKED")
    if any(word in lowered for word in ("err_name_not_resolved", "err_connection", "err_internet_disconnected", "timed out", "timeout")):
        return user_error("NETWORK")
    if stage == "download":
        return user_error("DOWNLOAD")
    if stage in ("collect", "screen"):
        return user_error("SCREEN_CHANGED")
    return user_error("BROWSER_START")


def detect_login_issue(body_text: str) -> str | None:
    compact = " ".join(str(body_text or "").split())
    credential_phrases = (
        "아이디 또는 비밀번호가 일치하지", "아이디나 비밀번호가 일치하지",
        "비밀번호가 올바르지", "로그인 정보를 다시 확인",
    )
    restricted_phrases = (
        "계정이 잠", "로그인이 제한", "휴면 계정", "본인인증이 필요",
        "비정상적인 로그인", "로그인 실패 횟수",
    )
    if any(phrase in compact for phrase in credential_phrases):
        return user_error("LOGIN_CREDENTIALS")
    if any(phrase in compact for phrase in restricted_phrases):
        return user_error("ACCOUNT_RESTRICTED")
    return None


def browser_channels() -> tuple[str, str]:
    """Use Chrome first and keep Edge only as the compatibility fallback."""
    return "chrome", "msedge"


def installed_browser() -> tuple[str, str]:
    """Return Chrome when installed; Edge is used only when Chrome is absent."""
    chrome_paths = (
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    )
    chrome = shutil.which("chrome") or next((str(path) for path in chrome_paths if path.is_file()), "")
    if chrome:
        return "chrome", chrome
    edge_paths = (
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
    )
    edge = shutil.which("msedge") or next((str(path) for path in edge_paths if path.is_file()), "")
    if edge:
        return "msedge", edge
    raise ValueError("Google Chrome 또는 Microsoft Edge가 설치되어 있지 않습니다.")


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


def is_logged_in_url(url_value: str) -> bool:
    url = urlparse(url_value)
    return url.hostname == "www.esmplus.com" and url.path.startswith(("/Home", "/Escrow"))


def minimize_browser_window(context, page) -> bool:
    """Minimize the authenticated Chromium window without restarting its session."""
    try:
        cdp = context.new_cdp_session(page)
        window = cdp.send("Browser.getWindowForTarget")
        cdp.send(
            "Browser.setWindowBounds",
            {"windowId": window["windowId"], "bounds": {"windowState": "minimized"}},
        )
        cdp.detach()
        return True
    except Exception:
        # 로그인 세션 유지가 우선이다. 최소화가 지원되지 않는 환경에서는 열린
        # 브라우저를 그대로 사용하고 세션 재실행은 시도하지 않는다.
        return False


class EsmBrowserWorker(QThread):
    status_changed = Signal(str)
    ready = Signal(bool)
    session_started = Signal(object)
    collected = Signal(object)
    failed = Signal(str)
    attention_required = Signal(str)
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
                selected_channel, executable = installed_browser()
                profile = ESM_PROFILE_ROOT / selected_channel
                profile.mkdir(parents=True, exist_ok=True)
                browser_label = "Chrome" if selected_channel == "chrome" else "Edge"
                def launch(headless):
                    try:
                        result = pw.chromium.launch_persistent_context(
                            str(profile), executable_path=executable, headless=headless, accept_downloads=True,
                        )
                    except Exception as exc:
                        raise ValueError(classify_browser_error(exc)) from exc
                    active = result.pages[0] if result.pages else result.new_page()
                    active.set_default_timeout(15000)
                    return result, active

                # 저장된 로그인 세션은 먼저 보이지 않는 브라우저에서 확인한다.
                context, page = launch(True)
                page.goto(LOGIN_URL, wait_until="domcontentloaded")
                if not is_logged_in_url(page.url):
                    context.close()
                    context, page = launch(False)
                    page.goto(LOGIN_URL, wait_until="domcontentloaded")
                    self.status_changed.emit(
                        f"{browser_label}에서 ESM 로그인을 완료해주세요. 로그인 후 수집 화면은 자동으로 백그라운드 전환됩니다."
                    )
                    login_started = time.monotonic()
                    delayed_notice_sent = False
                    detected_notice = None
                    while not self.stopping.is_set() and not page.is_closed() and not is_logged_in_url(page.url):
                        if int((time.monotonic() - login_started) * 4) % 4 == 0:
                            try:
                                issue = detect_login_issue(page.locator("body").inner_text(timeout=2000))
                            except Exception:
                                issue = None
                            if issue and issue != detected_notice:
                                detected_notice = issue
                                self.attention_required.emit(issue)
                        if not delayed_notice_sent and time.monotonic() - login_started >= 90:
                            delayed_notice_sent = True
                            self.attention_required.emit(user_error("LOGIN_DELAYED"))
                        page.wait_for_timeout(250)
                    if self.stopping.is_set() or page.is_closed():
                        context.close()
                        return
                    # 로그인 직후 컨텍스트를 닫고 headless로 다시 열면 ESM 인증
                    # 쿠키/스토리지가 기록되기 전에 세션이 끊길 수 있다. 현재 로그인된
                    # 컨텍스트를 그대로 사용하고 주문 화면 확인 후 창만 최소화한다.
                    page.goto(ORDERS_URL, wait_until="domcontentloaded")
                    if not is_logged_in_url(page.url):
                        raise ValueError(user_error("SESSION_EXPIRED"))
                    minimized = minimize_browser_window(context, page)
                    self.status_changed.emit(
                        "ESM 로그인 세션 확인 · 브라우저를 최소화하고 백그라운드 수집을 준비했습니다."
                        if minimized else
                        "ESM 로그인 세션 확인 · 현재 브라우저 세션을 유지해 수집을 준비했습니다."
                    )
                self.ready.emit(True)
                if page.url != ORDERS_URL:
                    page.goto(ORDERS_URL, wait_until="domcontentloaded")
                self.status_changed.emit("ESM 로그인 세션 확인 · 조회와 다운로드를 준비했습니다.")
                while not self.stopping.is_set():
                    if page.is_closed():
                        break
                    try:
                        request = self.commands.get_nowait()
                    except queue.Empty:
                        page.wait_for_timeout(250)
                        continue
                    if not is_logged_in_url(page.url):
                        self.ready.emit(False)
                        self.failed.emit(user_error("SESSION_EXPIRED"))
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
                        message = str(exc) if isinstance(exc, ValueError) else classify_browser_error(exc, "collect")
                        self.failed.emit(message)
                    finally:
                        self.collecting_changed.emit(False)
                context.close()
        except Exception as exc:
            if isinstance(exc, ValueError) and str(exc).startswith("[ESM-"):
                self.failed.emit(str(exc))
            elif isinstance(exc, ValueError) and "설치되어 있지" in str(exc):
                self.failed.emit(user_error("BROWSER_NOT_FOUND"))
            else:
                self.failed.emit(classify_browser_error(exc))
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
                raise ValueError(user_error("DOWNLOAD"))
            download = downloads[0]
            with tempfile.TemporaryDirectory(prefix="reqm-esm-") as temp:
                path = Path(temp) / "download.xls"
                download.save_as(path)
                if download.failure():
                    raise ValueError(user_error("DOWNLOAD", "브라우저가 파일 다운로드 실패를 반환했습니다."))
                session.archive(path, status, count, download.suggested_filename)
        finally:
            page.remove_listener("download", listener)
