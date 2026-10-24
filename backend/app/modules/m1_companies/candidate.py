from dataclasses import dataclass, field

APPLY_MARK = "[apply] "  # on a contact's context: the post asks candidates to write to this address


@dataclass
class ContactHint:
    email: str
    name: str | None = None
    role: str | None = None
    context: str | None = None


@dataclass
class Candidate:
    """What a scout emits. Identity resolution decides which company it belongs to."""

    scout: str
    external_id: str
    name: str
    domain: str | None = None  # anything normalize_domain() accepts: URL, host, email
    url: str | None = None  # breadcrumb: the listing / job / comment the company was seen in
    type_hint: str | None = None  # "startup" | "established"
    data: dict = field(default_factory=dict)
    contacts: list[ContactHint] = field(default_factory=list)
    # The sighting itself is enough to judge the company even without a website (a Hacker News post):
    # it goes straight to judgment instead of the web-search website-finder.
    name_only_ok: bool = False
