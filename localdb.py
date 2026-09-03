"""Local DB - the hot cache in the middle of the pipeline.

It holds three things, exactly as the architecture describes:

1. Active service-request cases. Rows are inserted or refreshed by the
    incremental sync and deleted when they disappear from a full active query.
2. Per-ruleset notification state, so each ruleset remembers what it has
   already alerted on and a case is never announced twice by the same ruleset.
3. The business and ruleset configuration JSON, cached so a run can start even
   if rulesets.json is momentarily unreachable.

SQLite is used because it is a single file, needs no server, and gives the
sync a transactional swap.
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

DEFAULT_PATH = os.environ.get("LOCAL_DB_PATH", "sla_cache.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id        TEXT PRIMARY KEY,
    case_number    TEXT NOT NULL DEFAULT '',
    business       TEXT NOT NULL DEFAULT '',
    sla_due_utc    TEXT,
    sla_state      TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT '',
    modified_on    TEXT,
    first_seen_utc TEXT NOT NULL,
    last_seen_utc  TEXT NOT NULL,
    payload        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cases_business ON cases (business);
CREATE INDEX IF NOT EXISTS ix_cases_due ON cases (sla_due_utc);

CREATE TABLE IF NOT EXISTS notifications (
    fingerprint TEXT PRIMARY KEY,
    ruleset_id  TEXT NOT NULL DEFAULT '',
    case_number TEXT NOT NULL DEFAULT '',
    sent_utc    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_notifications_ruleset ON notifications (ruleset_id);

CREATE TABLE IF NOT EXISTS destinations (
    conversation_id TEXT PRIMARY KEY,
    service_url     TEXT NOT NULL DEFAULT '',
    team_id         TEXT NOT NULL DEFAULT '',
    channel_id      TEXT NOT NULL DEFAULT '',
    updated_utc     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS configuration (
    name        TEXT PRIMARY KEY,
    document    TEXT NOT NULL,
    updated_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ruleset_id  TEXT NOT NULL DEFAULT '',
    started_utc TEXT NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    eligible    INTEGER NOT NULL DEFAULT 0,
    sent        INTEGER NOT NULL DEFAULT 0,
    skipped     INTEGER NOT NULL DEFAULT 0,
    outcome     TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT ''
);
"""


def _now():
    return datetime.now(timezone.utc).isoformat()


