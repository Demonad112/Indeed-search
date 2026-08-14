# jobpipe

Semi-autonomous job discovery and application pipeline for investigator /
loss-prevention / security-analyst roles in Calgary, Alberta.

Finds postings, scores them against a profile, drafts tailored materials, queues
them for one-click approval, and only then submits.

**Status: Phase 1 (discovery) is built and running. Phases 2–5 are not.**

| Phase | Module | State |
|---|---|---|
| 1 — discover | `jobpipe/discover.py` | ✅ built |
| 2 — score | `jobpipe/score.py` | not started |
| 3 — draft | `jobpipe/draft.py` | not started |
| 4 — review | `jobpipe/review.py` | not started |
| 5 — submit | `jobpipe/submit.py` | not started |

---

## Nothing submits without approval

This is the constraint everything else is built around, and it is enforced by the
**database**, not by application code. `jobpipe/db.py` installs SQLite triggers
that reject any attempt to mark a job `submitted` without both `approved_at` and
`approved_by` set — including a raw `UPDATE` typed into the `sqlite3` shell.

```
sqlite> UPDATE jobs SET status='submitted' WHERE id='...';
Runtime error: approval gate: a job cannot be marked submitted without both
approved_at and approved_by set. See constraint #1.
```

The approval record is also immutable once a job is submitted, so the audit
trail cannot be rewritten after the fact. `tests/test_approval_gate.py` verifies
all of this by hitting the database directly, bypassing every line of Python.

---

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv venv
uv pip install httpx feedparser PyYAML python-dotenv pytest

cp .env.example .env
$EDITOR .env            # JOBPIPE_CONTACT_EMAIL is required — discovery refuses to run without it
```

That email goes into the `User-Agent` on every outbound request so employers and
ATS operators can identify and contact you.

---

## Daily use

```bash
make daily              # discover, then show what is waiting
```

or directly:

```bash
python run.py discover              # pull everything
python run.py discover --since 7d   # only postings newer than a week
python run.py discover --dry-run    # fetch and report, write nothing
python run.py discover --source indeed
python run.py status                # what is in the database
python run.py probe --greenhouse <slug>   # test a board before adding it
```

Exit codes, for cron and GitHub Actions:

| Code | Meaning |
|---|---|
| 0 | all enabled sources succeeded |
| 2 | a source failed; the run still completed and wrote what it got |
| 3 | config error, nothing ran |
| 1 | unexpected fatal error |

---

## Sources

### Indeed — via the official MCP tool, not RSS

**The RSS feed in the original spec is dead.** `ca.indeed.com/rss` answers
HTTP 403 with a bot-challenge page. Scraping the HTML site is off the table, so
postings come through Indeed's official MCP search tool, which runs inside the
Claude Code session:

```
Claude session          →  Indeed MCP search_jobs / get_job_details
                        →  writes JSON to data/inbox/indeed/
python run.py discover  →  reads the inbox, normalises, dedupes into SQLite
                        →  archives consumed files to data/inbox/indeed/processed/
