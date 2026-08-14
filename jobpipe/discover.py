"""Phase 1 — discovery.

Pulls postings from every enabled source, normalises them, dedupes on a stable
hash, and writes them into SQLite. Repeats update last_seen_at instead of
inserting duplicates. Postings that stop appearing age out.

Exit codes (so this drops into cron / a GitHub Action cleanly):
    0  all enabled sources succeeded
    2  at least one source failed, but the run completed and wrote what it got
    3  config error — nothing ran
    1  unexpected fatal error
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from . import EXPIRE_AFTER_MISSES
from .config import ROOT, ConfigError, Settings, Sources, load_criteria, load_sources, load_settings
from .db import finish_run, job_id as make_job_id, log_event, log_skip, open_db, start_run, utcnow
from .deadline import extract as extract_deadline
from .log import get
from .net import FetchError, Http
from .salary import DEFAULT_HOURS_PER_WEEK, Salary, floor_to_annual
from .salary import parse as parse_salary
from .sources import AshbySource, GreenhouseSource, IndeedInboxSource, LeverSource
from .sources.base import Posting

log = get("jobpipe.discover")

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_PARTIAL = 2
EXIT_CONFIG = 3

# Statuses that discovery is allowed to age out. Anything you have acted on is
# left alone — an approved job does not silently expire out from under you.
EXPIRABLE = ("new", "scored", "drafted")


@dataclass
class Stats:
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    expired: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped += 1
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "inserted": self.inserted,
            "updated": self.updated,
            "skipped": self.skipped,
            "expired": self.expired,
            "skip_reasons": self.skip_reasons,
        }


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def location_ok(location: str | None, allow: list[str], deny: list[str]) -> tuple[bool, str]:
    """Coarse geographic prefilter.

    ATS boards list worldwide roles; without this a single big board floods the
    database. This is deliberately generous — real must_have evaluation is Phase 2's
    job. We only drop things that are unambiguously somewhere else.
    """
    if not location or not location.strip():
        # No location given. Keep it and let scoring decide.
        return True, "no_location_given"

    low = location.lower()
    for term in deny:
        if term in low:
            return False, f"location_denied:{term}"
    for term in allow:
        if term in low:
            return True, f"location_allowed:{term}"
    return False, "location_out_of_area"


def title_excluded(title: str, patterns: list[str]) -> str | None:
    """Hard title exclusions from criteria.yaml. Returns the matched term."""
    low = (title or "").lower()
    for term in patterns:
        t = term.lower().strip()
        if t and re.search(rf"\b{re.escape(t)}\b", low):
            return t
    return None


def parse_since(value: str | None) -> str | None:
    """`--since` accepts an ISO date or a relative span like 7d / 36h / 2w."""
    if not value:
        return None
    v = value.strip().lower()
    m = re.fullmatch(r"(\d+)\s*([hdw])", v)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
        return (datetime.now(timezone.utc) - delta).isoformat(timespec="seconds")
    try:
        return datetime.fromisoformat(v).replace(tzinfo=timezone.utc).isoformat(timespec="seconds")
    except ValueError as exc:
        raise ConfigError(
            f"--since={value!r} is neither an ISO date nor a span like 7d/36h/2w"
        ) from exc


# ---------------------------------------------------------------------------
# Source assembly
# ---------------------------------------------------------------------------


def build_sources(sources_cfg: Sources, http: Http, only: list[str] | None) -> list[Any]:
    built: list[Any] = []

    indeed_cfg = sources_cfg.indeed
    if indeed_cfg.get("enabled", True):
        built.append(IndeedInboxSource(indeed_cfg, ROOT))

    built.append(GreenhouseSource(http, sources_cfg.slugs("greenhouse")))
    built.append(LeverSource(http, sources_cfg.slugs("lever")))
    built.append(AshbySource(http, sources_cfg.slugs("ashby")))

    if only:
        wanted = {s.lower() for s in only}
        unknown = wanted - {s.name for s in built}
        if unknown:
            raise ConfigError(
                f"unknown source(s): {', '.join(sorted(unknown))}. "
                f"Known: {', '.join(sorted(s.name for s in built))}"
            )
        built = [s for s in built if s.name in wanted]

    return built


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def upsert(
    conn: sqlite3.Connection,
    posting: Posting,
    salary: Salary,
    *,
    run_id: int,
    stats: Stats,
    dry_run: bool,
) -> str:
    """Insert a new posting or refresh an existing one. Returns 'inserted'|'updated'."""
    jid = make_job_id(posting.source, posting.external_id)
    now = utcnow()

    existing = conn.execute(
        "SELECT id, status, title, company, url, closes_at, description_raw, miss_streak "
        "FROM jobs WHERE id = ?",
        (jid,),
    ).fetchone()

    if dry_run:
        # Still count it — a dry run is useless if the summary reads all zeroes.
        if existing:
            stats.updated += 1
            return "updated"
        stats.inserted += 1
        return "inserted"

    if existing is None:
        conn.execute(
            """
            INSERT INTO jobs (
                id, source, external_id, url, title, company, location,
                posted_at, closes_at, description_raw,
                salary_min, salary_max, salary_period,
                salary_annual_min, salary_annual_max, salary_note,
                employment_type, status,
                first_seen_at, last_seen_at, miss_streak, raw_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'new', ?,?,0,?)
            """,
            (
                jid,
                posting.source,
                posting.external_id,
                posting.url,
                posting.title,
                posting.company,
                posting.location,
                posting.posted_at,
                posting.closes_at,
                posting.description_raw,
                salary.min,
                salary.max,
                salary.period,
                salary.annual_min,
                salary.annual_max,
                salary.note,
                posting.employment_type,
                now,
                now,
                json.dumps(posting.raw, default=str),
            ),
        )
        log_event(
            conn,
            job_id_=jid,
            run_id=run_id,
            kind="discovered",
            to_status="new",
            reason=f"first seen via {posting.source}",
            detail={"title": posting.title, "company": posting.company, "url": posting.url},
        )
        stats.inserted += 1
        return "inserted"

    # Repeat sighting. Refresh volatile fields, reset the miss counter, and keep
    # whatever pipeline state the job already reached.
    revived = existing["status"] == "expired"
    new_status = "new" if revived else existing["status"]

    conn.execute(
        """
        UPDATE jobs SET
            last_seen_at = ?, miss_streak = 0, url = COALESCE(?, url),
            closes_at = COALESCE(?, closes_at),
            description_raw = COALESCE(?, description_raw),
            salary_min = COALESCE(?, salary_min),
            salary_max = COALESCE(?, salary_max),
            salary_period = COALESCE(?, salary_period),
            salary_annual_min = COALESCE(?, salary_annual_min),
            salary_annual_max = COALESCE(?, salary_annual_max),
            salary_note = COALESCE(?, salary_note),
            employment_type = COALESCE(?, employment_type),
            status = ?,
            raw_json = ?
        WHERE id = ?
        """,
        (
            now,
            posting.url,
            posting.closes_at,
            posting.description_raw,
            salary.min,
            salary.max,
            salary.period,
            salary.annual_min,
            salary.annual_max,
            salary.note,
            posting.employment_type,
            new_status,
            json.dumps(posting.raw, default=str),
            jid,
        ),
    )

    if revived:
        log_event(
            conn,
            job_id_=jid,
            run_id=run_id,
            kind="revived",
            from_status="expired",
            to_status="new",
            reason="posting reappeared on its source after expiring",
        )
    else:
        log_event(
            conn,
            job_id_=jid,
            run_id=run_id,
            kind="seen",
            from_status=existing["status"],
            to_status=existing["status"],
            reason="still listed",
        )

    stats.updated += 1
    return "updated"


def expire_stale(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    healthy_sources: Iterable[str],
    seen_ids: set[str],
    stats: Stats,
    dry_run: bool,
) -> None:
    """Age out postings that have stopped appearing, and ones past closes_at.

    Only sources that fetched successfully participate: if Greenhouse was down we
    must not punish Greenhouse jobs for not showing up.
    """
    healthy = list(healthy_sources)
    now = utcnow()

    # 1. Past their stated closing date, regardless of source health.
    rows = conn.execute(
        f"SELECT id, title, company, closes_at FROM jobs "
        f"WHERE status IN ({','.join('?' * len(EXPIRABLE))}) "
        f"AND closes_at IS NOT NULL AND closes_at < ?",
        (*EXPIRABLE, now),
    ).fetchall()
    for row in rows:
        if not dry_run:
            conn.execute("UPDATE jobs SET status='expired' WHERE id=?", (row["id"],))
            log_event(
                conn,
                job_id_=row["id"],
                run_id=run_id,
                kind="expired",
                to_status="expired",
                reason=f"closes_at {row['closes_at']} has passed",
            )
        stats.expired += 1
        log.info("expired (deadline passed): %s @ %s", row["title"], row["company"])

    if not healthy:
        log.warning("no source fetched successfully — skipping the miss-streak sweep")
        return

    # 2. Missing from a healthy source this run.
    placeholders = ",".join("?" * len(healthy))
    status_ph = ",".join("?" * len(EXPIRABLE))
    rows = conn.execute(
        f"SELECT id, title, company, source, miss_streak FROM jobs "
        f"WHERE source IN ({placeholders}) AND status IN ({status_ph})",
        (*healthy, *EXPIRABLE),
    ).fetchall()

    for row in rows:
        if row["id"] in seen_ids:
            continue
        streak = row["miss_streak"] + 1
        if streak >= EXPIRE_AFTER_MISSES:
            if not dry_run:
                conn.execute(
                    "UPDATE jobs SET miss_streak=?, status='expired' WHERE id=?",
                    (streak, row["id"]),
                )
                log_event(
                    conn,
                    job_id_=row["id"],
                    run_id=run_id,
                    kind="expired",
                    to_status="expired",
                    reason=f"absent from {row['source']} for {streak} consecutive runs",
                )
            stats.expired += 1
            log.info("expired (gone %d runs): %s @ %s", streak, row["title"], row["company"])
        else:
            if not dry_run:
                conn.execute("UPDATE jobs SET miss_streak=? WHERE id=?", (streak, row["id"]))
            log.debug("miss %d/%d: %s", streak, EXPIRE_AFTER_MISSES, row["title"])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(
    *,
    since: str | None = None,
    only: list[str] | None = None,
    dry_run: bool = False,
    apply_title_filter: bool = True,
    settings: Settings | None = None,
    db_path: Path | None = None,
) -> int:
    try:
        settings = settings or load_settings()
        sources_cfg = load_sources()
        criteria = load_criteria()
    except ConfigError as exc:
        log.error("config error: %s", exc)
        return EXIT_CONFIG

    try:
        since_iso = parse_since(since)
    except ConfigError as exc:
        log.error("%s", exc)
        return EXIT_CONFIG

    hours_per_week = float(
        (criteria.get("must_have", {}) or {}).get("hours_per_week", DEFAULT_HOURS_PER_WEEK)
    )
    floor_annual = floor_to_annual(
        (criteria.get("must_have", {}) or {}).get("salary_floor"), hours_per_week
    )
    exclude_titles = (criteria.get("exclude", {}) or {}).get("titles", []) or []
    if not apply_title_filter:
        log.warning(
            "title exclusions disabled — postings matching exclude.titles will be ingested"
        )

    conn = open_db(db_path or settings.db_path)
    run_id = start_run(
        conn,
        "discover",
        {
            "since": since,
            "only": only,
            "dry_run": dry_run,
            "apply_title_filter": apply_title_filter,
        },
    )

    stats = Stats()
    ok_sources: list[str] = []
    failed_sources: list[str] = []
    seen_ids: set[str] = set()

    log.info("run %d starting (dry_run=%s, since=%s)", run_id, dry_run, since_iso or "-")

    with Http(settings) as http:
        adapters = build_sources(sources_cfg, http, only)
        log.info("sources: %s", ", ".join(a.name for a in adapters) or "(none)")

        for adapter in adapters:
            try:
                postings = list(adapter.fetch())
            except (FetchError, ValueError, KeyError) as exc:
                # Loud, recorded, and the run continues with the other sources.
                log.error("source %s FAILED: %s", adapter.name, exc)
                failed_sources.append(adapter.name)
                log_event(
                    conn,
                    job_id_=None,
                    run_id=run_id,
                    kind="source_failed",
                    reason=str(exc),
                    detail={"source": adapter.name},
                )
                continue

            stats.fetched += len(postings)
            for posting in postings:
                jid = make_job_id(posting.source, posting.external_id)

                if since_iso and posting.posted_at and posting.posted_at < since_iso:
                    stats.skip("older_than_since")
                    log_skip(
                        conn,
                        run_id=run_id,
                        source=posting.source,
                        external_id=posting.external_id,
                        title=posting.title,
                        company=posting.company,
                        location=posting.location,
                        reason="older_than_since",
                        detail={"posted_at": posting.posted_at, "since": since_iso},
                    )
                    continue

                allowed, why = location_ok(
                    posting.location, sources_cfg.location_allow, sources_cfg.location_deny
                )
                if not allowed:
                    stats.skip(why)
                    log_skip(
                        conn,
                        run_id=run_id,
                        source=posting.source,
                        external_id=posting.external_id,
                        title=posting.title,
                        company=posting.company,
                        location=posting.location,
                        reason=why,
                    )
                    log.info("skip [%s] %s @ %s (%s)", why, posting.title, posting.company, posting.location)
                    continue

                hit = title_excluded(posting.title, exclude_titles) if apply_title_filter else None
                if hit:
                    stats.skip("excluded_title")
                    log_skip(
                        conn,
                        run_id=run_id,
                        source=posting.source,
                        external_id=posting.external_id,
                        title=posting.title,
                        company=posting.company,
                        location=posting.location,
                        reason="excluded_title",
                        detail={"matched": hit},
                    )
                    log.info("skip [excluded_title:%s] %s @ %s", hit, posting.title, posting.company)
                    continue

                # Some boards (Ashby) only return descriptions on a second
                # request. Now that the cheap filters have passed, it is worth
                # spending one. A failure here costs the description, not the
                # posting.
                if posting.description_raw is None and hasattr(adapter, "fetch_details"):
                    try:
                        adapter.fetch_details(posting)
                    except (FetchError, ValueError) as exc:
                        log.warning(
                            "could not fetch details for %s @ %s: %s",
                            posting.title,
                            posting.company,
                            exc,
                        )
                        log_event(
                            conn,
                            job_id_=None,
                            run_id=run_id,
                            kind="detail_fetch_failed",
                            reason=str(exc),
                            detail={"source": posting.source, "title": posting.title},
                        )

                salary = parse_salary(posting.salary_text, hours_per_week=hours_per_week)
                if salary.note:
                    log.warning("%s @ %s: %s", posting.title, posting.company, salary.note)

                # Most sources never populate a closing date; it is written into
                # the body text instead. Phase 2's closing_soon flag needs it.
                if not posting.closes_at:
                    posting.closes_at = extract_deadline(posting.description_raw)
                    if posting.closes_at:
                        log.debug(
                            "deadline for %s: %s", posting.title, posting.closes_at
                        )

                seen_ids.add(jid)
                action = upsert(
                    conn, posting, salary, run_id=run_id, stats=stats, dry_run=dry_run
                )
                pay = (
                    f"${salary.annual_min:,.0f}-${salary.annual_max:,.0f}/yr"
                    if salary.found
                    else "pay unstated"
                )
                below = (
                    " BELOW-FLOOR"
                    if floor_annual and salary.annual_max and salary.annual_max < floor_annual
                    else ""
                )
                log.info(
                    "%-8s %s @ %s [%s] %s%s",
                    action,
                    posting.title,
                    posting.company,
                    posting.location or "?",
                    pay,
                    below,
                )

            ok_sources.append(adapter.name)
            # Adapters with post-success bookkeeping (Indeed archives its inbox).
            if not dry_run and hasattr(adapter, "on_success"):
                adapter.on_success()

    expire_stale(
        conn,
        run_id=run_id,
        healthy_sources=ok_sources,
        seen_ids=seen_ids,
        stats=stats,
        dry_run=dry_run,
    )

    exit_code = EXIT_PARTIAL if failed_sources else EXIT_OK
    finish_run(
        conn,
        run_id,
        sources_ok=ok_sources,
        sources_failed=failed_sources,
        stats=stats.as_dict(),
        exit_code=exit_code,
    )

    _summary(conn, stats, ok_sources, failed_sources, dry_run, floor_annual)
    conn.close()
    return exit_code


def _summary(
    conn: sqlite3.Connection,
    stats: Stats,
    ok: list[str],
    failed: list[str],
    dry_run: bool,
    floor_annual: float | None,
) -> None:
    print()
    print("=" * 72)
    print(f"  DISCOVER {'(DRY RUN — nothing written)' if dry_run else ''}")
    print("=" * 72)
    print(f"  fetched   {stats.fetched:>4}")
    print(f"  inserted  {stats.inserted:>4}")
    print(f"  updated   {stats.updated:>4}")
    print(f"  skipped   {stats.skipped:>4}")
    for reason, n in sorted(stats.skip_reasons.items(), key=lambda kv: -kv[1]):
        print(f"              {n:>3}  {reason}")
    print(f"  expired   {stats.expired:>4}")
    print(f"  sources   ok={','.join(ok) or '-'}  failed={','.join(failed) or '-'}")
    if floor_annual:
        print(f"  pay floor ${floor_annual:,.0f}/yr equivalent")

    rows = conn.execute(
        "SELECT status, COUNT(*) n FROM jobs GROUP BY status ORDER BY n DESC"
    ).fetchall()
    if rows:
        print("  " + "-" * 68)
        print("  database totals by status:")
        for row in rows:
            print(f"              {row['n']:>3}  {row['status']}")

    queue = conn.execute(
        "SELECT title, company, location, salary_annual_min, salary_annual_max "
        "FROM jobs WHERE status='new' ORDER BY last_seen_at DESC LIMIT 15"
    ).fetchall()
    if queue:
        print("  " + "-" * 68)
        print("  newest unscored postings:")
        for row in queue:
            if row["salary_annual_min"]:
                pay = f"${row['salary_annual_min']:,.0f}-${row['salary_annual_max']:,.0f}"
            else:
                pay = "pay unstated"
            print(f"    · {row['title'][:44]:<44} {row['company'][:22]:<22} {pay}")
    print("=" * 72)
    print()
