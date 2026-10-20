from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from kapwatch.app import App
from kapwatch.config import Config
from kapwatch.notify import DeliveryError, Telegram
from kapwatch.source import (ISTANBUL, KapSource, SourceError, parse_records,
                             query_window, retry_after_seconds)
from kapwatch.store import Store


START = date(2026, 10, 10)
END = date(2026, 10, 11)
NOW = datetime(2026, 10, 11, 22, 0, tzinfo=ISTANBUL).timestamp()
DEST = {"telegram": "-1001234567890"}


def item(index=10, stamp="11.10.2026 10:00:00", blocked=False):
    return {"disclosureBasic": {"disclosureIndex": index, "publishDate": stamp,
            "companyTitle": "ÖRNEK A.Ş.", "stockCode": "ORNEK", "title": "Yeni iş ilişkisi",
            "summary": "Yeni sözleşme imzalandı.", "isBlocked": blocked}, "disclosureDetail": {}}


def records(*indices, stream="members"):
    return parse_records([item(index) for index in indices], stream, START, END)


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.folder.name), telegram_enabled=True,
                             telegram_token="123456:" + "x" * 35,
                             telegram_chat_id=DEST["telegram"])
        self.store = Store(self.config.data_dir)

    def tearDown(self):
        self.folder.cleanup()

    def baseline(self, stream="members", now=NOW):
        self.store.ingest(stream, records(10, stream=stream), END, DEST, 180, now)

    def test_first_batch_is_archived_without_historical_message_flood(self):
        added, queued, baseline = self.store.ingest("members", records(10, 11), END, DEST, 180, NOW)
        self.assertEqual((added, queued, baseline), (2, 0, True))
        self.assertIsNone(self.store.due_delivery(DEST, NOW))

    def test_overlap_and_process_restart_do_not_requeue_same_id(self):
        self.baseline()
        self.store.ingest("members", records(10, 11), END, DEST, 180, NOW + 1)
        restarted = Store(self.config.data_dir)
        restarted.ingest("members", records(10, 11), END, DEST, 180, NOW + 2)
        row = restarted.due_delivery(DEST, NOW + 2)
        self.assertEqual(row["message_key"], "kap:11")
        restarted.delivered(row, 1.2, NOW + 2)
        self.assertIsNone(restarted.due_delivery(DEST, NOW + 4))

    def test_lower_id_seen_later_is_not_lost_to_max_id_cursor(self):
        self.store.ingest("members", records(100), END, DEST, 180, NOW)
        self.store.ingest("members", records(100, 99), END, DEST, 180, NOW + 1)
        self.assertEqual(self.store.due_delivery(DEST, NOW + 1)["message_key"], "kap:99")

    def test_capture_only_does_not_claim_messages_delivered(self):
        config = replace(self.config, telegram_enabled=False)
        self.store.ingest("members", records(10), END, {}, 180, NOW)
        self.store.ingest("members", records(10, 11), END, {}, 180, NOW + 1)
        status = self.store.status(config, NOW + 2)
        self.assertEqual(status["mode"], "capture_only")
        self.assertEqual(status["outbox"], {})
        self.assertEqual(status["records"]["members"], 2)

    def test_source_failure_keeps_cursor_and_other_source_can_progress(self):
        self.baseline()
        self.store.source_failed("members", SourceError("http_503"), 180, DEST, NOW + 1)
        self.store.ingest("funds", records(20, stream="funds"), END, DEST, 180, NOW + 2)
        self.assertEqual(self.store.source_state("members")["last_success_at"], NOW)
        self.assertEqual(self.store.source_state("funds")["last_success_at"], NOW + 2)

    def test_access_restriction_cooldown_survives_restart(self):
        self.store.source_failed("funds", SourceError("http_403", 3600), 180, {}, NOW)
        source = type("Fake", (), {"fetch": lambda *args: self.fail("Cooldown ignored")})()
        app = App(self.config, source)
        self.assertIsNone(app.collect_stream("funds", force=True, now=NOW + 60))

    def test_outage_catchup_advances_in_bounded_overlapping_windows(self):
        state = {"covered_through": "2026-09-01"}
        windows = []
        now = datetime.fromtimestamp(NOW, ISTANBUL)
        for _ in range(20):
            start, end = query_window(state, now)
            windows.append((start, end))
            self.assertLessEqual((end - start).days, 6)
            state["covered_through"] = end.isoformat()
            if end == END:
                break
        self.assertEqual(windows[0][0], date(2026, 8, 31))
        self.assertEqual(windows[-1][1], END)
        for previous, following in zip(windows, windows[1:]):
            self.assertEqual(following[0], previous[1] - timedelta(days=1))

    def test_midnight_query_contains_previous_day(self):
        now = datetime(2026, 10, 12, 0, 1, tzinfo=ISTANBUL)
        start, end = query_window({"covered_through": "2026-10-11"}, now)
        self.assertLessEqual(start, date(2026, 10, 11))
        self.assertEqual(end, date(2026, 10, 12))

    def test_invalid_batch_is_rejected_and_cursor_not_advanced(self):
        self.baseline()
        bad = item(12)
        del bad["disclosureBasic"]["publishDate"]
        with self.assertRaises(SourceError):
            parse_records([item(11), bad], "members", START, END)
        self.assertEqual(self.store.source_state("members")["last_success_at"], NOW)
        self.assertEqual(self.store.status(self.config, NOW)["records"]["members"], 1)

    def test_query_result_is_not_silently_limited_to_250(self):
        parsed = parse_records([item(i) for i in range(1, 401)], "funds", START, END)
        self.assertEqual(len(parsed), 400)

    def test_blocked_disclosure_is_not_sent_and_pending_is_cancelled(self):
        self.baseline()
        self.store.ingest("members", records(10, 11), END, DEST, 180, NOW + 1)
        blocked = parse_records([item(11, blocked=True)], "members", START, END)
        self.store.ingest("members", blocked, END, DEST, 180, NOW + 2)
        self.assertIsNone(self.store.due_delivery(DEST, NOW + 2))
        self.assertEqual(self.store.status(self.config, NOW + 2)["outbox"]["cancelled"], 1)

    def test_updated_pending_body_uses_latest_summary(self):
        self.baseline()
        self.store.ingest("members", records(11), END, DEST, 180, NOW + 1)
        updated = item(11)
        updated["disclosureBasic"]["summary"] = "Düzeltilmiş açıklama"
        self.store.ingest("members", parse_records([updated], "members", START, END), END, DEST, 180, NOW + 2)
        self.assertIn("Düzeltilmiş açıklama", self.store.due_delivery(DEST, NOW + 2)["body"])

    def test_delivery_retry_and_channel_throttle_survive_restart(self):
        self.baseline()
        self.store.ingest("members", records(11, 12), END, DEST, 180, NOW + 1)
        row = self.store.due_delivery(DEST, NOW + 1)
        self.store.delivery_failed(row, DeliveryError("telegram_http_429", 90), NOW + 1)
        restarted = Store(self.config.data_dir)
        self.assertIsNone(restarted.due_delivery(DEST, NOW + 30))
        retry = restarted.due_delivery(DEST, NOW + 92)
        self.assertEqual(retry["id"], row["id"])
        restarted.delivered(retry, 1.2, NOW + 92)
        self.assertIsNone(restarted.due_delivery(DEST, NOW + 93))
        self.assertIsNotNone(restarted.due_delivery(DEST, NOW + 94))

    def test_worker_does_not_mark_failed_message_sent(self):
        self.baseline()
        self.store.ingest("members", records(11), END, DEST, 180, NOW + 1)
        class FailedSender:
            def send(self, destination, body):
                raise DeliveryError("telegram_transport_error")
        app = App(self.config)
        self.assertTrue(app.deliver_one({"telegram": FailedSender()}, NOW + 2))
        self.assertEqual(self.store.status(self.config, NOW + 2)["outbox"], {"pending": 1})

    def test_outage_alert_is_deduplicated_and_recovery_emitted(self):
        self.baseline()
        for attempt in range(5):
            self.store.source_failed("members", SourceError("http_503"), 180, DEST, NOW + attempt)
        self.assertEqual(self.store.status(self.config, NOW + 5)["outbox"], {"pending": 1})
        self.store.ingest("members", records(10), END, DEST, 180, NOW + 6)
        self.assertEqual(self.store.status(self.config, NOW + 6)["outbox"], {"pending": 2})

    def test_unicode_message_is_bounded_and_keeps_link(self):
        raw = item()
        for key in ("summary", "companyTitle", "title", "stockCode"):
            raw["disclosureBasic"][key] = "😀" * 10000
        record = parse_records([raw], "members", START, END)[0]
        text = record.message()
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4096)
        self.assertTrue(text.endswith(record.link))

    def test_telegram_429_retry_after_and_credentials_never_leak(self):
        error = urllib.error.HTTPError("https://api.telegram.org/botSECRET/sendMessage", 429,
                                      "error", {}, io.BytesIO(b'{"parameters":{"retry_after":120}}'))
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(DeliveryError) as caught:
                Telegram(self.config).send(DEST["telegram"], "test")
        self.assertEqual(caught.exception.retry_after, 120)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertNotIn(self.config.telegram_token, str(caught.exception))

    def test_telegram_api_false_is_not_a_success(self):
        response = io.BytesIO(b'{"ok":false,"description":"secret body"}')
        with patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(DeliveryError):
                Telegram(self.config).send(DEST["telegram"], "test")

    def test_kap_http_403_has_one_hour_cooldown(self):
        error = urllib.error.HTTPError("https://www.kap.org.tr", 403, "Forbidden", {}, io.BytesIO())
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(SourceError) as caught:
                KapSource().fetch("members", START, END)
        self.assertEqual(caught.exception.retry_after, 3600)

    def test_success_response_uses_real_telegram_message_id(self):
        response = io.BytesIO(b'{"ok":true,"result":{"message_id":42}}')
        with patch("urllib.request.urlopen", return_value=response):
            self.assertEqual(Telegram(self.config).send(DEST["telegram"], "test"), 42)

    def test_retry_after_http_date_and_large_delay_are_respected(self):
        now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(retry_after_seconds("Sun, 11 Oct 2026 12:01:00 GMT", now), 61)
        self.assertEqual(retry_after_seconds("100000"), 100000)

    def test_retention_preserves_pending_and_backup_is_consistent(self):
        self.baseline(now=NOW - 100 * 86400)
        self.store.ingest("members", records(11), END, DEST, 180, NOW - 100 * 86400 + 1)
        self.store.maintain(90, NOW)
        self.assertEqual(self.store.status(self.config, NOW)["records"]["members"], 1)
        destination = Path(self.folder.name) / "backup.sqlite3"
        self.store.backup(destination)
        db = sqlite3.connect(destination)
        try:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()[0], 1)
        finally:
            db.close()

    def test_bad_delivery_config_fails_before_live_sending(self):
        with self.assertRaises(ValueError):
            replace(self.config, telegram_token="").validate_delivery()
        with self.assertRaises(ValueError):
            replace(self.config, telegram_chat_id="@public").validate_delivery()


if __name__ == "__main__":
    unittest.main()
