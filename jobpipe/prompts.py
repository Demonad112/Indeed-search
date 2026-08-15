"""Prompt construction and the scoring JSON contract.

Kept separate from score.py so the contract is one readable file you can diff
when calibration drifts.

Caching note: `system_prompt()` is byte-stable for a given criteria.yaml — it
contains no timestamps, no job text, no per-request IDs. That is deliberate:
it renders first, so it caches across every posting in a batch. The posting
itself goes in the user turn, after the cache breakpoint.
"""

from __future__ import annotations

import json
from typing import Any

# ---------------------------------------------------------------------------
# The JSON contract
# ---------------------------------------------------------------------------

# Flags the model may raise. A closed enum so downstream code can switch on it
# without defensive string matching.
FLAGS = [
    "closing_soon",
    "salary_below_floor",
    "salary_not_stated",
    "requires_cert_i_lack",
    "requires_licence_i_lack",
    "requires_degree",
    "requires_years_i_lack",
    "employment_type_mismatch",
    "location_outside_area",
    "contract_or_casual",
    "seniority_gap",
    "career_change_stretch",
    "strong_match",
    "description_missing",
]

# NOTE: JSON Schema numeric constraints (minimum/maximum) are NOT supported by
# structured outputs, so `score` cannot be bounded to 0-100 here. score.py
# validates and clamps the range itself.
SCORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "score": {
            "type": "integer",
            "description": "0-100 fit score. Be harsh; see the calibration anchors.",
        },
        "rationale": {
            "type": "string",
            "description": "2-3 sentences. Concrete and specific to this posting.",
        },
        "matched_signals": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Strong signals from criteria this posting genuinely hits.",
        },
        "missing_requirements": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Things the posting requires that the candidate does not have.",
        },
        "must_have_misses": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Hard filters missed (location, employment type, salary floor).",
        },
        "flags": {
            "type": "array",
            "items": {"type": "string", "enum": FLAGS},
        },
        "instant_reject": {
            "type": "boolean",
            "description": "True only if the posting matches an exclude rule.",
        },
        "instant_reject_reason": {
            "type": "string",
            "description": "The matched exclude term, or an empty string.",
        },
    },
    "required": [
        "score",
        "rationale",
        "matched_signals",
        "missing_requirements",
        "must_have_misses",
        "flags",
        "instant_reject",
        "instant_reject_reason",
    ],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Calibration anchors
# ---------------------------------------------------------------------------
# Real Calgary postings with the score each should land near. These go into the
# system prompt as worked examples — they are the single most effective control
# on score inflation, far more so than telling the model "do not inflate".

CALIBRATION_ANCHORS = [
    {
        "label": "AMVIC Investigator, Calgary, $80,331/yr, permanent full-time",
        "detail": (
            "Regulatory investigator for Alberta's automotive regulator. Wants 5 years "
            "investigative experience from any sector — law enforcement, regulatory "
            "compliance, or a combination. Court procedures, exhibit handling, formal "
            "statements, major case files. Requires a post-secondary diploma/degree or "
            "journeyman certification, and Peace Officer training to be completed."
        ),
        "score": 82,
        "why": (
            "Squarely the target role. 'Investigative experience from any sector' is "
            "written for exactly this candidate: LP detentions, arrests, internal "
            "investigations and court-ready reports all count. Pay clears the floor by "
            "a wide margin. Docked for the missing post-secondary credential and Peace "
            "Officer status — real gaps, but the posting treats them as trainable."
        ),
    },
    {
        "label": "Calgary Police Service Digital Evidence Technician, $36.60-48.97/hr",
        "detail": (
            "Extract data from computers and phones for digital evidence. Preserve data, "
            "write formal reports, support investigators, maintain evidentiary integrity "
            "for court. Requires a 1-year certificate in criminal justice / computer "
            "science plus 3 years related experience, and CompTIA A+ (required). "
            "Enhanced security clearance and polygraph."
        ),
        "score": 78,
        "why": (
            "Rare overlap of both career tracks — six years IT (AD, Windows Server, DNS, "
            "diagnostics) plus evidence handling and court disclosure from LP work. "
            "Held back, not disqualified, by the hard CompTIA A+ requirement and the "
            "certificate; the experience behind them is there."
        ),
    },
    {
        "label": "Bravo Security Services / 365 Patrol 'Security Guard', full-time, $16.00/hr",
        "detail": (
            "Patrol assigned areas, monitor CCTV, respond to alarms, write incident "
            "reports. Security Guard Licence required. Previous security experience "
            "preferred."
        ),
        "score": 12,
        "why": (
            "A guard post, not an investigator post. It is a sideways-and-down move from "
            "current LP work: no case ownership, no investigative mandate, and $16/hr is "
            "below the $20/hr floor. Matching keywords (CCTV, reports) do not make it a "
            "fit — this is exactly the posting the exclusions exist to catch."
        ),
    },
]


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _bullets(items: Any, indent: str = "  ") -> str:
    if not items:
        return f"{indent}(none specified)"
    out = []
    for item in items:
        if isinstance(item, dict):
            name = item.get("name", "")
            note = item.get("note")
            extra = f" [{note}]" if note else ""
            out.append(f"{indent}- {name}{extra}")
        else:
            out.append(f"{indent}- {item}")
    return "\n".join(out)


