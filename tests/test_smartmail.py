from dataclasses import replace
from datetime import date
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from kapwatch.app import App
from kapwatch.backtest import metrics, replay
from kapwatch.config import Config
from kapwatch.smartmail import RuleEngine, normalize, parse_amount
from kapwatch.source import Disclosure
from kapwatch.store import Store


ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "config/smartmail_rules.json"
STAMP = "2026-10-12T10:00:00+03:00"
END = date(2026, 10, 12)
DEST = {"telegram": "-1001234567890", "email": "test@example.invalid"}
REVENUE = {"value": "50000000000", "currency": "TRY", "period_start": "2025-01-01",
           "period_end": "2025-12-31", "published_at": "2026-03-01T18:00:00+03:00",
           "consolidation_scope": "consolidated", "source_url": "https://www.kap.org.tr/tr/Bildirim/EXAMPLE",
           "verified": True}


def record(body="Şirketimiz konkordato talebinde bulunmuştur.", index=11, summary="", **fields):
    payload = {"smartmail": {"body": body, **fields}}
    return Disclosure(index, "members", STAMP, "ÖRNEK A.Ş.", "ORNEK",
                      "Özel Durum Açıklaması (Genel)", summary, False,
                      json.dumps(payload, ensure_ascii=False))


class SmartMailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.context = self.folder / "financials.json"
        self.set_revenues([REVENUE])
        self.engine = RuleEngine(RULES, self.context)

    def tearDown(self):
        self.temp.cleanup()

    def set_revenues(self, rows):
        self.context.write_text(json.dumps({"companies": {"ORNEK": rows}}))

    def contract(self, amount="12 milyar TL", **fields):
        fields.setdefault("consolidation_scope", "consolidated")
        return record(f"Şirketimiz {amount} tutarında sözleşme imzalanmıştır.", **fields)

    def test_turkish_normalization_and_inflected_application(self):
        self.assertEqual(normalize("İFLAS ŞİRKETİ GEÇİCİ MÜHLET"), "iflas sirketi gecici muhlet")
        result = self.engine.assess(record("Şirketimiz KONKORDATO TALEBİNDE bulunmuştur."))[0]
        self.assertEqual((result.priority, result.status, result.action), ("P0", "application", "flash"))

    def test_negation_does_not_create_crisis_flash(self):
        result = self.engine.assess(record("Şirketimizin konkordato başvurusu bulunmamaktadır.",
                                            summary="Konkordato başvurusu hakkında"))
        self.assertTrue(result)
        self.assertTrue(all(a.action == "suppressed" for a in result))

    def test_independent_bankruptcy_survives_other_event_denial(self):
        result = self.engine.assess(record("Konkordato başvurusu bulunmamaktadır. Şirketimiz hakkında iflas kararı verilmiştir."))
        self.assertTrue(any(a.event == "insolvency_bankruptcy" and a.action == "flash" for a in result))

    def test_unknown_or_supplier_actor_is_p0_with_review(self):
        for text in ["Konkordato talebi kabul edilmiştir.", "Tedarikçimizin konkordato talebi kabul edilmiştir."]:
            result = self.engine.assess(record(text))[0]
            self.assertEqual(result.action, "flash")
            # Counterparties require review too: no issuer financial implication invented.
            if result.affected_entity_role == "unknown":
                self.assertTrue(result.manual_review)

    def test_planned_maintenance_not_p0(self):
        result = self.engine.assess(record("Şirketimiz planlı bakım nedeniyle üretim faaliyetinin durdurulmasına karar vermiştir."))
        self.assertTrue(result)
        self.assertTrue(all(a.priority != "P0" for a in result))

    def test_independent_unplanned_halt_not_suppressed_by_maintenance(self):
        result = self.engine.assess(record("Şirketimiz planlı bakım yapmaktadır. Yangın nedeniyle üretim faaliyetinin durdurulmasına karar verilmiştir."))
        self.assertTrue(any(a.priority == "P0" and a.action == "flash" for a in result))

    def test_board_nomination_not_audit_disclaimer(self):
        result = self.engine.assess(record("SPK bağımsız yönetim kurulu adayları için görüş bildirmekten kaçınmıştır."))
        self.assertTrue(all(a.action != "flash" for a in result))

    def test_actual_audit_disclaimer_p0(self):
        result = self.engine.assess(record("Finansal tablolara ilişkin görüş bildirmekten kaçınma kararı verilmiştir."))
        self.assertTrue(any(a.event == "audit_adverse_disclaimer" and a.action == "flash" for a in result))

    def test_supplied_yaml_examples(self):
        config = json.loads(RULES.read_text())
        for example in config["examples"]:
            with self.subTest(text=example["text"]):
                result = self.engine.assess(record(example["text"]))
                if example.get("expected_priority") == "P0":
                    self.assertTrue(any(a.event == example["expected_event"] and a.priority == "P0" for a in result))
                elif example.get("expected_status") == "denied":
                    self.assertTrue(any(a.status == "denied" and a.action == "suppressed" for a in result))
                else:
                    self.assertFalse(any(a.action == "flash" for a in result))

    def test_amount_formats_and_ambiguous_amounts(self):
        self.assertEqual(str(parse_amount("12.000.000.000 TL")[0]), "12000000000")
        self.assertEqual(str(parse_amount("1,5 milyar TL")[0]), "1500000000.0")
        self.assertIsNone(parse_amount("12 milyar TL, ayrıca 2 milyar TL"))

    def test_material_contract_escalates_without_llm(self):
        result = self.engine.assess(self.contract())[0]
        self.assertEqual((result.priority, result.action), ("P1", "flash"))
        self.assertEqual(result.materiality["ratio"], "0.24")
        self.assertFalse(result.materiality["legal_threshold"])

    def test_exact_threshold_does_not_escalate(self):
        result = self.engine.assess(self.contract("10 milyar TL"))[0]
        self.assertEqual(result.priority, "P2")
        self.assertFalse(result.materiality["escalated"])

    def test_threshold_is_configurable(self):
        config = json.loads(RULES.read_text())
        config["execution"]["new_business_revenue_ratio_threshold"] = "0.30"
        path = self.folder / "rules.json"
        path.write_text(json.dumps(config))
        result = RuleEngine(path, self.context).assess(self.contract())[0]
        self.assertEqual(result.priority, "P2")

    def test_unknown_scope_no_computed_ratio(self):
        result = self.engine.assess(self.contract(consolidation_scope="unknown"))[0]
        self.assertIsNone(result.materiality["ratio"])
        self.assertTrue(result.manual_review)

    def test_missing_zero_foreign_currency_stale_and_unverified_revenue(self):
        rows = [[], [{**REVENUE, "value": "0"}], [{**REVENUE, "currency": "USD"}],
                [{**REVENUE, "period_start": "2020-01-01", "period_end": "2020-12-31"}],
                [{**REVENUE, "verified": False}], [{**REVENUE, "consolidation_scope": "standalone"}]]
        for revenues in rows:
            with self.subTest(revenues=revenues):
                self.set_revenues(revenues)
                result = RuleEngine(RULES, self.context).assess(self.contract())[0]
                self.assertIsNone(result.materiality["ratio"])
                self.assertEqual(result.priority, "P2")

    def test_backtest_does_not_use_future_financials(self):
        self.set_revenues([{**REVENUE, "published_at": "2026-10-13T10:00:00+03:00"}])
        result = RuleEngine(RULES, self.context).assess(self.contract())[0]
        self.assertIsNone(result.materiality["ratio"])

    def test_latest_known_revenue_revision_used(self):
        self.set_revenues([REVENUE, {**REVENUE, "value": "100000000000", "published_at": "2026-08-01T10:00:00+03:00"}])
        result = RuleEngine(RULES, self.context).assess(self.contract())[0]
        self.assertEqual(result.materiality["ratio"], "0.12")

    def test_vat_inclusive_amount_requires_review(self):
        result = self.engine.assess(self.contract("KDV dahil 12 milyar TL"))[0]
        self.assertIsNone(result.materiality["ratio"])

    def test_customer_contract_not_escalated_as_issuer_revenue(self):
        result = self.engine.assess(self.contract(affected_entity_role="customer"))[0]
        self.assertEqual(result.priority, "P2")
        self.assertFalse(result.materiality["escalated"])
        self.assertTrue(result.manual_review)

    def test_metrics_distinguish_fpr_and_false_alarm_share(self):
        result = metrics([(True, True), (True, False), (False, True), (False, False), (False, False)])
        self.assertAlmostEqual(result["false_positive_rate"], 1/3)
        self.assertEqual(result["false_alarm_share"], 0.5)
        self.assertEqual(result["critical_miss_rate"], 0.5)
        self.assertIsNone(metrics([])["critical_miss_rate"])

    def test_unlabeled_history_not_reported_as_success(self):
        path = self.folder / "unlabeled.jsonl"
        path.write_text(json.dumps({"kind": "historical", "record": {}}))
        with self.assertRaises(ValueError):
            replay(path, self.engine)

    def test_historical_replay_offline_and_separates_coverage(self):
        path = ROOT / "tests/fixtures/smartmail/historical.jsonl"
        with patch("urllib.request.urlopen", side_effect=AssertionError("Backtest called network")):
            full = replay(path, self.engine)
            short = replay(path, self.engine, True)
        self.assertEqual(full["metrics_by_dataset"]["historical"]["critical"]["fn"], 0)
        self.assertEqual(short["metrics_by_dataset"]["historical"]["critical"]["fn"], 3)
        self.assertEqual(full["sample_size"], 8)

    def baseline_store(self):
        store = Store(self.folder / "data")
        store.ingest("members", [record(index=10)], END, DEST, 180, 100, self.engine)
        return store

    def test_p0_prioritized_and_delivered_while_llm_job_pending(self):
        store = self.baseline_store()
        store.ingest("members", [record(index=11)], END, DEST, 180, 101, self.engine)
        config = Config(store.data_dir, telegram_enabled=True, telegram_chat_id=DEST["telegram"],
                        email_enabled=True, email_to=DEST["email"])
        app = App(config)
        sent = []
        class Sender:
            def send(self, destination, body):
                sent.append((destination, body))
        app.deliver_one({"email": Sender()}, 102)
        self.assertEqual(sent[0][0], DEST["email"])
        with store.connection() as db:
            self.assertEqual(db.execute("SELECT status FROM smartmail_llm_jobs WHERE disclosure_id=11").fetchone()[0], "pending")

    def test_routine_not_emailed_and_initial_history_not_flooded(self):
        store = self.baseline_store()
        store.ingest("members", [record("Adres değişikliği bildirilmiştir.")], END, DEST, 180, 101, self.engine)
        with store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox WHERE channel='email'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox WHERE channel='telegram'").fetchone()[0], 1)

    def test_restart_overlap_and_rules_revisions_do_not_duplicate_flash(self):
        store = self.baseline_store()
        store.ingest("members", [record()], END, DEST, 180, 101, self.engine)
        restarted = Store(store.data_dir)
        restarted.ingest("members", [record()], END, DEST, 180, 102, self.engine)
        with store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox WHERE channel='email'").fetchone()[0], 1)

    def test_new_status_same_disclosure_sent_as_update(self):
        store = self.baseline_store()
        store.ingest("members", [record()], END, DEST, 180, 101, self.engine)
        first = store.due_delivery({'email':DEST['email']}, 102)
        store.delivered(first, 1, 102)
        store.ingest("members", [record("Şirketimize kesin mühlet verilmesine karar verilmiştir.")], END, DEST, 180, 104, self.engine)
        second = store.due_delivery({'email':DEST['email']}, 104)
        self.assertEqual(second["channel"], "email")
        self.assertNotEqual(second["message_key"], first["message_key"])

    def test_correction_cancels_pending_crisis_and_can_restore_unsent_event(self):
        store = self.baseline_store()
        store.ingest("members", [record()], END, DEST, 180, 101, self.engine)
        store.ingest("members", [record("Şirketimizin konkordato talebi bulunmamaktadır.")], END, DEST, 180, 102, self.engine)
        with store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox WHERE channel='email' AND status='pending'").fetchone()[0], 0)
        store.ingest("members", [record()], END, DEST, 180, 103, self.engine)
        self.assertEqual(store.due_delivery({'email':DEST['email']}, 103)["channel"], "email")

    def test_blocked_cancels_flash_and_llm_job(self):
        store = self.baseline_store()
        store.ingest("members", [record()], END, DEST, 180, 101, self.engine)
        store.ingest("members", [replace(record(), blocked=True)], END, DEST, 180, 102, self.engine)
        self.assertIsNone(store.due_delivery(DEST, 102))
        with store.connection() as db:
            self.assertEqual(db.execute("SELECT status FROM smartmail_llm_jobs WHERE disclosure_id=11").fetchone()[0], "cancelled")

    def test_pending_flash_retention_and_backup(self):
        store = self.baseline_store()
        store.ingest("members", [record()], END, DEST, 180, 101, self.engine)
        # Deliver the ordinary Telegram record; only flash remains to protect the disclosure.
        with store.connection() as db:
            db.execute("UPDATE outbox SET status='sent' WHERE channel='telegram'")
        store.maintain(90, 200 * 86400)
        with store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM disclosures WHERE disclosure_id=11").fetchone()[0], 1)

    def test_migration_preserves_existing_outbox(self):
        folder = self.folder / "old"
        folder.mkdir()
        con = sqlite3.connect(folder / "kap.sqlite3")
        con.executescript("""CREATE TABLE outbox(id INTEGER PRIMARY KEY, message_key TEXT NOT NULL,
            channel TEXT NOT NULL, destination TEXT NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at REAL NOT NULL DEFAULT 0, delivered_at REAL, last_error TEXT,
            UNIQUE(message_key, channel, destination));
            INSERT INTO outbox(message_key, channel, destination, body, created_at)
            VALUES('old', 'telegram', '-1001234567890', 'existing', 1);""")
        con.close()
        store = Store(folder)
        self.assertEqual(store.due_delivery(DEST, 102)["body"], "existing")


if __name__ == "__main__":
    unittest.main()
