"""Part 3b, no database: the template file, filling, subject fitting, cutting unsupported claims."""

from types import SimpleNamespace

import pytest

from app.modules.m3_outreach import drafting as D
from app.modules.m3_outreach.template import TemplateError, fill, fit_subject, load_template, parse_template

MINI = """---
subject: "Application for {role_short} in {company} ({years} YoE)"
subject_max_chars: 60
greeting: "Hi {first_name},"
greeting_without_name: "Hi Hiring Team,"
---
{greeting}

About the {role} opening{req_ids} at {company}.

{{intro}}

{{points}}

Best regards,
{full_name}
Mobile: {mobile}

## intro
Who I am.
## points
Why I fit.
"""


def test_the_shipped_template_parses():
    t = load_template()
    assert {"intro", "points", "rules"} <= set(t.sections)
    assert "{{intro}}" in t.body and "{{points}}" in t.body and t.settings.attach_resume
    assert "gaps" in t.sections["rules"]   # owner decision: never mention gaps


def test_template_must_have_the_ai_parts():
    with pytest.raises(TemplateError):
        parse_template(MINI.replace("{{points}}", ""))


def test_fill_drops_lines_with_missing_private_details_but_not_inline_ones():
    t = parse_template(MINI)
    out = fill(t.body, {"role": "Data Analyst", "company": "Lilly", "req_ids": "", "full_name": "A B", "mobile": None})
    assert "Mobile:" not in out and "About the Data Analyst opening at Lilly." in out
    assert "{greeting}" in out and "{{intro}}" in out     # left for later steps


def test_subject_is_shortened_to_fit():
    t = parse_template(MINI)
    s = fit_subject(t, {"company": "Eli Lilly", "years": "3.5"}, "Data Analyst Consultant Senior Consultant Bengaluru")
    assert len(s) <= 60 and s.startswith("Application for Data Analyst") and s.endswith("(3.5 YoE)")
    assert fit_subject(t, {"company": "X", "years": ""}, "Analyst") == "Application for Analyst in X"


def test_strike_cuts_unsupported_sentences_and_empty_points():
    d = D.DraftOut(role_name="Data Analyst", role_short="Data Analyst",
                   intro="I work at ZS. I led a team of 40.",
                   points=[D.Point(heading="SQL", text="Built pipelines in SQL."),
                           D.Point(heading="Databricks", text="Ran Databricks for 5 years.")])
    claim = lambda s: D.Claim(part="x", sentence=s, claim="c", supported=False, evidence="")
    out, cut, clean = D.strike(d, [claim("I led a team of 40."), claim("Ran Databricks for 5 years.")])
    assert out.intro == "I work at ZS." and [p.heading for p in out.points] == ["SQL"] and clean and len(cut) == 2
    _, _, clean = D.strike(d, [claim("A sentence that is not in the draft.")])
    assert not clean   # can't cut it cleanly → the draft gets flagged


def test_req_ids_from_workday_links():
    job = SimpleNamespace(url="https://lilly.wd115.myworkdayjobs.com/LLY/job/IN/Data-Analyst_R-112054", external_id="x")
    assert D.req_id(job) == "R-112054"
    assert D.req_id(SimpleNamespace(url="", external_id="/job/12345/")) == "12345"


def test_greeting_names():
    assert D.greeting_name("Priya Sharma") == "Priya"
    assert D.greeting_name(None) is None and D.greeting_name("") is None
    t = parse_template(MINI)
    draft = SimpleNamespace(subject="S", body="{greeting}\n\nHello")
    assert D.render_for(draft, t, "Priya Sharma")[1].startswith("Hi Priya,")
    assert D.render_for(draft, t, None)[1].startswith("Hi Hiring Team,")
