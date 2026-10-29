"""SQLite schema, migrations, connection helper.

No ORM. Everything here is plain SQL so you can open the file with `sqlite3` and
read it yourself.

The approval gate (constraint #1) is enforced *in the schema* by triggers, not by
application code. Even a stray `UPDATE jobs SET status='submitted'` typed into the
sqlite3 shell is rejected unless approved_at and approved_by are set. Phase 5 will
also assert in Python, but the database is the backstop that cannot be bypassed by
forgetting an `if`.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import STATUSES
from .log import get

log = get("jobpipe.db")

SCHEMA_VERSION = 3

_STATUS_LIST = ", ".join(f"'{s}'" for s in STATUSES)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS jobs (
    id                    TEXT PRIMARY KEY,
    source                TEXT NOT NULL,
    external_id           TEXT NOT NULL,
    url                   TEXT,

    title                 TEXT,
    company               TEXT,
    location              TEXT,
    posted_at             TEXT,
    closes_at             TEXT,

    description_raw       TEXT,

    salary_min            REAL,
    salary_max            REAL,
    salary_period         TEXT,
    salary_currency       TEXT DEFAULT 'CAD',
    salary_annual_min     REAL,
    salary_annual_max     REAL,
    salary_note           TEXT,
    employment_type       TEXT,

    status                TEXT NOT NULL DEFAULT 'new'
                          CHECK (status IN ({_STATUS_LIST})),

    score                 INTEGER,
    score_rationale       TEXT,
    score_flags           TEXT,
    score_matched         TEXT,
    score_missing         TEXT,
    score_model           TEXT,
    score_raw             TEXT,
    scored_at             TEXT,

    draft_resume          TEXT,
    draft_cover           TEXT,
    draft_gaps            TEXT,
    draft_warnings        TEXT,
    draft_model           TEXT,
    draft_edited_at       TEXT,
    drafted_at            TEXT,

    approved_at           TEXT,
    approved_by           TEXT,
    rejected_reason       TEXT,

    submitted_at          TEXT,
    submit_result         TEXT,
    submit_screenshot_path TEXT,

    first_seen_at         TEXT NOT NULL,
    last_seen_at          TEXT NOT NULL,
    miss_streak           INTEGER NOT NULL DEFAULT 0,

    raw_json              TEXT,

    UNIQUE (source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status      ON jobs (status);
CREATE INDEX IF NOT EXISTS idx_jobs_source      ON jobs (source);
CREATE INDEX IF NOT EXISTS idx_jobs_score       ON jobs (score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_last_seen   ON jobs (last_seen_at);

-- Full audit trail: every state transition, with a reason.
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT,
    run_id      INTEGER,
    at          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    from_status TEXT,
    to_status   TEXT,
    reason      TEXT,
    detail      TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_events_job ON events (job_id, at);
CREATE INDEX IF NOT EXISTS idx_events_run ON events (run_id);

-- One row per invocation of discover.py, so "7 consecutive runs" is a real,
-- countable thing and not a guess.
CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    command        TEXT,
    args           TEXT,
    sources_ok     TEXT,
    sources_failed TEXT,
    stats          TEXT,
    exit_code      INTEGER
);

-- Constraint #5: every skipped posting is logged with a reason, and it is
-- queryable rather than buried in a log file.
CREATE TABLE IF NOT EXISTS skips (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER,
    at          TEXT NOT NULL,
    source      TEXT,
    external_id TEXT,
    title       TEXT,
    company     TEXT,
    location    TEXT,
    reason      TEXT NOT NULL,
    detail      TEXT
);

CREATE INDEX IF NOT EXISTS idx_skips_run    ON skips (run_id);
CREATE INDEX IF NOT EXISTS idx_skips_reason ON skips (reason);
"""

# ---------------------------------------------------------------------------
# The approval gate. Structural, per constraint #1.
# ---------------------------------------------------------------------------

_GATE_MSG = (
    "approval gate: a job cannot be marked submitted without both approved_at "
    "and approved_by set. See constraint #1."
)

