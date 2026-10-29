"""Phase 4 — the review dashboard.

A private, single-page app for triaging scored postings and approving the ones
worth applying to.

Privacy is enforced here, not documented
----------------------------------------
  - binds to 127.0.0.1 unless you explicitly pass --host, and passing a
    non-loopback host prints a loud warning first
  - every page requires a passphrase; the session cookie is HttpOnly, SameSite
    strict, and signed with an HMAC over a per-install secret
  - no external requests of any kind — no CDN, no fonts, no analytics. The
    resume never leaves the machine
  - X-Robots-Tag: noindex on everything, Cache-Control: no-store on anything
    carrying resume text
  - constant-time passphrase comparison, and a per-IP attempt limiter

The approval gate
-----------------
Approving sets `approved_at` and `approved_by`, which is what the Phase 1
database triggers require before a row may ever reach `submitted`. This app
never submits anything: it hands you the files and opens the employer's page.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import time
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

from .config import ConfigError, Settings, load_settings
from .db import log_event, open_db, utcnow
from .log import get

# Imported at module scope, not inside build_app(): this file uses
# `from __future__ import annotations`, so FastAPI resolves every handler's
# annotations by name at registration time. Function-local imports leave
# `Request` unresolvable and it silently becomes a query parameter — which
# shows up as a 422 on every POST rather than as an error you can read.
try:
    from fastapi import Body, Cookie, FastAPI, HTTPException, Request, Response
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

    FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without the extra installed
    FASTAPI_AVAILABLE = False

log = get("jobpipe.review")

WEB = Path(__file__).parent / "web"
COOKIE = "jobpipe_session"
SESSION_HOURS = 12
MAX_ATTEMPTS = 8
LOCKOUT_SECONDS = 300

_attempts: dict[str, list[float]] = {}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _secret(settings: Settings) -> bytes:
    """Per-install signing key, derived from the passphrase and db path.

    Deriving it means there is no extra secret to manage, and changing the
    passphrase invalidates every existing session for free.
    """
    passphrase = os.environ.get("JOBPIPE_PASSPHRASE", "")
    return hashlib.sha256(f"{passphrase}\x00{settings.db_path}".encode()).digest()


def make_token(settings: Settings) -> str:
    expires = int(time.time()) + SESSION_HOURS * 3600
    payload = str(expires).encode()
    sig = hmac.new(_secret(settings), payload, hashlib.sha256).hexdigest()[:32]
    return f"{expires}.{sig}"


def valid_token(token: str | None, settings: Settings) -> bool:
    if not token or "." not in token:
        return False
    expires, _, sig = token.partition(".")
    try:
        if int(expires) < time.time():
            return False
    except ValueError:
        return False
    expected = hmac.new(_secret(settings), expires.encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, expected)


def check_passphrase(candidate: str, settings: Settings) -> bool:
    real = os.environ.get("JOBPIPE_PASSPHRASE", "")
    if not real:
        return False
    return hmac.compare_digest(candidate.encode(), real.encode())


def rate_limited(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _attempts.get(ip, []) if now - t < LOCKOUT_SECONDS]
    _attempts[ip] = hits
    return len(hits) >= MAX_ATTEMPTS


def record_attempt(ip: str) -> None:
    _attempts.setdefault(ip, []).append(time.time())


# ---------------------------------------------------------------------------
# Row shaping
# ---------------------------------------------------------------------------

# Columns the browser is allowed to see. score_raw and raw_json are deliberately
# excluded — they are debugging artifacts, not review material.
FIELDS = (
    "id, source, url, title, company, location, employment_type, posted_at, closes_at, "
    "description_raw, salary_annual_min, salary_annual_max, status, score, score_rationale, "
    "score_flags, score_matched, score_missing, draft_resume, draft_cover, draft_gaps, "
    "draft_warnings, approved_at, approved_by, rejected_reason, drafted_at"
)


def shape(row: sqlite3.Row) -> dict[str, Any]:
    job = dict(row)
    for key, target in (
        ("score_flags", "flags"),
        ("score_matched", "matched"),
        ("score_missing", "missing"),
        ("draft_gaps", "draft_gaps"),
        ("draft_warnings", "draft_warnings"),
    ):
        try:
            job[target] = json.loads(job.pop(key) if key != target else job.get(key) or "[]") or []
        except (json.JSONDecodeError, TypeError):
            job[target] = []
    return job


def query(conn: sqlite3.Connection, view: str, q: str) -> list[dict[str, Any]]:
    where = {
        "queue": "status IN ('scored','drafted') AND approved_at IS NULL",
        "approved": "approved_at IS NOT NULL",
        "rejected": "status = 'rejected'",
        "all": "score IS NOT NULL",
    }.get(view, "status IN ('scored','drafted') AND approved_at IS NULL")

    params: list[Any] = []
    if q:
        where += " AND (title LIKE ? OR company LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]

    # closing_soon pinned to the top, then score desc — as the spec asks.
    sql = (
        f"SELECT {FIELDS} FROM jobs WHERE {where} ORDER BY "
        "CASE WHEN closes_at IS NOT NULL AND julianday(closes_at) - julianday('now') "
        "BETWEEN 0 AND 7 THEN 0 ELSE 1 END, score DESC, closes_at ASC"
    )
    return [shape(r) for r in conn.execute(sql, params).fetchall()]


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "queue": one(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('scored','drafted') AND approved_at IS NULL"
        ),
        "approved": one("SELECT COUNT(*) FROM jobs WHERE approved_at IS NOT NULL"),
        "flagged": one(
            "SELECT COUNT(*) FROM jobs WHERE draft_warnings IS NOT NULL "
            "AND draft_warnings NOT IN ('[]','')"
        ),
    }


def application_bundle(job: dict[str, Any]) -> tuple[bytes, str]:
    """Zip of the resume, cover letter, and a one-page brief for the interview."""
    slug = "".join(
        c if c.isalnum() else "-" for c in f"{job['company']}-{job['title']}".lower()
    ).strip("-")[:60]

    gaps = job.get("draft_gaps") or []
    brief = f"""# Application brief — {job['title']} @ {job['company']}

