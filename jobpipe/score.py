"""Phase 2 — scoring.

One Claude call per `new` posting, returning strict JSON.

On strict JSON
--------------
The spec asked for "instruct the model explicitly and validate the parse,
retrying once on failure". That still happens, but it is now the *backstop*
rather than the mechanism: the request uses structured outputs
(`output_config.format` with a JSON schema), so the response is constrained to
the schema at decode time and cannot come back as prose or fenced markdown.

This is why the model moved off the `claude-sonnet-4-6` named in the spec —
that model does not support structured outputs. `claude-sonnet-5` does, and is
also the current Sonnet. Set `scoring.model` in criteria.yaml to override.

Two invariants are enforced in Python, not left to the model:
  - a must_have miss caps the score at 30
  - an exclude match caps the score at 9
The model reports what it found; this module decides what the number becomes.

Exit codes match discover.py: 0 ok, 2 partial, 3 config, 1 fatal.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .config import ConfigError, Settings, load_criteria, load_settings
from .db import finish_run, log_event, open_db, start_run, utcnow
from .log import get
from .prompts import CALIBRATION_ANCHORS, FLAGS, SCORE_SCHEMA, system_prompt, user_prompt
from .salary import DEFAULT_HOURS_PER_WEEK, floor_to_annual

log = get("jobpipe.score")

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_PARTIAL = 2
EXIT_CONFIG = 3

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_EFFORT = "medium"
# Adaptive thinking counts against max_tokens, so this needs real headroom —
# a tight budget produces a response that is all thinking and truncated JSON.
DEFAULT_MAX_TOKENS = 8000

MUST_HAVE_CAP = 30
EXCLUDE_CAP = 9
CLOSING_SOON_DAYS = 7


class ScoreError(Exception):
    """A posting could not be scored. Recorded, never silently swallowed."""


@dataclass
class Verdict:
    """A validated scoring result."""

    score: int
    rationale: str
    matched_signals: list[str]
    missing_requirements: list[str]
    must_have_misses: list[str]
    flags: list[str]
    instant_reject: bool
    instant_reject_reason: str
    raw: dict[str, Any] = field(default_factory=dict)
    model_score: int | None = None  # pre-cap, when a cap was applied
    cap_reason: str | None = None


@dataclass
class Stats:
    scored: int = 0
    failed: int = 0
    capped_must_have: int = 0
    capped_exclude: int = 0
    retried: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "scored": self.scored,
            "failed": self.failed,
            "capped_must_have": self.capped_must_have,
            "capped_exclude": self.capped_exclude,
            "retried": self.retried,
        }


# ---------------------------------------------------------------------------
# Contract validation
# ---------------------------------------------------------------------------


def validate(payload: Any) -> Verdict:
    """Validate a model response against the contract.

    Structured outputs make a malformed shape very unlikely, but "very unlikely"
    is not "impossible", and the score range in particular cannot be expressed in
    the schema (numeric constraints are unsupported). So this checks everything.
    """
    if not isinstance(payload, dict):
        raise ScoreError(f"expected a JSON object, got {type(payload).__name__}")

    missing = [
        key
        for key in (
            "score",
            "rationale",
            "matched_signals",
            "missing_requirements",
            "must_have_misses",
            "flags",
            "instant_reject",
            "instant_reject_reason",
        )
        if key not in payload
    ]
    if missing:
        raise ScoreError(f"response missing required key(s): {', '.join(missing)}")

    score = payload["score"]
    if isinstance(score, bool) or not isinstance(score, int):
        raise ScoreError(f"score must be an integer, got {score!r}")
    if not 0 <= score <= 100:
        raise ScoreError(f"score {score} is outside 0-100")

    rationale = payload["rationale"]
    if not isinstance(rationale, str) or not rationale.strip():
        raise ScoreError("rationale must be a non-empty string")

    def _strings(key: str) -> list[str]:
        value = payload[key]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ScoreError(f"{key} must be an array of strings, got {value!r}")
        return [v.strip() for v in value if v.strip()]

    matched = _strings("matched_signals")
    missing_reqs = _strings("missing_requirements")
    must_misses = _strings("must_have_misses")

    flags = _strings("flags")
    unknown = [f for f in flags if f not in FLAGS]
    if unknown:
        raise ScoreError(f"unknown flag(s): {', '.join(unknown)}")

    reject = payload["instant_reject"]
    if not isinstance(reject, bool):
        raise ScoreError(f"instant_reject must be a boolean, got {reject!r}")

    reason = payload["instant_reject_reason"]
    if not isinstance(reason, str):
        raise ScoreError(f"instant_reject_reason must be a string, got {reason!r}")

    return Verdict(
        score=score,
        rationale=rationale.strip(),
        matched_signals=matched,
        missing_requirements=missing_reqs,
        must_have_misses=must_misses,
        flags=sorted(set(flags)),
        instant_reject=reject,
        instant_reject_reason=reason.strip(),
        raw=payload,
    )


def apply_caps(verdict: Verdict, stats: Stats | None = None) -> Verdict:
    """Enforce the two scoring invariants in code.

    The model is asked to respect these, but a rule that matters is not left to
    the thing being constrained.
    """
    if verdict.instant_reject and verdict.score > EXCLUDE_CAP:
        verdict.model_score = verdict.score
        verdict.cap_reason = f"exclude match ({verdict.instant_reject_reason or 'unnamed'})"
        verdict.score = EXCLUDE_CAP
        if stats:
            stats.capped_exclude += 1
    elif verdict.must_have_misses and verdict.score > MUST_HAVE_CAP:
        verdict.model_score = verdict.score
        verdict.cap_reason = f"must_have miss ({', '.join(verdict.must_have_misses)})"
        verdict.score = MUST_HAVE_CAP
        if stats:
            stats.capped_must_have += 1
    return verdict


def add_deadline_flag(verdict: Verdict, closes_at: str | None, today: date) -> Verdict:
    """`closing_soon` is a fact about the calendar, not a judgement call."""
    if not closes_at:
        return verdict
    try:
        closes = date.fromisoformat(closes_at[:10])
    except ValueError:
        return verdict
    days = (closes - today).days
    if 0 <= days <= CLOSING_SOON_DAYS and "closing_soon" not in verdict.flags:
        verdict.flags = sorted(verdict.flags + ["closing_soon"])
    return verdict


# ---------------------------------------------------------------------------
# The API call
# ---------------------------------------------------------------------------


class Scorer:
    """Wraps the Anthropic client. One instance per run."""

    def __init__(self, criteria: dict[str, Any], *, settings: Settings | None = None) -> None:
        self.criteria = criteria
        cfg = criteria.get("scoring", {}) or {}
        self.model = cfg.get("model", DEFAULT_MODEL)
        self.effort = cfg.get("effort", DEFAULT_EFFORT)
        self.max_tokens = int(cfg.get("max_tokens", DEFAULT_MAX_TOKENS))
        self.thinking = cfg.get("thinking", "adaptive")

        must = criteria.get("must_have", {}) or {}
        hours = float(must.get("hours_per_week", DEFAULT_HOURS_PER_WEEK))
        self.floor_annual = floor_to_annual(must.get("salary_floor"), hours)
        self.system = system_prompt(criteria, self.floor_annual)
        self._client: Any = None

    # -- client -------------------------------------------------------------

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover
                raise ConfigError(
                    "the `anthropic` package is not installed. "
                    "Run: uv pip install anthropic"
                ) from exc
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise ConfigError(
                    "ANTHROPIC_API_KEY is not set. Phase 2 needs it — add it to .env."
                )
            self._client = anthropic.Anthropic()
        return self._client

    def _request_kwargs(self, job: dict[str, Any], today: str) -> dict[str, Any]:
        thinking: dict[str, Any] = (
            {"type": "adaptive"} if self.thinking == "adaptive" else {"type": "disabled"}
        )
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "thinking": thinking,
            "output_config": {
                "effort": self.effort,
                # Structured outputs: the response is constrained to this schema.
                "format": {"type": "json_schema", "schema": SCORE_SCHEMA},
            },
            "system": [
                {
                    "type": "text",
                    "text": self.system,
                    # The system prompt is identical for every posting in the run,
                    # and renders before the messages — so it caches across the
                    # whole batch. The posting itself sits after this breakpoint.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": user_prompt(job, today)}],
        }

    # -- scoring ------------------------------------------------------------

    def score_job(self, job: dict[str, Any], *, today: str | None = None) -> Verdict:
        """Score one posting. Retries once on a contract violation."""
        today = today or date.today().isoformat()
        kwargs = self._request_kwargs(job, today)

        last_error: Exception | None = None
        for attempt in (1, 2):
            try:
                response = self._call(kwargs)
            except ConfigError:
                # Missing key / missing package is a config problem, not a
                # per-posting one. Let it out so the run aborts once.
                raise
            except ScoreError:
                raise
            except Exception as exc:
                # Transport/API failures are not contract failures — the SDK has
                # already retried 429/5xx internally. Do not burn the second
                # attempt on them.
                raise ScoreError(f"API call failed: {exc}") from exc

            try:
                payload = self._extract(response)
                return validate(payload)
            except ScoreError as exc:
                last_error = exc
                log.warning(
                    "contract violation on attempt %d for %r: %s",
                    attempt,
                    job.get("title"),
                    exc,
                )
                if attempt == 1:
                    # Show the model its own bad output and ask again.
                    kwargs = dict(kwargs)
                    kwargs["messages"] = [
                        *kwargs["messages"],
                        {"role": "assistant", "content": _response_text(response) or "{}"},
                        {
                            "role": "user",
                            "content": (
                                f"That response was rejected: {exc}. "
                                "Return a single valid JSON object matching the schema."
                            ),
                        },
                    ]

        raise ScoreError(f"failed the JSON contract twice: {last_error}")

    def _call(self, kwargs: dict[str, Any]) -> Any:
        response = self.client.messages.create(**kwargs)
        stop = getattr(response, "stop_reason", None)
        if stop == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise ScoreError(f"model declined to answer (category={category})")
        if stop == "max_tokens":
            raise ScoreError(
                f"hit max_tokens ({self.max_tokens}) before finishing — "
                "raise scoring.max_tokens or lower scoring.effort"
            )
        return response

    @staticmethod
    def _extract(response: Any) -> Any:
        text = _response_text(response)
        if not text:
            raise ScoreError("response contained no text block")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ScoreError(f"response was not valid JSON: {exc}; got {text[:200]!r}") from exc


def _response_text(response: Any) -> str:
    parts = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts).strip()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save(conn: sqlite3.Connection, job_id: str, verdict: Verdict, model: str, run_id: int) -> None:
    conn.execute(
        """
        UPDATE jobs SET
            score = ?, score_rationale = ?, score_flags = ?,
            score_matched = ?, score_missing = ?, score_model = ?, score_raw = ?,
            scored_at = ?, status = CASE WHEN status = 'new' THEN 'scored' ELSE status END
        WHERE id = ?
        """,
        (
            verdict.score,
            verdict.rationale,
            json.dumps(verdict.flags),
            json.dumps(verdict.matched_signals),
            json.dumps(verdict.missing_requirements),
            model,
            json.dumps(verdict.raw),
            utcnow(),
            job_id,
        ),
    )
    log_event(
        conn,
        job_id_=job_id,
        run_id=run_id,
        kind="scored",
        from_status="new",
        to_status="scored",
        reason=f"scored {verdict.score}/100",
        detail={
            "score": verdict.score,
            "model_score": verdict.model_score,
            "cap_reason": verdict.cap_reason,
            "flags": verdict.flags,
            "must_have_misses": verdict.must_have_misses,
            "model": model,
        },
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(
    *,
    limit: int | None = None,
    job_id: str | None = None,
    rescore: bool = False,
    calibrate: bool = False,
    dry_run: bool = False,
    settings: Settings | None = None,
    db_path: Path | None = None,
) -> int:
    try:
        settings = settings or load_settings()
        criteria = load_criteria()
    except ConfigError as exc:
        log.error("config error: %s", exc)
        return EXIT_CONFIG

    scorer = Scorer(criteria, settings=settings)

    # Preflight: surface a missing key or package once, before any work, rather
    # than as one failure per posting.
    if not dry_run:
        try:
            _ = scorer.client
        except ConfigError as exc:
            log.error("config error: %s", exc)
            return EXIT_CONFIG

    if calibrate:
        return _calibrate(scorer, dry_run=dry_run)

    conn = open_db(db_path or settings.db_path)
    run_id = start_run(
        conn,
        "score",
        {"limit": limit, "job_id": job_id, "rescore": rescore, "dry_run": dry_run},
    )

    statuses = ("new", "scored") if rescore else ("new",)
    placeholders = ",".join("?" * len(statuses))
    sql = f"SELECT * FROM jobs WHERE status IN ({placeholders})"
    params: list[Any] = list(statuses)
    if job_id:
        sql += " AND id = ?"
        params.append(job_id)
    sql += " ORDER BY (closes_at IS NULL), closes_at ASC, last_seen_at DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"

    jobs = [dict(row) for row in conn.execute(sql, params).fetchall()]
    if not jobs:
        log.info("nothing to score (status=%s)", "/".join(statuses))
        finish_run(conn, run_id, sources_ok=[], sources_failed=[], stats={}, exit_code=EXIT_OK)
        conn.close()
        return EXIT_OK

    log.info(
        "scoring %d posting(s) with %s (effort=%s, thinking=%s)%s",
        len(jobs),
        scorer.model,
        scorer.effort,
        scorer.thinking,
        " [DRY RUN]" if dry_run else "",
    )

    if dry_run:
        _dry_run(scorer, jobs[0])
        finish_run(conn, run_id, sources_ok=[], sources_failed=[], stats={}, exit_code=EXIT_OK)
        conn.close()
        return EXIT_OK

    stats = Stats()
    today = date.today()

    for job in jobs:
        try:
            verdict = scorer.score_job(job, today=today.isoformat())
        except ScoreError as exc:
            stats.failed += 1
            log.error("FAILED %s @ %s: %s", job["title"], job["company"], exc)
            log_event(
                conn,
                job_id_=job["id"],
                run_id=run_id,
                kind="score_failed",
                reason=str(exc),
                detail={"title": job["title"], "model": scorer.model},
            )
            continue

        verdict = apply_caps(verdict, stats)
        verdict = add_deadline_flag(verdict, job.get("closes_at"), today)
        save(conn, job["id"], verdict, scorer.model, run_id)
        stats.scored += 1

        cap = f"  (model said {verdict.model_score}, capped: {verdict.cap_reason})" if verdict.cap_reason else ""
        log.info(
            "%3d  %-46s %-26s %s%s",
            verdict.score,
            job["title"][:46],
            job["company"][:26],
            ",".join(verdict.flags) or "-",
            cap,
        )

    exit_code = EXIT_PARTIAL if stats.failed else EXIT_OK
    finish_run(
        conn, run_id, sources_ok=[], sources_failed=[], stats=stats.as_dict(), exit_code=exit_code
    )
    _summary(conn, stats, criteria)
    conn.close()
    return exit_code


def _dry_run(scorer: Scorer, job: dict[str, Any]) -> None:
    kwargs = scorer._request_kwargs(job, date.today().isoformat())
    print("\n" + "=" * 72)
    print(f"  DRY RUN — request that would be sent for: {job['title']}")
    print("=" * 72)
    print(f"  model       {kwargs['model']}")
    print(f"  max_tokens  {kwargs['max_tokens']}")
    print(f"  thinking    {kwargs['thinking']}")
    print(f"  effort      {kwargs['output_config']['effort']}")
    print(f"  format      json_schema ({len(SCORE_SCHEMA['properties'])} properties, strict)")
    print(f"  cache       ephemeral breakpoint on system ({len(scorer.system):,} chars)")
    print("-" * 72)
    print("SYSTEM PROMPT:\n")
    print(scorer.system)
    print("-" * 72)
    print("USER TURN:\n")
    print(kwargs["messages"][0]["content"][:3000])
    print("=" * 72 + "\n")


def _calibrate(scorer: Scorer, *, dry_run: bool) -> int:
    """Score the three reference postings and compare against their anchors.

    The spec asks for this before any batch run. Note the anchors are also in
    the system prompt, so this measures whether the model follows its own
    calibration — treat a pass as necessary, not sufficient.
    """
    print("\n" + "=" * 72)
    print("  CALIBRATION — three real Calgary postings")
    print("=" * 72)

    if dry_run:
        for anchor in CALIBRATION_ANCHORS:
            print(f"  expect ~{anchor['score']:>3}  {anchor['label']}")
        print("=" * 72 + "\n")
        return EXIT_OK

    failures = 0
    for anchor in CALIBRATION_ANCHORS:
        job = {
            "title": anchor["label"].split(",")[0],
            "company": "(calibration)",
            "location": "Calgary, AB",
            "employment_type": None,
            "description_raw": anchor["detail"],
            "closes_at": None,
            "posted_at": None,
        }
        try:
            verdict = apply_caps(scorer.score_job(job))
        except ScoreError as exc:
            print(f"  FAILED  {anchor['label']}: {exc}")
            failures += 1
            continue

        delta = verdict.score - anchor["score"]
        ok = abs(delta) <= 12
        failures += 0 if ok else 1
        print(f"\n  {'PASS' if ok else 'FAIL'}  {anchor['label']}")
        print(f"        expected ~{anchor['score']}, got {verdict.score} ({delta:+d})")
        print(f"        flags: {', '.join(verdict.flags) or '-'}")
        print(f"        {verdict.rationale}")

    print("\n" + "=" * 72)
    if failures:
        print(f"  {failures} anchor(s) outside tolerance — do NOT batch-run yet.")
        print("  Tune config/criteria.yaml or the anchors in jobpipe/prompts.py.")
    else:
        print("  All three anchors within tolerance. Safe to run `python run.py score`.")
    print("=" * 72 + "\n")
    return EXIT_OK if not failures else EXIT_PARTIAL


def _summary(conn: sqlite3.Connection, stats: Stats, criteria: dict[str, Any]) -> None:
    threshold = (criteria.get("scoring", {}) or {}).get("threshold_to_draft", 70)
    print()
    print("=" * 72)
    print("  SCORE")
    print("=" * 72)
    print(f"  scored             {stats.scored:>4}")
    print(f"  failed             {stats.failed:>4}")
    print(f"  capped (must_have) {stats.capped_must_have:>4}")
    print(f"  capped (exclude)   {stats.capped_exclude:>4}")

    rows = conn.execute(
        "SELECT title, company, score, score_flags, salary_annual_min, closes_at "
        "FROM jobs WHERE score IS NOT NULL ORDER BY score DESC LIMIT 20"
    ).fetchall()
    if rows:
        print("  " + "-" * 68)
        print(f"  ranked (threshold to draft: {threshold})")
        for row in rows:
            mark = "*" if row["score"] >= threshold else " "
            flags = ",".join(json.loads(row["score_flags"] or "[]"))
            pay = f"${row['salary_annual_min']:,.0f}" if row["salary_annual_min"] else "-"
            print(
                f"  {mark}{row['score']:>3}  {row['title'][:40]:<40} "
                f"{row['company'][:20]:<20} {pay:>9}  {flags[:28]}"
            )
    above = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE score >= ?", (threshold,)
    ).fetchone()[0]
    print("  " + "-" * 68)
    print(f"  {above} posting(s) at or above the draft threshold of {threshold}")
    print("=" * 72)
    print()
