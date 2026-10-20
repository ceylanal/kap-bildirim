from dataclasses import replace
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import io

from kapwatch.app import App
from kapwatch.backtest import replay
from kapwatch.config import Config
from kapwatch.detail import parse_detail
from kapwatch.notify import Email
from kapwatch.smartmail import RuleEngine
from kapwatch.source import KapSource, SourceError
from kapwatch.store import Store
from test_smartmail import ROOT, RULES, END, DEST, record


def page(index=11, body="Şirketimiz konkordato talebinde bulunmuştur.", opinion=None):
    body_html = '<table><tr><td class="content-tr">' + body + '</td><td class="content-en">ENGLISH MUST NOT SCORE</td></tr></table>'
    data = {"disclosure": {"disclosureBasic": {"disclosureIndex": index,
        "companyTitle": "ÖRNEK A.Ş.", "stockCode": "ORNEK", "title": "Özel Durum Açıklaması (Genel)",
        "summary": "", "publishDate": "2026.10.12 10:00:00"},
        "disclosureDetail": {"opinionType": opinion}}, "disclosureBody": ["$33"]}
    # Adjacent T records, with no newline: used by real financial disclosure pages.
    flight = '32:T4,test33:T' + format(len(body_html.encode()), 'x') + ',' + body_html
    flight += '26:["$","component",null,{"sendData":' + json.dumps(data) + '}]\n'
    return '<script>self.__next_f.push(' + json.dumps([1, flight]) + ')</script>'


class DetailShadowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.config = Config(self.folder, smartmail_enabled=True, smartmail_rules_file=str(RULES),
            telegram_enabled=True, telegram_chat_id=DEST["telegram"],
            email_enabled=True, email_to=DEST["email"], smartmail_mode="shadow")
        self.engine = RuleEngine(RULES)
        self.store = Store(self.folder)
        self.store.ingest("members", [record(index=10)], END, DEST, 180, 100, self.engine, "shadow", ("members",))

    def tearDown(self):
        self.temp.cleanup()

    def ingest(self, row=None, mode="shadow", now=101):
        row = row or record("Genel bilgilendirme yapılmıştır.")
        return self.store.ingest("members", [row], END, DEST, 180, now, self.engine, mode, ("members",))

    def test_production_parser_utf8_adjacent_flight_and_hidden_language(self):
        detail = parse_detail(page(), 11)
        self.assertIn("Şirketimiz", detail["body"])
        self.assertNotIn("ENGLISH", detail["body"])
        self.assertFalse(detail["attachments_parsed"])

    def test_id_mismatch_missing_schema_and_truncation_not_silently_accepted(self):
        for text in [page(index=12), '<html>access page</html>', page().replace(':T', ':BAD')]:
            with self.subTest(text=text[:30]):
                with self.assertRaises(SourceError):
                    parse_detail(text, 11)

    def test_official_audit_opinion_is_scanned_without_pdf_or_llm(self):
        detail = parse_detail(page(body="Faaliyet raporu ekte yer almaktadır.", opinion="AC"), 11)
        assessed = self.engine.assess(record(**detail))
        self.assertTrue(any(a.priority == "P0" and a.source_field == "structured_fields:opinionType" for a in assessed))

    def test_shadow_records_would_send_and_never_enqueues_email(self):
        self.ingest(record())
        report = self.store.smartmail_report()
        self.assertEqual(report["decisions"][0]["action"], "would_send")
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM outbox WHERE channel='email'").fetchone()[0], 0)
        self.assertNotIn("email", self.config.destinations)

    def test_full_text_finds_event_missing_from_summary_in_shadow(self):
        self.ingest()
        job = self.store.claim_detail(5, 2000, now=102)
        self.assertTrue(self.store.detail_completed(job, parse_detail(page(), 11), self.engine, DEST, "shadow", 103))
        latest = self.store.smartmail_report()["decisions"][0]
        self.assertEqual((latest["stage"], latest["action"]), ("full_text", "would_send"))
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM outbox WHERE channel='email'").fetchone()[0], 0)

    def test_p0_sends_as_soon_as_detected_before_detail_fetch(self):
        self.ingest(record(summary="Şirketimiz konkordato talebinde bulunmuştur.", body=""), mode="p0")
        mail = self.store.due_delivery({'email':DEST['email']}, 102)
        self.assertEqual((mail["channel"], mail["priority"]), ("email", 0))
        self.assertEqual(self.store.smartmail_report()["detail_jobs"], {"pending": 1})

    def test_p0_full_text_can_enqueue_email(self):
        self.ingest(mode="p0")
        job = self.store.claim_detail(5, 2000, now=102)
        self.store.detail_completed(job, parse_detail(page(), 11), self.engine, DEST, "p0", 103)
        self.assertEqual(self.store.due_delivery({'email':DEST['email']}, 103)["priority"], 0)

    def test_same_event_status_not_resent_when_full_text_adds_evidence(self):
        self.ingest(record(summary="Şirketimiz konkordato talebinde bulunmuştur.", body=""), mode="p0")
        mail = self.store.due_delivery({'email':DEST['email']}, 102)
        self.store.delivered(mail, 1, 102)
        job = self.store.claim_detail(5, 2000, now=103)
        detail = parse_detail(page(body="Şirketimiz konkordato talebinde bulunmuştur ve süreç devam etmektedir."), 11)
        self.store.detail_completed(job, detail, self.engine, DEST, "p0", 104)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM outbox WHERE channel='email'").fetchone()[0], 1)

    def test_detail_does_not_mutate_source_fingerprint_or_requeue_on_next_poll(self):
        source_record = record()
        self.ingest(source_record)
        job = self.store.claim_detail(5, 2000, now=102)
        self.store.detail_completed(job, parse_detail(page(), 11), self.engine, DEST, "shadow", 103)
        self.ingest(source_record, now=104)
        with self.store.connection() as db:
            row = db.execute("SELECT fingerprint FROM disclosures WHERE disclosure_id=11").fetchone()
            self.assertEqual(row[0], source_record.fingerprint)
            self.assertEqual(db.execute("SELECT status FROM smartmail_detail_jobs WHERE disclosure_id=11").fetchone()[0], "done")

    def test_old_detail_cannot_overwrite_new_source_revision(self):
        self.ingest()
        job = self.store.claim_detail(5, 2000, now=102)
        self.ingest(record("Düzeltilmiş açıklama."), now=103)
        self.assertFalse(self.store.detail_completed(job, parse_detail(page(), 11), self.engine, DEST, "shadow", 104))
        self.assertEqual(self.store.smartmail_report()["detail_jobs"], {"pending": 1})

    def test_historical_seed_never_sends_mail_even_in_p0_mode(self):
        self.assertEqual(self.store.seed_details(1, now=101), 1)
        job = self.store.claim_detail(5, 2000, now=102)
        self.store.detail_completed(job, parse_detail(page(index=10), 10), self.engine, DEST, "p0", 103)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM outbox").fetchone()[0], 0)
        self.assertEqual(self.store.smartmail_report()["decisions"][0]["action"], "historical_no_send")

    def test_shadow_backlog_never_becomes_old_email_on_mode_activation(self):
        self.store.configure_smartmail_mode("shadow", 100)
        self.ingest()
        self.store.configure_smartmail_mode("p0", 102)
        job = self.store.claim_detail(5, 2000, now=103)
        self.assertEqual(job["allow_alerts"], 0)
        self.store.detail_completed(job, parse_detail(page(), 11), self.engine, DEST, "p0", 104)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM outbox WHERE channel='email'").fetchone()[0], 0)

    def test_quota_spacing_and_restart_preserve_bounds(self):
        self.ingest()
        self.ingest(record(index=12), now=102)
        first = self.store.claim_detail(5, 1, now=103)
        self.store.detail_completed(first, parse_detail(page(), 11), self.engine, DEST, "shadow", 104)
        restarted = Store(self.folder)
        self.assertIsNone(restarted.claim_detail(5, 1, now=109))
        self.assertIsNotNone(restarted.claim_detail(5, 1, now=103 + 86400))

    def test_expired_fetch_lease_is_recoverable_after_restart(self):
        self.ingest()
        self.store.claim_detail(5, 2000, timeout=5, now=102)
        self.assertIsNone(Store(self.folder).claim_detail(5, 2000, timeout=5, now=110))
        self.assertIsNotNone(Store(self.folder).claim_detail(5, 2000, timeout=5, now=138))

    def test_access_restriction_stops_all_detail_and_list_requests(self):
        self.ingest()
        self.ingest(record(index=12), now=102)
        job = self.store.claim_detail(5, 2000, now=103)
        self.store.detail_failed(job, SourceError("detail_http_403", 3600), now=104)
        self.assertIsNone(Store(self.folder).claim_detail(5, 2000, now=120))
        self.assertGreaterEqual(self.store.source_state("members")["next_poll_at"], 3704)

    def test_detail_429_retry_after_also_pauses_list_requests(self):
        self.ingest()
        job = self.store.claim_detail(5, 2000, now=102)
        self.store.detail_failed(job, SourceError("detail_http_429", 90), now=103)
        class Source:
            def fetch(self, *args): raise AssertionError("Retry-After ignored")
        self.assertIsNone(App(self.config, Source()).collect_stream("funds", force=True, now=150))
        self.assertGreaterEqual(self.store.source_state("funds")["next_poll_at"], 193)

    def test_blocked_detail_cancels_already_queued_pending_flash(self):
        self.ingest(record(), mode="p0")
        job = self.store.claim_detail(5, 2000, now=102)
        self.store.detail_failed(job, SourceError("detail_source_blocked", 3600), now=103)
        self.assertIsNone(self.store.due_delivery(DEST, 104))
        self.assertEqual(self.store.smartmail_report()["detail_jobs"], {"cancelled":1})

    def test_old_blocked_result_cannot_cancel_a_new_revision_flash(self):
        self.ingest(record(), mode="p0")
        old = self.store.claim_detail(5, 2000, now=102)
        self.ingest(record("Şirketimize kesin mühlet verilmesine karar verilmiştir."), mode="p0", now=103)
        self.store.detail_failed(old, SourceError("detail_source_blocked"), now=104)
        self.assertEqual(self.store.due_delivery({'email':DEST['email']}, 105)["channel"], "email")

    def test_p0_smtp_delivery_includes_evidence_and_uses_tls(self):
        self.ingest(record(), mode="p0")
        config = replace(self.config, smartmail_mode="p0", smtp_host="smtp.example.invalid",
                         smtp_user="test-user", smtp_password="test-password",
                         email_from="sender@example.invalid")
        messages, login, tls = [], [], []
        class SMTP:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def starttls(self, context): tls.append(True)
            def login(self, user, password): login.append((user,password))
            def send_message(self, message):
                messages.append(message)
                return {}
        with patch("smtplib.SMTP", return_value=SMTP()):
            App(config).deliver_one({"email": Email(config)}, now=102)
        self.assertEqual(messages[0]["To"], DEST["email"])
        self.assertIn("Kaynak cümle", messages[0].get_content())
        self.assertIn("https://www.kap.org.tr/tr/Bildirim/11", messages[0].get_content())
        self.assertEqual(tls, [True])
        self.assertEqual(login, [("test-user","test-password")])

    def test_detail_errors_do_not_break_telegram_collection(self):
        self.ingest()
        class Source:
            def fetch_detail(self, index):
                raise SourceError("detail_transport_error")
        app = App(self.config, Source())
        self.assertTrue(app.detail_one(now=102))
        self.assertEqual(self.store.due_delivery({"telegram": DEST["telegram"]}, 103)["channel"], "telegram")

    def test_channel_workers_cannot_consume_other_channel_rows(self):
        self.ingest(record(), mode="p0")
        app = App(replace(self.config, smartmail_mode="p0"))
        sent = []
        class Sender:
            def send(self, destination, body):
                sent.append(destination)
        app.deliver_one({"telegram": Sender()}, now=102)
        self.assertEqual(sent, [DEST["telegram"]])
        self.assertEqual(self.store.due_delivery({"email": DEST["email"]}, 103)["channel"], "email")

    def test_shadow_only_email_config_is_not_a_delivery_destination(self):
        config = replace(self.config, telegram_enabled=False)
        self.assertEqual(config.destinations, {})

    def test_http403_retry_after_is_sanitized(self):
        error = urllib.error.HTTPError("https://www.kap.org.tr", 403, "Denied", {}, io.BytesIO())
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(SourceError) as caught:
                KapSource().fetch_detail(11)
        self.assertEqual(caught.exception.retry_after, 3600)

    def test_added_historical_dataset_uses_structured_audit_opinion(self):
        result = replay(ROOT / "tests/fixtures/smartmail/validation.jsonl", self.engine)
        self.assertEqual(result["metrics_by_dataset"]["historical"]["critical"],
                         {"tp":2,"fp":0,"fn":0,"tn":5,"false_positive_rate":0.0,
                          "false_alarm_share":0.0,"critical_miss_rate":0.0,"precision":1.0,"recall":1.0})


if __name__ == "__main__":
    unittest.main()
