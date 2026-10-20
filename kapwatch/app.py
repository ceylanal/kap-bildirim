from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import signal
import threading
import time

from .notify import DeliveryError, senders
from .source import ISTANBUL, KapSource, SourceError, STREAMS, query_window
from .store import Store
from .smartmail import RuleEngine

LOG = logging.getLogger("kapwatch")


def configure_logging(data_dir):
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(Path(data_dir) / "service.log", maxBytes=5 * 1024 * 1024,
                                  backupCount=2, encoding="utf-8")
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(formatter)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    LOG.setLevel(logging.INFO)
    LOG.handlers = [handler, console]


@contextmanager
def process_lock(data_dir):
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    with open(Path(data_dir) / "process.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Bu veri dizini için bir toplayıcı zaten çalışıyor.") from None
        yield


class App:
    def __init__(self, config, source=None):
        self.config = config
        self.store = Store(config.data_dir)
        self.source = source or KapSource(config.timeout_seconds, config.poll_timeout_seconds)
        self.rule_engine = RuleEngine(config.smartmail_rules_file,
            config.smartmail_financial_context_file or None) if config.smartmail_enabled else None
        self.stop = threading.Event()
        self.worker_error = None
        self.detail_worker_error = None
        if self.rule_engine:
            self.store.configure_smartmail_mode(config.smartmail_mode)
            with self.store.connection() as db:
                db.execute("DELETE FROM smartmail_runtime WHERE name='detail_worker_error'")

    def collect_stream(self, stream, force=False, now=None):
        fixed_time = now is not None
        now = time.time() if now is None else now
        state = self.store.source_state(stream)
        if state["next_poll_at"] > now:
            # A one-shot check also respects explicit server/access cooldowns.
            if not force or state["last_error"]:
                return None
        start, end = query_window(state, datetime.fromtimestamp(now, ISTANBUL), self.config.max_query_days)
        today = datetime.fromtimestamp(now, ISTANBUL).date()
        reconciled = True
        if (not force and self.config.poll_seconds < 60 and state['covered_through'] == today.isoformat()
                and state['last_reconcile_at'] is not None
                and now-state['last_reconcile_at'] < self.config.reconcile_seconds):
            # Frequent requests cover today; periodic overlap catches late records/corrections.
            start = end = today
            reconciled = False
        started = time.monotonic()
        try:
            records = self.source.fetch(stream, start, end)
        except SourceError as error:
            failed_at = now if fixed_time else time.time()
            failures, delay = self.store.source_failed(stream, error, self.config.poll_seconds,
                                                       self.config.destinations, failed_at)
            LOG.warning("source=%s error=%s failures=%s retry_in=%ss", stream, error.code, failures, delay)
            return False
        elapsed = time.monotonic()-started
        completed_at = now if fixed_time else time.time()
        added, queued, baseline = self.store.ingest(stream, records, end, self.config.destinations,
            self.config.poll_seconds, completed_at, self.rule_engine, self.config.smartmail_mode,
            self.config.smartmail_detail_streams if self.rule_engine and self.config.smartmail_details_enabled else (),
            reconciled=reconciled, request_seconds=elapsed)
        LOG.info("source=%s range=%s..%s received=%s added=%s queued=%s baseline=%s request_seconds=%.3f",
                 stream, start, end, len(records), added, queued, baseline, elapsed)
        return True

    def collection_worker(self, stream):
        try:
            while not self.stop.is_set():
                self.collect_stream(stream)
                self.stop.wait(0.5)
        except Exception as error:
            self.worker_error = type(error).__name__
            LOG.error("collection_worker_stopped source=%s error_class=%s", stream, self.worker_error)
            self.stop.set()

    def collect_once(self):
        results = [self.collect_stream(stream, force=True) for stream in STREAMS]
        self.write_health()
        return all(result is True for result in results)

    def write_health(self):
        status = self.store.status(self.config)
        if self.detail_worker_error:
            status["healthy"] = False
            status["problems"].append("smartmail:detail_worker_stopped")
        target = self.config.data_dir / "health.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(target)

    def deliver_one(self, clients, now=None):
        destinations = {k: v for k, v in self.config.destinations.items() if k in clients}
        row = self.store.due_delivery(destinations, now)
        if row is None:
            return False
        try:
            clients[row["channel"]].send(row["destination"], row["body"])
        except DeliveryError as error:
            delay = self.store.delivery_failed(row, error, now)
            LOG.warning("outbox=%s channel=%s error=%s retry_in=%ss", row["id"], row["channel"], error.code, delay)
        else:
            spacing = self.config.telegram_spacing if row["channel"] == "telegram" else 1
            self.store.delivered(row, spacing, now)
            accepted_at = time.time() if now is None else now
            LOG.info("outbox=%s channel=%s delivered=true queued_to_provider_seconds=%.3f",
                     row["id"], row["channel"], accepted_at-row['created_at'])
        return True

    def delivery_worker(self, channel=None):
        try:
            clients = senders(self.config)
            if channel:
                clients = {channel: clients[channel]}
            while not self.stop.is_set():
                self.deliver_one(clients)
                self.stop.wait(0.25)
        except Exception as error:
            # Avoid exception text: transport libraries can embed credential URLs.
            self.worker_error = type(error).__name__
            LOG.error("delivery_worker_stopped error_class=%s", self.worker_error)
            self.stop.set()

    def detail_one(self, now=None):
        if not self.rule_engine or not self.config.smartmail_details_enabled:
            return False
        job = self.store.claim_detail(self.config.smartmail_detail_spacing,
            self.config.smartmail_detail_daily_limit, self.config.timeout_seconds, now)
        if not job:
            return False
        try:
            detail = self.source.fetch_detail(job["disclosure_id"])
        except SourceError as error:
            delay = self.store.detail_failed(job, error, now)
            LOG.warning("detail=%s error=%s retry_in=%ss", job["disclosure_id"], error.code, delay)
        else:
            accepted = self.store.detail_completed(job, detail, self.rule_engine,
                self.config.destinations, self.config.smartmail_mode, now)
            LOG.info("detail=%s accepted=%s body_chars=%s", job["disclosure_id"], accepted, len(detail.get("body", "")))
        return True

    def detail_worker(self):
        try:
            while not self.stop.is_set():
                self.detail_one()
                self.stop.wait(0.5)
        except Exception as error:
            self.detail_worker_error = type(error).__name__
            with self.store.connection() as db:
                db.execute("INSERT INTO smartmail_runtime VALUES('detail_worker_error',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (self.detail_worker_error,))
            LOG.error("detail_worker_stopped error_class=%s", self.detail_worker_error)
            # Telegram collection and delivery remain running; health reports the failure.

    def run(self):
        self.config.validate_delivery()
        LOG.info("started mode=%s poll_seconds=%s", "delivery" if self.config.destinations else "capture_only", self.config.poll_seconds)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda _sig, _frame: self.stop.set())
        workers = [threading.Thread(target=self.delivery_worker, args=(channel,),
                    name="delivery-" + channel, daemon=True) for channel in self.config.destinations]
        for worker in workers:
            worker.start()
        collectors = [threading.Thread(target=self.collection_worker, args=(stream,),
                      name="collection-"+stream, daemon=True) for stream in STREAMS]
        for collector in collectors:
            collector.start()
        detail_worker = None
        if self.rule_engine and self.config.smartmail_details_enabled:
            detail_worker = threading.Thread(target=self.detail_worker, name="detail", daemon=True)
            detail_worker.start()
        last_maintenance = 0
        last_health = 0
        try:
            while not self.stop.is_set():
                now = time.time()
                if now - last_maintenance > 86400:
                    self.store.maintain(self.config.retention_days, now)
                    last_maintenance = now
                if now - last_health > 10:
                    self.write_health()
                    last_health = now
                self.stop.wait(1)
        finally:
            self.stop.set()
            for worker in workers:
                worker.join(timeout=40)
            for collector in collectors:
                collector.join(timeout=self.config.poll_timeout_seconds+5)
            if detail_worker:
                detail_worker.join(timeout=self.config.timeout_seconds + 5)
            self.write_health()
            LOG.info("stopped")
        if self.worker_error or any(worker.is_alive() for worker in workers+collectors):
            raise RuntimeError("Arka plan işçisi beklenmedik biçimde durdu.")
