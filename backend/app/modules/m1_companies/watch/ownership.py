"""Feed ownership (spec §1.5 step 3): a board slug can collide with a different company's.

Evidence, strongest first:
1. the board was linked from the company's own website;
2. the vendor's board name matches the company;
3. job URLs point back at the company's domain;
4. most job descriptions name the company.
A guessed slug with none of these is rejected.
"""

from app.core.hosts import normalize_domain
from app.modules.m1_companies.identity import normalize_name
from app.modules.m1_companies.watch.readers import FetchResult


def _names(company) -> list[str]:
    return [n for n in {normalize_name(company.name), *(normalize_name(a) for a in company.aliases or [])} if n]


def _same_name(a: str, b: str) -> bool:
    # Exact match after normalisation (legal suffixes already stripped). Containment is NOT enough:
    # "Quillmark Plumbing" must not vouch for "Quillmark" — that's the slug collision we're guarding against.
    a, b = a.replace(" ", ""), b.replace(" ", "")
    return bool(a) and a == b


def verify_ownership(company, result: FetchResult, *, linked_from_site: bool, careers_url: str | None) -> tuple[bool, str]:
    if linked_from_site:
        return True, f"linked from the company's own site ({careers_url})"

    names = _names(company)
    if result.board_name and any(_same_name(normalize_name(result.board_name), n) for n in names):
        return True, f"board name “{result.board_name}” matches"

    if not result.jobs:
        return False, "guessed board has no jobs to check"

    own = [j for j in result.jobs if j.url and normalize_domain(j.url) == company.domain]
    if own:
        return True, f"{len(own)} job links point to {company.domain}"

    sample = result.jobs[:10]
    first_word = names[0].split()[0] if names else ""
    mentions = sum(
        1 for j in sample
        if first_word and len(first_word) >= 3 and first_word in normalize_name(f"{j.title} {j.description or ''}")
    )
    if mentions * 2 > len(sample):
        return True, f"{mentions}/{len(sample)} job descriptions name the company"

    return False, "no evidence the guessed board belongs to this company"
