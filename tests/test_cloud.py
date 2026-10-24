import base64
import copy
import io
import json
import unittest
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
from unittest.mock import Mock, patch

from notifier.cloud import (CloudState, DeadlineReached, GitHubState, encoded_state,
                            fresh_state, next_sweep, run_once, validate_state)
from notifier.core import ServiceError
from test_core import AT, cinemas, sample, show


class MemoryState:
    def __init__(self, data=None):
        self.data = copy.deepcopy(data or fresh_state())
        self.writes = []

    def load(self):
        return copy.deepcopy(self.data)

    def save(self, data):
        self.data = copy.deepcopy(validate_state(data))
        self.writes.append(copy.deepcopy(data))


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

    def test_public_state_rejects_extra_sensitive_fields_and_bad_dates(self):
        for edit in (lambda s: s.update(token="must-not-publish"),
                     lambda s: s["cursor"].update(anchor="invalid"),
                     lambda s: s["scopes"].update({"1016:bad": True}),
                     lambda s: s.update(version=999)):
            state = fresh_state()
            edit(state)
            with self.assertRaises(ServiceError):
                encoded_state(state)

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
