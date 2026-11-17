"""GitHub Actions용 실행(단발 또는 장시간 반복). PC GUI/Windows 자격증명에 의존하지 않는다."""
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

from .core import (LotteClient, ServiceError, Telegram, is_stage, message_for, now_kst,
                   parse_screenings, prep_groups, prep_message_for)


WORKFLOW = "notify.yml"


def fresh_state():
    # watch: 상영준비중 회차가 있어 매 주기 다시 확인할 지점·날짜.
    return {"version": 1, "initialized": False, "scopes": {}, "shows": {}, "watch": {},
            "cursor": {"anchor": "", "index": 0}, "last_success": ""}


def valid_day(value):
    try:
        return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def validate_state(data):
    # 공개 브랜치에는 아래의 고정된 항목만 허용한다. 토큰/대화 ID/사용자 설정은 저장하지 않는다.
    try:
        assert isinstance(data, dict)
        # 'watch'는 나중에 추가한 항목이라 이전 기록에는 없다. 빈 값으로 채워 이어 쓴다.
        data.setdefault("watch", {})
        assert set(data) == set(fresh_state())
        assert data["version"] == 1 and isinstance(data["initialized"], bool)
        for name, limit in (("scopes", 20000), ("watch", 2000)):
            assert isinstance(data[name], dict) and len(data[name]) < limit
            for key, value in data[name].items():
                cid, day = key.split(":")
                assert cid.isdigit() and valid_day(day) and value is True
        assert isinstance(data["shows"], dict) and len(data["shows"]) < 10000
        for key, row in data["shows"].items():
            assert re.fullmatch(r"[0-9a-f]{64}", key)
            assert set(row) == {"day", "status"} and valid_day(row["day"])
            # prep: 상영준비중 알림만 보낸 상태. 예매가 열리면 sent로 바뀐다.
            assert row["status"] in {"seen", "baseline", "sent", "prep"}
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
                raw = response.read(2_000_001)
                # 실행 시작 요청(dispatch)처럼 본문 없이 성공하는 응답도 있다.
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and allow_missing:
                return None
            raise ServiceError(f"GitHub 요청 실패 (HTTP {exc.code}). 저장소 권한과 실행 상태를 확인하세요.") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise ServiceError("GitHub에 연결할 수 없습니다. 알림 전송을 중단합니다.") from None

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
            if show.bookable and prior["status"] in ("seen", "prep"):
                pending.append(show)
        self.data["scopes"][scope] = True
        return pending

    def delivered(self, show):
        self.data["shows"][hashlib.sha256(show.key.encode()).hexdigest()]["status"] = "sent"

    def preparing(self, shows):
        """상영준비중인데 아직 어떤 알림도 보내지 않은 회차. observe() 뒤에 부른다."""
        return [show for show in shows if show.preparing and self.data["shows"].get(
            hashlib.sha256(show.key.encode()).hexdigest(), {}).get("status") == "seen"]

    def prepared(self, shows):
        for show in shows:
            self.data["shows"][hashlib.sha256(show.key.encode()).hexdigest()]["status"] = "prep"

    def track(self, scope, shows):
        """상영준비중 회차가 남아 있는 지점·날짜만 매 주기 재확인 목록에 둔다."""
        if any(show.preparing for show in shows):
            self.data["watch"][scope] = True
        else:
            self.data["watch"].pop(scope, None)

    def watched(self):
        return [tuple(scope.split(":", 1)) for scope in self.data["watch"]]

    def prune(self):
        cutoff = (now_kst().date() - timedelta(days=2)).isoformat()
        self.data["shows"] = {k: v for k, v in self.data["shows"].items() if v["day"] >= cutoff}
        for name in ("scopes", "watch"):
            self.data[name] = {k: v for k, v in self.data[name].items() if k.split(":", 1)[1] >= cutoff}


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


