"""Dedupe, filtering, expiry and the Indeed adapter's ID handling."""

import json

import pytest

from jobpipe import EXPIRE_AFTER_MISSES
from jobpipe.db import job_id as make_job_id
from jobpipe.db import utcnow
from jobpipe.discover import Stats, expire_stale, location_ok, parse_since, title_excluded, upsert
from jobpipe.salary import Salary
from jobpipe.sources.base import Posting, dedupe_key, html_to_text, iso_date
from jobpipe.sources.indeed import IndeedInboxSource

ALLOW = ["calgary", "airdrie", "okotoks", "alberta", ", ab", "remote", "canada"]
DENY = ["edmonton", "red deer"]


def posting(**kw):
    base = dict(
        source="indeed",
        external_id="abc123",
        title="Investigator",
        company="AMVIC",
        location="Calgary, AB",
    )
    base.update(kw)
    return Posting(**base)


class TestJobId:
    def test_is_stable(self):
        assert make_job_id("indeed", "abc") == make_job_id("indeed", "abc")

    def test_differs_by_source(self):
        assert make_job_id("indeed", "abc") != make_job_id("lever", "abc")

    def test_differs_by_external_id(self):
        assert make_job_id("indeed", "abc") != make_job_id("indeed", "abd")

    def test_no_delimiter_collision(self):
        """('a','bc') and ('ab','c') must not hash the same."""
        assert make_job_id("a", "bc") != make_job_id("ab", "c")


class TestIndeedUnstableIds:
    """Indeed hands out a fresh job_id per response for the same posting."""

    def test_same_posting_different_job_ids_dedupes(self, tmp_path):
        inbox = tmp_path / "data" / "inbox" / "indeed"
        inbox.mkdir(parents=True)

        # The AMVIC posting exactly as it came back under two different queries.
        for name, jid, url in [
            ("a.json", "JOBSEARCH_31", "https://to.indeed.com/aadhk9c69vdw"),
            ("b.json", "JOBSEARCH_64", "https://to.indeed.com/aakgtngqmcfz"),
        ]:
            (inbox / name).write_text(
                json.dumps(
                    {
                        "query": "investigator",
                        "jobs": [
                            {
                                "job_id": jid,
                                "title": "Investigator",
                                "company": "Alberta Motor Vehicle Industry Council (AMVIC)",
                                "location": "Calgary, AB",
                                "posted_on": "July 27, 2026",
                                "job_type": "Permanent",
                                "url": url,
                            }
                        ],
                    }
                )
            )

        src = IndeedInboxSource({"inbox_dir": "data/inbox/indeed"}, tmp_path)
        postings = list(src.fetch())
        assert len(postings) == 2
        # Different Indeed IDs, but one identity.
        assert postings[0].raw["indeed_job_id"] != postings[1].raw["indeed_job_id"]
        assert postings[0].external_id == postings[1].external_id

    def test_dedupe_key_normalises_noise(self):
        a = dedupe_key("AMVIC", "Investigator", "Calgary, AB")
        b = dedupe_key("  amvic ", "INVESTIGATOR", "calgary,  ab")
        assert a == b

    def test_dedupe_key_separates_real_differences(self):
        a = dedupe_key("Walmart", "Asset protection associate", "Okotoks, AB")
        b = dedupe_key("Walmart", "Asset protection associate", "Rocky View, AB")
        assert a != b

    def test_missing_inbox_is_a_warning_not_a_crash(self, tmp_path):
        src = IndeedInboxSource({"inbox_dir": "nope"}, tmp_path)
        assert list(src.fetch()) == []

    def test_malformed_harvest_file_raises_loudly(self, tmp_path):
        inbox = tmp_path / "in"
        inbox.mkdir()
        (inbox / "bad.json").write_text('{"no_jobs_key": true}')
        src = IndeedInboxSource({"inbox_dir": str(inbox)}, tmp_path)
        with pytest.raises(ValueError, match="jobs"):
            list(src.fetch())

    def test_compensation_na_falls_back_to_description(self, tmp_path):
        inbox = tmp_path / "in"
        inbox.mkdir()
        (inbox / "x.json").write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "job_id": "J1",
                            "title": "Surveillance Investigator",
                            "company": "Risk Control Canada",
                            "location": "Calgary, AB",
                            "compensation": "N/A",
                            "description": "Pay: $30.00-$45.00 per hour",
                        }
                    ]
                }
            )
        )
        src = IndeedInboxSource({"inbox_dir": str(inbox)}, tmp_path)
        p = list(src.fetch())[0]
        assert "30.00" in p.salary_text


