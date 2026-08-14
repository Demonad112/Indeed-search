"""The scoring JSON contract — one of the two things the spec says must not break.

Structured outputs make a malformed response unlikely, but these tests assume it
happens anyway: every field is validated, the score range is checked (JSON Schema
cannot express numeric bounds), and the two scoring caps are enforced in Python
rather than trusted to the model.

No network. The API client is replaced with a stub.
"""

import json
from datetime import date
from types import SimpleNamespace

import pytest

from jobpipe.prompts import FLAGS, SCORE_SCHEMA, system_prompt
from jobpipe.score import (
    EXCLUDE_CAP,
    MUST_HAVE_CAP,
    ScoreError,
    Scorer,
    Stats,
    add_deadline_flag,
    apply_caps,
    validate,
)


def payload(**overrides):
    base = {
        "score": 82,
        "rationale": "Regulatory investigator role; the transferable-experience clause fits.",
        "matched_signals": ["investigative case management", "report writing for court"],
        "missing_requirements": ["Peace Officer designation"],
        "must_have_misses": [],
        "flags": ["strong_match"],
        "instant_reject": False,
        "instant_reject_reason": "",
    }
    base.update(overrides)
    return base


def fake_response(obj, *, stop_reason="end_turn"):
    text = obj if isinstance(obj, str) else json.dumps(obj)
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        stop_details=None,
    )


class StubClient:
    """Returns queued responses; records the kwargs it was called with."""

    def __init__(self, *responses):
        self._queue = list(responses)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._queue:
            raise AssertionError("stub called more times than expected")
        result = self._queue.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture()
def criteria():
    from jobpipe.config import load_criteria

    return load_criteria()


@pytest.fixture()
def scorer(criteria):
    return Scorer(criteria)


# ---------------------------------------------------------------------------


class TestSchema:
    def test_forbids_extra_properties(self):
        """additionalProperties: false is required by structured outputs."""
        assert SCORE_SCHEMA["additionalProperties"] is False

    def test_every_property_is_required(self):
        assert set(SCORE_SCHEMA["required"]) == set(SCORE_SCHEMA["properties"])

    def test_flags_are_a_closed_enum(self):
        assert SCORE_SCHEMA["properties"]["flags"]["items"]["enum"] == FLAGS

    def test_no_unsupported_numeric_constraints(self):
        """Structured outputs reject minimum/maximum — the range is checked in code."""
        score = SCORE_SCHEMA["properties"]["score"]
        assert "minimum" not in score and "maximum" not in score


class TestValidateAccepts:
    def test_a_good_payload(self):
        v = validate(payload())
        assert v.score == 82
        assert v.flags == ["strong_match"]
        assert not v.instant_reject

    def test_empty_arrays(self):
        v = validate(payload(matched_signals=[], missing_requirements=[], flags=[]))
        assert v.matched_signals == [] and v.flags == []

    def test_boundary_scores(self):
        assert validate(payload(score=0)).score == 0
        assert validate(payload(score=100)).score == 100

    def test_strips_and_dedupes_flags(self):
        v = validate(payload(flags=["strong_match", "strong_match", " closing_soon "]))
        assert v.flags == ["closing_soon", "strong_match"]

    def test_drops_blank_list_entries(self):
        v = validate(payload(matched_signals=["real", "  ", ""]))
        assert v.matched_signals == ["real"]


class TestValidateRejects:
    def test_not_an_object(self):
        with pytest.raises(ScoreError, match="expected a JSON object"):
            validate([1, 2, 3])

    @pytest.mark.parametrize(
        "key",
        ["score", "rationale", "matched_signals", "flags", "instant_reject", "must_have_misses"],
    )
    def test_missing_required_key(self, key):
        body = payload()
        del body[key]
        with pytest.raises(ScoreError, match="missing required key"):
            validate(body)

    @pytest.mark.parametrize("bad", [-1, 101, 1000])
    def test_score_out_of_range(self, bad):
        with pytest.raises(ScoreError, match="outside 0-100"):
            validate(payload(score=bad))

    @pytest.mark.parametrize("bad", ["82", 82.5, None])
    def test_score_wrong_type(self, bad):
        with pytest.raises(ScoreError, match="score must be an integer"):
            validate(payload(score=bad))

    def test_score_true_is_not_an_integer(self):
        """bool is an int subclass in Python — must be rejected explicitly."""
        with pytest.raises(ScoreError, match="score must be an integer"):
            validate(payload(score=True))

    @pytest.mark.parametrize("bad", ["", "   ", None, 5])
    def test_bad_rationale(self, bad):
        with pytest.raises(ScoreError, match="rationale"):
            validate(payload(rationale=bad))

    def test_unknown_flag(self):
        with pytest.raises(ScoreError, match="unknown flag"):
            validate(payload(flags=["vibes_are_off"]))

    def test_list_of_non_strings(self):
        with pytest.raises(ScoreError, match="matched_signals must be an array of strings"):
            validate(payload(matched_signals=[1, 2]))

    def test_instant_reject_not_boolean(self):
        with pytest.raises(ScoreError, match="instant_reject must be a boolean"):
            validate(payload(instant_reject="yes"))


