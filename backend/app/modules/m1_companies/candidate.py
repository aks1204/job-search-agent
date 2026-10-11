from dataclasses import dataclass, field


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
