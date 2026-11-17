import base64
import copy
import io
import json
import unittest
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
from unittest.mock import MagicMock, Mock, patch

from notifier.cloud import (CloudState, DeadlineReached, GitHubState, RunGuard, Session,
                            encoded_state, fresh_state, next_sweep, run_loop, run_once,
                            validate_state)
from notifier.core import ServiceError
from test_core import AT, cinemas, prep_row, prep_show, sample, show


class MemoryState:
    def __init__(self, data=None):
        self.data = copy.deepcopy(data or fresh_state())
        self.writes = []

    def load(self):
        return copy.deepcopy(self.data)

    def save(self, data):
        self.data = copy.deepcopy(validate_state(data))
        self.writes.append(copy.deepcopy(data))


class FakeClock:
    """기다리는 시간만큼 바로 앞으로 가는 시계. 조회 자체는 0초로 친다."""
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class PublicStateTests(unittest.TestCase):
    def test_baseline_then_new_show_and_restart(self):
        state = CloudState(fresh_state())
        self.assertEqual(state.observe("1016:2026-10-31", [show()]), [])
        added = replace(show(), key=show().key + "new")
        self.assertEqual(state.observe("1016:2026-10-31", [show(), added]), [added])
        state.delivered(added)
        restored = CloudState(json.loads(encoded_state(state.data)))
        self.assertEqual(restored.observe("1016:2026-10-31", [show(), added]), [])

    def test_waiting_to_open_and_failed_delivery_retry(self):
        state = CloudState(fresh_state())
        self.assertEqual(state.observe("1016:2026-10-31", [show(False)]), [])
        for _ in range(2):
            state = CloudState(json.loads(encoded_state(state.data)))
            self.assertEqual(state.observe("1016:2026-10-31", [show()]), [show()])
        state.delivered(show())
        self.assertEqual(state.observe("1016:2026-10-31", [show(False), show()]), [])

    def test_new_event_scope_after_baseline(self):
        state = CloudState(fresh_state())
        self.assertEqual(state.observe("1016:2026-10-31", [show()], True), [show()])

    def test_preparing_then_opening_survives_restart(self):
        state = CloudState(fresh_state())
        prep = prep_show()
        # 처음 보는 지점·날짜여도 상영준비중은 기준 상태로 묻지 않는다.
        self.assertEqual(state.observe("1016:2026-10-31", [prep]), [])
        self.assertEqual(state.preparing([prep]), [prep])
        state.prepared([prep])
        state = CloudState(json.loads(encoded_state(state.data)))
        self.assertEqual(state.observe("1016:2026-10-31", [prep]), [])
        self.assertEqual(state.preparing([prep]), [])
        self.assertEqual(state.observe("1016:2026-10-31", [show()]), [show()])
        state.delivered(show())
        self.assertEqual(state.observe("1016:2026-10-31", [show()]), [])
        self.assertEqual(state.preparing([prep]), [])

    def test_existing_seen_record_from_old_version_gets_prep_alert(self):
        # 이전 버전이 'seen'으로만 남긴 준비중 회차도 새 버전 첫 실행에서 알린다.
        state = CloudState(fresh_state())
        prep = prep_show()
        state.observe("1016:2026-10-31", [prep])
        old = json.loads(encoded_state(state.data))
        self.assertEqual({row["status"] for row in old["shows"].values()}, {"seen"})
        self.assertEqual(CloudState(old).preparing([prep]), [prep])

    def test_public_state_rejects_extra_sensitive_fields_and_bad_dates(self):
        for edit in (lambda s: s.update(token="must-not-publish"),
                     lambda s: s["cursor"].update(anchor="invalid"),
                     lambda s: s["scopes"].update({"1016:bad": True}),
                     lambda s: s.update(version=999)):
            state = fresh_state()
            edit(state)
            with self.assertRaises(ServiceError):
                encoded_state(state)

    def test_state_saved_by_older_version_is_upgraded(self):
        old = fresh_state()
        del old["watch"]
        self.assertEqual(validate_state(old)["watch"], {})
        self.assertEqual(CloudState(old).watched(), [])

    def test_watch_rejects_bad_entries(self):
        for bad in ({"1016:bad": True}, {"1016:2026-10-31": "token"}, {"abc:2026-10-31": True}):
            state = fresh_state()
            state["watch"] = bad
            with self.assertRaises(ServiceError):
                encoded_state(state)

    @patch("notifier.cloud.now_kst", return_value=AT)
    def test_watch_follows_preparing_shows_and_is_pruned(self, _):
        state = CloudState(fresh_state())
        state.track("1016:2026-10-31", [prep_show(), show()])
        state.track("1016:2026-10-20", [prep_show()])
        self.assertEqual(sorted(state.watched()), [("1016", "2026-10-20"), ("1016", "2026-10-31")])
        state.prune()
        self.assertEqual(state.watched(), [("1016", "2026-10-31")])
        state = CloudState(json.loads(encoded_state(state.data)))
        state.track("1016:2026-10-31", [show()])
        self.assertEqual(state.watched(), [])

    @patch("notifier.cloud.now_kst", return_value=AT)
    def test_sweep_resumes_and_wraps(self, _):
        state = CloudState(fresh_state())
        cs = {"1": {}, "2": {}}
        first = [next_sweep(state, cs, 2) for _ in range(3)]
        state = CloudState(json.loads(encoded_state(state.data)))
        self.assertEqual(first, [("1", "2026-10-24"), ("2", "2026-10-24"), ("1", "2026-10-25")])
        self.assertEqual(next_sweep(state, cs, 2), ("2", "2026-10-25"))
        self.assertEqual(next_sweep(state, cs, 2), ("1", "2026-10-24"))

    @patch("notifier.cloud.now_kst", return_value=AT)
    def test_sweep_skips_past_days_without_starving_future(self, _):
        state = CloudState(fresh_state())
        state.data["cursor"] = {"anchor": "2026-10-23", "index": 0}
        self.assertEqual(next_sweep(state, {"1": {}, "2": {}}, 3), ("1", "2026-10-24"))
        self.assertEqual(state.data["cursor"]["index"], 3)


