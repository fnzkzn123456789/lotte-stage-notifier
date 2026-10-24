from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
BASE = "https://www.lottecinema.co.kr"
REGIONS = {"0001", "0002"}  # 서울, 경기/인천 (공식 지역 코드)


def now_kst():
    return datetime.now(KST)


class ServiceError(Exception):
    def __init__(self, message, retry_after=60):
        super().__init__(message)
        self.retry_after = retry_after


class Stopped(Exception):
    pass


def checked_items(value, label):
    if not isinstance(value, list) or any(not isinstance(x, dict) for x in value):
        raise ServiceError(f"{label} 응답 형식이 바뀌었습니다. 조회를 중단합니다.")
    return value


class JsonHttp:
    def __init__(self):
        # urllib의 기본 프록시/인증서 검증을 사용한다. 토큰을 로그에 출력하지 않는다.
        self.opener = urllib.request.build_opener()

    def post(self, url, payload, form=False):
        if form:
            body = urllib.parse.urlencode({"paramList": json.dumps(payload)}).encode()
            content_type = "application/x-www-form-urlencoded; charset=UTF-8"
        else:
            body = json.dumps(payload).encode("utf-8")
            content_type = "application/json"
        request = urllib.request.Request(url, body, {
            "Content-Type": content_type,
            "User-Agent": "Mozilla/5.0 LotteStageNotifier/1.0",
            "Referer": BASE + "/NLCHS/Ticketing" if form else "https://telegram.org/",
        })
        try:
            with self.opener.open(request, timeout=20) as response:
                data = json.loads(response.read(10_000_001).decode("utf-8-sig"))
        except urllib.error.HTTPError as exc:
            retry = 60
            if exc.code == 429:
                retry = 120
                try:
                    raw = json.loads(exc.read(10000))
                    retry = max(retry, int(raw.get("parameters", {}).get("retry_after", 120)))
                except (ValueError, TypeError):
                    pass
            raise ServiceError(f"서버 응답 오류 (HTTP {exc.code}). 잠시 후 다시 확인합니다.", retry) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ServiceError("인터넷 연결 또는 서버 응답을 확인할 수 없습니다.") from None
        except (ValueError, UnicodeError):
            raise ServiceError("서버가 올바른 JSON 응답을 보내지 않았습니다.") from None
        if not isinstance(data, dict):
            raise ServiceError("서버 응답 형식이 올바르지 않습니다.")
        return data


