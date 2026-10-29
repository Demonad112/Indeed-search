# jobpipe

Semi-autonomous job discovery and application pipeline for investigator /
loss-prevention / security-analyst roles in Calgary, Alberta.

Finds postings, scores them against a profile, drafts tailored materials, queues
them for one-click approval, and only then submits.

**Status: Phases 1–4 are built. Phase 5 is deliberately not.**

| Phase | Module | State |
|---|---|---|
| 1 — discover | `jobpipe/discover.py` | ✅ built |
| 2 — score | `jobpipe/score.py` | ✅ built |
| 3 — draft | `jobpipe/draft.py` | ✅ built |
| 4 — review | `jobpipe/review.py` | ✅ built |
| 5 — submit | `jobpipe/submit.py` | not built — see below |

## The daily loop

```bash
make daily        # discover → score → draft
make dashboard    # open http://127.0.0.1:8000 and triage
```

The dashboard is where you live. Postings arrive ranked, with a tailored resume
and cover letter already written, deadlines pinned to the top. You read, edit if
you want, and click **Approve & apply** — which records your approval, downloads
a zip of your resume, cover letter and an interview brief, and opens the
employer's application page in a new tab.

### Privacy

Your resume never leaves the machine.

- **Binds to `127.0.0.1`.** Not reachable from your network, let alone the
  internet. Passing `--host` anything else prints a warning first.
- **Passphrase on every page.** Set `JOBPIPE_PASSPHRASE` in `.env`; the server
  refuses to start without one. Session cookie is HttpOnly, SameSite=strict, and
  HMAC-signed — changing the passphrase invalidates every existing session.
- **Zero external requests.** No CDN, no fonts, no analytics, no HTMX from a
  CDN — the page is vanilla JS and a Content-Security-Policy of `default-src
  'self'` blocks anything else. Nothing can phone home with your resume in it.
- `noindex` on every response, `no-store` on anything carrying resume text, and
  a per-IP attempt limiter on the login.

### Why there is no auto-submit

Phase 5 would drive Playwright against employer ATS forms. I'd advise against it
and haven't built it: Workday and BambooHR forms break constantly, each employer
needs its own adapter, and the spec's own "pause and prompt me on any unexpected
field" rule means it saves little over pasting the files yourself. The database
triggers that gate `submitted` are still in place, so if you ever want it, the
safety rail is already there.

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
make setup
cp .env.example .env
$EDITOR .env
```

Three values matter:

| Variable | Needed by | Why |
|---|---|---|
| `JOBPIPE_CONTACT_EMAIL` | discover | Goes in the `User-Agent` so employers can identify and contact you. Discovery refuses to run without it. |
| `ANTHROPIC_API_KEY` | score, draft | Scoring and drafting call the API. |
| `JOBPIPE_PASSPHRASE` | serve | The dashboard will not start without one. |

---

## Daily use

```bash
make daily        # discover → score → draft
make dashboard    # triage what came back
```

or directly:

```bash
python run.py discover              # pull everything
python run.py discover --since 7d   # only postings newer than a week
python run.py discover --dry-run    # fetch and report, write nothing
python run.py discover --source indeed

python run.py score --calibrate     # check scoring against the 3 reference postings FIRST
python run.py score                 # score everything at status='new'
python run.py score --dry-run       # print the exact request without calling the API
python run.py score --limit 5       # score the 5 nearest-deadline postings
python run.py score --rescore       # re-score things that already have a score

python run.py draft                 # write resumes + cover letters above the threshold
python run.py draft --dry-run       # list what would be drafted
python run.py draft --job-id X --regenerate --feedback "less formal"

python run.py serve                 # the dashboard, localhost only
python run.py serve --port 9000