class CloudRunTests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch("notifier.cloud.now_kst", return_value=AT),
                        patch("notifier.core.now_kst", return_value=AT)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = Mock()
        self.client.cinemas.return_value = cinemas()
        self.client.events.return_value = [{"EventID": 1}]
        self.client.event_rows.return_value = [{**sample(), "PlayDate": sample()["PlayDt"]}]
        self.client.schedule.return_value = [sample()]
        self.backend = MemoryState()
        self.telegram = Mock()

    def run_monitor(self, **kwargs):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return run_once(self.client, self.backend, self.telegram,
                            sweep_count=0, send_delay=0, **kwargs)

    def test_first_run_then_restart_and_new_opening(self):
        self.assertEqual(self.run_monitor()["sent"], 0)
        self.assertTrue(self.backend.data["initialized"])
        self.assertEqual(self.run_monitor()["sent"], 0)
        row = sample()
        row["PlaySequence"] = 2
        self.client.schedule.return_value = [sample(), row]
        self.assertEqual(self.run_monitor()["sent"], 1)
        self.assertEqual(self.run_monitor()["sent"], 0)
        self.telegram.send.assert_called_once()

    def test_delivery_failure_persists_pending_and_retries(self):
        self.backend.data["initialized"] = True
        self.telegram.send.side_effect = ServiceError("temporary")
        with self.assertRaises(ServiceError):
            self.run_monitor()
        self.assertEqual({s["status"] for s in self.backend.data["shows"].values()}, {"seen"})
        self.telegram.send.side_effect = None
        self.assertEqual(self.run_monitor()["sent"], 1)
        self.assertEqual(self.run_monitor()["sent"], 0)

    def test_preparing_alert_once_then_opening_alert(self):
        second = prep_row(PlaySequence=2, StartTime="19:00")
        self.client.schedule.return_value = [prep_row(), second]
        first = self.run_monitor()
        self.assertEqual((first["prep"], first["sent"]), (1, 0))
        self.assertIn("회차 2개", self.telegram.send.call_args.args[0])
        self.assertEqual({s["status"] for s in self.backend.data["shows"].values()}, {"prep"})
        again = self.run_monitor()
        self.assertEqual((again["prep"], again["sent"]), (0, 0))
        opened = sample()
        opened_second = {**sample(), "PlaySequence": 2, "StartTime": "19:00"}
        self.client.schedule.return_value = [opened, opened_second]
        last = self.run_monitor()
        self.assertEqual((last["prep"], last["sent"]), (0, 2))
        self.assertEqual(self.run_monitor()["sent"], 0)
        self.assertEqual(self.telegram.send.call_count, 3)

    def test_preparing_delivery_failure_retries(self):
        self.client.schedule.return_value = [prep_row()]
        self.telegram.send.side_effect = ServiceError("temporary")
        with self.assertRaises(ServiceError):
            self.run_monitor()
        self.assertEqual({s["status"] for s in self.backend.data["shows"].values()}, {"seen"})
        self.telegram.send.side_effect = None
        self.assertEqual(self.run_monitor()["prep"], 1)
        self.assertEqual(self.run_monitor()["prep"], 0)

    def test_dry_run_does_not_send_preparing_alert(self):
        self.client.schedule.return_value = [prep_row()]
        self.assertEqual(self.run_monitor(dry_run=True)["prep"], 0)
        self.telegram.send.assert_not_called()

    def test_storage_failure_prevents_send(self):
        self.backend.data["initialized"] = True
        self.backend.save = Mock(side_effect=ServiceError("storage failed"))
        with self.assertRaises(ServiceError):
            self.run_monitor()
        self.telegram.send.assert_not_called()

    def test_post_send_checkpoint_stops_further_sends_on_failure(self):
        self.backend.data["initialized"] = True
        row = sample()
        row["PlaySequence"] = 2
        self.client.schedule.return_value = [sample(), row]
        self.backend.save = Mock(side_effect=[None, ServiceError("save failed"), ServiceError("save failed")])
        with self.assertRaises(ServiceError):
            self.run_monitor()
        self.telegram.send.assert_called_once()

    def test_dry_run_never_reads_writes_or_sends(self):
        self.backend.load = Mock(side_effect=AssertionError("read"))
        self.backend.save = Mock(side_effect=AssertionError("write"))
        result = self.run_monitor(dry_run=True)
        self.assertEqual(result["stage_shows"], 1)
        self.telegram.send.assert_not_called()

    def test_failed_scope_does_not_become_empty_baseline(self):
        self.client.schedule.side_effect = ServiceError("bad response")
        result = self.run_monitor()
        self.assertEqual(result["errors"], 1)
        self.assertFalse(self.backend.data["initialized"])
        self.assertEqual(self.backend.data["scopes"], {})

    def test_rate_limit_stops_additional_lookups(self):
        self.client.event_rows.side_effect = ServiceError("rate limited", 120)
        with self.assertRaises(ServiceError):
            self.run_monitor()
        self.client.schedule.assert_not_called()

    def test_invalid_stage_event_date_is_reported(self):
        self.client.event_rows.return_value = [{**sample(), "PlayDate": "invalid"}]
        self.assertEqual(self.run_monitor()["errors"], 1)
        self.assertFalse(self.backend.data["initialized"])

    def test_deadline_saves_progress(self):
        self.client.event_rows.side_effect = DeadlineReached()
        result = self.run_monitor()
        self.assertTrue(result["deferred"])
        self.assertEqual(len(self.backend.writes), 1)


