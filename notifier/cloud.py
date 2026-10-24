"""GitHub Actions용 단발 실행. PC GUI/Windows 자격증명에 의존하지 않는다."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from .core import LotteClient, ServiceError, Telegram, is_stage, message_for, now_kst, parse_screenings


def fresh_state():
    return {"version": 1, "initialized": False, "scopes": {}, "shows": {},
            "cursor": {"anchor": "", "index": 0}, "last_success": ""}


def valid_day(value):
    try:
        return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def validate_state(data):
    # 공개 브랜치에는 아래의 고정된 항목만 허용한다. 토큰/대화 ID/사용자 설정은 저장하지 않는다.
    try:
        assert isinstance(data, dict) and set(data) == set(fresh_state())
        assert data["version"] == 1 and isinstance(data["initialized"], bool)
        assert isinstance(data["scopes"], dict) and len(data["scopes"]) < 20000
        for key, value in data["scopes"].items():
            cid, day = key.split(":")
            assert cid.isdigit() and valid_day(day) and value is True
        assert isinstance(data["shows"], dict) and len(data["shows"]) < 10000
        for key, row in data["shows"].items():
            assert re.fullmatch(r"[0-9a-f]{64}", key)
            assert set(row) == {"day", "status"} and valid_day(row["day"])
            assert row["status"] in {"seen", "baseline", "sent"}
        cursor = data["cursor"]
        assert set(cursor) == {"anchor", "index"}
        assert cursor["anchor"] == "" or valid_day(cursor["anchor"])
        assert type(cursor["index"]) is int and 0 <= cursor["index"] < 100000
        assert data["last_success"] == "" or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+09:00", data["last_success"])
    except (AssertionError, KeyError, TypeError, ValueError, AttributeError):
        raise ServiceError("저장된 감시 기록이 손상되었거나 형식이 다릅니다. 초기화하지 않고 중단합니다.") from None
    return data


def encoded_state(data):
    validate_state(data)
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"


class LocalState:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return fresh_state()
        try:
            return validate_state(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            raise ServiceError("로컬 감시 기록을 읽을 수 없습니다.") from None

    def save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(encoded_state(data))
        os.replace(tmp, self.path)


class GitHubState:
    BRANCH = "notifier-state"
    FILE = "notifier-state.json"

    def __init__(self, repository, token):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository or "") or not token:
            raise ServiceError("GitHub 저장소 또는 자동 실행 토큰이 없습니다.")
        self.url = "https://api.github.com/repos/" + repository
        self.token, self.sha, self.last = token, None, None

    def request(self, method, path, payload=None, allow_missing=False):
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.url + path, body, {
            "Authorization": "Bearer " + self.token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "lotte-stage-notifier",
        }, method=method)
        try:
            with urllib.request.urlopen(req, timeout=25) as response:
                return json.loads(response.read(2_000_001))
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and allow_missing:
                return None
            raise ServiceError(f"GitHub 알림 기록 접근 실패 (HTTP {exc.code}). 저장소 쓰기 권한과 실행 상태를 확인하세요.") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise ServiceError("GitHub 알림 기록을 읽거나 저장할 수 없습니다. 알림 전송을 중단합니다.") from None

    def load(self):
        repo = self.request("GET", "")
        if repo.get("private") is not False:
            raise ServiceError("무료 운영 보호: 이 설정은 공개 저장소에서만 실행합니다.")
        ref = self.request("GET", "/git/ref/heads/" + self.BRANCH, allow_missing=True)
        if ref is None:
            default = urllib.parse.quote(repo["default_branch"], safe="")
            base = self.request("GET", "/git/ref/heads/" + default)
            self.request("POST", "/git/refs", {"ref": "refs/heads/" + self.BRANCH, "sha": base["object"]["sha"]})
        content = self.request("GET", f"/contents/{self.FILE}?ref={self.BRANCH}", allow_missing=True)
        if content is None:
            # 새 브랜치를 만든 직후 중단된 실행도 안전한 빈 기준 상태부터 재개한다.
            if ref is not None and ref["object"]["sha"] != self.request(
                    "GET", "/git/ref/heads/" + urllib.parse.quote(repo["default_branch"], safe=""))["object"]["sha"]:
                raise ServiceError("알림 기록 브랜치에서 기록 파일이 사라졌습니다. 자동으로 초기화하지 않습니다.")
            data = fresh_state()
            self.save(data)
            return data
        try:
            if content.get("encoding") != "base64" or content.get("type") != "file":
                raise ValueError
            raw = base64.b64decode(content["content"])
            data = validate_state(json.loads(raw))
            self.sha = content["sha"]
            self.last = encoded_state(data)
            return data
        except (ValueError, KeyError, TypeError):
            raise ServiceError("GitHub 감시 기록을 해석할 수 없습니다. 덮어쓰지 않고 중단합니다.") from None

    def save(self, data):
        raw = encoded_state(data)
        if raw == self.last:
            return
        body = {"message": "Update notification state [skip ci]", "branch": self.BRANCH,
                "content": base64.b64encode(raw).decode("ascii")}
        if self.sha:
            body["sha"] = self.sha
        result = self.request("PUT", "/contents/" + self.FILE, body)
        try:
            self.sha = result["content"]["sha"]
        except (KeyError, TypeError):
            raise ServiceError("GitHub 기록 저장 결과를 확인할 수 없습니다.") from None
        self.last = raw


class CloudState:
    def __init__(self, data):
        self.data = validate_state(data)

    def observe(self, scope, shows, notify_existing=False):
        initial = scope not in self.data["scopes"]
        pending = []
        for show in shows:
            key = hashlib.sha256(show.key.encode()).hexdigest()
            prior = self.data["shows"].get(key)
            if prior is None:
                status = "baseline" if initial and show.bookable and not notify_existing else "seen"
                prior = self.data["shows"][key] = {"day": show.day, "status": status}
            if show.bookable and prior["status"] == "seen":
                pending.append(show)
        self.data["scopes"][scope] = True
        return pending

    def delivered(self, show):
        self.data["shows"][hashlib.sha256(show.key.encode()).hexdigest()]["status"] = "sent"

    def prune(self):
        cutoff = (now_kst().date() - timedelta(days=2)).isoformat()
        self.data["shows"] = {k: v for k, v in self.data["shows"].items() if v["day"] >= cutoff}
        self.data["scopes"] = {k: v for k, v in self.data["scopes"].items() if k.split(":", 1)[1] >= cutoff}


class DeadlineReached(Exception):
    pass


class BoundedLotteClient(LotteClient):
    def __init__(self, deadline):
        super().__init__()
        self.deadline = deadline

    def call(self, *args, **kwargs):
        if time.monotonic() >= self.deadline:
            raise DeadlineReached()
        return super().call(*args, **kwargs)


def next_sweep(state, cinemas, days):
    cursor = state.data["cursor"]
    ids = sorted(cinemas)
    today = now_kst().date()
    if not cursor["anchor"] or cursor["index"] >= len(ids) * days:
        cursor.update(anchor=today.isoformat(), index=0)
    anchor = date.fromisoformat(cursor["anchor"])
    index = cursor["index"]
    while index < len(ids) * days:
        day = anchor + timedelta(days=index // len(ids))
        cid = ids[index % len(ids)]
        index += 1
        cursor["index"] = index
        if day >= today:
            return cid, day.isoformat()
    cursor.update(anchor=today.isoformat(), index=0)
    return next_sweep(state, cinemas, days)


def run_once(client, backend, telegram=None, *, dry_run=False, sweep_count=180, days=28,
             deadline=None, send_delay=1.1):
    state = CloudState(fresh_state() if dry_run else backend.load())
    errors, checked = [], set()
    counts = {"cinemas": 0, "events": 0, "schedules": 0, "stage_shows": 0, "sent": 0, "deferred": False}

    def checkpoint():
        if not dry_run:
            backend.save(state.data)

    def check(cid, day, new_scope=False):
        if (cid, day) in checked:
            return
        if deadline is not None and time.monotonic() >= deadline:
            raise DeadlineReached()
        try:
            rows = client.schedule(cinemas[cid], day)
            shows = parse_screenings(rows, cinemas)
        except ServiceError as exc:
            if exc.retry_after > 60:
                raise
            errors.append(f"상영시간표 {cid}/{day}: {exc}")
            return
        pending = state.observe(f"{cid}:{day}", shows, new_scope)
        checked.add((cid, day))
        counts["schedules"] += 1
        counts["stage_shows"] += len(shows)
        if dry_run:
            return
        for show in pending:
            if deadline is not None and time.monotonic() >= deadline:
                raise DeadlineReached()
            # 기준/미전송 기록을 먼저 보존. 전송 실패는 다음 실행에서 재시도한다.
            checkpoint()
            telegram.send(message_for(show))
            state.delivered(show)
            # 저장 실패 시 즉시 종료해 추가 전송으로 중복 가능성을 키우지 않는다.
            checkpoint()
            counts["sent"] += 1
            if send_delay:
                time.sleep(send_delay)

    try:
        cinemas = client.cinemas()
        if not cinemas:
            raise ServiceError("감시할 영화관이 없습니다.")
        counts["cinemas"] = len(cinemas)
        events = client.events()
        counts["events"] = len(events)
        targets = set()
        for event in events:
            try:
                for row in client.event_rows(str(event["EventID"])):
                    cid, day = str(row.get("CinemaID", "")), str(row.get("PlayDate", ""))[:10]
                    if cid in cinemas and is_stage(row):
                        if not valid_day(day):
                            raise ServiceError("무대인사 공지의 날짜 형식이 바뀌었습니다.")
                        if day >= now_kst().date().isoformat():
                            targets.add((cid, day))
            except ServiceError as exc:
                if exc.retry_after > 60:
                    raise
                errors.append(f"이벤트 조회: {exc}")
        for cid, day in sorted(targets, key=lambda x: (x[1], x[0])):
            check(cid, day, state.data["initialized"])
        if not errors:
            state.data["initialized"] = True
            state.data["last_success"] = now_kst().replace(microsecond=0).isoformat()
        for _ in range(sweep_count):
            if deadline is not None and time.monotonic() >= deadline:
                raise DeadlineReached()
            cid, day = next_sweep(state, cinemas, days)
            check(cid, day)
    except DeadlineReached:
        counts["deferred"] = True
    finally:
        state.prune()
        checkpoint()
    counts["errors"] = len(errors)
    for error in errors[:5]:
        print(error, file=sys.stderr)
    print(json.dumps(counts, ensure_ascii=False))
    return counts


def write_summary(text):
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="롯데시네마 GitHub 알리미")
    parser.add_argument("--mode", choices=("check", "test", "dry-run"), default="dry-run")
    parser.add_argument("--github-state", action="store_true")
    parser.add_argument("--state", default="data/cloud-state.json")
    parser.add_argument("--sweep-count", type=int, default=180)
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--max-seconds", type=int, default=220)
    args = parser.parse_args(argv)
    if not 0 <= args.sweep_count <= 500 or not 1 <= args.days <= 42 or not 30 <= args.max_seconds <= 480:
        parser.error("조회 개수 0~500, 날짜 범위 1~42, 실행 시간 30~480초를 사용하세요.")
    try:
        telegram = None
        if args.mode != "dry-run":
            token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
            chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
            if not token or not chat:
                raise ServiceError("GitHub Secrets에 TELEGRAM_BOT_TOKEN과 TELEGRAM_CHAT_ID를 등록하세요.")
            telegram = Telegram(token, chat)
            telegram.call("getMe")
            telegram.call("getChat", chat_id=chat)
        if args.mode == "test":
            telegram.send("✅ GitHub 무대인사 알리미 연결 확인\n롯데시네마 · 서울/경기/인천 · 모든 영화\n예약 감시는 MONITOR_ENABLED=true로 켭니다.")
            write_summary("텔레그램 테스트 메시지를 전송했습니다. 이 실행은 감시 기록을 바꾸지 않습니다.")
            return 0
        backend = (GitHubState(os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_TOKEN", ""))
                   if args.github_state and args.mode != "dry-run" else LocalState(args.state))
        deadline = time.monotonic() + args.max_seconds
        result = run_once(BoundedLotteClient(deadline), backend, telegram,
                          dry_run=args.mode == "dry-run", sweep_count=args.sweep_count,
                          days=args.days, deadline=deadline)
        write_summary(f"영화관 {result['cinemas']}곳 · 시간표 {result['schedules']}건 · 무대인사 {result['stage_shows']}회차 · 전송 {result['sent']}건 · 오류 {result['errors']}건\n\n"
                      "첫 확인의 기존 회차는 기준 상태로 저장합니다. 다음 실행부터 새 회차/예매 가능 전환을 확인합니다.\n\n"
                      "공지에 없는 회차는 28일 시간표를 나누어 순회하므로 알림이 더 늦을 수 있습니다.")
        return 1 if result["errors"] else 0
    except ServiceError as exc:
        print(str(exc), file=sys.stderr)
        write_summary("확인 또는 전송 실패. 로그의 오류 내용을 확인하세요. 다음 예약 실행에서 다시 시도합니다.")
        return 1
    except Exception as exc:
        # 예외 원문에는 네트워크 URL 또는 환경 정보가 있을 수 있어 타입만 출력한다.
        print(f"실행 중단 ({type(exc).__name__}). 저장소 권한과 설정을 확인하세요.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
