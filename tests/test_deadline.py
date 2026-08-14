"""Deadline extraction, against phrasings from real Calgary postings."""

import pytest

from jobpipe.deadline import extract


class TestRealPostings:
    def test_city_of_calgary_apply_by(self):
        body = "Audience: Internal/External\nApply By: August 18, 2026\nJob ID: 315101"
        assert extract(body).startswith("2026-08-18")

    def test_government_of_alberta_closing_date(self):
        body = "Scope: Open Competition\nClosing Date: August 28, 2026\nClassification: SSC 5"
        assert extract(body).startswith("2026-08-28")

    def test_auc_accepted_until_with_weekday(self):
        body = "Applications will be accepted until *Friday, August 14, 2026.*"
        assert extract(body).startswith("2026-08-14")

    def test_end_of_day_not_midnight(self):
        """A posting closing today must not read as already closed."""
        assert "23:59:59" in extract("Apply By: August 18, 2026")


class TestPhrasings:
    @pytest.mark.parametrize(
        "text",
        [
            "Application deadline: September 1, 2026",
            "Deadline: 1 September 2026",
            "Competition closes September 1, 2026",
            "Posting closes: 2026-09-01",
            "Applications close on September 1st, 2026",
            "Apply before September 1, 2026",
            "Closing Date - Sept 1, 2026",
        ],
    )
    def test_variants(self, text):
        assert extract(text).startswith("2026-09-01")


class TestConservatism:
    def test_posted_date_is_not_a_deadline(self):
        assert extract("Posted on: August 05, 2026") is None

    def test_start_date_is_not_a_deadline(self):
        assert extract("Anticipated start date: September 1, 2026") is None

    def test_bare_date_is_not_a_deadline(self):
        assert extract("Our office opened on August 18, 2026.") is None

    def test_unrecognised_phrasing_yields_none(self):
        assert extract("Get your application in sometime next month") is None

    def test_empty_and_none(self):
        assert extract(None) is None
        assert extract("") is None

    def test_no_crash_on_impossible_date(self):
        assert extract("Closing Date: February 31, 2026") is None

    def test_first_labelled_date_wins(self):
        body = "Apply By: August 18, 2026\nClosing Date: December 25, 2026"
        assert extract(body).startswith("2026-08-18")