class TestLocationFilter:
    @pytest.mark.parametrize(
        "loc", ["Calgary, AB", "Okotoks, AB", "Rocky View, AB", "Alberta", "Canada", "Remote"]
    )
    def test_allowed(self, loc):
        assert location_ok(loc, ALLOW, DENY)[0]

    @pytest.mark.parametrize("loc", ["Edmonton, AB", "Red Deer, AB"])
    def test_denied_beats_allowed(self, loc):
        """Edmonton contains no allow term but is explicitly denied; and a
        string like 'Edmonton, AB' matches ', ab' — deny must still win."""
        ok, why = location_ok(loc, ALLOW, DENY)
        assert not ok
        assert why.startswith("location_denied")

    @pytest.mark.parametrize("loc", ["Toronto, ON", "London, UK"])
    def test_out_of_area(self, loc):
        ok, why = location_ok(loc, ALLOW, DENY)
        assert not ok
        assert why == "location_out_of_area"

    def test_blank_location_is_kept_for_scoring(self):
        ok, why = location_ok("", ALLOW, DENY)
        assert ok and why == "no_location_given"


class TestTitleExclusion:
    def test_matches_security_guard(self):
        assert title_excluded("Security Guard - Calgary Only", ["security guard"]) == "security guard"

    def test_is_word_bounded(self):
        # "cashier" must not fire on a title that merely contains the letters.
        assert title_excluded("Head Cashiering Systems Analyst", ["cashier"]) is None

    def test_investigator_survives(self):
        assert title_excluded("Investigator", ["security guard", "cashier"]) is None

    def test_empty_pattern_list(self):
        assert title_excluded("Anything", []) is None


class TestSince:
    def test_relative_spans(self):
        assert parse_since("7d") < utcnow()
        assert parse_since("36h") < utcnow()
        assert parse_since("2w") < parse_since("7d")

    def test_iso_date(self):
        assert parse_since("2026-01-01").startswith("2026-01-01")

    def test_none(self):
        assert parse_since(None) is None

    def test_garbage_raises(self):
        from jobpipe.config import ConfigError

        with pytest.raises(ConfigError):
            parse_since("last tuesday")


class TestUpsert:
    def test_insert_then_update_does_not_duplicate(self, conn):
        stats = Stats()
        p = posting()
        assert upsert(conn, p, Salary(), run_id=1, stats=stats, dry_run=False) == "inserted"
        assert upsert(conn, p, Salary(), run_id=1, stats=stats, dry_run=False) == "updated"
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert stats.inserted == 1 and stats.updated == 1

    def test_repeat_resets_miss_streak(self, conn):
        stats = Stats()
        p = posting()
        upsert(conn, p, Salary(), run_id=1, stats=stats, dry_run=False)
        jid = make_job_id(p.source, p.external_id)
        conn.execute("UPDATE jobs SET miss_streak=4 WHERE id=?", (jid,))
        upsert(conn, p, Salary(), run_id=2, stats=stats, dry_run=False)
        assert conn.execute("SELECT miss_streak FROM jobs WHERE id=?", (jid,)).fetchone()[0] == 0

    def test_update_preserves_pipeline_state(self, conn):
        stats = Stats()
        p = posting()
        upsert(conn, p, Salary(), run_id=1, stats=stats, dry_run=False)
        jid = make_job_id(p.source, p.external_id)
        conn.execute("UPDATE jobs SET status='drafted', score=82 WHERE id=?", (jid,))
        upsert(conn, p, Salary(), run_id=2, stats=stats, dry_run=False)
        row = conn.execute("SELECT status, score FROM jobs WHERE id=?", (jid,)).fetchone()
        assert row["status"] == "drafted" and row["score"] == 82

    def test_reappearing_expired_job_is_revived(self, conn):
        stats = Stats()
        p = posting()
        upsert(conn, p, Salary(), run_id=1, stats=stats, dry_run=False)
        jid = make_job_id(p.source, p.external_id)
        conn.execute("UPDATE jobs SET status='expired' WHERE id=?", (jid,))
        upsert(conn, p, Salary(), run_id=2, stats=stats, dry_run=False)
        assert conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "new"

    def test_dry_run_writes_nothing(self, conn):
        stats = Stats()
        upsert(conn, posting(), Salary(), run_id=1, stats=stats, dry_run=True)
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert stats.inserted == 1  # counted, but not written

    def test_existing_description_is_not_clobbered_by_null(self, conn):
        stats = Stats()
        upsert(
            conn,
            posting(description_raw="full text"),
            Salary(),
            run_id=1,
            stats=stats,
            dry_run=False,
        )
        upsert(conn, posting(description_raw=None), Salary(), run_id=2, stats=stats, dry_run=False)
        jid = make_job_id("indeed", "abc123")
        got = conn.execute("SELECT description_raw FROM jobs WHERE id=?", (jid,)).fetchone()[0]
        assert got == "full text"