python run.py status                # what is in the database
python run.py probe --greenhouse <slug>   # test a board before adding it
```

Keyboard in the dashboard: `j`/`k` move, `o` open, `a` approve, `x` reject,
`r` regenerate, `s` save edits, `/` filter.

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

## Scoring (Phase 2)

One Claude call per posting, returning JSON constrained to a schema.

**The model changed from the spec, for a concrete reason.** The spec named
`claude-sonnet-4-6`, which does not support structured outputs. `claude-sonnet-5`
does, and is the current Sonnet. That difference matters: with
`output_config.format` the response is constrained to the schema at decode time,
so "instruct the model to return strict JSON and retry on a parse failure" stops
being the mechanism and becomes the backstop. The retry is still there — it just
almost never fires. Override with `scoring.model` in `criteria.yaml`.

**Two rules are enforced in Python, not asked of the model:**

| Rule | Cap |
|---|---|
| A `must_have` miss (location, employment type, pay floor) | 30 |
| An `exclude` match | 9 |

The model reports what it found; `score.py` decides what the number becomes. If
the model says 88 for a Toronto posting, the stored score is 30 and the event log
records both numbers and the reason.

**Calibrate before batch-running.** `python run.py score --calibrate` scores the
three reference postings from the spec and checks them against their anchors:

| Posting | Anchor |
|---|---|
| AMVIC Investigator, $80,331/yr | 82 |
| CPS Digital Evidence Technician, $36.60–48.97/hr | 78 |
| 365 Patrol "Security Guard", $16.00/hr | 12 |

Those anchors are also embedded in the system prompt as worked examples — that is
the thing that actually controls score inflation, far more than telling the model
not to inflate. Because they serve both roles, a calibration pass is necessary
but not sufficient: it shows the model follows its own anchors, not that the
anchors are right for a posting it has not seen.

**Cost shape.** The system prompt (criteria, background, anchors — ~6.5KB) is
byte-identical for every posting and renders before the messages, so it carries a
cache breakpoint and is written once per batch rather than per posting. The
posting itself sits in the user turn, after the breakpoint. A test asserts the
system prompt stays byte-stable, because a stray timestamp in it would silently
cost full price on every call.

## Drafting (Phase 3)

Reorders and rewords the master resume for each posting, and writes a cover
letter in your voice. Both stored as markdown, both editable in the dashboard.

**"Never invent experience, certifications, or dates" is checked, not trusted.**
Asking the model to confirm it didn't fabricate is worthless — a model that
fabricates will also fabricate the attestation. So `check_fabrication()` does it
deterministically:

- every credential in `certifications_lacking` is scanned for, and any mention
  that isn't clearly negated is flagged (so *"I do not hold CompTIA A+"* passes,
  *"I hold CompTIA A+"* does not)
- expired certifications presented as current are flagged
- four-digit years that don't appear in the master resume are flagged
- banned cover-letter openers are flagged

A flagged draft is stored with `draft_warnings` and shows up red in the
dashboard, with a confirmation prompt before you can approve it. It's a smoke
alarm, not a proof — it catches the fabrications that would embarrass you in an
interview, not every conceivable one.

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

238 tests, no network. The ones the spec calls out as costliest to get wrong:

- **`tests/test_approval_gate.py`** — hits the database directly with raw SQL,
  bypassing every line of Python, to prove the gate is structural.
- **`tests/test_scoring_contract.py`** — every field validated, the score range
  checked (JSON Schema cannot express numeric bounds, so it must be checked in
  code), both caps, the retry path, and the request shape including the cache
  breakpoint. The Anthropic client is stubbed.

- **`tests/test_draft.py`** — the anti-fabrication check, hammered with drafts
  that lie in the specific ways that would hurt: claiming a credential, dressing
  up an expired one, inventing a year. Includes the inverse — honest disclaimers
  must *not* trip it, or the check would punish exactly the behaviour we want.
- **`tests/test_review.py`** — the auth wall (every resume-bearing route 401s
  while signed out), cookie flags, forged and expired tokens, and that approving
  sets both gate fields while leaving `submitted_at` NULL.

Plus `tests/test_salary.py` and `tests/test_discover.py`, built from strings
taken verbatim from real Calgary postings — including the $1/yr one.

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