class LoopTests(unittest.TestCase):
    def setUp(self):
        for p in (patch("notifier.cloud.now_kst", return_value=AT),
                  patch("notifier.core.now_kst", return_value=AT)):
            p.start()
            self.addCleanup(p.stop)
        self.client = Mock()
        self.client.cinemas.return_value = cinemas()
        self.client.events.return_value = [{"EventID": 1}]
        self.client.event_rows.return_value = [{**sample(), "PlayDate": sample()["PlayDt"]}]
        self.client.schedule.return_value = [sample()]
        self.backend = MemoryState()
        self.telegram = Mock()
        self.clock = FakeClock()

    def loop(self, seconds, **kwargs):
        options = {"sweep_count": 0, "send_delay": 0, **kwargs}
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return run_loop(self.client, self.backend, self.telegram, deadline=seconds,
                            clock=self.clock, sleep=self.clock.sleep, **options)

    def texts(self):
        return [call.args[0] for call in self.telegram.send.call_args_list]

    def test_opening_is_caught_on_the_next_minute(self):
        self.client.schedule.side_effect = [[prep_row()], [prep_row()], [sample()]]
        result = self.loop(180)
        self.assertEqual((result["cycles"], result["prep"], result["sent"]), (3, 1, 1))
        self.assertIn("상영준비중", self.texts()[0])
        self.assertIn("예매 가능", self.texts()[1])
        self.assertEqual(self.clock.sleeps, [60, 60, 60])
        self.assertEqual({s["status"] for s in self.backend.data["shows"].values()}, {"sent"})

    def test_notice_details_are_reused_until_refresh_or_change(self):
        self.loop(180)
        self.assertEqual(self.client.events.call_count, 3)
        self.assertEqual(self.client.event_rows.call_count, 1)
        # 새 실행은 처음에 한 번 받고, 100초가 지나면 다시 받는다.
        self.loop(self.clock.now + 180, notice_refresh=100)
        self.assertEqual(self.client.event_rows.call_count, 3)

    def test_new_notice_is_read_immediately(self):
        self.client.events.side_effect = [[{"EventID": 1}], [{"EventID": 1}, {"EventID": 2}],
                                          [{"EventID": 1}, {"EventID": 2}]]
        self.loop(180)
        self.assertEqual([c.args[0] for c in self.client.event_rows.call_args_list], ["1", "1", "2"])

    def test_preparing_show_found_by_sweep_is_watched_every_cycle(self):
        # 공지에 없는 회차: 순회로 찾은 뒤에는 매 주기 확인해 열리자마자 알린다.
        self.client.event_rows.return_value = []
        self.client.schedule.side_effect = [[prep_row()], [prep_row()], [sample()]]
        result = self.loop(60, sweep_count=1, days=1)
        self.assertEqual((result["prep"], result["sent"]), (1, 0))
        self.assertEqual(self.backend.data["watch"], {"1016:2026-10-24": True})
        # 다음 실행(이어받기)에서도 저장된 목록으로 바로 확인한다.
        result = self.loop(self.clock.now + 120, sweep_count=0)
        self.assertEqual((result["prep"], result["sent"]), (0, 1))
        self.assertEqual(self.backend.data["watch"], {})
        self.assertEqual(self.client.schedule.call_count, 3)

    def test_temporary_failure_does_not_stop_monitoring(self):
        self.client.events.side_effect = [ServiceError("down"), [{"EventID": 1}], [{"EventID": 1}]]
        self.backend.data["initialized"] = True
        result = self.loop(180)
        self.assertEqual((result["cycles"], result["error_cycles"], result["aborted"]), (3, 1, False))
        self.assertEqual(result["sent"], 1)

    def test_failed_send_is_retried_next_cycle(self):
        self.backend.data["initialized"] = True
        self.telegram.send.side_effect = [ServiceError("temporary"), None]
        result = self.loop(180)
        self.assertEqual((result["sent"], result["error_cycles"]), (1, 1))
        self.assertEqual(self.telegram.send.call_count, 2)

    def test_rate_limit_waits_longer_before_next_cycle(self):
        self.client.events.side_effect = [ServiceError("rate limited", 200), [{"EventID": 1}]]
        self.loop(260)
        self.assertEqual(self.clock.sleeps, [200, 60])

    def test_repeated_failures_abort(self):
        self.client.events.side_effect = ServiceError("down")
        result = self.loop(3600, max_failures=3)
        self.assertEqual((result["cycles"], result["aborted"]), (3, True))

    def test_one_bad_schedule_never_aborts(self):
        self.client.schedule.side_effect = ServiceError("bad response")
        result = self.loop(300, max_failures=2)
        self.assertEqual((result["cycles"], result["error_cycles"], result["aborted"]), (5, 5, False))

    def test_progress_is_saved_periodically_and_at_end(self):
        self.loop(300, save_every=120)
        self.assertEqual(len(self.backend.writes), 3)

    def test_sweep_stops_when_cycle_time_is_used_up(self):
        self.client.event_rows.return_value = []
        self.client.cinemas.return_value = {str(i): {} for i in range(1, 41)}
        self.client.schedule.side_effect = lambda *_: self.clock.sleep(10) or []
        session = Session(self.client, self.backend, self.telegram, clock=self.clock, sleep=self.clock.sleep)
        session.load_cinemas()
        # 한 건에 10초가 걸리면 60초 안에 6건만 돌고 다음 주기로 넘긴다.
        session.sweep(30, until=60, minimum=3)
        self.assertEqual(self.client.schedule.call_count, 6)
        # 시간이 이미 다 됐어도 최소 건수는 돌아 순회가 멈추지 않는다.
        session.sweep(30, until=60, minimum=3)
        self.assertEqual(self.client.schedule.call_count, 9)
        self.assertEqual(session.state.data["cursor"]["index"], 9)

    def test_deadline_inside_cycle_saves_and_stops(self):
        self.client.schedule.side_effect = DeadlineReached()
        result = self.loop(180)
        self.assertEqual((result["cycles"], result["aborted"]), (0, False))
        self.assertEqual(len(self.backend.writes), 1)