class TestExpiry:
    def _seed(self, conn):
        stats = Stats()
        upsert(conn, posting(external_id="a"), Salary(), run_id=1, stats=stats, dry_run=False)
        return make_job_id("indeed", "a")

    def test_streak_builds_then_expires(self, conn):
        jid = self._seed(conn)
        stats = Stats()
        for _ in range(EXPIRE_AFTER_MISSES - 1):
            expire_stale(
                conn, run_id=1, healthy_sources=["indeed"], seen_ids=set(), stats=stats, dry_run=False
            )
        row = conn.execute("SELECT status, miss_streak FROM jobs WHERE id=?", (jid,)).fetchone()
        assert row["status"] == "new"
        assert row["miss_streak"] == EXPIRE_AFTER_MISSES - 1

        expire_stale(
            conn, run_id=1, healthy_sources=["indeed"], seen_ids=set(), stats=stats, dry_run=False
        )
        assert conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "expired"

    def test_seen_job_never_expires(self, conn):
        jid = self._seed(conn)
        stats = Stats()
        for _ in range(EXPIRE_AFTER_MISSES + 3):
            expire_stale(
                conn, run_id=1, healthy_sources=["indeed"], seen_ids={jid}, stats=stats, dry_run=False
            )
        assert conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "new"

    def test_unhealthy_source_does_not_expire_its_jobs(self, conn):
        """If Greenhouse is down, Greenhouse jobs must not age out."""
        jid = self._seed(conn)
        stats = Stats()
        for _ in range(EXPIRE_AFTER_MISSES + 3):
            expire_stale(
                conn, run_id=1, healthy_sources=["greenhouse"], seen_ids=set(), stats=stats, dry_run=False
            )
        row = conn.execute("SELECT status, miss_streak FROM jobs WHERE id=?", (jid,)).fetchone()
        assert row["status"] == "new" and row["miss_streak"] == 0

    def test_approved_job_never_expires(self, conn):
        jid = self._seed(conn)
        conn.execute(
            "UPDATE jobs SET status='approved', approved_at=?, approved_by='addison' WHERE id=?",
            (utcnow(), jid),
        )
        stats = Stats()
        for _ in range(EXPIRE_AFTER_MISSES + 3):
            expire_stale(
                conn, run_id=1, healthy_sources=["indeed"], seen_ids=set(), stats=stats, dry_run=False
            )
        assert conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "approved"

    def test_past_closing_date_expires_immediately(self, conn):
        stats = Stats()
        upsert(
            conn,
            posting(external_id="closing", closes_at="2020-01-01T00:00:00+00:00"),
            Salary(),
            run_id=1,
            stats=stats,
            dry_run=False,
        )
        jid = make_job_id("indeed", "closing")
        expire_stale(
            conn, run_id=1, healthy_sources=["indeed"], seen_ids={jid}, stats=stats, dry_run=False
        )
        assert conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "expired"


class TestNormalisation:
    def test_iso_date_from_indeed_format(self):
        assert iso_date("July 27, 2026").startswith("2026-07-27")

    def test_iso_date_from_epoch_millis(self):
        assert iso_date(1754352000000).startswith("2025-")

    def test_iso_date_passthrough(self):
        assert iso_date("2026-08-14T12:00:00+00:00").startswith("2026-08-14")

    def test_iso_date_garbage(self):
        assert iso_date("sometime soon") is None
        assert iso_date(None) is None

    def test_html_to_text(self):
        html = "<p>Duties:</p><ul><li>Investigate&nbsp;fraud</li><li>Write reports</li></ul>"
        text = html_to_text(html)
        assert "Investigate fraud" in text
        assert "<" not in text

    def test_html_to_text_strips_script(self):
        assert "alert" not in (html_to_text("<script>alert(1)</script><p>Hi</p>") or "")

    def test_posting_requires_a_title(self):
        with pytest.raises(ValueError, match="title"):
            Posting(source="indeed", external_id="x", title="", company="c", location="l")


class TestAshbyCompensation:
    """Ashby ships pay as a compact tier summary, not a sentence."""

    def test_annual_k_range(self):
        from jobpipe.salary import parse as parse_salary
        from jobpipe.sources.ashby import expand_compensation

        text = expand_compensation("$342K – $555K • Offers Equity")
        s = parse_salary(text)
        assert s.annual_min == pytest.approx(342_000)
        assert s.annual_max == pytest.approx(555_000)

    def test_canadian_prefix(self):
        from jobpipe.salary import parse as parse_salary
        from jobpipe.sources.ashby import expand_compensation

        s = parse_salary(expand_compensation("CA$70K – CA$90K"))
        assert s.annual_min == pytest.approx(70_000)
        assert s.annual_max == pytest.approx(90_000)

    def test_hourly_tier(self):
        from jobpipe.salary import parse as parse_salary
        from jobpipe.sources.ashby import expand_compensation

        s = parse_salary(expand_compensation("$25 – $30 / hr"))
        assert s.period == "hourly"
        assert s.annual_min == pytest.approx(52_000)

    def test_single_amount(self):
        from jobpipe.salary import parse as parse_salary
        from jobpipe.sources.ashby import expand_compensation

        s = parse_salary(expand_compensation("$80K"))
        assert s.annual_min == pytest.approx(80_000)

    def test_no_dollar_figure(self):
        from jobpipe.sources.ashby import expand_compensation

        assert expand_compensation("Offers Equity") is None
        assert expand_compensation(None) is None