APPROVAL_GATE = f"""
CREATE TRIGGER IF NOT EXISTS trg_gate_submit_insert
BEFORE INSERT ON jobs FOR EACH ROW
WHEN NEW.status = 'submitted'
     AND (NEW.approved_at IS NULL OR NEW.approved_by IS NULL)
BEGIN
    SELECT RAISE(ABORT, '{_GATE_MSG}');
END;

CREATE TRIGGER IF NOT EXISTS trg_gate_submit_update
BEFORE UPDATE ON jobs FOR EACH ROW
WHEN NEW.status = 'submitted'
     AND (NEW.approved_at IS NULL OR NEW.approved_by IS NULL)
BEGIN
    SELECT RAISE(ABORT, '{_GATE_MSG}');
END;

-- submitted_at is the Phase 5 audit stamp; it must never appear on an
-- unapproved row either.
CREATE TRIGGER IF NOT EXISTS trg_gate_submitted_at
BEFORE UPDATE ON jobs FOR EACH ROW
WHEN NEW.submitted_at IS NOT NULL
     AND (NEW.approved_at IS NULL OR NEW.approved_by IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'approval gate: submitted_at set on an unapproved job.');
END;

-- Approval itself must carry a timestamp and an approver.
CREATE TRIGGER IF NOT EXISTS trg_gate_approve
BEFORE UPDATE ON jobs FOR EACH ROW
WHEN NEW.status = 'approved'
     AND (NEW.approved_at IS NULL OR NEW.approved_by IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'approval gate: status=approved requires approved_at and approved_by.');
END;

-- Once a row is submitted, its approval record is immutable. Otherwise the
-- audit trail could be rewritten after the fact.
CREATE TRIGGER IF NOT EXISTS trg_gate_approval_immutable
BEFORE UPDATE ON jobs FOR EACH ROW
WHEN OLD.status = 'submitted'
     AND (NEW.approved_at IS NOT OLD.approved_at OR NEW.approved_by IS NOT OLD.approved_by)
BEGIN
    SELECT RAISE(ABORT, 'approval gate: approval record is immutable once submitted.');
END;
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def job_id(source: str, external_id: str) -> str:
    """Idempotent dedupe key: hash of (source, external_id)."""
    h = hashlib.sha256(f"{source}\x00{external_id}".encode()).hexdigest()
    return h[:32]


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# Columns added after v1. CREATE TABLE IF NOT EXISTS will not add these to a
# database that already exists, so they are applied explicitly.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: (
        "ALTER TABLE jobs ADD COLUMN score_matched TEXT",
        "ALTER TABLE jobs ADD COLUMN score_missing TEXT",
        "ALTER TABLE jobs ADD COLUMN score_model TEXT",
        "ALTER TABLE jobs ADD COLUMN score_raw TEXT",
    ),
    3: (
        "ALTER TABLE jobs ADD COLUMN draft_gaps TEXT",
        "ALTER TABLE jobs ADD COLUMN draft_warnings TEXT",
        "ALTER TABLE jobs ADD COLUMN draft_model TEXT",
        "ALTER TABLE jobs ADD COLUMN draft_edited_at TEXT",
    ),
}


def migrate(conn: sqlite3.Connection) -> None:
    """Apply schema. Idempotent — safe to call on every run."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.executescript(SCHEMA)
    conn.executescript(APPROVAL_GATE)

    for version in sorted(MIGRATIONS):
        if current < version:
            for statement in MIGRATIONS[version]:
                try:
                    conn.execute(statement)
                except sqlite3.OperationalError as exc:
                    # A fresh database already has the column from SCHEMA above.
                    if "duplicate column name" not in str(exc):
                        raise
            log.info("applied migration to schema v%d", version)

    if current < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        log.info("schema at version %d", SCHEMA_VERSION)


def open_db(path: Path) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def start_run(conn: sqlite3.Connection, command: str, args: dict[str, Any]) -> int:
    cur = conn.execute(
        "INSERT INTO runs (started_at, command, args) VALUES (?, ?, ?)",
        (utcnow(), command, json.dumps(args, default=str)),
    )
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    sources_ok: Iterable[str],
    sources_failed: Iterable[str],
    stats: dict[str, Any],
    exit_code: int,
) -> None:
    conn.execute(
        "UPDATE runs SET finished_at=?, sources_ok=?, sources_failed=?, stats=?, exit_code=? "
        "WHERE id=?",
        (
            utcnow(),
            json.dumps(sorted(sources_ok)),
            json.dumps(sorted(sources_failed)),
            json.dumps(stats, default=str),
            exit_code,
            run_id,
        ),
    )


def log_event(
    conn: sqlite3.Connection,
    *,
    job_id_: str | None,
    run_id: int | None,
    kind: str,
    reason: str,
    from_status: str | None = None,
    to_status: str | None = None,
    detail: Any = None,
) -> None:
    conn.execute(
        "INSERT INTO events (job_id, run_id, at, kind, from_status, to_status, reason, detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job_id_,
            run_id,
            utcnow(),
            kind,
            from_status,
            to_status,
            reason,
            json.dumps(detail, default=str) if detail is not None else None,
        ),
    )


def log_skip(
    conn: sqlite3.Connection,
    *,
    run_id: int | None,
    source: str,
    external_id: str | None,
    title: str | None,
    company: str | None,
    location: str | None,
    reason: str,
    detail: Any = None,
) -> None:
    conn.execute(
        "INSERT INTO skips (run_id, at, source, external_id, title, company, location, reason, detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            utcnow(),
            source,
            external_id,
            title,
            company,
            location,
            reason,
            json.dumps(detail, default=str) if detail is not None else None,
        ),
    )
