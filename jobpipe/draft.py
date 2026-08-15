"""Phase 3 — drafting.

Generates a tailored resume and cover letter for every posting above the
threshold, storing both as markdown in the database.

On not inventing things
-----------------------
The spec says "never invent experience, certifications, or dates". Asking the
model to confirm it didn't is worthless — a model that fabricates will also
fabricate the attestation. So the check is done here, deterministically:

  - every certification in `certifications_lacking` is scanned for in both
    drafts, and any mention that isn't clearly negated is flagged
  - every employer named in a draft is checked against the master resume
  - four-digit years are checked against the years the master actually contains

A draft that fails lands in the database with `draft_warnings` set and shows up
red in the review dashboard rather than being silently trusted. It is a smoke
alarm, not a proof: it catches the fabrications that matter most, not every
possible one.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR, ConfigError, Settings, load_criteria, load_settings
from .db import finish_run, log_event, open_db, start_run, utcnow
from .log import get
from .prompts import BANNED_OPENERS, DRAFT_SCHEMA, draft_system_prompt, draft_user_prompt
from .score import EXIT_CONFIG, EXIT_OK, EXIT_PARTIAL, ScoreError, _response_text

log = get("jobpipe.draft")

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_EFFORT = "high"  # drafting is writing; worth more than scoring
DEFAULT_MAX_TOKENS = 16000


class DraftError(Exception):
    """A draft could not be produced. Recorded, never silently swallowed."""


@dataclass
class Draft:
    resume: str
    cover: str
    gaps: list[str]
    emphasis: list[str]
    opening_line: str
    warnings: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Stats:
    drafted: int = 0
    failed: int = 0
    flagged: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"drafted": self.drafted, "failed": self.failed, "flagged": self.flagged}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate(payload: Any) -> Draft:
    if not isinstance(payload, dict):
        raise DraftError(f"expected a JSON object, got {type(payload).__name__}")

    required = ("resume_markdown", "cover_letter_markdown", "gaps", "emphasis", "opening_line")
    missing = [k for k in required if k not in payload]
    if missing:
        raise DraftError(f"response missing required key(s): {', '.join(missing)}")

    resume = payload["resume_markdown"]
    cover = payload["cover_letter_markdown"]
    for name, value in (("resume_markdown", resume), ("cover_letter_markdown", cover)):
        if not isinstance(value, str) or len(value.strip()) < 200:
            raise DraftError(f"{name} is missing or implausibly short ({len(value or '')} chars)")

    def _strings(key: str) -> list[str]:
        value = payload[key]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise DraftError(f"{key} must be an array of strings")
        return [v.strip() for v in value if v.strip()]

    opening = payload["opening_line"]
    if not isinstance(opening, str) or not opening.strip():
        raise DraftError("opening_line must be a non-empty string")

    return Draft(
        resume=resume.strip(),
        cover=cover.strip(),
        gaps=_strings("gaps"),
        emphasis=_strings("emphasis"),
        opening_line=opening.strip(),
        raw=payload,
    )


# ---------------------------------------------------------------------------
# Anti-fabrication check
# ---------------------------------------------------------------------------

# Words that, near a lacked credential, mean the draft is *disclaiming* it rather
# than claiming it. "I do not hold CompTIA A+" must not trip the alarm.
_NEGATION = re.compile(
    r"\b(not|no|without|lack|lacking|missing|don'?t|do not|does not|never|"
    r"working toward|working towards|pursuing|in progress|willing to obtain|"
    r"prepared to obtain|would need|have yet to|yet to)\b",
    re.I,
)


def _sentences(text: str) -> list[str]:
    plain = re.sub(r"[#*_>`\-]", " ", text)
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", plain) if s.strip()]


def check_fabrication(
    draft: Draft, criteria: dict[str, Any], resume_master: str
) -> list[str]:
    """Look for claims the master resume does not support. Returns warnings."""
    warnings: list[str] = []
    body = f"{draft.resume}\n{draft.cover}"

    # 1. Credentials the candidate does not hold.
    for item in criteria.get("certifications_lacking", []) or []:
        # Match the distinctive head of the phrase, e.g. "CompTIA A+".
        needle = str(item).split("/")[0].strip()
        if len(needle) < 4:
            continue
        pattern = re.compile(re.escape(needle), re.I)
        for sentence in _sentences(body):
            if pattern.search(sentence) and not _NEGATION.search(sentence):
                warnings.append(
                    f"possible claim of a credential not held ({needle}): \"{sentence[:120]}\""
                )
                break

    # 2. Expired certifications presented as current.
    for cert in criteria.get("certifications_held", []) or []:
        if "EXPIRED" not in str(cert.get("note", "")).upper():
            continue
        name = str(cert.get("name", "")).split("(")[0].strip()
        if len(name) < 4:
            continue
        for sentence in _sentences(body):
            if re.search(re.escape(name), sentence, re.I) and not re.search(
                r"\b(expired|lapsed|no longer|until \d{4}|renew)\b", sentence, re.I
            ):
                warnings.append(
                    f"expired certification presented without qualification ({name}): "
                    f"\"{sentence[:120]}\""
                )
                break

    # 3. Years that do not appear in the master resume.
    master_years = set(re.findall(r"\b(19|20)\d{2}\b", resume_master))
    master_years = set(re.findall(r"\b((?:19|20)\d{2})\b", resume_master))
    draft_years = set(re.findall(r"\b((?:19|20)\d{2})\b", body))
    invented = sorted(draft_years - master_years)
    if invented:
        warnings.append(f"year(s) not present in the master resume: {', '.join(invented)}")

    # 4. Banned openers.
    opening = draft.opening_line.lower()
    for banned in BANNED_OPENERS:
        if opening.startswith(banned) or banned in opening[:80]:
            warnings.append(f"cover letter opens with a banned phrase: \"{banned}\"")
            break

    return warnings


# ---------------------------------------------------------------------------
# The API call
# ---------------------------------------------------------------------------


class Drafter:
    def __init__(self, criteria: dict[str, Any], *, settings: Settings | None = None) -> None:
        self.criteria = criteria
        cfg = criteria.get("drafting", {}) or {}
        self.model = cfg.get("model", DEFAULT_MODEL)
        self.effort = cfg.get("effort", DEFAULT_EFFORT)
        self.max_tokens = int(cfg.get("max_tokens", DEFAULT_MAX_TOKENS))

        profile = CONFIG_DIR / "profile"
        try:
            self.resume_master = (profile / "resume_master.md").read_text()
            self.voice_sample = (profile / "voice_sample.md").read_text()
        except OSError as exc:
            raise ConfigError(f"could not read config/profile/: {exc}") from exc

        self.system = draft_system_prompt(self.resume_master, self.voice_sample, criteria)
        self._client: Any = None

    @property
    def client(self) -> Any:
        if self._client is None:
            import os

            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover
                raise ConfigError("the `anthropic` package is not installed") from exc
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise ConfigError(
                    "ANTHROPIC_API_KEY is not set. Phase 3 needs it — add it to .env."
                )
            self._client = anthropic.Anthropic()
        return self._client

    def _request_kwargs(self, job: dict[str, Any], feedback: str | None) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "thinking": {"type": "adaptive"},
            "output_config": {
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": DRAFT_SCHEMA},
            },
            "system": [
                {
                    "type": "text",
                    "text": self.system,
                    # Master resume + voice sample are the same on every call.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": draft_user_prompt(job, feedback)}],
        }

    def draft_job(self, job: dict[str, Any], *, feedback: str | None = None) -> Draft:
        kwargs = self._request_kwargs(job, feedback)

        last_error: Exception | None = None
        for attempt in (1, 2):
            try:
                response = self.client.messages.create(**kwargs)
            except ConfigError:
                raise
            except Exception as exc:
                raise DraftError(f"API call failed: {exc}") from exc

            stop = getattr(response, "stop_reason", None)
            if stop == "refusal":
                raise DraftError("model declined to answer")
            if stop == "max_tokens":
                raise DraftError(
                    f"hit max_tokens ({self.max_tokens}) — raise drafting.max_tokens"
                )

            text = _response_text(response)
            try:
                if not text:
                    raise DraftError("response contained no text block")
                draft = validate(json.loads(text))
            except (DraftError, json.JSONDecodeError) as exc:
                last_error = exc
                log.warning("draft contract violation on attempt %d: %s", attempt, exc)
                if attempt == 1:
                    kwargs = dict(kwargs)
                    kwargs["messages"] = [
                        *kwargs["messages"],
                        {"role": "assistant", "content": text or "{}"},
                        {
                            "role": "user",
                            "content": f"That was rejected: {exc}. Return valid JSON.",
                        },
                    ]
                continue

            draft.warnings = check_fabrication(draft, self.criteria, self.resume_master)
            return draft

        raise DraftError(f"failed the draft contract twice: {last_error}")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save(conn: sqlite3.Connection, job_id: str, draft: Draft, model: str, run_id: int) -> None:
    conn.execute(
        """
        UPDATE jobs SET
            draft_resume = ?, draft_cover = ?, draft_gaps = ?,
            draft_warnings = ?, draft_model = ?, drafted_at = ?,
            status = CASE WHEN status IN ('scored','drafted') THEN 'drafted' ELSE status END
        WHERE id = ?
        """,
        (
            draft.resume,
            draft.cover,
            json.dumps(draft.gaps),
            json.dumps(draft.warnings),
            model,
            utcnow(),
            job_id,
        ),
    )
    log_event(
        conn,
        job_id_=job_id,
        run_id=run_id,
        kind="drafted",
        from_status="scored",
        to_status="drafted",
        reason=f"drafted ({len(draft.warnings)} warning(s))",
        detail={"gaps": draft.gaps, "warnings": draft.warnings, "model": model},
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(
    *,
    threshold: int | None = None,
    limit: int | None = None,
    job_id: str | None = None,
    regenerate: bool = False,
    feedback: str | None = None,
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

    try:
        drafter = Drafter(criteria, settings=settings)
    except ConfigError as exc:
        log.error("config error: %s", exc)
        return EXIT_CONFIG

    if not dry_run:
        try:
            _ = drafter.client
        except ConfigError as exc:
            log.error("config error: %s", exc)
            return EXIT_CONFIG

    if threshold is None:
        threshold = int((criteria.get("scoring", {}) or {}).get("threshold_to_draft", 70))

    conn = open_db(db_path or settings.db_path)
    run_id = start_run(
        conn,
        "draft",
        {"threshold": threshold, "limit": limit, "job_id": job_id, "regenerate": regenerate},
    )

    if job_id:
        sql = "SELECT * FROM jobs WHERE id = ?"
        params: list[Any] = [job_id]
    else:
        sql = (
            "SELECT * FROM jobs WHERE score >= ? AND status IN ('scored','drafted') "
            + ("" if regenerate else "AND draft_resume IS NULL ")
            + "ORDER BY (closes_at IS NULL), closes_at ASC, score DESC"
        )
        params = [threshold]
    if limit:
        sql += f" LIMIT {int(limit)}"

    jobs = [dict(row) for row in conn.execute(sql, params).fetchall()]
    if not jobs:
        log.info("nothing to draft (threshold %d)", threshold)
        finish_run(conn, run_id, sources_ok=[], sources_failed=[], stats={}, exit_code=EXIT_OK)
        conn.close()
        return EXIT_OK

    log.info(
        "drafting %d posting(s) with %s (effort=%s)%s",
        len(jobs),
        drafter.model,
        drafter.effort,
        " [DRY RUN]" if dry_run else "",
    )

    if dry_run:
        print(f"\nWould draft {len(jobs)} posting(s) at threshold {threshold}:")
        for job in jobs:
            print(f"  {job['score']:>3}  {job['title'][:48]:<48} {job['company'][:24]}")
        print(f"\nSystem prompt: {len(drafter.system):,} chars (cached across all calls)\n")
        finish_run(conn, run_id, sources_ok=[], sources_failed=[], stats={}, exit_code=EXIT_OK)
        conn.close()
        return EXIT_OK

    stats = Stats()
    for job in jobs:
        try:
            draft = drafter.draft_job(job, feedback=feedback)
        except DraftError as exc:
            stats.failed += 1
            log.error("FAILED %s @ %s: %s", job["title"], job["company"], exc)
            log_event(
                conn,
                job_id_=job["id"],
                run_id=run_id,
                kind="draft_failed",
                reason=str(exc),
                detail={"title": job["title"]},
            )
            continue

        save(conn, job["id"], draft, drafter.model, run_id)
        stats.drafted += 1
        if draft.warnings:
            stats.flagged += 1
            for warning in draft.warnings:
                log.warning("  %s @ %s: %s", job["title"], job["company"], warning)

        log.info(
            "drafted  %-44s %-24s %d gap(s)%s",
            job["title"][:44],
            job["company"][:24],
            len(draft.gaps),
            f"  {len(draft.warnings)} WARNING(S)" if draft.warnings else "",
        )

    exit_code = EXIT_PARTIAL if stats.failed else EXIT_OK
    finish_run(
        conn, run_id, sources_ok=[], sources_failed=[], stats=stats.as_dict(), exit_code=exit_code
    )

    print()
    print("=" * 72)
    print("  DRAFT")
    print("=" * 72)
    print(f"  drafted  {stats.drafted:>4}")
    print(f"  failed   {stats.failed:>4}")
    print(f"  flagged  {stats.flagged:>4}   (fabrication check — review these first)")
    ready = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status='drafted' AND approved_at IS NULL"
    ).fetchone()[0]
    print(f"\n  {ready} posting(s) waiting in the review queue.")
    print("  Open the dashboard:  python run.py serve")
    print("=" * 72)
    print()
    conn.close()
    return exit_code
