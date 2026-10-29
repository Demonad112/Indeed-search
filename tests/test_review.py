"""The dashboard's privacy and approval behaviour.

Two things are being protected here: the resume must not be reachable without
the passphrase, and the approval gate must still be the only route to
`approved`. Everything runs against a temporary database over the real ASGI
app — no network, no live server.
"""

import os

import pytest

from jobpipe.db import open_db, utcnow
from jobpipe.review import (
    COOKIE,
    application_bundle,
    build_app,
    check_passphrase,
    counts,
    make_token,
    query,
    rate_limited,
    record_attempt,
    valid_token,
    _attempts,
)

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

PASSPHRASE = "correct-horse-battery-staple"


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    from jobpipe.config import Settings

    monkeypatch.setenv("JOBPIPE_PASSPHRASE", PASSPHRASE)
    monkeypatch.setenv("JOBPIPE_APPROVER", "addison")
    _attempts.clear()
    return Settings(
        contact_email="a@b.com",
        db_path=tmp_path / "t.db",
        min_interval=2.0,
        jitter=0.0,
        http_timeout=5.0,
        http_retries=1,
    )


@pytest.fixture()
def seeded(settings):
    conn = open_db(settings.db_path)
    conn.execute(
        """INSERT INTO jobs (id, source, external_id, title, company, location, url,
             status, score, score_rationale, score_flags, score_matched, score_missing,
             draft_resume, draft_cover, draft_gaps, draft_warnings,
             first_seen_at, last_seen_at)
           VALUES ('j1','indeed','e1','Investigator','AMVIC','Calgary, AB',
             'https://example.test/apply','drafted',82,'Strong fit.','["strong_match"]',
             '["court reports"]','["CompTIA A+"]','# Resume','Dear team','["CompTIA A+"]','[]',
             ?, ?)""",
        (utcnow(), utcnow()),
    )
    conn.execute(
        """INSERT INTO jobs (id, source, external_id, title, company, status, score,
             draft_warnings, first_seen_at, last_seen_at)
           VALUES ('j2','indeed','e2','Guard','Acme','scored',14,
             '["possible claim of a credential not held"]', ?, ?)""",
        (utcnow(), utcnow()),
    )
    conn.close()
    return settings


@pytest.fixture()
def client(seeded):
    return TestClient(build_app(seeded), follow_redirects=False)


def sign_in(client):
    r = client.post("/api/login", json={"passphrase": PASSPHRASE})
    assert r.status_code == 200
    return r


# ---------------------------------------------------------------------------


class TestTokens:
    def test_roundtrip(self, settings):
        assert valid_token(make_token(settings), settings)

    def test_garbage_rejected(self, settings):
        for bad in (None, "", "nonsense", "abc.def", "9999999999.deadbeef"):
            assert not valid_token(bad, settings)

    def test_tampered_signature_rejected(self, settings):
        token = make_token(settings)
        expires, _, sig = token.partition(".")
        assert not valid_token(f"{expires}.{'0' * len(sig)}", settings)

    def test_expired_rejected(self, settings):
        import hashlib
        import hmac

        from jobpipe.review import _secret

        past = "1000000000"
        sig = hmac.new(_secret(settings), past.encode(), hashlib.sha256).hexdigest()[:32]
        assert not valid_token(f"{past}.{sig}", settings)

    def test_changing_the_passphrase_invalidates_sessions(self, settings, monkeypatch):
        token = make_token(settings)
        monkeypatch.setenv("JOBPIPE_PASSPHRASE", "a-different-one")
        assert not valid_token(token, settings)


class TestPassphrase:
    def test_correct(self, settings):
        assert check_passphrase(PASSPHRASE, settings)

    def test_incorrect(self, settings):
        assert not check_passphrase("nope", settings)

    def test_empty_never_matches(self, settings):
        assert not check_passphrase("", settings)

    def test_unset_passphrase_denies_everything(self, settings, monkeypatch):
        """No passphrase must mean no access — never an open door."""
        monkeypatch.delenv("JOBPIPE_PASSPHRASE", raising=False)
        assert not check_passphrase("", settings)
        assert not check_passphrase("anything", settings)