class Session:
    """한 번의 실행(단발 또는 반복)이 함께 쓰는 조회·전송·저장 상태."""

    def __init__(self, client, backend, telegram=None, *, dry_run=False, days=28, deadline=None,
                 send_delay=1.1, clock=time.monotonic, sleep=time.sleep):
        self.client, self.backend, self.telegram = client, backend, telegram
        self.dry_run, self.days, self.deadline = dry_run, days, deadline
        self.send_delay, self.clock, self.sleep = send_delay, clock, sleep
        self.state = CloudState(fresh_state() if dry_run else backend.load())
        self.cinemas, self.errors, self.checked = {}, [], set()
        self.counts = {"cinemas": 0, "events": 0, "schedules": 0, "stage_shows": 0, "sent": 0,
                       "prep": 0, "deferred": False}
        # 공지 상세는 공지 목록이 그대로인 동안 잠시 재사용한다(반복 실행의 요청 수를 줄임).
        self.notice, self.notice_ids, self.notice_at = set(), None, None

    def checkpoint(self):
        if not self.dry_run:
            self.backend.save(self.state.data)

    def expired(self):
        return self.deadline is not None and self.clock() >= self.deadline

    def load_cinemas(self):
        cinemas = self.client.cinemas()
        if not cinemas:
            raise ServiceError("감시할 영화관이 없습니다.")
        self.cinemas = cinemas
        self.counts["cinemas"] = len(cinemas)

    def check(self, cid, day, new_scope=False):
        if (cid, day) in self.checked:
            return
        if self.expired():
            raise DeadlineReached()
        try:
            rows = self.client.schedule(self.cinemas[cid], day)
            shows = parse_screenings(rows, self.cinemas)
        except ServiceError as exc:
            if exc.retry_after > 60:
                raise
            self.errors.append(f"상영시간표 {cid}/{day}: {exc}")
            return
        scope = f"{cid}:{day}"
        pending = self.state.observe(scope, shows, new_scope)
        self.state.track(scope, shows)
        self.checked.add((cid, day))
        self.counts["schedules"] += 1
        self.counts["stage_shows"] += len(shows)
        if self.dry_run:
            return
        for show in pending:
            if self.expired():
                raise DeadlineReached()
            # 기준/미전송 기록을 먼저 보존. 전송 실패는 다음 확인에서 재시도한다.
            self.checkpoint()
            self.telegram.send(message_for(show))
            self.state.delivered(show)
            # 저장 실패 시 즉시 멈춰 추가 전송으로 중복 가능성을 키우지 않는다.
            self.checkpoint()
            self.counts["sent"] += 1
            if self.send_delay:
                self.sleep(self.send_delay)
        # 예매 가능 알림을 먼저 보낸 뒤, 상영준비중 회차를 극장·날짜·영화별로 묶어 알린다.
        for group in prep_groups(self.state.preparing(shows)):
            if self.expired():
                raise DeadlineReached()
            self.checkpoint()
            self.telegram.send(prep_message_for(group))
            self.state.prepared(group)
            self.checkpoint()
            self.counts["prep"] += 1
            if self.send_delay:
                self.sleep(self.send_delay)

    def notice_targets(self, max_age=0):
        """이벤트 공지에 올라온 수도권 무대인사의 (지점, 날짜)."""
        events = self.client.events()
        self.counts["events"] = len(events)
        ids = [str(event["EventID"]) for event in events]
        today = now_kst().date().isoformat()
        if (self.notice_at is not None and ids == self.notice_ids
                and self.clock() - self.notice_at < max_age):
            return {(cid, day) for cid, day in self.notice if day >= today}
        targets, complete = set(), True
        for event_id in ids:
            try:
                for row in self.client.event_rows(event_id):
                    cid, day = str(row.get("CinemaID", "")), str(row.get("PlayDate", ""))[:10]
                    if cid in self.cinemas and is_stage(row):
                        if not valid_day(day):
                            raise ServiceError("무대인사 공지의 날짜 형식이 바뀌었습니다.")
                        if day >= today:
                            targets.add((cid, day))
            except ServiceError as exc:
                if exc.retry_after > 60:
                    raise
                self.errors.append(f"이벤트 조회: {exc}")
                complete = False
        # 일부라도 실패했으면 재사용하지 않고 다음 확인에서 전부 다시 받는다.
        self.notice, self.notice_ids = targets, ids if complete else None
        self.notice_at = self.clock() if complete else None
        return targets

    def check_targets(self, max_age=0):
        """공지에 있는 회차와 상영준비중으로 지켜보는 회차의 시간표를 확인한다."""
        targets = self.notice_targets(max_age)
        today = now_kst().date().isoformat()
        watch = {(cid, day) for cid, day in self.state.watched()
                 if cid in self.cinemas and day >= today}
        for cid, day in sorted(targets | watch, key=lambda x: (x[1], x[0])):
            self.check(cid, day, self.state.data["initialized"])
        if not self.errors:
            self.state.data["initialized"] = True
            self.state.data["last_success"] = now_kst().replace(microsecond=0).isoformat()

    def sweep(self, count, until=None, minimum=0):
        """공지에 없는 회차를 찾는 전체 순회. until이 지나면 minimum건만 채우고 멈춘다."""
        for done in range(count):
            if self.expired():
                raise DeadlineReached()
            if until is not None and done >= minimum and self.clock() >= until:
                break
            cid, day = next_sweep(self.state, self.cinemas, self.days)
            self.check(cid, day)

    def finish(self):
        self.state.prune()
        self.checkpoint()


