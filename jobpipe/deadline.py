"""Application-deadline extraction.

`closes_at` drives expiry in Phase 1 and the `closing_soon` flag in Phase 2, but
almost no source exposes it as a field — employers bury it in the body text:

    Apply By: August 18, 2026                               (City of Calgary)
    Closing Date: August 28, 2026                           (Government of Alberta)
    Applications will be accepted until Friday, August 14, 2026   (AUC)

So we go looking for it. Conservative by design: a wrong deadline silently
expires a live posting, so an unrecognised phrasing yields None rather than a
guess.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

# Phrases that actually introduce a closing date. "Posted on" and "start date"
# are deliberately absent.
_LABELS = (
    r"appl(?:y|ications?)\s+(?:by|before|close[sd]?(?:\s+on)?|will\s+be\s+accepted\s+until|"
    r"are\s+accepted\s+until|accepted\s+until|must\s+be\s+received\s+by)"
    r"|closing\s+date"
    r"|close[sd]?\s+on"
    r"|application\s+deadline"
    r"|deadline(?:\s+for\s+applications?)?"
    r"|competition\s+closes"
    r"|posting\s+closes"
)

_MONTH = (
    r"(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
)

# "Friday, August 14, 2026" / "August 18, 2026" / "18 August 2026"
_DATE = (
    rf"(?:(?:Mon|Tues?|Wed(?:nes)?|Thur?s?|Fri|Satur?|Sun)(?:day)?,?\s+)?"
    rf"(?:{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
    rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH},?\s+\d{{4}}"
    rf"|\d{{4}}-\d{{2}}-\d{{2}})"
)

# Postings arrive with markdown emphasis baked into the text
# ("accepted until *Friday, August 14, 2026.*"), so tolerate stray * and _
# between the label and the date.
_RE = re.compile(rf"(?:{_LABELS})\s*[:\-–]?\s*[*_]*\s*({_DATE})", re.I)

_FORMATS = (
    "%B %d, %Y",
    "%B %d %Y",
    "%b %d, %Y",
    "%b %d %Y",
    "%d %B %Y",
    "%d %b %Y",
    "%Y-%m-%d",
)


def _clean(raw: str) -> str:
    s = re.sub(
        r"^(?:Mon|Tues?|Wed(?:nes)?|Thur?s?|Fri|Satur?|Sun)(?:day)?,?\s+", "", raw.strip(), flags=re.I
    )
    s = re.sub(r"(\d{1,2})(?:st|nd|rd|th)", r"\1", s, flags=re.I)
    return s.replace("Sept ", "Sep ").strip().rstrip(".")


def extract(text: str | None) -> str | None:
    """Return an ISO-8601 closing datetime, or None if none is stated.

    The date is taken as end-of-day UTC so a posting closing today is not
    treated as already closed.
    """
    if not text:
        return None

    for match in _RE.finditer(text):
        cleaned = _clean(match.group(1))
        for fmt in _FORMATS:
            try:
                dt = datetime.strptime(cleaned, fmt)
            except ValueError:
                continue
            return (
                dt.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
                .isoformat(timespec="seconds")
            )
    return None
