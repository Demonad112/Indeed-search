# Harvesting Indeed postings

## Why this is a manual-ish step

The original spec called for Indeed's RSS feed:

```
https://ca.indeed.com/rss?q={query}&l=Calgary%2C+AB&radius=50
```

That endpoint is retired. As of 2026-08-14 it answers:

```
HTTP 403 | text/html | 25,742 bytes   (bot-challenge page, not a feed)
```

Scraping the HTML site instead is off the table — constraint #2, and their bot
detection would flag the account. So the route we use is Indeed's **official MCP
job-search tool**, which is authenticated per-user and sanctioned.

That tool runs inside the Claude Code session, not inside this Python process.
Hence the split:

```
Claude session          →  Indeed MCP search_jobs / get_job_details
                        →  writes JSON to data/inbox/indeed/
python run.py discover  →  reads the inbox, normalises, dedupes into SQLite
                        →  archives consumed files to data/inbox/indeed/processed/
```

## Running a harvest

In a Claude Code session with the Indeed MCP server connected, ask for:

> Harvest Indeed for each query in `config/sources.yaml` under `indeed.queries`,
> location "Calgary, AB", country CA. For every Calgary-area result that isn't an
> obvious exclude, also call `get_job_details` to pull the full description.
> Write one file per query to `data/inbox/indeed/YYYY-MM-DD-<query-slug>.json`
> in the format in `docs/indeed_harvest.md`.

Then run `python run.py discover`.

## File format

One file per query. `jobs` may be empty — that is a valid result and is recorded.

```json
{
  "source": "indeed",
  "harvester": "Indeed MCP search_jobs + get_job_details",
  "query": "surveillance investigator",
  "location": "Calgary, AB",
  "country_code": "CA",
  "harvested_at": "2026-08-14T19:55:00+00:00",
  "jobs": [
    {
      "job_id": "JOBSEARCH_38",
      "title": "Surveillance Investigator",
      "company": "Risk Control Canada",
      "location": "Calgary, AB",
      "posted_on": "July 10, 2026",
      "job_type": "Casual",
      "compensation": "N/A",
      "url": "https://to.indeed.com/aasb9mbq7hbt",
      "description": "full text from get_job_details, or null"
    }
  ]
}
```

Field notes:

- `posted_on` — accepts `"July 10, 2026"`, ISO, or epoch. Normalised on ingest.
- `compensation` — Indeed's list view almost always says `"N/A"`. The real figure
  is inside the description body (`Pay: $30.00-$45.00 per hour`), which is where
  the salary parser looks when `compensation` is unusable.
- `description` — `null` is allowed but the posting is then much less useful to
  Phase 2. Pull details for anything Calgary-area and plausibly relevant.

## Two traps, both real

**1. Indeed's `job_id` is not stable.** The same posting comes back under a
different `job_id` *and* a different `to.indeed.com` short URL depending on which
query surfaced it. In the 2026-08-14 harvest the AMVIC Investigator posting was
`JOBSEARCH_31` under the `investigator` query and `JOBSEARCH_64` under
`regulatory investigator` — same job, same company, different IDs and links.

So `job_id` is a per-response sequence number, not an identifier. The adapter
keys on a normalised `company|title|location` hash instead
(`jobpipe/sources/base.py::dedupe_key`). The Indeed `job_id` and the latest apply
URL are kept in `raw_json` for reference.

**2. Indeed's salary parser emits garbage.** The Alberta Utilities Commission
posting in this harvest advertises `Pay: $1.00-$2.00 per year`. That is Indeed
misreading the employer's form, not the offer. `jobpipe/salary.py` discards
annualised figures outside $15k–$1.5M and writes the reason to `salary_note`
rather than recording a $1/yr salary.

## Reference snapshot

`data/harvests/2026-08-14/` holds the first real harvest, committed so discovery
can be replayed from a clean checkout:

```bash
cp data/harvests/2026-08-14/*.json data/inbox/indeed/
python run.py discover
```
