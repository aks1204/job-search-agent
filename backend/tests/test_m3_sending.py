"""Part 3c, no database: review-file round trip, waves, quiet hours, time zones, the message itself."""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.modules.m3_outreach import campaigns as C
from app.modules.m3_outreach import gmail
from app.modules.m3_outreach import sender as S


def test_export_import_round_trip_keeps_the_greeting_line():
    c = SimpleNamespace(subject="Application for Data Analyst in Lilly (3 YoE)", body="{greeting}\n\nHello there.\n\nBest,\nA")
    subject, body = C.import_text(C.export_text(c))
    assert (subject, body) == (c.subject, c.body)
    edited = C.export_text(c).replace("Hello there.", "Hello again.").replace("(3 YoE)", "(3+ YoE)")
    assert C.import_text(edited.replace("\n", "\r\n")) == ("Application for Data Analyst in Lilly (3+ YoE)",
                                                            "{greeting}\n\nHello again.\n\nBest,\nA")
    with pytest.raises(C.ReviewError):
        C.import_text("no subject line here")


def test_waves_are_two_two_then_the_rest():
    assert [C.wave_for(i) for i in range(7)] == [0, 0, 1, 1, 2, 2, 2]


def test_go_ahead_needed_only_for_unproven_unvouched_addresses():
    assert not C.needs_go_ahead(SimpleNamespace(confidence="verified", vouched=False))
    assert not C.needs_go_ahead(SimpleNamespace(confidence="low", vouched=True))
    assert C.needs_go_ahead(SimpleNamespace(confidence="low", vouched=False))


def test_quiet_hours_use_the_recipients_clock():
    ist, ny = ZoneInfo("Asia/Kolkata"), ZoneInfo("America/New_York")
    noon_ist = datetime(2026, 10, 19, 6, 30, tzinfo=timezone.utc)      # 12:00 in India, 02:30 in New York
    assert not S.in_quiet_hours(noon_ist, ist) and S.in_quiet_hours(noon_ist, ny)
    assert S.recipient_tz(SimpleNamespace(location="Hyderabad, Telangana", geo="india")).key == "Asia/Kolkata"
    assert S.recipient_tz(SimpleNamespace(location="Indianapolis, Indiana", geo="elsewhere")).key == "America/New_York"


def test_message_has_the_resume_and_comes_from_you(tmp_path: Path):
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    msg = gmail.build_message(sender="me@gmail.com", sender_name="Ashish Kumar Singh", to="hr@acme.com",
                              subject="Application", body="Hi Priya,\n\nHello", attachment=pdf)
    assert msg["From"] == "Ashish Kumar Singh <me@gmail.com>" and msg["To"] == "hr@acme.com"
    names = [p.get_filename() for p in msg.iter_attachments()]
    assert names == ["resume.pdf"]


@pytest.mark.parametrize("sender, subject, bounce", [
    ("Mail Delivery Subsystem <mailer-daemon@googlemail.com>", "Delivery Status Notification (Failure)", True),
    ("postmaster@acme.com", "Undeliverable: Application", True),
    ("Priya Sharma <priya@acme.com>", "Re: Application", False),
])
def test_bounce_detection(sender, subject, bounce):
    assert bool(S.BOUNCE_FROM.search(sender) or S.BOUNCE_SUBJECT.search(subject)) == bounce
