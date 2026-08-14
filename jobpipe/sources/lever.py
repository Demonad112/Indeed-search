"""Lever public postings API.

    GET https://api.lever.co/v0/postings/{slug}?mode=json

Public, no key required. Returns full descriptions inline.
"""

from __future__ import annotations

from typing import Any, Iterator

from ..log import get
from ..net import Http
from .base import Posting, html_to_text, iso_date, normalise_employment_type

log = get("jobpipe.sources.lever")

SOURCE_NAME = "lever"
API = "https://api.lever.co/v0/postings/{slug}?mode=json"


class LeverSource:
    name = SOURCE_NAME

    def __init__(self, http: Http, companies: list[dict[str, Any]]) -> None:
        self.http = http
        self.companies = companies

    def fetch(self) -> Iterator[Posting]:
        if not self.companies:
            log.info("lever: no company slugs configured — skipping")
            return
        for entry in self.companies:
            yield from self._fetch_board(entry["slug"], entry.get("name", entry["slug"]))

    def _fetch_board(self, slug: str, display_name: str) -> Iterator[Posting]:
        data = self.http.get_json(API.format(slug=slug))
        if not isinstance(data, list):
            raise ValueError(f"lever board {slug!r}: expected a JSON array, got {type(data).__name__}")

        log.info("lever: %s -> %d posting(s)", slug, len(data))
        for job in data:
            posting = self._to_posting(job, slug, display_name)
            if posting is not None:
                yield posting

    def _to_posting(self, job: dict[str, Any], slug: str, display_name: str) -> Posting | None:
        job_id = job.get("id")
        title = (job.get("text") or "").strip()
        if not job_id or not title:
            log.warning("lever %s: dropping malformed job %r", slug, job)
            return None

        categories = job.get("categories") or {}
        location = (categories.get("location") or "").strip()

        # Lever splits the body into descriptionPlain plus a list of `lists`
        # (Requirements, Benefits, ...). Stitch them back together.
        parts: list[str] = []
        if job.get("descriptionPlain"):
            parts.append(str(job["descriptionPlain"]).strip())
        elif job.get("description"):
            parts.append(html_to_text(job["description"]) or "")
        for block in job.get("lists") or []:
            heading = (block.get("text") or "").strip()
            body = html_to_text(block.get("content")) or ""
            if heading or body:
                parts.append(f"{heading}\n{body}".strip())
        if job.get("additionalPlain"):
            parts.append(str(job["additionalPlain"]).strip())

        description = "\n\n".join(p for p in parts if p) or None

        return Posting(
            source=SOURCE_NAME,
            external_id=f"{slug}:{job_id}",
            title=title,
            company=display_name,
            location=location,
            url=job.get("hostedUrl") or job.get("applyUrl"),
            posted_at=iso_date(job.get("createdAt")),
            description_raw=description,
            employment_type=normalise_employment_type(categories.get("commitment")),
            salary_text=description,
            raw={
                "board": slug,
                "lever_id": job_id,
                "team": categories.get("team"),
                "department": categories.get("department"),
            },
        )
