import copy
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

from notifier.core import (KST, LotteClient, Monitor, Screening, ServiceError, StateStore,
                           Telegram, is_stage, load_settings, parse_screenings, prep_groups,
                           prep_message_for, recipient_key, save_settings, show_time)

FIXTURES = Path(__file__).parent / "fixtures"
TEMP_ROOT = Path(__file__).resolve().parent / ".tmp"
TEMP_ROOT.mkdir(exist_ok=True)
AT = datetime(2026, 10, 24, 12, tzinfo=KST)


def sample():
    return json.loads((FIXTURES / "screening.json").read_text(encoding="utf-8"))


def cinemas():
    return {"1016": {"DivisionCode": 1, "DetailDivisionCode": "0001", "CinemaID": 1016, "CinemaNameKR": "월드타워"}}


def show(bookable=True):
    value = sample()
    value["IsBookingYN"] = "Y" if bookable else "N"
    return parse_screenings([value], cinemas(), AT)[0]


def prep_row(**changes):
    """상영준비중(J) 회차. 실제 응답처럼 잔여석이 전체 좌석과 같다."""
    value = sample()
    value.update(IsBookingYN="J", **changes)
    return value


def prep_show(**changes):
    return parse_screenings([prep_row(**changes)], cinemas(), AT)[0]


