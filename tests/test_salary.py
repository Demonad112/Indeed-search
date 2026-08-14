"""Salary parsing, against strings taken verbatim from real Calgary postings."""

import pytest

from jobpipe.salary import annualise, detect_period, floor_to_annual
from jobpipe.salary import parse as parse_salary


class TestPeriodDetection:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Pay: $16.00 per hour", "hourly"),
            ("Compensation: Pay Grade 7 $36.60 - 48.97 per hour", "hourly"),
            ("Pay: From $80,331.00 per year", "yearly"),
            ("Salary: 3,477.50 to 4,334.98 bi-weekly", "biweekly"),
            ("$5,000 a month", "monthly"),
            ("$1,200/wk", "weekly"),
            ("$60,000 per annum", "yearly"),
        ],
    )
    def test_detects(self, text, expected):
        assert detect_period(text) == expected

    def test_biweekly_beats_weekly(self):
        # "bi-weekly" also matches the weekly pattern; the specific one must win.
        assert detect_period("paid bi-weekly") == "biweekly"

    def test_no_period_returns_none(self):
        assert detect_period("Job ID: 315101") is None


class TestRealPostings:
    def test_amvic_annual_single_figure(self):
        s = parse_salary("Pay: From $80,331.00 per year")
        assert s.period == "yearly"
        assert s.annual_min == pytest.approx(80_331)
        assert s.annual_max == pytest.approx(80_331)

    def test_calgary_police_hourly_range(self):
        s = parse_salary("Compensation: Pay Grade 7 $36.60 - 48.97 per hour")
        assert s.period == "hourly"
        assert s.min == pytest.approx(36.60)
        assert s.max == pytest.approx(48.97)
        # 36.60 * 40 * 52
        assert s.annual_min == pytest.approx(76_128)
        assert s.annual_max == pytest.approx(101_857.6)

    def test_security_guard_below_floor(self):
        s = parse_salary("Pay: $16.00 per hour")
        assert s.annual_max == pytest.approx(33_280)
        assert s.annual_max < floor_to_annual({"amount": 20, "period": "hourly"})

    def test_surveillance_investigator_range_clears_floor(self):
        s = parse_salary("Pay: $30.00-$45.00 per hour")
        assert s.annual_min == pytest.approx(62_400)
        assert s.annual_min > floor_to_annual({"amount": 20, "period": "hourly"})

    def test_government_of_alberta_biweekly(self):
        s = parse_salary("Salary: 3,477.50 to 4,334.98 bi-weekly ($90,726 - $113,142 /year)")
        assert s.found
        # Either reading is right; both must land near the stated annual band.
        assert 88_000 < s.annual_min < 92_000
        assert 110_000 < s.annual_max < 115_000


class TestGarbageRejection:
    def test_auc_one_dollar_per_year_is_discarded(self):
        """Real posting. Indeed's parser misread the employer's form."""
        s = parse_salary("Pay: $1.00-$2.00 per year")
        assert not s.found
        assert s.annual_min is None
        assert s.note is not None and "implausible" in s.note

    def test_absurdly_high_is_discarded(self):
        s = parse_salary("Pay: $99,999,999.00 per year")
        assert not s.found
        assert s.note is not None

    def test_no_salary_anywhere(self):
        s = parse_salary("We offer competitive compensation and great benefits.")
        assert not s.found
        assert s.note is None

    def test_none_input(self):
        assert not parse_salary(None).found

    def test_ignores_unrelated_numbers(self):
        """Job IDs and addresses must not be mistaken for pay."""
        body = "Location: 5111 47 Street N.E.\nJob ID: 315101\nApply By: August 18, 2026"
        assert not parse_salary(body).found

    def test_prefers_labelled_pay_line_over_stray_dollar_amounts(self):
        body = (
            "We saved clients $2,000,000 last year.\n"
            "Pay: $25.00-$40.00 per hour\n"
        )
        s = parse_salary(body)
        assert s.period == "hourly"
        assert s.max == pytest.approx(40.0)


class TestAnnualise:
    def test_hourly_uses_configured_week(self):
        assert annualise(20, "hourly", 40) == pytest.approx(41_600)
        # Calgary Police Service works a 35-hour week.
        assert annualise(36.60, "hourly", 35) == pytest.approx(66_612)

    def test_twenty_dollar_floor(self):
        assert floor_to_annual({"amount": 20, "period": "hourly"}) == pytest.approx(41_600)

    def test_null_floor(self):
        assert floor_to_annual(None) is None

    def test_unknown_period_raises(self):
        with pytest.raises(ValueError):
            annualise(100, "per fortnight")
