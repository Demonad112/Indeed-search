"""Greenhouse public job board API.

    GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true

Public, documented, no key required. `content=true` returns the full HTML
description inline, so one request per board covers everything.
"""

from __future__ import annotations

from typing import Any, Iterator

from ..log import get
from ..net import Http
from .base import Posting, html_to_text, iso_date

log = get("jobpipe.sources.greenhouse")

SOURCE_NAME = "greenhouse"
API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"


class GreenhouseSource:
    name = SOURCE_NAME

    def __init__(self, http: Http, companies: list[dict[str, Any]]) -> None:
        self.http = http
        self.companies = companies

    def fetch(self) -> Iterator[Posting]:
        if not self.companies:
            log.info("greenhouse: no company slugs configured — skipping")
            return
        for entry in self.companies:
            yield from self._fetch_board(entry["slug"], entry.get("name", entry["slug"]))

    def _fetch_board(self, slug: str, display_name: str) -> Iterator[Posting]:
        url = API.format(slug=slug)
        data = self.http.get_json(url)
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if jobs is None:
            raise ValueError(f"greenhouse board {slug!r}: response has no 'jobs' array")

        log.info("greenhouse: %s -> %d posting(s)", slug, len(jobs))
        for job in jobs:
            posting = self._to_posting(job, slug, display_name)
            if posting is not None:
                yield posting

    def _to_posting(self, job: dict[str, Any], slug: str, display_name: str) -> Posting | None:
        job_id = job.get("id")
        title = (job.get("title") or "").strip()
        if job_id is None or not title:
            log.warning("greenhouse %s: dropping malformed job %r", slug, job)
            return None

        location = ((job.get("location") or {}).get("name") or "").strip()
        description = html_to_text(job.get("content"))

        # Greenhouse exposes free-form metadata; employment type sometimes lives there.
        employment_type = None
        for meta in job.get("metadata") or []:
            if str(meta.get("name", "")).strip().lower() in {
                "employment type",
                "job type",
                "employment_type",
            }:
                value = meta.get("value")
                if isinstance(value, str):
                    employment_type = value

        return Posting(
            source=SOURCE_NAME,
            external_id=f"{slug}:{job_id}",
            title=title,
            company=display_name,
            location=location,
            url=job.get("absolute_url"),
            posted_at=iso_date(job.get("first_published") or job.get("updated_at")),
            description_raw=description,
            employment_type=employment_type,
            salary_text=description,
            raw={"board": slug, "greenhouse_id": job_id, "departments": job.get("departments")},
        )