class LocalStore:
    """The hot cache. Safe to share across threads; each call is its own txn."""

    def __init__(self, path=None):
        self.path = path or DEFAULT_PATH
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.executescript(SCHEMA)
            # WAL lets the dashboard read while the sync writes.
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.commit()

    def close(self):
        with self._lock:
            self._connection.close()

    # --- cases ------------------------------------------------------------
    def upsert_cases(self, records, business_for=None):
        """Insert or refresh the pending cases returned by this sync."""
        now = _now()
        rows = []
        for record in records:
            case_id = str(record.get("id") or record.get("case_number") or "")
            if not case_id:
                continue
            business = (business_for(record) if business_for else "") or record.get(
                "business", ""
            )
            rows.append(
                (
                    case_id,
                    str(record.get("case_number") or ""),
                    business,
                    record.get("sla_due_utc"),
                    str(record.get("sla_state") or ""),
                    str(record.get("status") or ""),
                    record.get("modified_on"),
                    now,
                    now,
                    json.dumps(record, default=str),
                )
            )
        if not rows:
            return 0
        with self._lock:
            self._connection.executemany(
                """
                INSERT INTO cases (case_id, case_number, business, sla_due_utc,
                                   sla_state, status, modified_on,
                                   first_seen_utc, last_seen_utc, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                    case_number   = excluded.case_number,
                    business      = excluded.business,
                    sla_due_utc   = excluded.sla_due_utc,
                    sla_state     = excluded.sla_state,
                    status        = excluded.status,
                    modified_on   = excluded.modified_on,
                    last_seen_utc = excluded.last_seen_utc,
                    payload       = excluded.payload
                """,
                rows,
            )
            self._connection.commit()
        return len(rows)

    def purge_completed(self, active_case_ids):
        """Delete cases that are no longer pending - the cache stays hot."""
        with self._lock:
            existing = {
                row["case_id"] for row in self._connection.execute("SELECT case_id FROM cases")
            }
            stale = existing - set(active_case_ids)
            if stale:
                self._connection.executemany(
                    "DELETE FROM cases WHERE case_id = ?", [(i,) for i in stale]
                )
                self._connection.commit()
        return len(stale)

    def pending_cases(self, business=""):
        query = "SELECT payload FROM cases"
        params = ()
        if business:
            query += " WHERE business = ?"
            params = (business,)
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def count_cases(self):
        with self._lock:
            row = self._connection.execute("SELECT COUNT(*) AS n FROM cases").fetchone()
        return int(row["n"])

    # --- notification state (matches the NotificationState interface) -----
    def was_sent(self, fingerprint):
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM notifications WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
        return row is not None

    def mark_sent(self, fingerprint, case_number="", ruleset_id=""):
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO notifications (fingerprint, ruleset_id, case_number, sent_utc)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET sent_utc = excluded.sent_utc
                """,
                (fingerprint, ruleset_id, case_number, _now()),
            )
            self._connection.commit()

    def prune_notifications(self, older_than_days=7):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM notifications WHERE sent_utc < ?", (cutoff,)
            )
            self._connection.commit()
        return cursor.rowcount

    def save_conversation(self, conversation_id, service_url="", team_id="", channel_id=""):
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO destinations (conversation_id, service_url, team_id,
                                          channel_id, updated_utc)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    service_url = excluded.service_url,
                    team_id     = excluded.team_id,
                    channel_id  = excluded.channel_id,
                    updated_utc = excluded.updated_utc
                """,
                (conversation_id, service_url, team_id, channel_id, _now()),
            )
            self._connection.commit()

    def get_destination(self, configured_id=""):
        with self._lock:
            if configured_id:
                rows = self._connection.execute(
                    "SELECT * FROM destinations WHERE conversation_id = ?",
                    (configured_id,),
                ).fetchall()
            else:
                rows = self._connection.execute("SELECT * FROM destinations").fetchall()
        if len(rows) != 1:
            return None
        row = rows[0]
        return {
            "conversation_id": row["conversation_id"],
            "service_url": row["service_url"],
            "team_id": row["team_id"],
            "channel_id": row["channel_id"],
        }

    # --- configuration ----------------------------------------------------
    def save_config(self, name, document):
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO configuration (name, document, updated_utc)
                VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    document    = excluded.document,
                    updated_utc = excluded.updated_utc
                """,
                (name, json.dumps(document), _now()),
            )
            self._connection.commit()

    def load_config(self, name):
        with self._lock:
            row = self._connection.execute(
                "SELECT document FROM configuration WHERE name = ?", (name,)
            ).fetchone()
        return json.loads(row["document"]) if row else None

    # --- sync watermark ---------------------------------------------------
    def get_watermark(self, name="dataverse"):
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM sync_state WHERE name = ?", (name,)
            ).fetchone()
        return row["value"] if row else ""

    def set_watermark(self, value, name="dataverse"):
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO sync_state (name, value) VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET value = excluded.value
                """,
                (name, value),
            )
            self._connection.commit()

    # --- run log ----------------------------------------------------------
    def log_run(self, ruleset_id, started_utc, duration_ms, eligible, sent, skipped,
                outcome, detail=""):
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO run_log (ruleset_id, started_utc, duration_ms, eligible,
                                     sent, skipped, outcome, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ruleset_id, started_utc, int(duration_ms), int(eligible), int(sent),
                 int(skipped), outcome, detail[:2000]),
            )
            self._connection.commit()

    def recent_runs(self, limit=100):
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM run_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]