def system_prompt(criteria: dict[str, Any], floor_annual: float | None) -> str:
    """Build the scoring system prompt. Byte-stable for a given criteria.yaml."""
    must = criteria.get("must_have", {}) or {}
    exclude = criteria.get("exclude", {}) or {}

    floor_line = (
        f"${floor_annual:,.0f}/year equivalent "
        f"(${must['salary_floor']['amount']}/{must['salary_floor']['period'].rstrip('ly')})"
        if floor_annual
        else "not set"
    )

    anchors = "\n\n".join(
        f"POSTING: {a['label']}\n{a['detail']}\nCORRECT SCORE: {a['score']}\nWHY: {a['why']}"
        for a in CALIBRATION_ANCHORS
    )

    return f"""You score job postings for one specific candidate. You return JSON only.

# The candidate

Addison Denholm, Calgary AB. Currently a Loss Prevention Officer. Two career
tracks that rarely appear together:

Investigative / loss prevention (2018-present)
{_bullets(criteria.get("background", [])[:6])}

Technical (2014-2020, IT Specialist)
{_bullets(criteria.get("background", [])[6:])}

Certifications actually held:
{_bullets(criteria.get("certifications_held", []))}

Commonly required and NOT held — treat these as real gaps:
{_bullets(criteria.get("certifications_lacking", []))}

# Hard filters (must_have)

Location: {", ".join(must.get("location", []))}
Employment type: {", ".join(must.get("employment_type", []))}
Pay floor: {floor_line}

Any hard filter missed caps the score at 30. Record which in `must_have_misses`.

# Instant reject (exclude)

Titles: {", ".join(exclude.get("titles", []))}
Keywords in body: {", ".join(exclude.get("keywords", []))}

If the posting is one of these roles, set `instant_reject` true, name the term in
`instant_reject_reason`, and score it in single digits. Judge the actual role, not
the literal string — a "Security Analyst" in a SOC is not a security guard, and a
"Loss Prevention Investigator" is not excluded by the word "retail" appearing once.

# Strong signals (weighted positives)

{_bullets(criteria.get("strong_signals", []))}

# Calibration — match these anchors

These are real Calgary postings and the scores they must receive. Calibrate
against them. If your score for a new posting differs by more than ~10 from what
these anchors imply, you are miscalibrated.

{anchors}

# Scoring bands

85-100  Ideal. Investigative mandate, experience explicitly transferable, no blocking requirement.
70-84   Strong. Clear investigative or evidence work; one or two real but surmountable gaps.
50-69   Plausible stretch. Adjacent field, or a hard requirement that would take real time to meet.
30-49   Weak. Tangential, or a must_have missed.
10-29   Poor. Guard work, retail, or unrelated fields that merely share vocabulary.
0-9     Instant reject.

# Rules

- Do NOT inflate. A generic security guard posting is a 15, not a 60. Shared
  vocabulary ("surveillance", "reports", "CCTV") is not evidence of fit — ask
  whether the actual day-to-day work is investigative.
- Never credit a certification, licence, degree or year-count the candidate does
  not hold. If the posting requires one, it goes in `missing_requirements` and
  raises the matching flag.
- `rationale` is 2-3 sentences, specific to THIS posting. No boilerplate. Name
  the concrete thing that fits and the concrete thing that does not.
- `matched_signals` must quote or closely paraphrase something actually in the
  posting. If the posting text is missing or near-empty, raise
  `description_missing`, score conservatively from the title alone, and say so.
- Salary: if stated and below the floor, raise `salary_below_floor` and treat it
  as a must_have miss. If not stated at all, raise `salary_not_stated` — that is
  not a miss, just unknown.
- Contract, casual and part-time postings miss the full-time/permanent filter.
  Score them accordingly rather than pretending the mismatch away."""


def user_prompt(job: dict[str, Any], today: str) -> str:
    """The per-posting turn. Volatile — must sit after the cache breakpoint."""
    salary = "not stated"
    if job.get("salary_annual_min"):
        salary = f"${job['salary_annual_min']:,.0f}-${job['salary_annual_max']:,.0f}/yr equivalent"
        if job.get("salary_period") and job.get("salary_min"):
            salary += f" (posted as ${job['salary_min']}-${job['salary_max']} {job['salary_period']})"
    elif job.get("salary_note"):
        salary = f"not usable — {job['salary_note']}"

    description = (job.get("description_raw") or "").strip()
    if not description:
        description = "(no description was available from the source)"
    elif len(description) > 18000:
        description = description[:18000] + "\n\n[...truncated...]"

    closes = job.get("closes_at") or "not stated"

    return f"""Today is {today}.

TITLE: {job.get('title')}
COMPANY: {job.get('company')}
LOCATION: {job.get('location') or 'not stated'}
EMPLOYMENT TYPE: {job.get('employment_type') or 'not stated'}
PAY: {salary}
POSTED: {job.get('posted_at') or 'not stated'}
CLOSES: {closes}

DESCRIPTION:
{description}

Score this posting. Return only the JSON object."""


def contract_example() -> str:
    """A valid response, for tests and for the --dry-run output."""
    return json.dumps(
        {
            "score": 82,
            "rationale": "…",
            "matched_signals": ["…"],
            "missing_requirements": ["…"],
            "must_have_misses": [],
            "flags": ["strong_match"],
            "instant_reject": False,
            "instant_reject_reason": "",
        },
        indent=2,
    )
