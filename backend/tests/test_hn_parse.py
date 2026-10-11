from app.modules.m1_companies.scouts.hn import parse_comment


def test_standard_comment_with_link_and_obfuscated_email():
    raw = (
        "Acme AI (YC W23) | Senior Data Analyst | Bengaluru or REMOTE (India) | Full-time"
        "<p>We build forecasting tools. Apply: "
        '<a href="https:&#x2F;&#x2F;acme.ai&#x2F;careers" rel="nofollow">https:&#x2F;&#x2F;acme.ai&#x2F;careers</a>'
        " or email jobs [at] acme [dot] ai"
    )
    parsed = parse_comment(raw)
    assert parsed.name == "Acme AI"
    assert parsed.website == "acme.ai"
    assert parsed.emails == ["jobs@acme.ai"]
    assert parsed.headline.startswith("Acme AI (YC W23) |")


def test_free_text_comment_is_left_for_the_llm():
    assert parse_comment("We are hiring lots of people, come join us<p>Details below") is None


def test_ats_link_does_not_become_the_website_but_email_domain_can():
    raw = (
        "Zenith | Product Analyst | Remote"
        '<p><a href="https:&#x2F;&#x2F;jobs.lever.co&#x2F;zenith">https:&#x2F;&#x2F;jobs.lever.co&#x2F;zenith</a>'
        " — or write to founders@zenithhq.com"
    )
    parsed = parse_comment(raw)
    assert parsed.website == "zenithhq.com"
    assert parsed.ats_links == ["https://jobs.lever.co/zenith"]
    assert parsed.emails == ["founders@zenithhq.com"]


def test_domain_as_company_name():
    assert parse_comment("Acme.io | Analyst | Remote").website == "acme.io"


def test_unrelated_links_leave_website_unknown():
    raw = (
        "Quuxly | Analyst | Remote<p>"
        '<a href="https:&#x2F;&#x2F;blog-one.com">a</a> <a href="https:&#x2F;&#x2F;other-two.com">b</a>'
    )
    assert parse_comment(raw).website is None