def run_once(client, backend, telegram=None, *, dry_run=False, sweep_count=180, days=28,
             deadline=None, send_delay=1.1):
    session = Session(client, backend, telegram, dry_run=dry_run, days=days, deadline=deadline,
                      send_delay=send_delay)
    try:
        session.load_cinemas()
        session.check_targets()
        session.sweep(sweep_count)
    except DeadlineReached:
        session.counts["deferred"] = True
    finally:
        session.finish()
    counts = session.counts
    counts["errors"] = len(session.errors)
    for error in session.errors[:5]:
        print(error, file=sys.stderr)
    print(json.dumps(counts, ensure_ascii=False))
    return counts


def run_loop(client, backend, telegram, *, deadline, days=28, interval=60, sweep_count=15,
             notice_refresh=300, cinema_refresh=900, save_every=600, max_failures=10,
             send_delay=1.1, clock=time.monotonic, sleep=time.sleep):
    """deadline까지 interval초마다 확인을 되풀이한다. 일시적인 오류로는 멈추지 않는다."""
    session = Session(client, backend, telegram, days=days, deadline=deadline,
                      send_delay=send_delay, clock=clock, sleep=sleep)
    totals = {"cycles": 0, "error_cycles": 0, "errors": 0, "aborted": False}
    failures, saved_at, cinemas_at = 0, clock(), None
    try:
        while clock() < deadline:
            began, fatal, pause = clock(), False, 0
            session.errors, session.checked = [], set()
            alerts = session.counts["sent"] + session.counts["prep"]
            try:
                if cinemas_at is None or began - cinemas_at >= cinema_refresh:
                    session.load_cinemas()
                    cinemas_at = began
                session.check_targets(notice_refresh)
                session.sweep(sweep_count, until=began + interval, minimum=min(3, sweep_count))
                # 순회 위치 같은 진행 기록은 가끔만 저장한다(알림 전후 저장은 check가 따로 한다).
                if clock() - saved_at >= save_every:
                    session.state.prune()
                    session.checkpoint()
                    saved_at = clock()
            except DeadlineReached:
                break
            except ServiceError as exc:
                # 목록 조회·전송·저장 실패는 이번 확인만 건너뛰고 다음 확인에서 다시 한다.
                fatal = True
                session.errors.append(str(exc))
                pause = min(300, max(0, exc.retry_after - interval))
            totals["cycles"] += 1
            failures = failures + 1 if fatal else 0
            if session.errors:
                totals["error_cycles"] += 1
                totals["errors"] += len(session.errors)
                print(f"확인 오류 {len(session.errors)}건: {session.errors[0]}", file=sys.stderr, flush=True)
            if session.counts["sent"] + session.counts["prep"] != alerts:
                print(f"알림 전송 누적: 예매 가능 {session.counts['sent']}건 · 상영준비중 {session.counts['prep']}건",
                      flush=True)
            if failures >= max_failures:
                # 계속 실패하면 끝낸다. 다음 예약 실행이 새로 시작한다.
                totals["aborted"] = True
                break
            wait = min(began + interval + pause, deadline) - clock()
            if wait > 0:
                sleep(wait)
    finally:
        session.finish()
    result = {key: value for key, value in session.counts.items() if key not in ("deferred", "stage_shows")}
    result.update(totals)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


