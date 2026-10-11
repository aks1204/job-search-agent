"""Feed ownership (spec §1.5 step 3): a board slug can collide with a different company's.

A board linked from the company's own website is trusted. A board we *guessed*
(slug guesses, AI suggestions) must show evidence:

- strong, enough on its own:
  - job URLs point back at the company's domain;
  - a Workday tenant named after the company's domain (tenants are provisioned per
    employer, so "lilly" on lilly.com is not a coincidence);
- name-only (exact board-name match, or most descriptions name the company): accepted
  only when the board's size is plausible for the company — a 10,000-person employer
  with a 1-job board is a namesake ("Uber" on SmartRecruiters was one).
"""

from app.core.hosts import domain_label, normalize_domain
from app.modules.m1_companies.identity import normalize_name
from app.modules.m1_companies.watch.readers import FetchResult

LARGE = ("1001-10000", "10000+")
MIN_JOBS_FOR_LARGE = 10


def _names(company) -> list[str]:
    return [n for n in {normalize_name(company.name), *(normalize_name(a) for a in company.aliases or [])} if n]


def _same_name(a: str, b: str) -> bool:
    # Exact match after normalisation (legal suffixes already stripped). Containment is NOT enough:
    # "Quillmark Plumbing" must not vouch for "Quillmark".
    a, b = a.replace(" ", ""), b.replace(" ", "")
    return bool(a) and a == b


def verify_ownership(
    company,
    result: FetchResult,
    *,
    linked_from_site: bool,
    careers_url: str | None,
    method: str | None = None,
    slug: str | None = None,
    employee_estimate: str | None = None,
) -> tuple[bool, str]:
    if linked_from_site:
        return True, f"linked from the company's own site ({careers_url})"
    if not result.jobs:
        return False, "guessed board has no jobs to check"

    own = [j for j in result.jobs if j.url and normalize_domain(j.url) == company.domain]
    if own:
        return True, f"{len(own)} job links point to {company.domain}"

    if method == "workday" and slug and company.domain and slug.lower() == domain_label(company.domain).lower():
        return True, f"Workday tenant “{slug}” matches {company.domain}"

    names = _names(company)
    name_evidence = None
    if result.board_name and any(_same_name(normalize_name(result.board_name), n) for n in names):
        name_evidence = f"board name “{result.board_name}” matches"
    else:
        sample = result.jobs[:10]
        first_word = names[0].split()[0] if names else ""
        mentions = sum(
            1 for j in sample
            if first_word and len(first_word) >= 3 and first_word in normalize_name(f"{j.title} {j.description or ''}")
        )
        if mentions * 2 > len(sample):
            name_evidence = f"{mentions}/{len(sample)} job descriptions name the company"

    if name_evidence is None:
        return False, "no evidence the guessed board belongs to this company"
    if employee_estimate in LARGE and len(result.jobs) < MIN_JOBS_FOR_LARGE:
        return False, (f"{name_evidence}, but only {len(result.jobs)} job(s) for a {employee_estimate}-person "
                       "company — probably a namesake")
    return True, name_evidence
