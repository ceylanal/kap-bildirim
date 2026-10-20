from dataclasses import replace
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from kapwatch.app import App
from kapwatch.config import Config
from kapwatch.source import ISTANBUL, KapSource, SourceError, parse_records
from kapwatch.store import Store


NOW = datetime(2026, 10, 12, 10, tzinfo=ISTANBUL).timestamp()
TODAY = datetime.fromtimestamp(NOW, ISTANBUL).date()
DEST = {'telegram': '-1001234567890', 'email': 'test@example.invalid'}
RULES = Path(__file__).resolve().parents[1] / 'config/smartmail_rules.json'


def record(index=11, critical=False, published=NOW-12):
    stamp = datetime.fromtimestamp(published, ISTANBUL).strftime('%d.%m.%Y %H:%M:%S')
    return parse_records([{'disclosureBasic': {
        'disclosureIndex': index, 'publishDate': stamp, 'companyTitle': 'ÖRNEK A.Ş.',
        'stockCode': 'ORNEK', 'title': 'Özel Durum Açıklaması (Genel)',
        'summary': 'Şirketimiz konkordato talebinde bulunmuştur.' if critical else 'Şirket adresi değişmiştir.',
        'isBlocked': False}}], 'members', TODAY, TODAY)[0]


class FastPollTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.temp.name), poll_seconds=15,
            telegram_enabled=True, telegram_chat_id=DEST['telegram'],
            email_enabled=True, email_to=DEST['email'], smartmail_enabled=True,
            smartmail_mode='p0', smartmail_rules_file=str(RULES))
        self.app = App(self.config)
        self.store = self.app.store

    def tearDown(self):
        self.temp.cleanup()

    def baseline(self, covered=TODAY, reconciled_at=NOW-100):
        self.store.ingest('members', [], covered, DEST, 15, reconciled_at,
                          reconciled=True)

    def test_fast_interval_is_allowed_but_sub_ten_second_polling_is_rejected(self):
        with patch.dict(os.environ, {'POLL_SECONDS': '15'}, clear=True):
            self.assertEqual(Config.from_env().poll_seconds, 15)
        with patch.dict(os.environ, {'POLL_SECONDS': '9'}, clear=True):
            with self.assertRaises(ValueError):
                Config.from_env()

    def test_hot_query_only_fetches_today_and_waits_for_next_poll(self):
        self.baseline()
        calls = []
        self.app.source = type('Source', (), {'fetch': lambda _, s, a, b: calls.append((s,a,b)) or []})()
        self.assertTrue(self.app.collect_stream('members', now=NOW))
        self.assertEqual(calls, [('members', TODAY, TODAY)])
        self.assertIsNone(self.app.collect_stream('members', now=NOW+14))
        self.assertEqual(len(calls), 1)
        self.assertTrue(self.app.collect_stream('members', now=NOW+15))

    def test_periodic_overlap_preserves_previous_day_reconciliation(self):
        self.baseline(reconciled_at=NOW-181)
        calls = []
        self.app.source = type('Source', (), {'fetch': lambda _, s, a, b: calls.append((a,b)) or []})()
        self.app.collect_stream('members', now=NOW)
        self.assertEqual(calls, [(TODAY-timedelta(days=1), TODAY)])
        self.assertEqual(self.store.source_state('members')['last_reconcile_at'], NOW)

    def test_midnight_rollover_runs_overlap_before_hot_polling(self):
        self.baseline(covered=TODAY-timedelta(days=1), reconciled_at=NOW-16)
        calls = []
        self.app.source = type('Source', (), {'fetch': lambda _, s, a, b: calls.append((a,b)) or []})()
        self.app.collect_stream('members', now=NOW)
        self.assertEqual(calls, [(TODAY-timedelta(days=2), TODAY)])
        self.assertEqual(self.store.source_state('members')['covered_through'], TODAY.isoformat())

    def test_success_from_inflight_request_does_not_erase_access_cooldown(self):
        self.baseline()
        self.store.source_failed('members', SourceError('http_429', 90), 15, {}, NOW)
        self.store.ingest('members', [], TODAY, DEST, 15, NOW+1)
        state = self.store.source_state('members')
        self.assertEqual(state['next_poll_at'], NOW+90)
        self.assertEqual(state['last_error'], 'http_429')
        self.app.source = type('Source', (), {'fetch': lambda *_: self.fail('Cooldown ignored')})()
        self.assertIsNone(self.app.collect_stream('members', force=True, now=NOW+20))

    def test_slow_fund_request_does_not_block_company_collection(self):
        fund_started, release_fund, members_seen = (threading.Event() for _ in range(3))
        class Source:
            def fetch(_, stream, start, end):
                if stream == 'funds':
                    fund_started.set()
                    release_fund.wait(3)
                else:
                    members_seen.set()
                return []
        app = App(replace(self.config, smartmail_enabled=False), Source())
        funds = threading.Thread(target=app.collection_worker, args=('funds',))
        members = threading.Thread(target=app.collection_worker, args=('members',))
        funds.start()
        try:
            self.assertTrue(fund_started.wait(1))
            members.start()
            self.assertTrue(members_seen.wait(1))
            self.assertFalse(release_fund.is_set())
        finally:
            app.stop.set()
            release_fund.set()
            funds.join(2)
            if members.ident is not None:
                members.join(2)
        self.assertFalse(funds.is_alive())
        self.assertFalse(members.is_alive())

    def test_inflight_transport_failure_keeps_a_shared_access_cooldown(self):
        self.baseline()
        self.store.source_failed('members', SourceError('http_403', 3600), 15, {}, NOW)
        self.store.source_failed('members', SourceError('transport_error'), 15, {}, NOW+1)
        state = self.store.source_state('members')
        self.assertEqual(state['last_error'], 'http_403')
        self.assertEqual(state['next_poll_at'], NOW+3600)

    def test_p0_overtakes_routine_telegram_and_email_is_independently_due(self):
        self.baseline()
        self.store.ingest('members', [record(index=10)], TODAY, DEST, 15, NOW-4, self.app.rule_engine, 'p0')
        self.store.ingest('members', [record(critical=True)], TODAY, DEST, 15, NOW-3, self.app.rule_engine, 'p0')
        telegram = self.store.due_delivery({'telegram': DEST['telegram']}, NOW)
        email = self.store.due_delivery({'email': DEST['email']}, NOW)
        self.assertEqual((telegram['message_key'],telegram['priority']), ('kap:11',0))
        self.assertEqual(email['priority'], 0)
        self.assertIn('konkordato', email['body'])

    def test_latency_uses_event_snapshot_and_excludes_service_messages(self):
        self.baseline()
        self.store.ingest('members', [record()], TODAY, DEST, 15, NOW-5, self.app.rule_engine, 'p0')
        row = self.store.due_delivery({'telegram': DEST['telegram']}, NOW)
        self.store.delivered(row, 1.2, NOW)
        with self.store.connection() as db:
            # A later disclosure correction must not change the first send's timing.
            db.execute('UPDATE disclosures SET published_at=?', (datetime.fromtimestamp(NOW, ISTANBUL).isoformat(),))
            self.store.enqueue(db, 'health:members:down:test', 'test', {'telegram':DEST['telegram']}, NOW)
        health = self.store.due_delivery({'telegram':DEST['telegram']}, NOW+2)
        self.store.delivered(health, 1.2, NOW+2)
        metrics = self.store.delivery_latency(NOW+2)['telegram']
        self.assertEqual(metrics['sample_count'], 1)
        self.assertEqual(metrics['p95_seconds'], 12)
        self.assertEqual(metrics['latest'], {'published_to_detected_seconds':7,
            'queued_to_provider_seconds':5, 'published_to_provider_seconds':12})
        self.assertIsNone(self.store.delivery_latency(NOW+2)['email']['p95_seconds'])

    def test_list_timeout_is_separate_from_full_text_timeout(self):
        source = KapSource(timeout=25, list_timeout=8)
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'[]')) as request:
            source.fetch('members', TODAY, TODAY)
            self.assertEqual(request.call_args.kwargs['timeout'], 8)
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'page')) as request:
            with patch('kapwatch.detail.parse_detail', return_value={}):
                source.fetch_detail(11)
            self.assertEqual(request.call_args.kwargs['timeout'], 25)

    def test_collector_crash_is_visible_and_requests_service_restart(self):
        class Source:
            def fetch(_, *args):
                raise RuntimeError('must not appear in logs')
        self.app.source = Source()
        with self.assertLogs('kapwatch', level='ERROR') as logs:
            self.app.collection_worker('members')
        self.assertTrue(self.app.stop.is_set())
        self.assertEqual(self.app.worker_error, 'RuntimeError')
        self.assertNotIn('must not appear', ''.join(logs.output))


if __name__ == '__main__':
    unittest.main()
