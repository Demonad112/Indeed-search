"""Salary parsing and normalisation to an annual figure.

Job boards report pay in whatever unit the employer typed. To compare anything
against a floor we have to normalise. We also refuse to trust obvious garbage:
one real Calgary posting in this pipeline advertises "Pay: $1.00-$2.00 per year",
which is Indeed's parser misreading the employer's form. Numbers like that are
discarded and flagged rather than silently treated as a $1/yr salary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Hours/weeks used to annualise. Overridable via criteria.yaml.
DEFAULT_HOURS_PER_WEEK = 40.0
WEEKS_PER_YEAR = 52.0

PERIOD_MULTIPLIER = {
    "hourly": None,  # depends on hours_per_week
    "daily": 260.0,
    "weekly": 52.0,
    "biweekly": 26.0,
    "semimonthly": 24.0,
    "monthly": 12.0,
    "yearly": 1.0,
}

_PERIOD_WORDS = [
    (r"\b(?:per\s+hour|an?\s+hour|hourly|/\s*h(?:r|our)?\b|\bp/?h\b)", "hourly"),
    (r"\b(?:per\s+day|a\s+day|daily|/\s*day)", "daily"),
    (r"\b(?:per\s+week|a\s+week|weekly|/\s*w(?:k|eek)?\b)", "weekly"),
    (r"\b(?:bi-?weekly|every\s+two\s+weeks|per\s+two\s+weeks)", "biweekly"),
    (r"\b(?:semi-?monthly|twice\s+a\s+month)", "semimonthly"),
    (r"\b(?:per\s+month|a\s+month|monthly|/\s*mo(?:nth)?\b)", "monthly"),
    (r"\b(?:per\s+year|a\s+year|per\s+annum|annually|yearly|/\s*(?:yr|year)\b)", "yearly"),
]

# Money with optional $ and thousands separators: 80,331.00 / 36.60 / 113142
_MONEY = r"\$?\s*(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
# A range: "$36.60 - 48.97", "$30.00-$45.00", "3,477.50 to 4,334.98"
_RANGE_RE = re.compile(_MONEY + r"\s*(?:-|–|—|to)\s*" + _MONEY, re.I)
_SINGLE_RE = re.compile(_MONEY)

# Plausibility bounds once annualised (CAD). Anything outside is treated as a
# parse failure, not as a real offer.
MIN_PLAUSIBLE_ANNUAL = 15_000.0
MAX_PLAUSIBLE_ANNUAL = 1_500_000.0


@dataclass
class Salary:
    min: float | None = None
    max: float | None = None
    period: str | None = None
    annual_min: float | None = None
    annual_max: float | None = None
    note: str | None = None

    @property
    def found(self) -> bool:
        return self.annual_min is not None or self.annual_max is not None


def _to_float(s: str) -> float:
    return float(s.replace(",", "").strip())


def detect_period(text: str) -> str | None:
    lowered = text.lower()
    # Check biweekly/semimonthly before weekly/monthly so the more specific
    # phrase wins ("bi-weekly" also matches "weekly").
    order = ["biweekly", "semimonthly", "hourly", "daily", "weekly", "monthly", "yearly"]
    found = {}
    for pattern, name in _PERIOD_WORDS:
        m = re.search(pattern, lowered)
        if m:
            found[name] = m.start()
    for name in order:
        if name in found:
            return name
    return None


def annualise(amount: float, period: str, hours_per_week: float = DEFAULT_HOURS_PER_WEEK) -> float:
    if period == "hourly":
        return amount * hours_per_week * WEEKS_PER_YEAR
    mult = PERIOD_MULTIPLIER.get(period)
    if mult is None:
        raise ValueError(f"unknown salary period: {period!r}")
    return amount * mult


def parse(text: str | None, *, hours_per_week: float = DEFAULT_HOURS_PER_WEEK) -> Salary:
    """Pull a pay figure out of free text. Returns an empty Salary if none found."""
    if not text:
        return Salary()

    # Prefer an explicit "Pay:" / "Salary:" / "Compensation:" line — job bodies are
    # full of unrelated numbers (job IDs, addresses, years of experience).
    best: Salary | None = None
    rejected: Salary | None = None
    for line in _candidate_lines(text):
        cand = _parse_line(line, hours_per_week)
        if cand is None:
            continue
        if cand.found:
            # Keep the widest plausible range we saw.
            best = cand if best is None else _widen(best, cand)
        elif rejected is None:
            # A figure was present but implausible. Hold on to it so the reason
            # surfaces instead of looking like "no salary stated".
            rejected = cand
    return best or rejected or Salary()


def _candidate_lines(text: str) -> list[str]:
    labelled: list[str] = []
    other: list[str] = []
    for raw in re.split(r"[\r\n]+", text):
        line = raw.strip()
        if not line or "$" not in line and not re.search(r"\d", line):
            continue
        if re.search(r"\b(pay|salary|compensation|wage|rate|pay\s+grade)\b\s*[:\-]", line, re.I):
            labelled.append(line)
        elif "$" in line:
            other.append(line)
    # Labelled lines are far more reliable; only fall back to bare "$" lines.
    return labelled or other[:12]


def _parse_line(line: str, hours_per_week: float) -> Salary | None:
    period = detect_period(line)
    if period is None:
        return None

    m = _RANGE_RE.search(line)
    if m:
        lo, hi = _to_float(m.group(1)), _to_float(m.group(2))
        if lo > hi:
            lo, hi = hi, lo
    else:
        m2 = _SINGLE_RE.search(line)
        if not m2:
            return None
        lo = hi = _to_float(m2.group(1))

    a_lo = annualise(lo, period, hours_per_week)
    a_hi = annualise(hi, period, hours_per_week)

    if a_hi < MIN_PLAUSIBLE_ANNUAL or a_lo > MAX_PLAUSIBLE_ANNUAL:
        return Salary(
            min=lo,
            max=hi,
            period=period,
            note=(
                f"implausible pay parsed from {line.strip()[:80]!r} "
                f"(≈${a_lo:,.0f}-${a_hi:,.0f}/yr) — discarded, verify on the posting"
            ),
        )

    return Salary(
        min=lo,
        max=hi,
        period=period,
        annual_min=a_lo,
        annual_max=a_hi,
        note=None,
    )


def _widen(a: Salary, b: Salary) -> Salary:
    if not a.found:
        return b
    if not b.found:
        return a
    return a if (a.annual_max or 0) >= (b.annual_max or 0) else b


def floor_to_annual(floor: dict | None, hours_per_week: float = DEFAULT_HOURS_PER_WEEK) -> float | None:
    """Turn criteria.yaml's must_have.salary_floor into an annual number."""
    if not floor:
        return None
    return annualise(float(floor["amount"]), str(floor["period"]), hours_per_week)
