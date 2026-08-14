"""Indeed source — reads harvested JSON from an inbox directory.

Why an inbox and not an HTTP client
-----------------------------------
The spec asked for Indeed's RSS feed (`ca.indeed.com/rss?q=...`). That endpoint is
retired: it now answers HTTP 403 with a bot-challenge HTML page. Scraping the HTML
site instead is ruled out by constraint #2, and correctly so.

The working, sanctioned route is Indeed's own MCP job-search tool, which is
authenticated per-user and lives inside the Claude Code session — not in this
process. So the split is:

    Claude session   ->  runs the Indeed MCP search, writes JSON to data/inbox/indeed/
    discover.py      ->  reads that JSON, normalises it, dedupes it into SQLite

See docs/indeed_harvest.md for the harvest format and the prompt to regenerate it.

Dedupe warning
--------------
Indeed's API returns a *different* job_id and a different apply URL for the same
posting depending on which query surfaced it — the AMVIC Investigator posting came
back as both JOBSEARCH_31 and JOBSEARCH_64 in one session. Its IDs are therefore
per-response sequence numbers, not stable identifiers. We key on a normalised
company|title|location hash instead and keep the latest apply URL alongside.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

from ..log import get
from .base import Posting, dedupe_key, iso_date, normalise_employment_type

log = get("jobpipe.sources.indeed")

SOURCE_NAME = "indeed"


class IndeedInboxSource:
    """Ingests harvest files written by the Indeed MCP tool."""

    name = SOURCE_NAME

    def __init__(self, config: dict[str, Any], root: Path) -> None:
        self.config = config
        inbox = config.get("inbox_dir", "data/inbox/indeed")
        self.inbox = (root / inbox) if not Path(inbox).is_absolute() else Path(inbox)
        self.archive = self.config.get("archive", True)
        self.files_seen: list[Path] = []

    def fetch(self) -> Iterator[Posting]:
        if not self.inbox.exists():
            log.warning(
                "indeed inbox %s does not exist — no Indeed postings this run. "
                "See docs/indeed_harvest.md.",
                self.inbox,
            )
            return

        files = sorted(p for p in self.inbox.glob("*.json") if p.is_file())
        if not files:
            log.warning(
                "indeed inbox %s is empty — no Indeed postings this run. "
                "Re-harvest with the Indeed MCP tool (docs/indeed_harvest.md).",
                self.inbox,
            )
            return

        for path in files:
            self.files_seen.append(path)
            yield from self._read_file(path)

    def _read_file(self, path: Path) -> Iterator[Posting]:
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            # Loud, and we keep going with the other files.
            raise ValueError(f"indeed harvest file {path.name} is unreadable: {exc}") from exc

        if not isinstance(payload, dict) or "jobs" not in payload:
            raise ValueError(
                f"indeed harvest file {path.name} must be an object with a 'jobs' array"
            )

        query = payload.get("query")
        harvested_at = payload.get("harvested_at")
        jobs = payload.get("jobs") or []
        log.info("indeed: %s -> %d posting(s) [query=%r]", path.name, len(jobs), query)

        for entry in jobs:
            posting = self._to_posting(entry, query=query, harvested_at=harvested_at, file=path.name)
            if posting is not None:
                yield posting

    def _to_posting(
        self, entry: dict[str, Any], *, query: str | None, harvested_at: str | None, file: str
    ) -> Posting | None:
        title = (entry.get("title") or "").strip()
        company = (entry.get("company") or "").strip()
        location = (entry.get("location") or "").strip()

        if not title or not company:
            log.warning("indeed: dropping entry from %s with no title/company: %r", file, entry)
            return None

        key = dedupe_key(company, title, location)
        external_id = hashlib.sha256(key.encode()).hexdigest()[:24]

        description = entry.get("description")
        salary_text = entry.get("compensation")
        # Indeed's list view reports compensation as "N/A"; the real figure is in
        # the description body, which get_job_details returns.
        if salary_text in ("N/A", "None", "", None):
            salary_text = None
        if not salary_text and description:
            salary_text = description

        return Posting(
            source=SOURCE_NAME,
            external_id=external_id,
            title=title,
            company=company,
            location=location,
            url=entry.get("url") or entry.get("apply_url"),
            posted_at=iso_date(entry.get("posted_on") or entry.get("posted_at")),
            closes_at=iso_date(entry.get("closes_at")),
            description_raw=description,
            employment_type=normalise_employment_type(entry.get("job_type")),
            salary_text=salary_text,
            raw={
                "indeed_job_id": entry.get("job_id"),
                "harvest_query": query,
                "harvest_file": file,
                "harvested_at": harvested_at,
                "dedupe_key": key,
            },
        )

    def on_success(self) -> None:
        """Move consumed harvest files aside so the next run does not re-read them."""
        if not self.archive:
            return
        processed = self.inbox / "processed"
        processed.mkdir(parents=True, exist_ok=True)
        for path in self.files_seen:
            target = processed / path.name
            if target.exists():
                target.unlink()
            path.rename(target)
            log.debug("indeed: archived %s", path.name)
        if self.files_seen:
            log.info("indeed: archived %d harvest file(s) to %s", len(self.files_seen), processed)