class TestCaps:
    """Enforced in Python — a rule that matters is not left to the model."""

    def test_must_have_miss_caps_at_30(self):
        v = apply_caps(validate(payload(score=88, must_have_misses=["location: Toronto"])))
        assert v.score == MUST_HAVE_CAP
        assert v.model_score == 88
        assert "location: Toronto" in v.cap_reason

    def test_exclude_caps_at_9(self):
        v = apply_caps(
            validate(payload(score=61, instant_reject=True, instant_reject_reason="security guard"))
        )
        assert v.score == EXCLUDE_CAP
        assert v.model_score == 61

    def test_exclude_beats_must_have(self):
        v = apply_caps(
            validate(
                payload(
                    score=90,
                    instant_reject=True,
                    instant_reject_reason="cashier",
                    must_have_misses=["part-time"],
                )
            )
        )
        assert v.score == EXCLUDE_CAP

    def test_already_low_scores_are_untouched(self):
        v = apply_caps(validate(payload(score=12, must_have_misses=["part-time"])))
        assert v.score == 12 and v.model_score is None and v.cap_reason is None

    def test_clean_posting_is_untouched(self):
        v = apply_caps(validate(payload(score=82)))
        assert v.score == 82 and v.cap_reason is None

    def test_stats_are_counted(self):
        stats = Stats()
        apply_caps(validate(payload(score=90, must_have_misses=["x"])), stats)
        apply_caps(validate(payload(score=90, instant_reject=True)), stats)
        assert stats.capped_must_have == 1 and stats.capped_exclude == 1


class TestDeadlineFlag:
    TODAY = date(2026, 8, 14)

    def test_closing_within_a_week_is_flagged(self):
        v = add_deadline_flag(validate(payload()), "2026-08-18T23:59:59+00:00", self.TODAY)
        assert "closing_soon" in v.flags

    def test_closing_today_is_flagged(self):
        v = add_deadline_flag(validate(payload()), "2026-08-14T23:59:59+00:00", self.TODAY)
        assert "closing_soon" in v.flags

    def test_far_off_is_not_flagged(self):
        v = add_deadline_flag(validate(payload()), "2026-12-01T23:59:59+00:00", self.TODAY)
        assert "closing_soon" not in v.flags

    def test_already_passed_is_not_flagged(self):
        v = add_deadline_flag(validate(payload()), "2026-01-01T23:59:59+00:00", self.TODAY)
        assert "closing_soon" not in v.flags

    def test_no_deadline(self):
        assert "closing_soon" not in add_deadline_flag(validate(payload()), None, self.TODAY).flags

    def test_garbage_deadline_does_not_crash(self):
        add_deadline_flag(validate(payload()), "whenever", self.TODAY)

    def test_not_duplicated(self):
        v = add_deadline_flag(
            validate(payload(flags=["closing_soon"])), "2026-08-15T23:59:59+00:00", self.TODAY
        )
        assert v.flags.count("closing_soon") == 1


class TestRequestShape:
    JOB = {
        "title": "Investigator",
        "company": "AMVIC",
        "location": "Calgary, AB",
        "description_raw": "Conduct investigations under the Consumer Protection Act.",
    }

    def test_uses_structured_outputs(self, scorer):
        kwargs = scorer._request_kwargs(self.JOB, "2026-08-14")
        fmt = kwargs["output_config"]["format"]
        assert fmt["type"] == "json_schema"
        assert fmt["schema"] is SCORE_SCHEMA

    def test_system_prompt_is_cached(self, scorer):
        """The system prompt is identical per posting, so it must carry a breakpoint."""
        kwargs = scorer._request_kwargs(self.JOB, "2026-08-14")
        assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}

    def test_posting_goes_after_the_breakpoint(self, scorer):
        """Volatile content in the user turn, never in the cached system prompt.

        The sentinel is a company that is NOT a calibration anchor — AMVIC and
        the City of Calgary legitimately appear in the system prompt as anchors.
        """
        job = {**self.JOB, "company": "Zeta Risk Consulting", "title": "Fraud Analyst"}
        kwargs = scorer._request_kwargs(job, "2026-08-14")
        assert "Zeta Risk Consulting" not in kwargs["system"][0]["text"]
        assert "Zeta Risk Consulting" in kwargs["messages"][0]["content"]

    def test_system_prompt_is_byte_stable(self, scorer):
        """Any per-request content in the system prompt would defeat caching."""
        a = scorer._request_kwargs(self.JOB, "2026-08-14")["system"][0]["text"]
        b = scorer._request_kwargs({**self.JOB, "title": "Other"}, "2026-09-01")["system"][0]["text"]
        assert a == b

    def test_max_tokens_leaves_room_for_thinking(self, scorer):
        kwargs = scorer._request_kwargs(self.JOB, "2026-08-14")
        if kwargs["thinking"]["type"] == "adaptive":
            assert kwargs["max_tokens"] >= 4000

    def test_model_supports_structured_outputs(self, scorer):
        """claude-sonnet-4-6 does not — guard against it being set by mistake."""
        assert scorer.model in {
            "claude-sonnet-5",
            "claude-opus-5",
            "claude-opus-4-8",
            "claude-haiku-4-5",
            "claude-fable-5",
        }, f"{scorer.model} does not support structured outputs"


