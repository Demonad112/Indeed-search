"""Ashby public job board GraphQL endpoint.

    POST https://jobs.ashbyhq.com/api/non-user-graphql

Public (it backs the jobs.ashbyhq.com/{slug} page), no key required.
Introspection is disabled, so the field sets below were established by probing
the live endpoint on 2026-08-14.

Two queries, because Ashby splits them:

  ApiJobBoardWithTeams  — the whole board, but NO description and NO date.
                          Gives id, title, locationName, employmentType,
                          secondaryLocations, compensationTierSummary.
  ApiJobPosting         — one posting: descriptionHtml, publishedDate,
                          departmentName, teamNames.

Fetching details for every posting would be an N+1 against a 2s-per-host rate
limit — 738 postings on the OpenAI board is over 25 minutes. So `fetch()`
returns the cheap list and `fetch_details()` is called by discover.py only for
postings that survive the location and title filters.
"""

from __future__ import annotations

import re
from typing import Any, Iterator

from ..log import get
from ..net import Http
from .base import Posting, html_to_text, iso_date, normalise_employment_type

log = get("jobpipe.sources.ashby")

SOURCE_NAME = "ashby"
API = "https://jobs.ashbyhq.com/api/non-user-graphql"

LIST_QUERY = """
query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {
  jobBoard: jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName) {
    jobPostings {
      id
      title
      locationName
      employmentType
      compensationTierSummary
      secondaryLocations { locationName }
    }
  }
}
"""

DETAIL_QUERY = """
query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {
  jobPosting(
    organizationHostedJobsPageName: $organizationHostedJobsPageName
    jobPostingId: $jobPostingId
  ) {
    id
    descriptionHtml
    publishedDate
    departmentName
    teamNames
  }
}
"""

# "$342K – $555K • Offers Equity" / "CA$70K – CA$90K" / "$25 – $30 / hr"
_TIER_AMOUNT = re.compile(r"(?:CA|US|C|A)?\$\s*(\d+(?:\.\d+)?)\s*([KkMm])?")


def expand_compensation(summary: str | None) -> str | None:
    """Turn Ashby's compact tier summary into something salary.py can read.

    Ashby writes "$342K – $555K"; the salary parser wants real numbers and an
    explicit period. Tier summaries are annual unless they say otherwise.
    """
    if not summary or "$" not in summary:
        return None

    amounts: list[float] = []
    for m in _TIER_AMOUNT.finditer(summary):
        value = float(m.group(1))
        suffix = (m.group(2) or "").lower()
        if suffix == "k":
            value *= 1_000
        elif suffix == "m":
            value *= 1_000_000
        amounts.append(value)

    if not amounts:
        return None

    low = re.search(r"/\s*(hr|hour|day|wk|week|mo|month|yr|year)", summary, re.I)
    period = {
        "hr": "per hour",
        "hour": "per hour",
        "day": "per day",
        "wk": "per week",
        "week": "per week",
        "mo": "per month",
        "month": "per month",
        "yr": "per year",
        "year": "per year",
    }.get(low.group(1).lower() if low else "", "per year")

    if len(amounts) >= 2:
        return f"Pay: ${amounts[0]:,.2f} - ${amounts[1]:,.2f} {period}"
    return f"Pay: ${amounts[0]:,.2f} {period}"


class AshbySource:
    name = SOURCE_NAME

    def __init__(self, http: Http, companies: list[dict[str, Any]]) -> None:
        self.http = http
        self.companies = companies
        self._slug_by_id: dict[str, str] = {}

    def fetch(self) -> Iterator[Posting]:
        if not self.companies:
            log.info("ashby: no company slugs configured — skipping")
            return
        for entry in self.companies:
            yield from self._fetch_board(entry["slug"], entry.get("name", entry["slug"]))

    def _fetch_board(self, slug: str, display_name: str) -> Iterator[Posting]:
        data = self.http.post_json(
            API,
            {
                "operationName": "ApiJobBoardWithTeams",
                "query": LIST_QUERY,
                "variables": {"organizationHostedJobsPageName": slug},
            },
        )
        if data.get("errors"):
            raise ValueError(f"ashby board {slug!r}: GraphQL errors: {data['errors']}")

        board = (data.get("data") or {}).get("jobBoard")
        if board is None:
            raise ValueError(f"ashby board {slug!r}: no jobBoard in response (bad slug?)")

        postings = board.get("jobPostings") or []
        log.info("ashby: %s -> %d posting(s) (descriptions fetched on demand)", slug, len(postings))

        for job in postings:
            posting = self._to_posting(job, slug, display_name)
            if posting is not None:
                yield posting

    def _to_posting(self, job: dict[str, Any], slug: str, display_name: str) -> Posting | None:
        job_id = job.get("id")
        title = (job.get("title") or "").strip()
        if not job_id or not title:
            log.warning("ashby %s: dropping malformed job %r", slug, job)
            return None

        locations = [job.get("locationName")]
        locations += [
            (sec or {}).get("locationName") for sec in job.get("secondaryLocations") or []
        ]
        location = ", ".join(sorted({loc.strip() for loc in locations if loc and loc.strip()}))

        tier = job.get("compensationTierSummary")
        self._slug_by_id[job_id] = slug

        return Posting(
            source=SOURCE_NAME,
            external_id=f"{slug}:{job_id}",
            title=title,
            company=display_name,
            location=location,
            url=f"https://jobs.ashbyhq.com/{slug}/{job_id}",
            description_raw=None,  # filled in by fetch_details()
            employment_type=normalise_employment_type(job.get("employmentType")),
            salary_text=expand_compensation(tier),
            raw={"board": slug, "ashby_id": job_id, "compensation_tier": tier},
        )

    def fetch_details(self, posting: Posting) -> None:
        """Second request: pull the description for a posting worth keeping.

        Called by discover.py after the cheap filters pass. Mutates `posting`.
        """
        job_id = posting.raw.get("ashby_id")
        slug = posting.raw.get("board")
        if not job_id or not slug:
            return

        data = self.http.post_json(
            API,
            {
                "operationName": "ApiJobPosting",
                "query": DETAIL_QUERY,
                "variables": {
                    "organizationHostedJobsPageName": slug,
                    "jobPostingId": job_id,
                },
            },
        )
        if data.get("errors"):
            raise ValueError(f"ashby detail {slug}/{job_id}: {data['errors']}")

        detail = (data.get("data") or {}).get("jobPosting")
        if not detail:
            log.warning("ashby: no detail returned for %s/%s", slug, job_id)
            return

        posting.description_raw = html_to_text(detail.get("descriptionHtml"))
        posting.posted_at = iso_date(detail.get("publishedDate")) or posting.posted_at
        posting.raw["department"] = detail.get("departmentName")
        posting.raw["teams"] = detail.get("teamNames")

        # Prefer the tier summary for pay; fall back to the body text.
        if not posting.salary_text:
            posting.salary_text = posting.description_raw