```

See [`docs/indeed_harvest.md`](docs/indeed_harvest.md) for the format and the
prompt to run a harvest. A real harvest from 2026-08-14 is committed under
`data/harvests/` so discovery can be replayed from a clean checkout:

```bash
cp data/harvests/2026-08-14/*.json data/inbox/indeed/
python run.py discover
```

### Public ATS boards

Greenhouse, Lever and Ashby adapters are built and verified against live
endpoints. Add slugs to `config/sources.yaml` and flip `enabled: true`. Check a
slug first:

```bash
python run.py probe --lever palantir
```

Ashby needs a second request per posting for its description, so `discover.py`
only spends that request on postings that survive the location and title filters.

### Government portals — blocked, awaiting a decision

Checked 2026-08-14 as the spec asked:

- **Government of Alberta** (`jobpostings.alberta.ca`) — SAP SuccessFactors,
  HTML only. No JSON or RSS endpoint found.
- **City of Calgary** (`calgary.ca/careers`) — HTML landing page; the underlying
  ATS host was unreachable from this environment.

Both would need an HTML parser, which the spec says to raise first. Not written.
In the meantime both employers' Calgary postings **do** come through the Indeed
harvest — the GoA "Vehicle Safety Investigator" and the City of Calgary "Calgary
Police Service Digital Evidence Technician" both arrived that way.

---

## What discovery actually does

1. **Fetch** from every enabled source. A source that fails is logged loudly,
   recorded in `events`, and does not stop the other sources.
2. **Filter** — `--since`, then a coarse location prefilter, then hard title
   exclusions from `criteria.yaml`. Every skip is written to the `skips` table
   with a reason.
3. **Enrich** — fetch the description for surviving postings on boards that
   withhold it from the list view.
4. **Parse** — salary normalised to an annual figure; application deadline
   extracted from the body text.
5. **Upsert** — dedupe on `sha256(source, external_id)`. Repeats refresh
   `last_seen_at` and reset the miss counter; they never insert a duplicate, and
   they never clobber pipeline state you have already reached.
6. **Expire** — a posting absent from a *healthy* source for 7 consecutive runs
   is marked `expired`, as is anything past its `closes_at`. Jobs you have
   approved or submitted never expire.

### Two things worth knowing

**Indeed's `job_id` is not stable.** The same posting comes back under a
different `job_id` *and* a different apply URL depending on which query surfaced
it — the AMVIC Investigator posting was `JOBSEARCH_31` under `investigator` and
`JOBSEARCH_64` under `regulatory investigator`. Dedupe therefore keys on a
normalised `company|title|location` hash, not Indeed's ID.

**Indeed's salary parser emits garbage.** The Alberta Utilities Commission
posting advertises `Pay: $1.00-$2.00 per year`. Annualised figures outside
$15k–$1.5M are discarded and the reason recorded in `salary_note`, rather than
stored as a real $1/yr salary.

---

## Data model

One `jobs` table, plus `events` (full audit trail of every state transition),
`runs` (one row per discovery run, which is what makes "7 consecutive runs"
countable), and `skips` (every rejected posting with its reason).

No ORM — open it and read it:

```bash
sqlite3 data/jobpipe.db "SELECT title, company, salary_annual_min, closes_at FROM jobs WHERE status='new' ORDER BY salary_annual_max DESC"
```

Status flow: `new → scored → drafted → approved → submitted`, with `rejected`
and `expired` as terminals. Enforced by a CHECK constraint.

---

## Configuration

| File | Purpose |
|---|---|
| `config/criteria.yaml` | targeting rules, pay floor, exclusions, your background |
| `config/sources.yaml` | feeds, board slugs, location filter |
| `config/profile/resume_master.md` | source of truth for Phase 3 — never invent beyond it |
| `config/profile/voice_sample.md` | writing samples so drafts sound like you |

The pay floor is currently **$20/hour** (`$41,600/yr` at 40h × 52w). Note this
is a low bar for the roles being targeted: all three calibration postings clear
it comfortably and the only Calgary postings it actually excludes so far are
guard roles at $16/hr. Raise it in `criteria.yaml` when you want it doing more
work.

---

## Tests

```bash
python -m pytest
```

104 tests. The two that matter most, per the spec, are the approval gate
(`tests/test_approval_gate.py`) and salary/dedupe correctness
(`tests/test_salary.py`, `tests/test_discover.py`) — the latter built from
strings taken verbatim from real Calgary postings, including the $1/yr one.

`tests/test_scoring_contract.py` for the Phase 2 JSON contract lands with Phase 2.

---

## Constraints this code is built to

1. **Never auto-submit.** Enforced by database triggers, not documentation.
2. **No Indeed HTML scraping.** RSS and public JSON only; Indeed comes via its
   official MCP tool.
3. **Playwright is for employer ATS forms only** — Phase 5, never for job boards.
4. **Rate limit everything.** 2s minimum per host plus jitter, enforced centrally
   in `jobpipe/net.py` so no adapter can forget. Configurable, but `config.py`
   refuses to start below the 2s floor.
5. **Fail loudly.** No silent `except: pass`. Every skipped posting is logged
   with a reason and stored in a queryable table.
