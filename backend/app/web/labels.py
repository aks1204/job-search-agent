"""The app's words, in one place (owner 2026-11-04: the same names and labels on every screen and in the mail).

- **Pursuing**: the roles you're working on — the Jobs tab, the mail's section and its subject ("2 pursuing").
  A role stays on the tab once it has ended, marked "ended"; the mail's section only has the ones in play.
- **Where a role stands**: the same chips on the Pursuing tab, the role page and the mail (`role_chips`).
- **Pings**: what the agent wants you to know (the Pings screen). **Email alerts**: the two pings that can also
  come to your inbox at once — a fast-lane find and a reply to your outreach (Settings).
"""

PURSUING = "Pursuing"
EMAIL_ALERT = "Email alert"

# Chip colours: red = waiting for you, green = moving, blue = being tracked, grey = over (as the card stripes)
TO_APPLY = ("to apply", "red")
TO_REVIEW = ("to review", "red")
TO_ANSWER = ("to answer", "red")
TO_CHECK = ("to check", "red")
OUTREACH_ON = ("outreach on", "green")
OUTREACH_HELD = ("outreach on hold", "amber")
APPLIED = ("applied", "blue")
INTERVIEWING = ("interviewing", "green")
OFFER = ("offer", "green")
ENDED = {"rejected": "ended · rejected", "silent": "ended · no reply", "closed": "ended · posting closed",
         "skipped": "ended · skipped"}
APPLIED_STATES = ("applied", "confirmed", "in_review")


def role_chips(app, campaign_status: str | None, to_apply: bool) -> list[tuple[str, str]]:
    """Where a role stands, as (label, colour) chips, most urgent first. One "ended" chip once it's over."""
    status = app.status if app is not None else None
    if status in ENDED:
        return [(ENDED[status], "gray")]
    chips = []
    if to_apply:
        chips.append(TO_APPLY)
    if campaign_status == "pending_review":
        chips.append(TO_REVIEW)
    elif campaign_status in ("scheduled", "running"):
        chips.append(OUTREACH_ON)
    elif campaign_status == "paused":
        chips.append(OUTREACH_HELD)
    if status in APPLIED_STATES:
        chips.append(APPLIED)
    elif status == "interviewing":
        chips.append(INTERVIEWING)
    elif status == "offer":
        chips.append(OFFER)
    return chips or [APPLIED if status else TO_APPLY]


def in_play(chips: list[tuple[str, str]]) -> bool:
    return not any(color == "gray" for _, color in chips)


COLOR_ORDER = {"red": 0, "amber": 1, "green": 2, "blue": 3, "gray": 4}
CSS = {"red": "ch-red", "amber": "ch-amber", "green": "ch-green", "blue": "ch-blue", "gray": "ch-gray"}