class RunGuardTests(unittest.TestCase):
    def guard(self, responses, number=10):
        api = Mock()
        api.request.side_effect = [{"workflow_runs": runs} for runs in responses]
        clock = FakeClock()
        return RunGuard(api, number, clock=clock, sleep=clock.sleep), api, clock

    def run_row(self, number, status="in_progress"):
        return {"run_number": number, "status": status}

    def test_starts_when_nothing_older_is_active(self):
        guard, api, _ = self.guard([[self.run_row(10), self.run_row(11, "queued"), self.run_row(9, "completed")]])
        self.assertTrue(guard.my_turn(False))
        self.assertIn("/actions/workflows/notify.yml/runs", api.request.call_args.args[1])

    def test_scheduled_run_steps_aside_for_older_run(self):
        for status in ("in_progress", "queued", "pending", "waiting"):
            guard, _, clock = self.guard([[self.run_row(10), self.run_row(7, status)]])
            self.assertFalse(guard.my_turn(False))
            self.assertEqual(clock.sleeps, [])

    def test_handoff_run_waits_for_older_run_to_finish(self):
        guard, _, clock = self.guard([[self.run_row(9)], [self.run_row(9)], [self.run_row(9, "completed")]])
        self.assertTrue(guard.my_turn(True))
        self.assertEqual(clock.sleeps, [10, 10])

    def test_handoff_run_gives_up_after_patience(self):
        guard, _, clock = self.guard([[self.run_row(9)]] * 5)
        self.assertFalse(guard.my_turn(True, patience=30))
        self.assertEqual(clock.sleeps, [10, 10, 10])

    def test_unexpected_run_list_is_an_error(self):
        guard, _, _ = self.guard([[{"status": "in_progress"}]])
        with self.assertRaises(ServiceError):
            guard.my_turn(False)

    def test_hand_off_starts_next_check_run(self):
        api = Mock()
        RunGuard(api, 10).hand_off("main")
        method, path, body = api.request.call_args.args
        self.assertEqual((method, path), ("POST", "/actions/workflows/notify.yml/dispatches"))
        self.assertEqual(body, {"ref": "main", "inputs": {"mode": "check", "handoff": "true"}})


