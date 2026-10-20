from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import time
import json
import hashlib
import math

from .source import ISTANBUL, STREAMS
from .source import Disclosure
from .smartmail import PRIORITIES, flash_body


class Store:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "kap.sqlite3"
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS disclosures (
                    disclosure_id INTEGER PRIMARY KEY, stream TEXT NOT NULL,
                    published_at TEXT NOT NULL, company TEXT NOT NULL, code TEXT NOT NULL,
                    title TEXT NOT NULL, summary TEXT NOT NULL, blocked INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL, raw_json TEXT NOT NULL,
                    first_seen_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS disclosures_publish ON disclosures(published_at);
                CREATE TABLE IF NOT EXISTS sources (
                    name TEXT PRIMARY KEY, bootstrapped INTEGER NOT NULL DEFAULT 0,
                    covered_through TEXT, last_success_at REAL,
                    failures INTEGER NOT NULL DEFAULT 0, next_poll_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT, incident_id TEXT
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, message_key TEXT NOT NULL,
                    channel TEXT NOT NULL, destination TEXT NOT NULL, body TEXT NOT NULL,
                    created_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0,
                    delivered_at REAL, last_error TEXT,
                    UNIQUE(message_key, channel, destination)
                );
                CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(status, next_attempt_at, id);
                CREATE TABLE IF NOT EXISTS channel_state (
                    channel TEXT NOT NULL, destination TEXT NOT NULL,
                    next_send_at REAL NOT NULL DEFAULT 0, last_error TEXT,
                    PRIMARY KEY(channel, destination)
                );
                CREATE TABLE IF NOT EXISTS smartmail_assessments (
                    disclosure_id INTEGER NOT NULL, revision TEXT NOT NULL,
                    rules_revision TEXT NOT NULL, result_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(disclosure_id, revision, rules_revision)
                );
                CREATE TABLE IF NOT EXISTS smartmail_llm_jobs (
                    disclosure_id INTEGER NOT NULL, revision TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL,
                    PRIMARY KEY(disclosure_id, revision)
                );
                CREATE TABLE IF NOT EXISTS smartmail_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, disclosure_id INTEGER NOT NULL,
                    revision TEXT NOT NULL, rules_revision TEXT NOT NULL, stage TEXT NOT NULL,
                    mode TEXT NOT NULL, priority INTEGER NOT NULL, action TEXT NOT NULL,
                    body TEXT NOT NULL, created_at REAL NOT NULL,
                    UNIQUE(disclosure_id, revision, rules_revision, stage, mode)
                );
                CREATE TABLE IF NOT EXISTS smartmail_detail_jobs (
                    disclosure_id INTEGER PRIMARY KEY, base_revision TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, fetched_at REAL, detail_json TEXT,
                    last_error TEXT, allow_alerts INTEGER NOT NULL DEFAULT 1,
                    priority INTEGER NOT NULL DEFAULT 3
                );
                CREATE TABLE IF NOT EXISTS smartmail_runtime (
                    name TEXT PRIMARY KEY, value TEXT NOT NULL
                );
            """)
            if "priority" not in {row["name"] for row in db.execute("PRAGMA table_info(outbox)")}:
                db.execute("ALTER TABLE outbox ADD COLUMN priority INTEGER NOT NULL DEFAULT 3")
            outbox_columns = {row['name'] for row in db.execute('PRAGMA table_info(outbox)')}
            for name, kind in (('source_published_at', 'TEXT'), ('detected_at', 'REAL')):
                if name not in outbox_columns:
                    db.execute('ALTER TABLE outbox ADD COLUMN '+name+' '+kind)
            if "priority" not in {row["name"] for row in db.execute("PRAGMA table_info(smartmail_detail_jobs)")}:
                db.execute("ALTER TABLE smartmail_detail_jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 3")
            source_columns = {row["name"] for row in db.execute("PRAGMA table_info(sources)")}
            for name in ("last_reconcile_at", "request_seconds"):
                if name not in source_columns:
                    db.execute("ALTER TABLE sources ADD COLUMN " + name + " REAL")
            db.execute("CREATE INDEX IF NOT EXISTS outbox_delivery_metrics ON outbox(channel,status,delivered_at)")
            for stream in STREAMS:
                db.execute("INSERT OR IGNORE INTO sources(name) VALUES(?)", (stream,))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def source_state(self, stream):
        with self.connection() as db:
            return dict(db.execute("SELECT * FROM sources WHERE name=?", (stream,)).fetchone())

    def configure_smartmail_mode(self, mode, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            previous = db.execute("SELECT value FROM smartmail_runtime WHERE name='operating_mode'").fetchone()
            if previous and previous[0] == "shadow" and mode != "shadow":
                # Work accumulated during shadow evaluation never becomes old crisis mail.
                db.execute("UPDATE smartmail_detail_jobs SET allow_alerts=0 WHERE created_at<=?", (now,))
            if mode == "shadow":
                db.execute("UPDATE outbox SET status='cancelled',last_error='shadow_mode' WHERE channel='email' AND message_key LIKE 'kap:%:smartmail:%' AND status='pending'")
            db.execute("INSERT INTO smartmail_runtime VALUES('operating_mode',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (mode,))

    @staticmethod
    def enqueue(db, key, body, destinations, now, priority=3, published_at=None, detected_at=None):
        inserted = 0
        for channel, destination in destinations.items():
            cursor = db.execute("""INSERT OR IGNORE INTO outbox
                (message_key, channel, destination, body, created_at, priority, source_published_at, detected_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (key, channel, destination, body, now, priority, published_at, detected_at))
            inserted += cursor.rowcount
        return inserted

    def assess_smartmail(self, db, record, engine, destinations, now,
                        allow_alerts, mode="live", stage="summary"):
        assessments = engine.assess(record)
        result_json = json.dumps([a.to_dict() for a in assessments], ensure_ascii=False, sort_keys=True)
        db.execute("INSERT OR IGNORE INTO smartmail_assessments VALUES(?, ?, ?, ?, ?)",
                   (record.index, record.fingerprint, engine.revision, result_json, now))
        flash = [a for a in assessments if a.action == "flash" and (mode != "p0" or a.priority == "P0")]
        priority = min((PRIORITIES[a.priority] for a in flash), default=3)
        body = flash_body(record, flash) if flash else ""
        action = "would_send" if flash else "no_flash"
        if not allow_alerts:
            action = "historical_no_send"
        if record.blocked:
            action = "source_blocked"
        db.execute("INSERT OR IGNORE INTO smartmail_decisions(disclosure_id,revision,rules_revision,stage,mode,priority,action,body,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   (record.index, record.fingerprint, engine.revision, stage, mode, priority, action, body, now))
        queued = 0
        key = "kap:" + str(record.index)
        if allow_alerts and not record.blocked:
            if any(a.priority == "P0" and a.action == "flash" for a in assessments):
                db.execute("UPDATE outbox SET priority=0 WHERE message_key=? AND channel='telegram' AND status='pending'", (key,))
            if flash and mode != "shadow" and "email" in destinations:
                # Evidence changes alone do not resend an already alerted event/status.
                event_revision = hashlib.sha256(json.dumps(
                    [(a.event, a.status, a.materiality) for a in flash],
                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
                flash_key = key + ":smartmail:" + event_revision
                db.execute("UPDATE outbox SET status='cancelled', last_error='superseded' WHERE message_key LIKE ? AND message_key != ? AND status='pending'",
                           (key + ":smartmail:%", flash_key))
                queued += self.enqueue(db, flash_key, body, {"email": destinations["email"]}, now,
                                       priority, record.published_at, now)
                db.execute("UPDATE outbox SET body=?, priority=? WHERE message_key=? AND status='pending'", (body, priority, flash_key))
                revived = db.execute("UPDATE outbox SET status='pending', last_error=NULL, body=?, priority=?, next_attempt_at=0,source_published_at=?,detected_at=? WHERE message_key=? AND status='cancelled' AND last_error='superseded' AND delivered_at IS NULL",
                                     (body, priority, record.published_at, now, flash_key))
                queued += revived.rowcount
            else:
                db.execute("UPDATE outbox SET status='cancelled', last_error='superseded' WHERE message_key LIKE ? AND status='pending'", (key + ":smartmail:%",))
            if any(a.action != "suppressed" and a.priority != "P3" for a in assessments):
                db.execute("INSERT OR IGNORE INTO smartmail_llm_jobs(disclosure_id, revision, created_at) VALUES(?, ?, ?)",
                           (record.index, record.fingerprint, now))
        return queued

    def ingest(self, stream, records, covered_through, destinations, poll_seconds, now=None,
               rule_engine=None, smartmail_mode="live", detail_streams=(),
               reconciled=False, request_seconds=None):
        now = time.time() if now is None else now
        added = 0
        queued = 0
        with self.connection() as db:
            state = dict(db.execute("SELECT * FROM sources WHERE name=?", (stream,)).fetchone())
            cooldown_active = (state['next_poll_at'] > now and
                state['last_error'] in {'http_401','http_403','http_429','http_666'})
            for record in records:
                existing = db.execute("SELECT fingerprint FROM disclosures WHERE disclosure_id=?", (record.index,)).fetchone()
                key = "kap:" + str(record.index)
                changed = existing is None or existing["fingerprint"] != record.fingerprint
                telegram_destinations = {k: v for k, v in destinations.items() if k != "email"} if rule_engine else destinations
                if existing is None:
                    db.execute("""INSERT INTO disclosures VALUES
                        (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
                        record.index, record.stream, record.published_at, record.company,
                        record.code, record.title, record.summary, int(record.blocked),
                        record.fingerprint, record.raw_json, now, now,
                    ))
                    added += 1
                    if state["bootstrapped"] and not record.blocked:
                        queued += self.enqueue(db, key, record.message(), telegram_destinations, now,
                                               published_at=record.published_at, detected_at=now)
                elif existing["fingerprint"] != record.fingerprint:
                    db.execute("""UPDATE disclosures SET published_at=?, company=?, code=?,
                        title=?, summary=?, blocked=?, fingerprint=?, raw_json=?, updated_at=?
                        WHERE disclosure_id=?""", (
                        record.published_at, record.company, record.code, record.title,
                        record.summary, int(record.blocked), record.fingerprint,
                        record.raw_json, now, record.index,
                    ))
                    db.execute("UPDATE outbox SET body=?,priority=3,source_published_at=?,detected_at=? WHERE message_key=? AND status='pending'",
                               (record.message(), record.published_at, now, key))
                if rule_engine and changed:
                    queued += self.assess_smartmail(db, record, rule_engine, destinations, now,
                                                  bool(state["bootstrapped"]), smartmail_mode)
                    if state["bootstrapped"] and stream in detail_streams and not record.blocked:
                        hint = (record.title + " " + record.summary).casefold()
                        detail_priority = 0 if any(term in hint for term in ("konkordato", "iflas", "temerrüt", "mühlet")) else (1 if "finansal" in hint or "faaliyet raporu" in hint or "denetim" in hint else 3)
                        db.execute("""INSERT INTO smartmail_detail_jobs(disclosure_id,base_revision,created_at,priority)
                            VALUES(?,?,?,?) ON CONFLICT(disclosure_id) DO UPDATE SET
                            base_revision=excluded.base_revision,status='pending',attempts=0,
                            next_attempt_at=0,lease_until=0,created_at=excluded.created_at,
                            fetched_at=NULL,detail_json=NULL,last_error=NULL,allow_alerts=1,priority=excluded.priority""",
                                   (record.index, record.fingerprint, now, detail_priority))
                if record.blocked:
                    db.execute("UPDATE outbox SET status='cancelled', last_error='source_blocked' WHERE (message_key=? OR message_key LIKE ?) AND status='pending'", (key, key + ":smartmail:%"))
                    db.execute("UPDATE smartmail_llm_jobs SET status='cancelled' WHERE disclosure_id=? AND status='pending'", (record.index,))
                    db.execute("UPDATE smartmail_detail_jobs SET status='cancelled',last_error='source_blocked' WHERE disclosure_id=?", (record.index,))
            if not cooldown_active and state["failures"] >= 3 and state["incident_id"]:
                self.enqueue(db, "health:" + stream + ":recovered:" + state["incident_id"],
                             "KAP servis bilgisi: " + stream + " veri erişimi yeniden çalışıyor.", destinations, now)
            db.execute("""UPDATE sources SET bootstrapped=1, covered_through=?,
                last_success_at=?, failures=?, next_poll_at=?, last_error=?,
                incident_id=?, last_reconcile_at=CASE WHEN ? THEN ? ELSE last_reconcile_at END,
                request_seconds=? WHERE name=?""",
                       (covered_through.isoformat(), now, state['failures'] if cooldown_active else 0,
                        max(state['next_poll_at'],now+poll_seconds) if cooldown_active else now+poll_seconds,
                        state['last_error'] if cooldown_active else None,
                        state['incident_id'] if cooldown_active else None,
                        reconciled, now, request_seconds, stream))
        return added, queued, not bool(state["bootstrapped"])

    @staticmethod
    def stored_record(row, detail=None):
        raw = json.loads(row["raw_json"])
        if detail:
            raw["smartmail"] = detail
        return Disclosure(row["disclosure_id"], row["stream"], row["published_at"],
                          row["company"], row["code"], row["title"], row["summary"],
                          bool(row["blocked"]), json.dumps(raw, ensure_ascii=False, sort_keys=True))

    def seed_details(self, limit=10, streams=("members",), now=None):
        """Explicit finite historical seed for validation; never alert historical records."""
        now = time.time() if now is None else now
        placeholders = ",".join("?" for _ in streams)
        with self.connection() as db:
            rows = db.execute(f"SELECT disclosure_id,fingerprint FROM disclosures WHERE blocked=0 AND stream IN ({placeholders}) ORDER BY published_at DESC LIMIT ?", (*streams, limit)).fetchall()
            inserted = 0
            for row in rows:
                cursor = db.execute("INSERT OR IGNORE INTO smartmail_detail_jobs(disclosure_id,base_revision,created_at,allow_alerts) VALUES(?,?,?,0)",
                                    (row["disclosure_id"], row["fingerprint"], now))
                inserted += cursor.rowcount
            return inserted

    def claim_detail(self, spacing, daily_limit, timeout=25, now=None):
        now = time.time() if now is None else now
        day = datetime.fromtimestamp(now, ISTANBUL).date().isoformat()
        with self.connection() as db:
            runtime = dict(db.execute("SELECT name,value FROM smartmail_runtime"))
            if float(runtime.get("detail_next_request_at", "0")) > now:
                return None
            if int(runtime.get("detail_count", "0")) >= daily_limit and runtime.get("detail_day") == day:
                return None
            # Respect access/rate cooldowns raised by either list stream too.
            if db.execute("SELECT 1 FROM sources WHERE next_poll_at>? AND (last_error IN ('http_401','http_403','http_429','http_666','invalid_json_or_access_page')) LIMIT 1", (now,)).fetchone():
                return None
            db.execute("UPDATE smartmail_detail_jobs SET status='pending' WHERE status='fetching' AND lease_until<=?", (now,))
            row = db.execute("""SELECT j.* FROM smartmail_detail_jobs j JOIN disclosures d USING(disclosure_id)
                WHERE j.status='pending' AND j.next_attempt_at<=? AND d.blocked=0
                ORDER BY j.allow_alerts DESC,j.priority,j.created_at,j.disclosure_id LIMIT 1""", (now,)).fetchone()
            if not row:
                return None
            db.execute("UPDATE smartmail_detail_jobs SET status='fetching', attempts=attempts+1, lease_until=? WHERE disclosure_id=?", (now + timeout + 30, row["disclosure_id"]))
            count = int(runtime.get("detail_count", "0")) + 1 if runtime.get("detail_day") == day else 1
            for name, value in {"detail_day": day, "detail_count": str(count),
                                "detail_next_request_at": str(now + spacing)}.items():
                db.execute("INSERT INTO smartmail_runtime VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (name, value))
            return dict(row)

    def detail_completed(self, job, detail, engine, destinations, mode="shadow", now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            current = db.execute("SELECT * FROM smartmail_detail_jobs WHERE disclosure_id=?", (job["disclosure_id"],)).fetchone()
            row = db.execute("SELECT * FROM disclosures WHERE disclosure_id=?", (job["disclosure_id"],)).fetchone()
            if not row or row["blocked"] or not current or current["base_revision"] != job["base_revision"] or row["fingerprint"] != job["base_revision"]:
                return False
            db.execute("UPDATE smartmail_detail_jobs SET status='done',detail_json=?,fetched_at=?,last_error=NULL,lease_until=0 WHERE disclosure_id=?",
                       (json.dumps(detail, ensure_ascii=False, sort_keys=True), now, job["disclosure_id"]))
            record = self.stored_record(row, detail)
            self.assess_smartmail(db, record, engine, destinations, now,
                                 bool(job["allow_alerts"]), mode, "full_text")
            return True

    def detail_failed(self, job, error, now=None):
        now = time.time() if now is None else now
        delay = max(min(3600, 30 * 2 ** min(job["attempts"], 7)), error.retry_after or 0)
        with self.connection() as db:
            if error.code == "detail_source_blocked":
                current = db.execute("SELECT base_revision FROM smartmail_detail_jobs WHERE disclosure_id=?", (job["disclosure_id"],)).fetchone()
                if not current or current[0] != job["base_revision"]:
                    return delay
                db.execute("UPDATE smartmail_detail_jobs SET status='cancelled',last_error='source_blocked',lease_until=0 WHERE disclosure_id=? AND base_revision=?", (job["disclosure_id"], job["base_revision"]))
                key = "kap:" + str(job["disclosure_id"])
                db.execute("UPDATE outbox SET status='cancelled',last_error='source_blocked' WHERE (message_key=? OR message_key LIKE ?) AND status='pending'", (key, key + ":smartmail:%"))
                db.execute("UPDATE smartmail_llm_jobs SET status='cancelled' WHERE disclosure_id=? AND status='pending'", (job["disclosure_id"],))
                return delay
            db.execute("UPDATE smartmail_detail_jobs SET status='pending',last_error=?,next_attempt_at=?,lease_until=0 WHERE disclosure_id=? AND base_revision=? AND status='fetching'",
                       (error.code, now + delay, job["disclosure_id"], job["base_revision"]))
            if error.code in {"detail_http_401", "detail_http_403", "detail_http_429", "detail_http_666"}:
                db.execute("INSERT INTO smartmail_runtime VALUES('detail_next_request_at',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (str(now + delay),))
                source_delay = delay if error.code == "detail_http_429" else max(3600, delay)
                db.execute("UPDATE sources SET next_poll_at=MAX(next_poll_at,?),last_error=?", (now + source_delay, error.code.removeprefix("detail_")))
        return delay

    def smartmail_report(self, limit=20):
        with self.connection() as db:
            decisions = [dict(row) for row in db.execute("SELECT disclosure_id,stage,mode,priority,action,body,created_at FROM smartmail_decisions ORDER BY id DESC LIMIT ?", (limit,))]
            details = {row["status"]: row["n"] for row in db.execute("SELECT status,count(*) n FROM smartmail_detail_jobs GROUP BY status")}
        return {"detail_jobs": details, "decisions": decisions}

    def source_failed(self, stream, error, poll_seconds, destinations, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            state = dict(db.execute("SELECT * FROM sources WHERE name=?", (stream,)).fetchone())
            failures = state["failures"] + 1
            delay = max(min(1800, poll_seconds * 2 ** min(failures - 1, 4)), error.retry_after or 0)
            code = error.code
            if (state['next_poll_at'] > now and
                    state['last_error'] in {'http_401','http_403','http_429','http_666'}):
                delay = max(delay, state['next_poll_at']-now)
                code = state['last_error']
            incident = state["incident_id"] or str(int(now * 1000))
            db.execute("""UPDATE sources SET failures=?, next_poll_at=?, last_error=?,
                incident_id=? WHERE name=?""", (failures, now + delay, code, incident, stream))
            if failures >= 3:
                self.enqueue(db, "health:" + stream + ":down:" + incident,
                             "KAP veri erişimi uyarısı: " + stream + " sorgusu arka arkaya başarısız. "
                             "Hata: " + error.code + ". Yeniden denenecek; yeni bildirimlerin takibi gecikebilir.",
                             destinations, now)
        return failures, delay

    def due_delivery(self, destinations, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            candidates = []
            for channel, destination in destinations.items():
                row = db.execute("""SELECT o.* FROM outbox o
                    LEFT JOIN channel_state c ON c.channel=o.channel AND c.destination=o.destination
                    WHERE o.channel=? AND o.destination=? AND o.status='pending'
                    AND o.next_attempt_at<=? AND COALESCE(c.next_send_at, 0)<=?
                    ORDER BY o.priority, o.id LIMIT 1""", (channel, destination, now, now)).fetchone()
                if row:
                    candidates.append(dict(row))
            return min(candidates, key=lambda row: (row["priority"], row["id"])) if candidates else None

    def delivered(self, row, spacing, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            db.execute("UPDATE outbox SET status='sent', delivered_at=?, attempts=attempts+1, last_error=NULL WHERE id=? AND status='pending'", (now, row["id"]))
            db.execute("""INSERT INTO channel_state VALUES (?, ?, ?, NULL)
                ON CONFLICT(channel, destination) DO UPDATE SET next_send_at=excluded.next_send_at, last_error=NULL""",
                       (row["channel"], row["destination"], now + spacing))

    def delivery_failed(self, row, error, now=None):
        now = time.time() if now is None else now
        attempts = row["attempts"] + 1
        delay = max(min(1800, 10 * 2 ** min(attempts - 1, 8)), error.retry_after or 0)
        with self.connection() as db:
            db.execute("UPDATE outbox SET attempts=?, next_attempt_at=?, last_error=? WHERE id=? AND status='pending'", (attempts, now + delay, error.code, row["id"]))
            db.execute("""INSERT INTO channel_state VALUES (?, ?, ?, ?)
                ON CONFLICT(channel, destination) DO UPDATE SET next_send_at=excluded.next_send_at, last_error=excluded.last_error""",
                       (row["channel"], row["destination"], now + delay, error.code))
        return delay

    def delivery_latency(self, now=None, limit=100):
        """Actual disclosure timings; provider acceptance is not inbox receipt."""
        now = time.time() if now is None else now
        result = {}
        with self.connection() as db:
            for channel in ("telegram", "email"):
                rows = db.execute("""SELECT o.created_at,o.delivered_at,
                    COALESCE(o.source_published_at,d.published_at) published_at,
                    COALESCE(o.detected_at,d.first_seen_at) detected_at
                    FROM outbox o JOIN disclosures d
                    ON d.disclosure_id=CAST(substr(o.message_key,5) AS INTEGER)
                    WHERE o.channel=? AND o.status='sent' AND o.delivered_at>=?
                    AND o.message_key LIKE 'kap:%'
                    ORDER BY o.delivered_at DESC LIMIT ?""", (channel, now-86400, limit)).fetchall()
                metrics = {"sample_count": len(rows), "window_seconds": 86400,
                           "endpoint": "provider_acceptance", "latest": None}
                values = []
                for row in rows:
                    published = datetime.fromisoformat(row['published_at']).timestamp()
                    if row['delivered_at'] < published:
                        continue
                    total = row['delivered_at'] - published
                    values.append(total)
                    if metrics['latest'] is None:
                        metrics['latest'] = {
                            'published_to_detected_seconds': round(row['detected_at']-published, 3),
                            'queued_to_provider_seconds': round(row['delivered_at']-row['created_at'], 3),
                            'published_to_provider_seconds': round(total, 3)}
                values.sort()
                for name, fraction in (("p50_seconds", 0.5), ("p95_seconds", 0.95)):
                    metrics[name] = round(values[max(0, math.ceil(len(values)*fraction)-1)], 3) if values else None
                metrics['valid_sample_count'] = len(values)
                result[channel] = metrics
        return result

    def status(self, config, now=None):
        now = time.time() if now is None else now
        today = datetime.fromtimestamp(now, ISTANBUL).date().isoformat()
        with self.connection() as db:
            sources = [dict(row) for row in db.execute("SELECT * FROM sources ORDER BY name")]
            counts = {row["stream"]: row["n"] for row in db.execute("SELECT stream, COUNT(*) n FROM disclosures GROUP BY stream")}
            queue = {row["status"]: row["n"] for row in db.execute("SELECT status, COUNT(*) n FROM outbox GROUP BY status")}
            channels = [dict(row) for row in db.execute("SELECT channel, next_send_at, last_error FROM channel_state")]
            oldest = db.execute("SELECT MIN(created_at) t FROM outbox WHERE status='pending'").fetchone()["t"]
            details = {row["status"]: row["n"] for row in db.execute("SELECT status,count(*) n FROM smartmail_detail_jobs GROUP BY status")}
            detail_oldest = db.execute("SELECT MIN(created_at) FROM smartmail_detail_jobs WHERE status IN ('pending','fetching')").fetchone()[0]
            detail_errors = {row["last_error"]: row["n"] for row in db.execute("SELECT last_error,count(*) n FROM smartmail_detail_jobs WHERE status='pending' AND last_error IS NOT NULL GROUP BY last_error")}
            decision_count = db.execute("SELECT count(*) FROM smartmail_decisions").fetchone()[0]
            runtime = dict(db.execute("SELECT name,value FROM smartmail_runtime"))
        problems = []
        for state in sources:
            success = state["last_success_at"]
            if success is None or now - success > max(config.poll_seconds * 3, 600):
                problems.append(state["name"] + ":stale")
            if state["covered_through"] and state["covered_through"] < today:
                problems.append(state["name"] + ":recovering")
            if state["last_error"]:
                problems.append(state["name"] + ":" + state["last_error"])
        for state in channels:
            if state["channel"] in config.destinations and state["last_error"]:
                problems.append(state["channel"] + ":" + state["last_error"])
        if oldest and now - oldest > 1800 and config.destinations:
            problems.append("outbox:delivery_delayed")
        if config.smartmail_enabled and detail_oldest and now - detail_oldest > 1800:
            problems.append("smartmail:detail_backlog")
        if config.smartmail_enabled and runtime.get("detail_worker_error"):
            problems.append("smartmail:detail_worker_stopped")
        if config.smartmail_enabled and detail_errors:
            problems.append("smartmail:detail_errors")
        return {"mode": "delivery" if config.destinations else "capture_only",
                "healthy": not problems, "problems": problems,
                "poll_seconds": config.poll_seconds, "records": counts, "outbox": queue,
                "poll_timeout_seconds": config.poll_timeout_seconds,
                "reconcile_seconds": config.reconcile_seconds,
                "delivery_latency": self.delivery_latency(now),
                "oldest_pending_age_seconds": round(now - oldest) if oldest else None,
                "sources": sources, "channels": channels,
                "smartmail": {"enabled": config.smartmail_enabled, "mode": config.smartmail_mode,
                    "detail_jobs": details, "detail_errors": detail_errors,
                    "oldest_detail_age_seconds": round(now-detail_oldest) if detail_oldest else None,
                    "decisions": decision_count, "detail_requests_day": runtime.get("detail_day"),
                    "detail_requests_count": int(runtime.get("detail_count", "0")),
                    "detail_daily_limit": config.smartmail_detail_daily_limit},
                "checked_at": datetime.fromtimestamp(now, timezone.utc).isoformat()}

    def maintain(self, retention_days, now=None):
        now = time.time() if now is None else now
        cutoff = now - retention_days * 86400
        # Preserve pending messages regardless of age. No daily VACUUM on a busy VPS.
        with self.connection() as db:
            db.execute("DELETE FROM outbox WHERE status!='pending' AND created_at<?", (cutoff,))
            db.execute("""DELETE FROM disclosures WHERE first_seen_at<? AND NOT EXISTS
                (SELECT 1 FROM outbox WHERE (message_key='kap:' || disclosures.disclosure_id OR
                 message_key LIKE 'kap:' || disclosures.disclosure_id || ':smartmail:%') AND status='pending')""", (cutoff,))
            db.execute("DELETE FROM smartmail_assessments WHERE disclosure_id NOT IN (SELECT disclosure_id FROM disclosures)")
            db.execute("DELETE FROM smartmail_llm_jobs WHERE disclosure_id NOT IN (SELECT disclosure_id FROM disclosures)")
            db.execute("DELETE FROM smartmail_decisions WHERE disclosure_id NOT IN (SELECT disclosure_id FROM disclosures)")
            db.execute("DELETE FROM smartmail_detail_jobs WHERE disclosure_id NOT IN (SELECT disclosure_id FROM disclosures)")
        with self.connection() as db:
            db.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def backup(self, destination):
        destination = Path(destination).resolve()
        if destination == self.path.resolve() or destination.exists():
            raise ValueError("Yedek için yeni bir dosya yolu gerekli.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()
        destination.chmod(0o600)

    def previews(self, limit=3):
        with self.connection() as db:
            return [dict(row) for row in db.execute("""SELECT disclosure_id, stream, published_at,
                company, code, title, summary FROM disclosures ORDER BY published_at DESC, disclosure_id DESC LIMIT ?""", (limit,))]