class TestRateLimit:
    def test_trips_after_repeated_failures(self):
        _attempts.clear()
        ip = "10.0.0.9"
        for _ in range(8):
            assert not rate_limited(ip)
            record_attempt(ip)
        assert rate_limited(ip)

    def test_is_per_ip(self):
        _attempts.clear()
        for _ in range(9):
            record_attempt("10.0.0.1")
        assert rate_limited("10.0.0.1")
        assert not rate_limited("10.0.0.2")


class TestAuthWall:
    """Nothing that carries resume text is reachable while signed out."""

    @pytest.mark.parametrize(
        "method,path",
        [
            ("get", "/api/jobs"),
            ("get", "/api/jobs/j1/download"),
            ("post", "/api/jobs/j1/approve"),
            ("post", "/api/jobs/j1/reject"),
            ("post", "/api/jobs/j1/draft"),
            ("post", "/api/jobs/j1/regenerate"),
        ],
    )
    def test_401_without_a_session(self, client, method, path):
        kwargs = {"json": {}} if method == "post" else {}
        assert getattr(client, method)(path, **kwargs).status_code == 401

    def test_root_redirects_to_login(self, client):
        r = client.get("/")
        assert r.status_code == 302 and r.headers["location"] == "/login"

    def test_login_page_leaks_nothing(self, client):
        body = client.get("/login").text
        assert "AMVIC" not in body and "Resume" not in body

    def test_bad_passphrase_rejected(self, client):
        assert client.post("/api/login", json={"passphrase": "wrong"}).status_code == 401

    def test_cookie_is_httponly_and_samesite(self, client):
        header = sign_in(client).headers["set-cookie"].lower()
        assert "httponly" in header and "samesite=strict" in header

    def test_logout_clears_the_cookie(self, client):
        sign_in(client)
        assert client.get("/api/jobs").status_code == 200
        client.post("/api/logout")
        client.cookies.clear()
        assert client.get("/api/jobs").status_code == 401

    def test_forged_cookie_rejected(self, client):
        client.cookies.set(COOKIE, "9999999999.forged")
        assert client.get("/api/jobs").status_code == 401


class TestHeaders:
    def test_noindex_everywhere(self, client):
        assert "noindex" in client.get("/login").headers["x-robots-tag"]

    def test_csp_blocks_external_requests(self, client):
        csp = client.get("/login").headers["content-security-policy"]
        assert "default-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp

    def test_resume_responses_are_not_cached(self, client):
        sign_in(client)
        assert "no-store" in client.get("/api/jobs").headers["cache-control"]


class TestQueue:
    def test_queue_excludes_approved(self, seeded):
        conn = open_db(seeded.db_path)
        assert {j["id"] for j in query(conn, "queue", "")} == {"j1", "j2"}
        conn.execute(
            "UPDATE jobs SET status='approved', approved_at=?, approved_by='x' WHERE id='j1'",
            (utcnow(),),
        )
        assert {j["id"] for j in query(conn, "queue", "")} == {"j2"}
        assert {j["id"] for j in query(conn, "approved", "")} == {"j1"}
        conn.close()

    def test_search_filters(self, seeded):
        conn = open_db(seeded.db_path)
        assert {j["id"] for j in query(conn, "queue", "AMVIC")} == {"j1"}
        conn.close()

    def test_json_columns_are_parsed(self, seeded):
        conn = open_db(seeded.db_path)
        job = next(j for j in query(conn, "queue", "") if j["id"] == "j1")
        assert job["flags"] == ["strong_match"]
        assert job["draft_gaps"] == ["CompTIA A+"]
        conn.close()

    def test_counts(self, seeded):
        conn = open_db(seeded.db_path)
        c = counts(conn)
        assert c["queue"] == 2 and c["approved"] == 0 and c["flagged"] == 1
        conn.close()

    def test_debug_columns_are_not_exposed(self, seeded):
        conn = open_db(seeded.db_path)
        job = query(conn, "queue", "")[0]
        assert "score_raw" not in job and "raw_json" not in job
        conn.close()


