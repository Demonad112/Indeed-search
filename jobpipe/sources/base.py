"""Shared adapter interface and the normalised Posting record."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Protocol

from ..net import Http


@dataclass
class Posting:
    """A job as it comes off a source, before it hits the database."""

    source: str
    external_id: str
    title: str
    company: str
    location: str
    url: str | None = None
    posted_at: str | None = None
    closes_at: str | None = None
    description_raw: str | None = None
    employment_type: str | None = None
    salary_text: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Fail loudly on adapters that produce junk rather than storing it.
        for name in ("source", "external_id", "title"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"Posting.{name} is required (source={self.source!r})")


class Source(Protocol):
    """Every adapter implements this."""

    name: str

    def fetch(self) -> Iterator[Posting]:
        ...


class HttpSource:
    """Base for adapters that talk to a network endpoint."""

    name = "http"

    def __init__(self, http: Http, config: dict[str, Any]) -> None:
        self.http = http
        self.config = config

    def fetch(self) -> Iterator[Posting]:  # pragma: no cover - interface only
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Normalisation helpers shared by adapters
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\xa0]+")
_NL_RE = re.compile(r"\n{3,}")

_ENTITIES = {
    "&nbsp;": " ",
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&apos;": "'",
    "&rsquo;": "’",
    "&ldquo;": "“",
    "&rdquo;": "”",
    "&ndash;": "–",
    "&mdash;": "—",
}


def html_to_text(html: str | None) -> str | None:
    """Flatten an ATS HTML description into readable plain text."""
    if not html:
        return None
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "\n- ", text)
    text = _TAG_RE.sub("", text)
    for ent, ch in _ENTITIES.items():
        text = text.replace(ent, ch)
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _NL_RE.sub("\n\n", text)
    return text.strip() or None


def iso_date(value: Any) -> str | None:
    """Best-effort date normalisation to ISO-8601. Returns None if unparseable."""
    if value in (None, "", 0):
        return None

    if isinstance(value, (int, float)):
        # Lever uses epoch milliseconds.
        seconds = value / 1000 if value > 10_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None

    s = str(value).strip()
    if not s:
        return None

    # ISO first.
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat(timespec="seconds")
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).isoformat(
                timespec="seconds"
            )
        except ValueError:
            continue
    return None


def normalise_employment_type(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().lower().replace("_", "-").replace(" ", "-")
    mapping = {
        "full-time": "full-time",
        "fulltime": "full-time",
        "part-time": "part-time",
        "parttime": "part-time",
        "permanent": "permanent",
        "temporary": "temporary",
        "temp": "temporary",
        "contract": "contract",
        "contractor": "contract",
        "casual": "casual",
        "internship": "internship",
        "intern": "internship",
        "seasonal": "seasonal",
        "volunteer": "volunteer",
    }
    return mapping.get(v, v)


def dedupe_key(company: str, title: str, location: str) -> str:
    """Stable identity for a posting when the source's own IDs are unreliable.

    Used by the Indeed adapter: Indeed's API hands out a fresh, per-response job_id
    for the same posting on every query, so its IDs cannot be a dedupe key.
    """
    def norm(s: str) -> str:
        s = (s or "").lower()
        s = re.sub(r"[^a-z0-9]+", " ", s)
        return " ".join(s.split())

    return f"{norm(company)}|{norm(title)}|{norm(location)}"
