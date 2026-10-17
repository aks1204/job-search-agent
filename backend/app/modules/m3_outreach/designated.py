"""Designated-contact override (spec §3.1, strict): if a posting names an address for applications
("email us at…", "send your CV to…"), that address is the ONLY channel for the role."""

import re

EMAIL = re.compile(r"(?<![\w.+-])([a-z0-9][a-z0-9._%+-]*@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,})(?![\w-])", re.I)

# Near the address, one of these means "write here to apply / reach the hiring team".
_APPLY_CUES = re.compile(
    r"\b(e-?mail|send|write|reach out|contact|apply|application|applications|resume|résumé|cv|portfolio|"
    r"interested|drop (us|me) a (line|note)|get in touch|ping)\b", re.I)
# …and these mean the address is for something else (accessibility requests, privacy, scam warnings).
_OTHER_PURPOSE = re.compile(
    r"\b(accommodation|accessib|disabilit|privacy|gdpr|data protection|fraud|scam|phishing|"
    r"grievance|whistle|ethics|complaint|unsubscribe|noreply|no-reply|donotreply)", re.I)
_OTHER_LOCALS = re.compile(r"^(privacy|accommodations?|accessibility|ada|fraud|security|legal|dpo|noreply|"
                           r"no-reply|donotreply|ethics|compliance|abuse|press|media|support)\b", re.I)

WINDOW = 160  # characters each side of the address that we read for cues


def find_designated(text: str | None) -> tuple[str, str] | None:
    """(address, the sentence around it) for the first address the posting asks applicants to use."""
    if not text:
        return None
    for m in EMAIL.finditer(text):
        email = m.group(1).rstrip(".").lower()
        local = email.split("@", 1)[0]
        if _OTHER_LOCALS.match(local):
            continue
        before = text[max(0, m.start() - WINDOW):m.start()]
        after = text[m.end():m.end() + WINDOW // 2]
        clause = re.split(r"[.!?;]\s|\n", before[-100:])[-1]  # the purpose is stated just before the address
        if _OTHER_PURPOSE.search(clause):
            continue
        if _APPLY_CUES.search(before + " " + after):
            return email, " ".join((before[-100:] + m.group(1) + after[:60]).split())
    return None