class LotteClient:
    def __init__(self, stop=None, http=None, min_interval=0.8):
        self.stop = stop or threading.Event()
        self.http = http or JsonHttp()
        self.last_call = 0.0
        self.min_interval = min_interval

    def call(self, area, method, **params):
        delay = max(0, self.last_call + self.min_interval - time.monotonic())
        if self.stop.wait(delay):
            raise Stopped()
        self.last_call = time.monotonic()
        path = "Ticketing/TicketingData.aspx" if area == "ticket" else "Event/EventData.aspx"
        data = self.http.post(BASE + "/LCWS/" + path, {
            "MethodName": method, "channelType": "HO", "osType": "W",
            "osVersion": "Mozilla/5.0", **params,
        }, form=True)
        if str(data.get("IsOK")).lower() != "true":
            raise ServiceError(f"롯데시네마 {method} 조회에 실패했습니다.")
        return data

    def cinemas(self):
        data = self.call("ticket", "GetTicketingPageTOBE", memberOnNo="0")
        try:
            rows = checked_items(data["Cinemas"]["Cinemas"]["Items"], "영화관")
            cinemas = {str(x["CinemaID"]): x for x in rows
                       if str(x.get("DivisionCode")) == "1"
                       and str(x.get("DetailDivisionCode")).zfill(4) in REGIONS}
        except (KeyError, TypeError):
            raise ServiceError("영화관 목록 구조가 바뀌었습니다.") from None
        if not cinemas:
            raise ServiceError("서울·경기·인천 영화관 목록이 비어 있습니다.")
        return cinemas

    def events(self):
        result, seen = [], set()
        for page in range(1, 101):
            data = self.call("event", "GetEventLists", EventClassificationCode="40",
                             SearchText="", CinemaID="", PageNo=page, PageSize=100,
                             MemberNo="0", sortDivCd="1")
            rows = checked_items(data.get("Items"), "이벤트 목록")
            try:
                total = int(data["TotalCount"])
            except (KeyError, TypeError, ValueError):
                raise ServiceError("이벤트 목록의 전체 개수를 확인할 수 없습니다.") from None
            for row in rows:
                event_id = str(row.get("EventID", ""))
                if not event_id or event_id in seen:
                    raise ServiceError("이벤트 목록 페이지가 중복되거나 올바르지 않습니다.")
                seen.add(event_id)
                if str(row.get("EventTypeCode")) == "107":
                    result.append(row)
            if len(seen) >= total:
                return result
            if not rows:
                raise ServiceError("이벤트 목록 일부를 가져오지 못했습니다.")
        raise ServiceError("이벤트 목록이 예상 범위를 초과했습니다.")

    def event_rows(self, event_id):
        data = self.call("event", "GetStageGreetingEventDetailTOBE", EventID=event_id)
        details = checked_items(data.get("StageGreetingEventDetail"), "무대인사 상세")
        result = []
        for detail in details:
            for row in checked_items(detail.get("Items"), "무대인사 회차"):
                result.append({**row, "EventID": event_id})
        return result

    def schedule(self, cinema, day):
        key = f"{cinema['DivisionCode']}|{cinema['DetailDivisionCode']}|{cinema['CinemaID']}"
        data = self.call("ticket", "GetPlaySequence", playDate=day,
                         cinemaID=key, representationMovieCode="")
        try:
            return checked_items(data["PlaySeqs"]["Items"], "상영시간표")
        except (KeyError, TypeError):
            raise ServiceError("상영시간표 구조가 바뀌었습니다.") from None


def is_stage(row):
    # 107번 이벤트 양식에는 GV/굿즈 상영도 있으므로 회차의 실제 유형으로 구분한다.
    text = " ".join(str(row.get(key) or "") for key in
                    ("AccompanyTypeNameKR", "BkShowLabelTxtNm1", "BkShowLabelTxtNm2"))
    return str(row.get("AccompanyTypeCode")) == "30" or "무대인사" in re.sub(r"\s+", "", text)


def show_time(day, start):
    try:
        base = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=KST)
        hour, minute = map(int, start.split(":"))
        if not 0 <= hour <= 47 or not 0 <= minute <= 59:
            raise ValueError
        return base + timedelta(hours=hour, minutes=minute)
    except (ValueError, TypeError):
        raise ServiceError("회차 날짜 또는 시간 형식을 확인할 수 없습니다.") from None


@dataclass(frozen=True)
class Screening:
    key: str
    cinema_id: str
    cinema: str
    movie: str
    day: str
    start: str
    screen: str
    seats: int
    bookable: bool
    url: str