class DetectionTests(unittest.TestCase):
    def test_real_response_remaining_seats(self):
        s = show()
        self.assertEqual(s.seats, 87)
        self.assertTrue(s.bookable)
        self.assertEqual(s.movie, "최종면접")

    def test_zero_sequence_is_valid(self):
        self.assertIn("|0|", show().key)

    def test_other_region_is_excluded(self):
        row = sample()
        row["CinemaID"] = 9999
        self.assertEqual(parse_screenings([row], cinemas(), AT), [])

    def test_gv_and_normal_not_stage(self):
        for code, name in ((10, "일반"), (40, "GV")):
            row = sample()
            row.update(AccompanyTypeCode=code, AccompanyTypeNameKR=name)
            self.assertFalse(is_stage(row))

    def test_stage_label_with_spaces(self):
        row = {"AccompanyTypeCode": 10, "BkShowLabelTxtNm1": "무대 인사"}
        self.assertTrue(is_stage(row))

    def test_closed_statuses_and_sold_out(self):
        for status in ["N", "E", "J"]:
            row = sample()
            row["IsBookingYN"] = status
            self.assertFalse(parse_screenings([row], cinemas(), AT)[0].bookable)
        row = sample()
        row["BookingSeatCount"] = 0
        self.assertFalse(parse_screenings([row], cinemas(), AT)[0].bookable)

    def test_only_j_status_is_preparing(self):
        self.assertTrue(prep_show().preparing)
        self.assertFalse(prep_show().bookable)
        for status in ["Y", "N", "E"]:
            row = sample()
            row["IsBookingYN"] = status
            self.assertFalse(parse_screenings([row], cinemas(), AT)[0].preparing)

    def test_prep_message_groups_same_cinema_day_movie(self):
        early, late = prep_show(StartTime="13:00", PlaySequence=1), prep_show(StartTime="17:10", PlaySequence=2)
        other = prep_show(MovieNameKR="다른 영화", MovieCode="99999")
        groups = prep_groups([late, other, early])
        self.assertEqual(sorted(len(g) for g in groups), [1, 2])
        pair = next(g for g in groups if len(g) == 2)
        self.assertEqual(pair, [early, late])
        text = prep_message_for(pair)
        self.assertIn("상영준비중", text)
        self.assertIn("회차 2개", text)
        self.assertLess(text.index("13:00"), text.index("17:10"))
        self.assertNotIn("다른 영화", text)

    def test_missing_or_unknown_fields_fail_closed(self):
        row = sample()
        row.pop("BookingSeatCount")
        with self.assertRaises(ServiceError):
            parse_screenings([row], cinemas(), AT)
        row = sample()
        row["IsBookingYN"] = "UNKNOWN"
        with self.assertRaises(ServiceError):
            parse_screenings([row], cinemas(), AT)

    def test_past_screening_ignored(self):
        self.assertEqual(parse_screenings([sample()], cinemas(), datetime(2026, 11, 1, tzinfo=KST)), [])

    def test_after_midnight_show(self):
        self.assertEqual(show_time("2026-10-24", "25:10"), datetime(2026, 10, 25, 1, 10, tzinfo=KST))


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = StateStore(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_existing_open_baseline_and_restart(self):
        self.assertEqual(self.store.observe("scope", [show()], "me"), [])
        self.store.close()
        self.store = StateStore(self.path)
        self.assertEqual(self.store.observe("scope", [show()], "me"), [])

    def test_closed_becomes_open(self):
        self.store.observe("scope", [show(False)], "me")
        self.assertEqual(self.store.observe("scope", [show()], "me"), [show()])

    def test_new_show_in_initialized_scope(self):
        self.store.observe("scope", [], "me")
        self.assertEqual(self.store.observe("scope", [show()], "me"), [show()])

    def test_failed_send_retries_after_restart(self):
        self.store.observe("scope", [], "me")
        self.assertEqual(self.store.observe("scope", [show()], "me"), [show()])
        self.store.close()
        self.store = StateStore(self.path)
        self.assertEqual(self.store.observe("scope", [show()], "me"), [show()])

    def test_successful_send_deduplicates_across_scans(self):
        self.store.observe("scope", [show()], "me", True)
        self.store.delivered("me", show())
        self.assertEqual(self.store.observe("other_scope", [show()], "me", True), [])

    def test_seat_return_not_duplicate_opening(self):
        self.store.delivered("me", show())
        self.store.observe("scope", [show(False)], "me")
        self.assertEqual(self.store.observe("scope", [show()], "me"), [])

    def test_preparing_notified_once_then_opening_notified_once(self):
        prep = prep_show()
        self.assertEqual(self.store.observe("scope", [prep], "me"), [])
        self.assertEqual(self.store.preparing("me", [prep]), [prep])
        # 전송 실패(기록 안 함) 뒤 재시작해도 다시 시도한다.
        self.store.close()
        self.store = StateStore(self.path)
        self.assertEqual(self.store.preparing("me", [prep]), [prep])
        self.store.prepared("me", [prep])
        self.assertEqual(self.store.preparing("me", [prep]), [])
        self.assertEqual(self.store.observe("scope", [show()], "me"), [show()])
        self.store.delivered("me", show())
        self.assertEqual(self.store.observe("scope", [show()], "me"), [])
        self.assertEqual(self.store.preparing("me", [prep]), [])

    def test_preparing_in_first_seen_scope_is_not_baselined(self):
        prep = prep_show()
        self.store.observe("scope", [prep], "me")
        self.store.prepared("me", [prep])
        self.assertEqual(self.store.observe("scope", [show()], "me"), [show()])

    def test_baseline_or_sent_show_never_gets_prep_alert(self):
        self.store.observe("scope", [show()], "me")
        self.assertEqual(self.store.preparing("me", [prep_show()]), [])

    def test_recipient_isolation(self):
        self.store.delivered("one", show())
        self.assertEqual(self.store.observe("scope", [show()], "two", True), [show()])

    def test_new_event_scope_can_notify_after_initial_cycle(self):
        self.store.mark_initialized("events:me")
        self.assertTrue(self.store.initialized("events:me"))
        self.assertEqual(self.store.observe("new", [show()], "me", self.store.initialized("events:me")), [show()])

    def test_token_rotation_keeps_identity(self):
        self.assertEqual(recipient_key("123:a", "1"), recipient_key("123:b", "1"))


class ClientTests(unittest.TestCase):
    def test_region_cinema_selection(self):
        client = LotteClient()
        rows = [dict(CinemaID=i, DivisionCode=1, DetailDivisionCode=region)
                for i, region in enumerate(["0001", "0002", "0005"])]
        client.call = Mock(return_value={"Cinemas": {"Cinemas": {"Items": rows}}})
        self.assertEqual(set(client.cinemas()), {"0", "1"})

    def test_pagination(self):
        client = LotteClient()
        client.call = Mock(side_effect=[
            {"Items": [{"EventID": "1", "EventTypeCode": "107"}], "TotalCount": 2},
            {"Items": [{"EventID": "2", "EventTypeCode": "101"}], "TotalCount": 2}])
        self.assertEqual(len(client.events()), 1)
        self.assertEqual(client.call.call_args.kwargs["PageNo"], 2)

    def test_malformed_schedule_is_not_empty_success(self):
        client = LotteClient()
        client.call = Mock(return_value={"PlaySeqs": {}})
        with self.assertRaises(ServiceError):
            client.schedule(cinemas()["1016"], "2026-10-31")

    def test_api_failure_rejected(self):
        client = LotteClient(http=Mock(post=Mock(return_value={"IsOK": "false"})))
        with self.assertRaises(ServiceError):
            client.cinemas()

    def test_telegram_payload_and_failure(self):
        http = Mock()
        http.post.return_value = {"ok": True, "result": {"message_id": 1}}
        bot = Telegram("123456:" + "x"*30, "1234", http)
        bot.send("hello")
        self.assertEqual(http.post.call_args.args[1]["chat_id"], "1234")
        http.post.return_value = {"ok": False}
        with self.assertRaises(ServiceError):
            bot.send("hello")

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_credentials_encrypted_round_trip(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as d:
            path = Path(d)
            save_settings(path, {"interval": 90}, "secret-token", "chat")
            self.assertNotIn(b"secret-token", (path / "credentials.bin").read_bytes())
            settings, token, chat = load_settings(path)
            self.assertEqual((settings["interval"], token, chat), (90, "secret-token", "chat"))


class MonitorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.monitor = Monitor(Path(self.temp.name), {"notify_existing": False, "interval": 60, "days": 28, "sweep": True},
                               "123456:"+"x"*30, "123", Mock(), threading.Event())
        self.monitor.store = StateStore(Path(self.temp.name) / "state.sqlite3")
        self.monitor.cinemas = cinemas()
        self.monitor.client = Mock()
        self.monitor.telegram = Mock()

    def tearDown(self):
        self.monitor.store.close()
        self.temp.cleanup()

    def test_opening_send_failure_retries_without_marking_delivered(self):
        closed = sample()
        closed["IsBookingYN"] = "N"
        self.monitor.client.schedule.side_effect = [[closed], [sample()], [sample()], [sample()]]
        with patch("notifier.core.now_kst", return_value=AT):
            self.monitor.check_schedule("1016", "2026-10-31", force=True)
            self.monitor.telegram.send.side_effect = [ServiceError("send failed"), None]
            with self.assertRaises(ServiceError):
                self.monitor.check_schedule("1016", "2026-10-31", force=True)
            self.monitor.check_schedule("1016", "2026-10-31", force=True)
            self.monitor.check_schedule("1016", "2026-10-31", force=True)
        self.assertEqual(self.monitor.telegram.send.call_count, 2)

    def test_preparing_alert_then_opening_alert(self):
        self.monitor.client.schedule.side_effect = [[prep_row()], [prep_row()], [sample()], [sample()]]
        with patch("notifier.core.now_kst", return_value=AT):
            for _ in range(4):
                self.monitor.check_schedule("1016", "2026-10-31", force=True)
        texts = [call.args[0] for call in self.monitor.telegram.send.call_args_list]
        self.assertEqual(len(texts), 2)
        self.assertIn("상영준비중", texts[0])
        self.assertIn("예매 가능", texts[1])

    def test_preparing_send_failure_retries(self):
        self.monitor.client.schedule.return_value = [prep_row()]
        self.monitor.telegram.send.side_effect = [ServiceError("send failed"), None]
        with patch("notifier.core.now_kst", return_value=AT):
            with self.assertRaises(ServiceError):
                self.monitor.check_schedule("1016", "2026-10-31", force=True)
            self.monitor.check_schedule("1016", "2026-10-31", force=True)
            self.monitor.check_schedule("1016", "2026-10-31", force=True)
        self.assertEqual(self.monitor.telegram.send.call_count, 2)

    def test_one_bad_schedule_does_not_prevent_other_event_targets(self):
        self.monitor.client.events.return_value = [{"EventID": "1"}]
        self.monitor.client.event_rows.return_value = [
            {"CinemaID": "1016", "PlayDate": d, "AccompanyTypeCode": 30}
            for d in ("2026-10-30", "2026-10-31")]
        self.monitor.check_schedule = Mock(side_effect=[ServiceError("one failed"), None])
        with patch("notifier.core.now_kst", return_value=AT):
            with self.assertRaises(ServiceError):
                self.monitor.event_cycle()
        self.assertEqual(self.monitor.check_schedule.call_count, 2)

    def test_next_cycle_new_scope_is_not_suppressed(self):
        self.monitor.client.events.return_value = [{"EventID": "1"}]
        self.monitor.client.event_rows.side_effect = [[], [
            {"CinemaID": "1016", "PlayDate": "2026-10-31", "AccompanyTypeCode": 30}]]
        self.monitor.client.schedule.return_value = [sample()]
        with patch("notifier.core.now_kst", return_value=AT):
            self.monitor.event_cycle()
            self.monitor.event_cycle()
        self.monitor.telegram.send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
