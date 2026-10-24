from app.core.llm import LLMResult
from app.modules.m1_companies.candidate import APPLY_MARK
from app.modules.m1_companies.identity import tidy_name
from app.modules.m1_companies.scouts.hn import HNContact, HNRead, HNRole, read_by_ai, read_by_rules


class FakeLLM:
    def __init__(self, parsed):
        self.parsed = parsed

    def structured(self, tier, *, system, user, schema, purpose, session=None, subject=None):
        return LLMResult(parsed=self.parsed, model="fake", cost_usd=0.0, input_tokens=1, output_tokens=1)


def _read(**kw) -> HNRead:
    base = dict(is_hiring_post=True, company_name="Acme AI", website="unknown", careers_url="unknown",
                what_they_build="Forecasting tools", hires_in_india="yes", remote_from_india="unknown",
                visa="unknown", roles=[], contacts=[])
    return HNRead(**{**base, **kw})


ACME = (
    "Acme AI (YC W23) | Senior Data Analyst | Bengaluru or REMOTE (India) | Full-time"
    "<p>We build forecasting tools. Apply: "
    '<a href="https:&#x2F;&#x2F;jobs.lever.co&#x2F;acme&#x2F;123" rel="nofollow">https:&#x2F;&#x2F;jobs.lever.co&#x2F;acme&#x2F;123</a>'
    " or email jobs [at] acme [dot] ai"
)


# --- without an AI key: line by line ----------------------------------------------------------------------

def test_rules_read_a_standard_post():
    r = read_by_rules(ACME)
    assert r.name == "Acme AI" and r.website == "acme.ai"                 # from the email domain
    assert [c.email for c in r.contacts] == ["jobs@acme.ai"]
    assert r.data["roles"][0]["title"] == "Senior Data Analyst"
    assert r.data["board_links"] == ["https://jobs.lever.co/acme/123"]


def test_rules_leave_a_free_text_post_for_the_ai():
    assert read_by_rules("We are hiring lots of people, come join us<p>Details below") is None


def test_a_lone_link_that_doesnt_fit_the_name_is_not_their_website():
    # GiveDirectly once got perfectwiki.page this way: the only link was a page hosting the job description.
    raw = 'GiveDirectly | Data Analyst | Remote<p>Details: <a href="https:&#x2F;&#x2F;perfectwiki.page&#x2F;jd">here</a>'
    assert read_by_rules(raw).website is None


def test_an_email_domain_made_of_the_names_words_is_their_website():
    raw = ("Strategy &amp; Transformation Consulting (s&amp;t) | Head of Development | REMOTE (Germany)"
           "<p>Send your CV to recruiting@strategy-transformation.com")
    r = read_by_rules(raw)
    assert r.name == "Strategy & Transformation Consulting" and r.website == "strategy-transformation.com"


# --- with the AI: every answer checked against the post ---------------------------------------------------

def test_ai_read_takes_every_role_and_marks_the_apply_address():
    raw = ("Acme AI | Hiring | Bengaluru<p>Roles: Senior Data Analyst (Bengaluru) "
           '<a href="https:&#x2F;&#x2F;jobs.ashbyhq.com&#x2F;acme&#x2F;1">apply</a>, Analytics Engineer (Remote, India)'
           "<p>Email our founder priya [at] acme.ai to apply. Press: press@acme.ai")
    llm = FakeLLM(_read(
        website="acme.ai",
        roles=[HNRole(title="Senior Data Analyst", location="Bengaluru", apply_url="https://jobs.ashbyhq.com/acme/1"),
               HNRole(title="Analytics Engineer", location="Remote, India", apply_url="unknown")],
        contacts=[HNContact(email="priya@acme.ai", name="Priya", role="founder", to_apply=True)]))
    r = read_by_ai(llm, None, raw)
    assert r.website == "acme.ai" and r.data["parsed_by"] == "ai"
    assert [x["title"] for x in r.data["roles"]] == ["Senior Data Analyst", "Analytics Engineer"]
    assert r.data["roles"][0]["apply_url"] == "https://jobs.ashbyhq.com/acme/1" and r.data["roles"][1]["apply_url"] is None
    priya = next(c for c in r.contacts if c.email == "priya@acme.ai")
    assert priya.name == "Priya" and priya.context.startswith(APPLY_MARK)
    assert any(c.email == "press@acme.ai" and not c.context.startswith(APPLY_MARK) for c in r.contacts)


def test_ai_answers_the_post_doesnt_contain_are_thrown_away():
    raw = "Zenith | Product Analyst | Remote<p>Write to us at founders@zenithhq.com"
    llm = FakeLLM(_read(company_name="Zenith", website="zenith.io",       # not in the post
                        careers_url="https://zenith.io/careers",           # not in the post
                        roles=[HNRole(title="Product Analyst", location="Remote", apply_url="https://made.up/x")],
                        contacts=[HNContact(email="ceo@zenith.io", name="unknown", role="unknown", to_apply=True)]))
    r = read_by_ai(llm, None, raw)
    assert r.website == "zenithhq.com"            # falls back to what the post contains and fits the name
    assert r.data["careers_url"] is None and r.data["roles"][0]["apply_url"] is None
    assert [c.email for c in r.contacts] == ["founders@zenithhq.com"]


def test_ai_read_of_a_post_without_any_website_keeps_it_name_only():
    raw = "Tinyco | Data Analyst | Remote (worldwide)<p>Email tinyco.jobs@gmail.com"
    r = read_by_ai(FakeLLM(_read(company_name="Tinyco", remote_from_india="yes",
                                 roles=[HNRole(title="Data Analyst", location="Remote (worldwide)", apply_url="unknown")])),
                   None, raw)
    assert r.website is None and r.name == "Tinyco"                     # gmail is a contact, never a website
    assert [c.email for c in r.contacts] == ["tinyco.jobs@gmail.com"]


def test_not_a_hiring_post_is_skipped():
    assert read_by_ai(FakeLLM(_read(is_hiring_post=False)), None, "Just a comment | about | things") is None


def test_shouting_names_are_tidied_but_acronyms_stay():
    assert tidy_name("ARTIFICIAL ANALYSIS") == "Artificial Analysis"
    assert tidy_name("BREAKFAST Studio") == "Breakfast Studio"
    assert tidy_name("ICEYE") == "ICEYE" and tidy_name("FUTO") == "FUTO"