def parse_screenings(rows, cinemas, at=None):
    at = at or now_kst()
    result = []
    for row in rows:
        cid = str(row.get("CinemaID", ""))
        if cid not in cinemas or not is_stage(row):
            continue
        required = ("PlayDt", "StartTime", "ScreenID", "PlaySequence", "MovieCode", "IsBookingYN", "BookingSeatCount")
        if any(row.get(k) is None for k in required):
            raise ServiceError("무대인사 회차에 필수 예매 정보가 없습니다.")
        day, start = str(row["PlayDt"])[:10], str(row["StartTime"])
        if show_time(day, start) <= at:
            continue
        try:
            seats = int(row["BookingSeatCount"])
        except (TypeError, ValueError):
            raise ServiceError("무대인사 잔여석을 확인할 수 없습니다.") from None
        # BookingSeatCount는 예매 수가 아니라 잔여석. 공식 UI도 이 값을 그대로 표시한다.
        status = str(row["IsBookingYN"])
        if status not in {"Y", "N", "E", "J"}:
            raise ServiceError(f"알 수 없는 예매 상태 ({status[:10]})입니다.")
        key = "|".join((cid, str(row["ScreenID"]), day, str(row["PlaySequence"]), str(row["MovieCode"]), start))
        query = urllib.parse.urlencode({"cinemaId": f"|{cid}|"})
        result.append(Screening(key, cid, str(row.get("CinemaNameKR") or cinemas[cid]["CinemaNameKR"]),
                                str(row.get("MovieNameKR") or "제목 미확인"), day, start,
                                str(row.get("ScreenNameKR") or row["ScreenID"]), seats,
                                status == "Y" and seats > 0, BASE + "/NLCHS/Ticketing?" + query))
    return result


def message_for(show):
    return ("🎬 롯데시네마 무대인사 예매 가능\n\n"
            f"영화: {show.movie}\n극장: {show.cinema} · {show.screen}\n"
            f"상영: {show.day} {show.start}\n잔여석: {show.seats}석 (조회 시점)\n\n"
            "아래 예매 페이지에서 날짜와 영화를 선택해 주세요.\n"
            f"{show.url}")


class Telegram:
    def __init__(self, token, chat_id="", http=None):
        self.token = token.strip()
        self.chat_id = str(chat_id).strip()
        self.http = http or JsonHttp()
        if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", self.token):
            raise ServiceError("BotFather에서 받은 봇 토큰을 확인해 주세요.")

    def call(self, method, **params):
        data = self.http.post(f"https://api.telegram.org/bot{self.token}/{method}", params)
        if data.get("ok") is not True:
            raise ServiceError("텔레그램 요청에 실패했습니다. 토큰·대화 ID·봇 차단 여부를 확인해 주세요.")
        return data.get("result")

    def chats(self):
        updates = self.call("getUpdates", timeout=0, limit=100)
        if not isinstance(updates, list):
            raise ServiceError("텔레그램 대화 목록 형식을 확인할 수 없습니다.")
        chats = {}
        for update in updates:
            msg = update.get("message") or update.get("channel_post") or {}
            chat = msg.get("chat") or {}
            if "id" in chat:
                name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
                chats[str(chat["id"])] = name or chat.get("username") or "대화"
        return chats

    def send(self, text):
        if not re.fullmatch(r"-?\d+|@[A-Za-z0-9_]+", self.chat_id):
            raise ServiceError("알림을 받을 대화 ID를 선택하거나 입력해 주세요.")
        return self.call("sendMessage", chat_id=self.chat_id, text=text,
                         link_preview_options={"is_disabled": True})