Score: {job.get('score')}/100
{job.get('score_rationale') or ''}

Location: {job.get('location') or 'not stated'}
Employment type: {job.get('employment_type') or 'not stated'}
Closes: {(job.get('closes_at') or 'not stated')[:10]}
Apply at: {job.get('url') or 'not recorded'}

## Matched signals
{chr(10).join('- ' + s for s in (job.get('matched') or [])) or '- none recorded'}

## Requirements you do not currently meet
{chr(10).join('- ' + s for s in (job.get('missing') or [])) or '- none recorded'}

## Gaps the draft deliberately did not paper over
{chr(10).join('- ' + s for s in gaps) or '- none'}

Be ready to speak to each gap above. The drafts do not claim any of them.
"""

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{slug}/resume.md", job.get("draft_resume") or "")
        z.writestr(f"{slug}/cover-letter.md", job.get("draft_cover") or "")
        z.writestr(f"{slug}/BRIEF.md", brief)
    return buf.getvalue(), f"{slug}.zip"


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def build_app(settings: Settings) -> Any:
    if not FASTAPI_AVAILABLE:
        raise ConfigError(
            "FastAPI is not installed. Run: uv pip install 'fastapi' 'uvicorn[standard]'"
        )

    app = FastAPI(title="jobpipe", docs_url=None, redoc_url=None, openapi_url=None)

    def db() -> sqlite3.Connection:
        return open_db(settings.db_path)

    def authed(token: str | None) -> bool:
        return valid_token(token, settings)

    def guard(token: str | None) -> None:
        if not authed(token):
            raise HTTPException(status_code=401, detail="not signed in")

    @app.middleware("http")
    async def headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        # Self-only. No CDN, no fonts, no beacons — the resume cannot leak.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        if request.url.path.startswith("/api/") or request.url.path == "/":
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        return response

    # -- pages ---------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(session: str | None = Cookie(default=None, alias=COOKIE)):  # type: ignore[no-untyped-def]
        if not authed(session):
            return RedirectResponse("/login", status_code=302)
        return HTMLResponse((WEB / "app.html").read_text())

    @app.get("/login", response_class=HTMLResponse)
    def login_page():  # type: ignore[no-untyped-def]
        return HTMLResponse((WEB / "login.html").read_text())

    @app.post("/api/login")
    def login(request: Request, payload: dict = Body(...)):  # type: ignore[no-untyped-def]
        ip = request.client.host if request.client else "local"
        if rate_limited(ip):
            raise HTTPException(status_code=429, detail="too many attempts; wait 5 minutes")
        if not check_passphrase(str(payload.get("passphrase", "")), settings):
            record_attempt(ip)
            log.warning("failed sign-in from %s", ip)
            raise HTTPException(status_code=401, detail="incorrect passphrase")
        _attempts.pop(ip, None)
        response = JSONResponse({"ok": True})
        response.set_cookie(
            COOKIE,
            make_token(settings),
            httponly=True,
            samesite="strict",
            max_age=SESSION_HOURS * 3600,
            path="/",
        )
        log.info("signed in from %s", ip)
        return response

    @app.post("/api/logout")
    def logout():  # type: ignore[no-untyped-def]
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    # -- data ----------------------------------------------------------------

    @app.get("/api/jobs")
    def list_jobs(  # type: ignore[no-untyped-def]
        view: str = "queue",
        q: str = "",
        session: str | None = Cookie(default=None, alias=COOKIE),
    ):
        guard(session)
        conn = db()
        try:
            return {"jobs": query(conn, view, q), "counts": counts(conn)}
        finally:
            conn.close()

    @app.post("/api/jobs/{job_id}/draft")
    def save_draft(  # type: ignore[no-untyped-def]
        job_id: str,
        payload: dict = Body(...),
        session: str | None = Cookie(default=None, alias=COOKIE),
    ):
        guard(session)
        conn = db()
        try:
            cur = conn.execute(
                "UPDATE jobs SET draft_resume = ?, draft_cover = ?, draft_edited_at = ? "
                "WHERE id = ?",
                (
                    payload.get("draft_resume", ""),
                    payload.get("draft_cover", ""),
                    utcnow(),
                    job_id,
                ),
            )
            if not cur.rowcount:
                raise HTTPException(status_code=404, detail="no such job")
            log_event(
                conn,
                job_id_=job_id,
                run_id=None,
                kind="draft_edited",
                reason="edited by hand in the review dashboard",
            )
            return {"ok": True}
        finally:
            conn.close()

    @app.post("/api/jobs/{job_id}/approve")
    def approve(  # type: ignore[no-untyped-def]
        job_id: str, session: str | None = Cookie(default=None, alias=COOKIE)
    ):
        guard(session)
        conn = db()
        try:
            row = conn.execute(
                f"SELECT {FIELDS} FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="no such job")
            job = shape(row)

            if not (job.get("draft_resume") or job.get("draft_cover")):
                raise HTTPException(
                    status_code=400,
                    detail="this posting has no draft yet — generate one first",
                )

            # Sets both fields the Phase 1 database triggers require.
            conn.execute(
                "UPDATE jobs SET status='approved', approved_at=?, approved_by=? WHERE id=?",
                (utcnow(), os.environ.get("JOBPIPE_APPROVER", "dashboard"), job_id),
            )
            log_event(
                conn,
                job_id_=job_id,
                run_id=None,
                kind="approved",
                from_status=job["status"],
                to_status="approved",
                reason="approved in the review dashboard",
                detail={"title": job["title"], "company": job["company"]},
            )
            log.info("APPROVED %s @ %s", job["title"], job["company"])
            return {
                "ok": True,
                "url": job.get("url"),
                "download": f"/api/jobs/{job_id}/download",
            }
        finally:
            conn.close()

    @app.post("/api/jobs/{job_id}/reject")
    def reject(  # type: ignore[no-untyped-def]
        job_id: str,
        payload: dict = Body(default={}),
        session: str | None = Cookie(default=None, alias=COOKIE),
    ):
        guard(session)
        reason = str(payload.get("reason", "")).strip() or "no reason given"
        conn = db()
        try:
            cur = conn.execute(
                "UPDATE jobs SET status='rejected', rejected_reason=? WHERE id=?",
                (reason, job_id),
            )
            if not cur.rowcount:
                raise HTTPException(status_code=404, detail="no such job")
            log_event(
                conn,
                job_id_=job_id,
                run_id=None,
                kind="rejected",
                to_status="rejected",
                reason=reason,
            )
            return {"ok": True}
        finally:
            conn.close()

    @app.post("/api/jobs/{job_id}/regenerate")
    def regenerate(  # type: ignore[no-untyped-def]
        job_id: str,
        payload: dict = Body(default={}),
        session: str | None = Cookie(default=None, alias=COOKIE),
    ):
        guard(session)
        from . import draft as draft_mod

        feedback = str(payload.get("feedback", "")).strip() or None
        code = draft_mod.run(job_id=job_id, regenerate=True, feedback=feedback, settings=settings)
        if code != 0:
            raise HTTPException(
                status_code=502,
                detail="drafting failed — check the server log (is ANTHROPIC_API_KEY set?)",
            )
        return {"ok": True}

    @app.get("/api/jobs/{job_id}/download")
    def download(  # type: ignore[no-untyped-def]
        job_id: str, session: str | None = Cookie(default=None, alias=COOKIE)
    ):
        guard(session)
        conn = db()
        try:
            row = conn.execute(f"SELECT {FIELDS} FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="no such job")
            blob, filename = application_bundle(shape(row))
            return Response(
                content=blob,
                media_type="application/zip",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Cache-Control": "no-store",
                },
            )
        finally:
            conn.close()

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    settings: Settings | None = None,
) -> int:
    try:
        settings = settings or load_settings()
    except ConfigError as exc:
        log.error("config error: %s", exc)
        return 3

    if not os.environ.get("JOBPIPE_PASSPHRASE"):
        generated = secrets.token_urlsafe(12)
        log.error(
            "JOBPIPE_PASSPHRASE is not set — the dashboard will not start without one.\n\n"
            "    Add this to your .env file:\n\n"
            "        JOBPIPE_PASSPHRASE=%s\n\n"
            "    (that is a freshly generated suggestion; any passphrase works)",
            generated,
        )
        return 3

    loopback = host in ("127.0.0.1", "localhost", "::1")
    if not loopback:
        log.warning("=" * 68)
        log.warning("  Binding to %s — NOT loopback.", host)
        log.warning("  Anything on your network can now reach the sign-in page,")
        log.warning("  and your resume and drafts sit behind it.")
        log.warning("  Use 127.0.0.1 unless you genuinely need this.")
        log.warning("=" * 68)

    try:
        import uvicorn
    except ImportError:
        log.error("uvicorn is not installed. Run: uv pip install 'fastapi' 'uvicorn[standard]'")
        return 3

    try:
        app = build_app(settings)
    except ConfigError as exc:
        log.error("%s", exc)
        return 3

    conn = open_db(settings.db_path)
    c = counts(conn)
    conn.close()

    print()
    print("=" * 68)
    print("  jobpipe review dashboard")
    print("=" * 68)
    print(f"    http://{host}:{port}")
    print()
    print(f"    {c['queue']} waiting for review   {c['approved']} approved   "
          f"{c['flagged']} with draft warnings")
    print()
    print("    Bound to loopback — not reachable from your network." if loopback
          else "    WARNING: reachable from your network.")
    print("    Nothing is ever submitted automatically.")
    print("=" * 68)
    print()

    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0