class GitHubStoreTests(unittest.TestCase):
    def store(self, responses):
        store = GitHubState("example/lotte-stage-notifier", "fake-token")
        store.request = Mock(side_effect=responses)
        return store

    def content(self, data=None):
        return {"type": "file", "encoding": "base64", "sha": "old-sha",
                "content": base64.b64encode(encoded_state(data or fresh_state())).decode()}

    def test_load_and_optimistic_update(self):
        store = self.store([{"private": False, "default_branch": "main"}, {"object": {"sha": "state-head"}},
                            self.content(), {"content": {"sha": "new-sha"}}])
        data = store.load()
        store.save(data)
        self.assertEqual(store.request.call_count, 3)
        data["initialized"] = True
        store.save(data)
        self.assertEqual(store.request.call_args.args[2]["sha"], "old-sha")
        self.assertEqual(store.sha, "new-sha")
        self.assertNotIn("fake-token", base64.b64decode(store.request.call_args.args[2]["content"]).decode())

    def test_create_initial_branch_and_state_before_monitoring(self):
        store = self.store([{"private": False, "default_branch": "main"}, None,
                            {"object": {"sha": "main-sha"}}, {}, None, {"content": {"sha": "initial"}}])
        self.assertEqual(store.load(), fresh_state())
        self.assertEqual(store.request.call_args_list[3].args[2],
                         {"ref": "refs/heads/notifier-state", "sha": "main-sha"})

    def test_empty_success_body_is_accepted(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b""
        store = GitHubState("example/lotte-stage-notifier", "fake-token")
        with patch("notifier.cloud.urllib.request.urlopen", return_value=response):
            self.assertIsNone(store.request("POST", "/actions/workflows/notify.yml/dispatches", {"ref": "main"}))

    def test_private_repo_refused(self):
        store = self.store([{"private": True}])
        with self.assertRaises(ServiceError):
            store.load()
        self.assertEqual(store.request.call_count, 1)

    def test_missing_file_on_existing_state_branch_fails_closed(self):
        store = self.store([{"private": False, "default_branch": "main"},
                            {"object": {"sha": "state-head"}}, None, {"object": {"sha": "main-head"}}])
        with self.assertRaises(ServiceError):
            store.load()
        self.assertTrue(all(c.args[0] == "GET" for c in store.request.call_args_list))

    def test_corrupt_json_never_overwrites_history(self):
        bad = self.content()
        bad["content"] = base64.b64encode(b"not-json").decode()
        store = self.store([{"private": False, "default_branch": "main"}, {"object": {"sha": "head"}}, bad])
        with self.assertRaises(ServiceError):
            store.load()
        self.assertTrue(all(c.args[0] == "GET" for c in store.request.call_args_list))


if __name__ == "__main__":
    unittest.main()
