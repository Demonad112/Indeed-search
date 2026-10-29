"""The drafting contract and the anti-fabrication check.

The check is the point of this file. "Never invent experience, certifications,
or dates" is only a real guarantee if something other than the model verifies
it, so these tests hammer the verifier with drafts that lie in the specific
ways that would actually hurt in an interview.
"""

import pytest

from jobpipe.draft import Draft, DraftError, check_fabrication, validate
from jobpipe.prompts import BANNED_OPENERS, DRAFT_SCHEMA

MASTER = """# Addison Denholm

### Loss Prevention Officer — London Drugs, Calgary, AB
**November 2023 – present**
- Detained and arrested suspects for theft, fraud, and mischief.
- Conducted internal investigations against staff when required.

### Security — Nashville North Tent, Calgary Stampede
**July 2018 – present**
- Manage a security team of 30 personnel.

### Loss Prevention — Save-On-Foods, Victoria, BC
**May 2021 – October 2021**
- Monitored CCTV systems for the Victoria Police Department.

### IT Specialist — The Net Group, Calgary, AB
**July 2014 – November 2020**
- Active Directory and Office 365; Windows Server 2008-2019; DNS, DHCP.
"""

CRITERIA = {
    "certifications_lacking": [
        "Alberta Private Investigator licence",
        "Peace Officer designation / Alberta Peace Officer Act training",
        "CompTIA A+",
        "post-secondary degree",
    ],
    "certifications_held": [
        {"name": "Emergency Medical Responder (EMR)", "issuer": "JIBC", "obtained": "2021-11"},
        {
            "name": "Security Services Individual Licence (handcuff)",
            "issuer": "Alberta Security Services",
            "valid": "2022-05 to 2024-05",
            "note": "EXPIRED — do not present as current",
        },
    ],
}


def draft(resume="", cover="", opening="Your posting asks for court-ready files."):
    return Draft(
        resume=resume or MASTER,
        cover=cover or "Your posting asks for court-ready files. I write them weekly.",
        gaps=[],
        emphasis=[],
        opening_line=opening,
    )


def payload(**over):
    base = {
        "resume_markdown": MASTER,
        "cover_letter_markdown": "Your posting asks for court-ready files. " * 12,
        "gaps": ["CompTIA A+"],
        "emphasis": ["led with the investigative bullets"],
        "opening_line": "Your posting asks for court-ready files.",
    }
    base.update(over)
    return base


class TestSchema:
    def test_forbids_extra_properties(self):
        assert DRAFT_SCHEMA["additionalProperties"] is False

    def test_every_property_required(self):
        assert set(DRAFT_SCHEMA["required"]) == set(DRAFT_SCHEMA["properties"])


class TestValidate:
    def test_accepts_a_good_payload(self):
        d = validate(payload())
        assert d.gaps == ["CompTIA A+"]

    @pytest.mark.parametrize("key", list(payload().keys()))
    def test_missing_key_rejected(self, key):
        body = payload()
        del body[key]
        with pytest.raises(DraftError, match="missing required key"):
            validate(body)

    def test_short_resume_rejected(self):
        with pytest.raises(DraftError, match="implausibly short"):
            validate(payload(resume_markdown="too short"))

    def test_short_cover_rejected(self):
        with pytest.raises(DraftError, match="implausibly short"):
            validate(payload(cover_letter_markdown="hi"))

    def test_empty_opening_rejected(self):
        with pytest.raises(DraftError, match="opening_line"):
            validate(payload(opening_line="  "))

    def test_gaps_must_be_strings(self):
        with pytest.raises(DraftError, match="gaps must be an array of strings"):
            validate(payload(gaps=[{"cert": "A+"}]))


class TestFabricationCheck:
    """The drafts that would actually cause damage."""

    def test_clean_draft_passes(self):
        assert check_fabrication(draft(), CRITERIA, MASTER) == []

    def test_claiming_a_certification_not_held(self):
        w = check_fabrication(
            draft(cover="I hold CompTIA A+ and maintain it annually."), CRITERIA, MASTER
        )
        assert any("CompTIA A+" in x for x in w)

    def test_claiming_a_licence_not_held(self):
        w = check_fabrication(
            draft(resume=MASTER + "\n- Alberta Private Investigator licence, current."),
            CRITERIA,
            MASTER,
        )
        assert any("Private Investigator" in x for x in w)

    @pytest.mark.parametrize(
        "sentence",
        [
            "I do not hold CompTIA A+ but have six years of hands-on Windows Server work.",
            "I am working toward CompTIA A+.",
            "CompTIA A+ is not something I have yet to complete.",
            "I would need to obtain CompTIA A+ for this role.",
        ],
    )
    def test_honest_disclaimers_do_not_trip_it(self, sentence):
        """Naming a gap honestly is exactly what we asked for — don't punish it."""
        w = check_fabrication(draft(cover=sentence), CRITERIA, MASTER)
        assert not any("CompTIA" in x for x in w), w

    def test_expired_cert_presented_as_current(self):
        w = check_fabrication(
            draft(resume=MASTER + "\n- Security Services Individual Licence (handcuff)"),
            CRITERIA,
            MASTER,
        )
        assert any("expired" in x.lower() for x in w)

    def test_expired_cert_properly_qualified(self):
        w = check_fabrication(
            draft(resume=MASTER + "\n- Security Services Individual Licence — expired 2024."),
            CRITERIA,
            MASTER,
        )
        assert not any("expired certification" in x for x in w)

    def test_invented_year(self):
        w = check_fabrication(
            draft(resume=MASTER + "\n### Investigator — Acme Corp\n**2012 – 2013**"),
            CRITERIA,
            MASTER,
        )
        assert any("2012" in x for x in w)

    def test_years_from_the_master_are_fine(self):
        w = check_fabrication(
            draft(resume=MASTER + "\nSix years at The Net Group, 2014 to 2020."), CRITERIA, MASTER
        )
        assert not any("not present in the master" in x for x in w)

    @pytest.mark.parametrize("banned", BANNED_OPENERS[:5])
    def test_banned_openers_are_caught(self, banned):
        w = check_fabrication(
            draft(opening=f"{banned.title()} the Investigator role."), CRITERIA, MASTER
        )
        assert any("banned phrase" in x for x in w)

    def test_a_good_opener_passes(self):
        w = check_fabrication(
            draft(opening="Your posting wants five years of investigative work from any sector."),
            CRITERIA,
            MASTER,
        )
        assert not any("banned" in x for x in w)

    def test_multiple_problems_all_reported(self):
        w = check_fabrication(
            draft(
                resume=MASTER + "\n- CompTIA A+ certified\n**2011 – 2012** Acme",
                opening="I am writing to express my interest in this role.",
            ),
            CRITERIA,
            MASTER,
        )
        assert len(w) >= 3