class TestApproval:
    def test_approve_sets_both_gate_fields(self, client, seeded):
        sign_in(client)
        r = client.post("/api/jobs/j1/approve")
        assert r.status_code == 200
        assert r.json()["url"] == "https://example.test/apply"

        conn = open_db(seeded.db_path)
        row = conn.execute(
            "SELECT status, approved_at, approved_by FROM jobs WHERE id='j1'"
        ).fetchone()
        assert row["status"] == "approved"
        assert row["approved_at"] and row["approved_by"] == "addison"
        conn.close()

    def test_approving_without_a_draft_is_refused(self, client):
        sign_in(client)
        r = client.post("/api/jobs/j2/approve")
        assert r.status_code == 400 and "no draft" in r.json()["detail"]

    def test_approve_writes_an_audit_event(self, client, seeded):
        sign_in(client)
        client.post("/api/jobs/j1/approve")
        conn = open_db(seeded.db_path)
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM events WHERE job_id='j1'")]
        assert "approved" in kinds
        conn.close()

    def test_the_dashboard_never_submits(self, client, seeded):
        """Approval must stop at `approved` — submission is not this app's job."""
        sign_in(client)
        client.post("/api/jobs/j1/approve")
        conn = open_db(seeded.db_path)
        row = conn.execute(
            "SELECT status, submitted_at FROM jobs WHERE id='j1'"
        ).fetchone()
        assert row["status"] == "approved"
        assert row["submitted_at"] is None
        conn.close()

    def test_reject_records_the_reason(self, client, seeded):
        sign_in(client)
        client.post("/api/jobs/j1/reject", json={"reason": "commute too far"})
        conn = open_db(seeded.db_path)
        row = conn.execute("SELECT status, rejected_reason FROM jobs WHERE id='j1'").fetchone()
        assert row["status"] == "rejected" and row["rejected_reason"] == "commute too far"
        conn.close()

    def test_unknown_job_is_404(self, client):
        sign_in(client)
        assert client.post("/api/jobs/nope/approve").status_code == 404


class TestEditing:
    def test_saving_edits_persists(self, client, seeded):
        sign_in(client)
        r = client.post(
            "/api/jobs/j1/draft",
            json={"draft_resume": "# Edited", "draft_cover": "Edited cover"},
        )
        assert r.status_code == 200
        conn = open_db(seeded.db_path)
        row = conn.execute(
            "SELECT draft_resume, draft_cover, draft_edited_at FROM jobs WHERE id='j1'"
        ).fetchone()
        assert row["draft_resume"] == "# Edited"
        assert row["draft_edited_at"] is not None
        conn.close()


class TestBundle:
    def test_zip_contains_all_three_files(self, seeded):
        import zipfile
        from io import BytesIO

        conn = open_db(seeded.db_path)
        job = next(j for j in query(conn, "queue", "") if j["id"] == "j1")
        conn.close()

        blob, name = application_bundle(job)
        names = zipfile.ZipFile(BytesIO(blob)).namelist()
        assert name.endswith(".zip")
        assert any(n.endswith("resume.md") for n in names)
        assert any(n.endswith("cover-letter.md") for n in names)
        assert any(n.endswith("BRIEF.md") for n in names)

    def test_brief_lists_the_gaps(self, seeded):
        import zipfile
        from io import BytesIO

        conn = open_db(seeded.db_path)
        job = next(j for j in query(conn, "queue", "") if j["id"] == "j1")
        conn.close()

        blob, _ = application_bundle(job)
        z = zipfile.ZipFile(BytesIO(blob))
        brief = z.read([n for n in z.namelist() if n.endswith("BRIEF.md")][0]).decode()
        assert "CompTIA A+" in brief
        assert "https://example.test/apply" in brief

    def test_download_requires_auth_then_works(self, client):
        assert client.get("/api/jobs/j1/download").status_code == 401
        sign_in(client)
        r = client.get("/api/jobs/j1/download")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        assert "attachment" in r.headers["content-disposition"]