class RunGuard:
    """같은 워크플로의 실행이 겹치지 않게 한다. 먼저 만들어진 실행이 우선한다."""

    def __init__(self, api, run_number, workflow=WORKFLOW, clock=time.monotonic, sleep=time.sleep):
        self.api, self.run_number, self.workflow = api, run_number, workflow
        self.clock, self.sleep = clock, sleep

    def older_active(self):
        data = self.api.request("GET", f"/actions/workflows/{self.workflow}/runs?per_page=30")
        try:
            return [run for run in data["workflow_runs"]
                    if run["status"] != "completed" and int(run["run_number"]) < self.run_number]
        except (KeyError, TypeError, ValueError):
            raise ServiceError("GitHub 실행 목록 형식을 확인할 수 없습니다.") from None

    def my_turn(self, handoff, patience=600, poll=10):
        """True면 감시를 시작한다. 이어받기 실행만 앞 실행이 끝나기를 기다린다."""
        started = self.clock()
        while self.older_active():
            if not handoff or self.clock() - started >= patience:
                return False
            self.sleep(poll)
        return True

    def hand_off(self, ref):
        """끝나기 직전에 다음 실행을 시작시켜 감시가 끊기지 않게 한다."""
        self.api.request("POST", f"/actions/workflows/{self.workflow}/dispatches",
                         {"ref": ref, "inputs": {"mode": "check", "handoff": "true"}})


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
    parser.add_argument("--loop", action="store_true",
                        help="max-seconds 동안 interval초마다 반복 확인 (sweep-count는 한 번 확인당 개수)")
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args(argv)
    longest = 21000 if args.loop else 480
    if (not 0 <= args.sweep_count <= 500 or not 1 <= args.days <= 42
            or not 30 <= args.max_seconds <= longest or not 30 <= args.interval <= 600):
        parser.error(f"조회 개수 0~500, 날짜 범위 1~42, 실행 시간 30~{longest}초, 확인 간격 30~600초를 사용하세요.")
    if args.loop and args.mode != "check":
        parser.error("--loop는 check 모드에서만 사용합니다.")
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
        guard = None
        run_number = os.environ.get("GITHUB_RUN_NUMBER", "")
        if isinstance(backend, GitHubState) and run_number.isdigit():
            # 감시 기록을 두 실행이 동시에 쓰지 않도록, 먼저 시작한 실행이 있으면 물러난다.
            guard = RunGuard(backend, int(run_number))
            if not guard.my_turn(os.environ.get("HANDOFF") == "true"):
                print("다른 감시 실행이 진행 중입니다. 이 실행은 조회 없이 끝냅니다.")
                write_summary("다른 감시 실행이 이미 진행 중이어서 이 실행은 조회 없이 끝냈습니다. 정상 동작입니다.")
                return 0
        deadline = time.monotonic() + args.max_seconds
        if args.loop:
            result = run_loop(BoundedLotteClient(deadline), backend, telegram, deadline=deadline,
                              days=args.days, interval=args.interval, sweep_count=args.sweep_count)
            write_summary(f"반복 감시 {result['cycles']}회 확인 · 시간표 조회 누적 {result['schedules']}건 · 예매 가능 알림 {result['sent']}건 · 상영준비중 알림 {result['prep']}건 · 오류가 있던 확인 {result['error_cycles']}회\n\n"
                          f"약 {args.interval}초마다 공지된 회차와 상영준비중 회차를 확인하고, 그 사이에 28일 시간표를 나누어 순회합니다.")
            if result["aborted"]:
                write_summary("오류가 계속되어 감시를 끝냈습니다. 다음 예약 실행이 새로 시작합니다.")
            elif guard:
                try:
                    guard.hand_off(os.environ.get("GITHUB_REF_NAME") or "main")
                    write_summary("다음 실행을 시작시켰습니다. 감시가 이어집니다.")
                except ServiceError as exc:
                    print(f"다음 실행을 시작시키지 못했습니다: {exc}", file=sys.stderr)
                    write_summary("다음 실행을 시작시키지 못했습니다. 다음 예약 실행이 새로 시작합니다.")
            return 1 if result["aborted"] or result["error_cycles"] * 2 > result["cycles"] else 0
        result = run_once(BoundedLotteClient(deadline), backend, telegram,
                          dry_run=args.mode == "dry-run", sweep_count=args.sweep_count,
                          days=args.days, deadline=deadline)
        write_summary(f"영화관 {result['cinemas']}곳 · 시간표 {result['schedules']}건 · 무대인사 {result['stage_shows']}회차 · 예매 가능 알림 {result['sent']}건 · 상영준비중 알림 {result['prep']}건 · 오류 {result['errors']}건\n\n"
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
