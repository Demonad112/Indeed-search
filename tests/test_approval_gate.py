"""The approval gate — constraint #1.

These tests hit the database directly with raw SQL, deliberately bypassing every
line of application code. If the gate only lived in Python, all of these would
pass silently. They must fail at the sqlite level.
"""

import sqlite3

import pytest

from jobpipe.db import utcnow


def _insert(conn, jid="job1", **overrides):
    fields = {
        "id": jid,
        "source": "test",
        "external_id": jid,
        "title": "Investigator",
        "company": "AMVIC",
        "location": "Calgary, AB",
        "status": "new",
        "first_seen_at": utcnow(),
        "last_seen_at": utcnow(),
    }
    fields.update(overrides)
    cols = ", ".join(fields)
    marks = ", ".join("?" * len(fields))
    conn.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks})", tuple(fields.values()))
    return jid


class TestSubmitRequiresApproval:
    def test_update_to_submitted_without_approval_is_rejected(self, conn):
        jid = _insert(conn)
        with pytest.raises(sqlite3.IntegrityError, match="approval gate"):
            conn.execute("UPDATE jobs SET status='submitted' WHERE id=?", (jid,))
        assert conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "new"

    def test_update_to_submitted_with_only_approved_at_is_rejected(self, conn):
        jid = _insert(conn)
        with pytest.raises(sqlite3.IntegrityError, match="approval gate"):
            conn.execute(
                "UPDATE jobs SET status='submitted', approved_at=? WHERE id=?", (utcnow(), jid)
            )

    def test_update_to_submitted_with_only_approved_by_is_rejected(self, conn):
        jid = _insert(conn)
        with pytest.raises(sqlite3.IntegrityError, match="approval gate"):
            conn.execute(
                "UPDATE jobs SET status='submitted', approved_by='addison' WHERE id=?", (jid,)
            )

    def test_insert_as_submitted_without_approval_is_rejected(self, conn):
        with pytest.raises(sqlite3.IntegrityError, match="approval gate"):
            _insert(conn, jid="sneaky", status="submitted")

    def test_submitted_at_cannot_be_set_on_unapproved_row(self, conn):
        jid = _insert(conn)
        with pytest.raises(sqlite3.IntegrityError, match="approval gate"):
            conn.execute("UPDATE jobs SET submitted_at=? WHERE id=?", (utcnow(), jid))

    def test_properly_approved_job_can_be_submitted(self, conn):
        jid = _insert(conn)
        conn.execute(
            "UPDATE jobs SET status='approved', approved_at=?, approved_by=? WHERE id=?",
            (utcnow(), "addison", jid),
        )
        conn.execute(
            "UPDATE jobs SET status='submitted', submitted_at=?, submit_result='ok' WHERE id=?",
            (utcnow(), jid),
        )
        row = conn.execute("SELECT status, submitted_at FROM jobs WHERE id=?", (jid,)).fetchone()
        assert row["status"] == "submitted"
        assert row["submitted_at"] is not None


class TestApprovalIntegrity:
    def test_approving_without_timestamp_is_rejected(self, conn):
        jid = _insert(conn)
        with pytest.raises(sqlite3.IntegrityError, match="approval gate"):
            conn.execute("UPDATE jobs SET status='approved' WHERE id=?", (jid,))

    def test_approval_record_is_immutable_once_submitted(self, conn):
        jid = _insert(conn)
        conn.execute(
            "UPDATE jobs SET status='approved', approved_at=?, approved_by=? WHERE id=?",
            (utcnow(), "addison", jid),
        )
        conn.execute("UPDATE jobs SET status='submitted', submitted_at=? WHERE id=?", (utcnow(), jid))

        # Rewriting who approved it after the fact would corrupt the audit trail.
        with pytest.raises(sqlite3.IntegrityError, match="approval gate"):
            conn.execute("UPDATE jobs SET approved_by='someone else' WHERE id=?", (jid,))

        # And clearing it is caught too.
        with pytest.raises(sqlite3.IntegrityError, match="approval gate"):
            conn.execute("UPDATE jobs SET approved_at=NULL WHERE id=?", (jid,))

    def test_unknown_status_is_rejected(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            _insert(conn, jid="bogus", status="totally-made-up")


def test_gate_survives_a_reopened_connection(tmp_path):
    """Triggers live in the file, so a fresh connection is still gated."""
    from jobpipe.db import connect, open_db

    path = tmp_path / "gate.db"
    c1 = open_db(path)
    _insert(c1, jid="persist")
    c1.close()

    c2 = connect(path)
    with pytest.raises(sqlite3.IntegrityError, match="approval gate"):
        c2.execute("UPDATE jobs SET status='submitted' WHERE id='persist'")
    c2.close()