class TestRetry:
    JOB = {"title": "Investigator", "company": "AMVIC", "description_raw": "..."}

    def test_good_response_first_time_makes_one_call(self, scorer):
        scorer._client = StubClient(fake_response(payload()))
        assert scorer.score_job(self.JOB, today="2026-08-14").score == 82
        assert len(scorer._client.calls) == 1

    def test_retries_once_on_bad_json(self, scorer):
        scorer._client = StubClient(fake_response("not json at all"), fake_response(payload()))
        assert scorer.score_job(self.JOB, today="2026-08-14").score == 82
        assert len(scorer._client.calls) == 2

    def test_retries_once_on_contract_violation(self, scorer):
        scorer._client = StubClient(
            fake_response(payload(score=500)), fake_response(payload(score=44))
        )
        assert scorer.score_job(self.JOB, today="2026-08-14").score == 44

    def test_retry_shows_the_model_its_own_output(self, scorer):
        scorer._client = StubClient(fake_response("garbage"), fake_response(payload()))
        scorer.score_job(self.JOB, today="2026-08-14")
        retry_messages = scorer._client.calls[1]["messages"]
        assert retry_messages[1]["role"] == "assistant"
        assert "garbage" in retry_messages[1]["content"]
        assert "rejected" in retry_messages[2]["content"]

    def test_gives_up_after_two_attempts(self, scorer):
        scorer._client = StubClient(fake_response("bad"), fake_response("also bad"))
        with pytest.raises(ScoreError, match="failed the JSON contract twice"):
            scorer.score_job(self.JOB, today="2026-08-14")
        assert len(scorer._client.calls) == 2

    def test_api_failure_does_not_consume_the_retry(self, scorer):
        """Transport errors are the SDK's job; don't burn the contract retry."""
        scorer._client = StubClient(RuntimeError("connection reset"))
        with pytest.raises(ScoreError, match="API call failed"):
            scorer.score_job(self.JOB, today="2026-08-14")
        assert len(scorer._client.calls) == 1

    def test_refusal_is_surfaced(self, scorer):
        scorer._client = StubClient(fake_response(payload(), stop_reason="refusal"))
        with pytest.raises(ScoreError, match="declined"):
            scorer.score_job(self.JOB, today="2026-08-14")

    def test_max_tokens_truncation_is_actionable(self, scorer):
        scorer._client = StubClient(fake_response(payload(), stop_reason="max_tokens"))
        with pytest.raises(ScoreError, match="max_tokens"):
            scorer.score_job(self.JOB, today="2026-08-14")

    def test_empty_response_is_rejected(self, scorer):
        scorer._client = StubClient(
            SimpleNamespace(content=[], stop_reason="end_turn", stop_details=None),
            SimpleNamespace(content=[], stop_reason="end_turn", stop_details=None),
        )
        with pytest.raises(ScoreError):
            scorer.score_job(self.JOB, today="2026-08-14")


class TestSystemPrompt:
    def test_contains_the_calibration_anchors(self, criteria):
        text = system_prompt(criteria, 41_600.0)
        assert "82" in text and "78" in text and "12" in text
        assert "AMVIC" in text and "Digital Evidence" in text

    def test_states_the_pay_floor(self, criteria):
        assert "41,600" in system_prompt(criteria, 41_600.0)

    def test_lists_certifications_not_held(self, criteria):
        text = system_prompt(criteria, 41_600.0)
        assert "CompTIA A+" in text
        assert "Private Investigator" in text

    def test_states_the_must_have_cap(self, criteria):
        assert "caps the score at 30" in system_prompt(criteria, 41_600.0)

    def test_handles_a_null_floor(self, criteria):
        assert "not set" in system_prompt(criteria, None)