class StateStore:
    """성공한 조회만 관측하고, 전송 확인 뒤에만 중복 방지 기록을 남긴다."""
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS observations (
              key TEXT PRIMARY KEY, payload TEXT NOT NULL, observed_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS scopes (scope TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS deliveries (
              recipient TEXT NOT NULL, key TEXT NOT NULL, status TEXT NOT NULL,
              PRIMARY KEY (recipient, key));
        """)

    def observe(self, scope, shows, recipient, notify_existing=False):
        initial = not self.db.execute("SELECT 1 FROM scopes WHERE scope=?", (scope,)).fetchone()
        pending = []
        with self.db:
            for show in shows:
                previous = self.db.execute("SELECT payload FROM observations WHERE key=?", (show.key,)).fetchone()
                status = self.db.execute("SELECT status FROM deliveries WHERE recipient=? AND key=?", (recipient, show.key)).fetchone()
                if show.bookable and not status:
                    # 처음 조회한 지점/날짜의 이미 열린 회차는 기본적으로 기존 상태로 저장.
                    suppress = initial and previous is None and not notify_existing
                    if suppress:
                        self.db.execute("INSERT INTO deliveries VALUES (?, ?, 'baseline')", (recipient, show.key))
                    else:
                        pending.append(show)
                self.db.execute("INSERT OR REPLACE INTO observations VALUES (?, ?, ?)",
                                (show.key, json.dumps(asdict(show), ensure_ascii=False), now_kst().isoformat()))
            self.db.execute("INSERT OR IGNORE INTO scopes VALUES (?)", (scope,))
        return pending

    def delivered(self, recipient, show):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO deliveries VALUES (?, ?, 'sent')", (recipient, show.key))

    def initialized(self, name):
        return bool(self.db.execute("SELECT 1 FROM metadata WHERE name=?", (name,)).fetchone())

    def mark_initialized(self, name):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES (?)", (name,))

    def close(self):
        self.db.close()


def recipient_key(token, chat):
    # 토큰 재발급에도 같은 봇/수신자는 기존 중복 방지 기록을 유지.
    return hashlib.sha256((token.split(":")[0] + ":" + str(chat)).encode()).hexdigest()


def protect(data, decrypt=False):
    """Windows 현재 사용자 DPAPI. 다른 OS에서는 평문 저장으로 후퇴하지 않는다."""
    if os.name != "nt":
        raise ServiceError("인증정보 저장은 Windows에서 지원합니다.")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, output = Blob(len(data), buffer), Blob()
    crypt = ctypes.windll.crypt32
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ServiceError("Windows 인증정보 암호화/복호화에 실패했습니다.")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(output.pbData)


def save_settings(directory, settings, token, chat):
    directory.mkdir(parents=True, exist_ok=True)
    sealed = protect(json.dumps({"token": token, "chat": chat}).encode())
    for filename, data in (("settings.json", json.dumps(settings, ensure_ascii=False, indent=2).encode("utf-8")),
                           ("credentials.bin", sealed)):
        temp = directory / (filename + ".tmp")
        temp.write_bytes(data)
        os.replace(temp, directory / filename)


def load_settings(directory):
    settings = {"interval": 60, "days": 28, "notify_existing": False, "sweep": True}
    token = chat = ""
    try:
        if (directory / "settings.json").exists():
            settings.update(json.loads((directory / "settings.json").read_text(encoding="utf-8")))
        if (directory / "credentials.bin").exists():
            credentials = json.loads(protect((directory / "credentials.bin").read_bytes(), decrypt=True))
            token, chat = credentials["token"], credentials["chat"]
    except (ValueError, OSError, KeyError, TypeError, ServiceError):
        raise ServiceError("저장된 설정을 읽을 수 없습니다. 같은 Windows 계정에서 다시 설정해 주세요.") from None
    return settings, token, chat


class Monitor:
    def __init__(self, directory, settings, token, chat, emit, stop):
        self.directory, self.settings = directory, settings
        self.emit, self.stop = emit, stop
        self.client = LotteClient(stop)
        self.telegram = Telegram(token, chat)
        self.recipient = recipient_key(token, chat)
        self.cinemas = {}
        self.store = None
        self.scope_seen = {}
        self.last_event_success = None

    def check_schedule(self, cid, day, force=False, new_event_scope=False):
        scope = f"{cid}:{day}"
        if not force and time.monotonic() - self.scope_seen.get(scope, -1e9) < 30:
            return
        rows = self.client.schedule(self.cinemas[cid], day)
        shows = parse_screenings(rows, self.cinemas)
        pending = self.store.observe(scope, shows, self.recipient,
                                     self.settings["notify_existing"] or new_event_scope)
        self.emit("shows", (cid, day, shows))
        for show in pending:
            if self.stop.is_set():
                raise Stopped()
            self.telegram.send(message_for(show))
            self.store.delivered(self.recipient, show)
            self.emit("log", f"알림 전송: {show.movie} / {show.cinema} / {show.day} {show.start}")
            if self.stop.wait(1.1):
                raise Stopped()
        self.scope_seen[scope] = time.monotonic()

    def event_cycle(self):
        initialized = self.store.initialized("events:" + self.recipient)
        events = self.client.events()
        targets = set()
        errors = []
        for event in events:
            try:
                for row in self.client.event_rows(str(event["EventID"])):
                    cid, day = str(row.get("CinemaID", "")), str(row.get("PlayDate", ""))[:10]
                    if cid in self.cinemas and is_stage(row) and day >= now_kst().date().isoformat():
                        targets.add((cid, day))
            except ServiceError as exc:
                errors.append(str(exc))
                if exc.retry_after > 60:
                    raise
        for cid, day in sorted(targets, key=lambda x: (x[1], x[0])):
            try:
                self.check_schedule(cid, day, force=True, new_event_scope=initialized)
            except ServiceError as exc:
                errors.append(str(exc))
                if exc.retry_after > 60:
                    raise
        if errors:
            raise ServiceError(f"일부 조회/전송 {len(errors)}건 실패. " + errors[0])
        self.store.mark_initialized("events:" + self.recipient)
        self.last_event_success = now_kst().strftime("%H:%M:%S")
        self.emit("log", f"공지 확인 완료: 이벤트 {len(events)}건 · 수도권 {len(targets)}개 지점/날짜")

    def sweep_targets(self):
        today = now_kst().date()
        # 가까운 날짜부터 모든 수도권 지점 점검. 공지에 아직 없는 회차도 포함한다.
        return [(cid, (today + timedelta(days=i)).isoformat())
                for i in range(self.settings["days"]) for cid in self.cinemas]

    def run(self):
        self.store = StateStore(self.directory / "state.sqlite3")
        next_event = next_catalog = 0.0
        sweep, index, failures = [], 0, 0
        try:
            while not self.stop.is_set():
                phase = "catalog"
                try:
                    if time.monotonic() >= next_catalog:
                        self.cinemas = self.client.cinemas()
                        next_catalog = time.monotonic() + 900
                        self.emit("log", f"서울·경기·인천 {len(self.cinemas)}개 지점 감시")
                    if time.monotonic() >= next_event:
                        phase = "events"
                        self.emit("status", "무대인사 공지와 예매 상태 확인 중…")
                        self.event_cycle()
                        next_event = time.monotonic() + self.settings["interval"]
                    if self.settings["sweep"]:
                        phase = "sweep"
                        if index >= len(sweep):
                            sweep, index = self.sweep_targets(), 0
                            self.emit("log", f"전체 시간표 순회 시작: {len(sweep)}개 지점/날짜")
                        cid, day = sweep[index]
                        if cid in self.cinemas and day >= now_kst().date().isoformat():
                            self.check_schedule(cid, day)
                        index += 1
                        self.emit("status", f"감시 중 · 공지 확인 {self.last_event_success or '대기'} · 전체 순회 {index}/{len(sweep)}")
                    else:
                        self.emit("status", f"감시 중 · 공지 확인 {self.last_event_success or '대기'}")
                        self.stop.wait(1)
                    failures = 0
                except ServiceError as exc:
                    failures += 1
                    delay = min(900, max(exc.retry_after, 15 * 2 ** min(failures, 6)))
                    if phase == "events":
                        next_event = time.monotonic() + delay
                    elif phase == "sweep":
                        # 특정 폐점 지점/조회 실패 한 건이 나머지 전체 순회를 막지 않게 한다.
                        index += 1
                    self.emit("log", f"확인 지연: {exc} ({delay}초 후 재시도)")
                    self.emit("status", f"연결/조회 오류 · {delay}초 후 재시도")
                    self.stop.wait(delay)
        except Stopped:
            pass
        finally:
            self.store.close()

