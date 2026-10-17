"""Verification doctrine (spec §3.1): nothing sends unproven.

DNS/MX → mailbox handshake (local SMTP first, then the verification-API pool) → catch-all detection
(a made-up address at the same domain: accepted ⇒ the domain accepts everything ⇒ every address there
is permanently low-confidence) → mailbox history (delivered / replied) overrides all of it.

Results land on ContactEmail.confidence: proven | verified | low | invalid.
"""

import logging
import secrets
import smtplib
import socket
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.core.http import PoliteClient
from app.core.quota import try_consume, try_consume_daily
from app.core.redis_client import get_redis
from app.models.contact import ContactEmail, MailDomain
from app.settings import get_settings, redact

log = logging.getLogger(__name__)

MX_REFRESH = timedelta(days=30)
SMTP_BLOCKED_KEY = "m3:smtp_blocked"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- DNS -----------------------------------------------------------------------------------------

def lookup_mx(domain: str) -> list[str] | None:
    """MX hosts, best first; [] when the domain has none; None when DNS itself failed."""
    import dns.exception
    import dns.resolver

    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=8)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except (dns.exception.DNSException, OSError):
        return None
    hosts = sorted(((r.preference, str(r.exchange).rstrip(".")) for r in answers), key=lambda x: x[0])
    return [h for _, h in hosts if h]


def mail_domain(session: Session, domain: str, resolver=lookup_mx) -> MailDomain:
    row = session.get(MailDomain, domain)
    if row is None:
        row = MailDomain(domain=domain, mx_hosts=[])
        session.add(row)
        session.flush()
    if row.mx_checked_at is None or _now() - row.mx_checked_at > MX_REFRESH:
        hosts = resolver(domain)
        if hosts is not None:
            row.mx_hosts, row.has_mx, row.mx_checked_at = hosts, bool(hosts), _now()
    return row


# --- local SMTP handshake ------------------------------------------------------------------------

def smtp_blocked() -> bool:
    try:
        return bool(get_redis().exists(SMTP_BLOCKED_KEY))
    except Exception:
        return False


def _mark_smtp_blocked(hours: int) -> None:
    try:
        get_redis().set(SMTP_BLOCKED_KEY, "1", ex=hours * 3600)
    except Exception:
        pass


def smtp_check(mx_hosts: list[str], emails: list[str], *, timeout: float) -> dict[str, str] | None:
    """Ask the mail server about each address (RCPT TO), plus one made-up address for catch-all.

    Returns {email: valid|invalid|unknown, "__catch_all__": "yes"|"no"|"unknown"}, or None when we
    couldn't talk to any mail server (outbound port 25 is blocked on most home connections)."""
    helo = get_settings().smtp_helo_host or socket.getfqdn() or "localhost"
    domain = emails[0].split("@", 1)[1]
    probe = f"zz{secrets.token_hex(6)}@{domain}"
    for host in mx_hosts[:2]:
        try:
            with smtplib.SMTP(host, 25, timeout=timeout, local_hostname=helo) as smtp:
                smtp.ehlo_or_helo_if_needed()
                code, _ = smtp.mail("")
                if code >= 400:
                    return {e: "unknown" for e in emails} | {"__catch_all__": "unknown"}
                out: dict[str, str] = {}
                for addr in [*emails, probe]:
                    code, _ = smtp.rcpt(addr)
                    out[addr] = "valid" if 200 <= code < 300 else "invalid" if code in (550, 551, 553) else "unknown"
                verdict = out.pop(probe)
                out["__catch_all__"] = {"valid": "yes", "invalid": "no"}.get(verdict, "unknown")
                return out
        except (OSError, smtplib.SMTPException) as e:
            log.info("smtp check via %s failed: %s", host, e)
            continue
    return None


# --- verification-API pool -----------------------------------------------------------------------

@dataclass
class ProviderResult:
    status: str                      # valid | invalid | catch_all | unknown
    raw: dict = field(default_factory=dict)


class EmailVerifier(Protocol):
    name: str

    def available(self) -> bool: ...

    def check(self, http: PoliteClient, email: str) -> ProviderResult | None:
        """None when this provider's budget is spent (try the next one)."""


class MyEmailVerifier:
    name = "myemailverifier"
    URL = "https://client.myemailverifier.com/verifier/validate_single/{email}/{key}"

    def available(self) -> bool:
        return bool(get_settings().myemailverifier_api_key) and \
            get_config().outreach.contacts.verification.myemailverifier_per_day > 0

    def check(self, http: PoliteClient, email: str) -> ProviderResult | None:
        if not try_consume_daily("myemailverifier", get_config().outreach.contacts.verification.myemailverifier_per_day):
            return None
        resp = http.get(self.URL.format(email=email, key=get_settings().myemailverifier_api_key))
        resp.raise_for_status()
        data = resp.json()
        status = str(data.get("Status", "")).strip().lower()
        catch_all = str(data.get("catch_all", "")).strip().lower() in ("true", "1", "yes")
        if status == "valid" and not catch_all:
            mapped = "valid"
        elif status in ("catch all", "catch_all", "catchall") or catch_all:
            mapped = "catch_all"
        elif status == "invalid":
            mapped = "invalid"
        else:
            mapped = "unknown"  # Unknown / Grey-listed
        return ProviderResult(mapped, {k: data.get(k) for k in ("Status", "catch_all", "Diagnosis")})


class HunterVerifier:
    name = "hunter"
    URL = "https://api.hunter.io/v2/email-verifier"

    def available(self) -> bool:
        return bool(get_settings().hunter_api_key) and \
            get_config().outreach.contacts.verification.hunter_verifier_per_month > 0

    def check(self, http: PoliteClient, email: str) -> ProviderResult | None:
        if not try_consume("hunter:verifier", get_config().outreach.contacts.verification.hunter_verifier_per_month):
            return None
        resp = http.get(self.URL, params={"email": email}, headers={"X-API-KEY": get_settings().hunter_api_key})
        if resp.status_code == 202:  # still checking on Hunter's side
            return ProviderResult("unknown", {"pending": True})
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        status = data.get("status")
        mapped = {"valid": "valid", "invalid": "invalid", "accept_all": "catch_all"}.get(status, "unknown")
        return ProviderResult(mapped, {"status": status, "score": data.get("score")})


def default_pool() -> list[EmailVerifier]:
    """MyEmailVerifier first (daily free credits), Hunter's verifier as the reserve.
    (The spec's EmailDetective has no API we could confirm; add it here when it does.)"""
    return [MyEmailVerifier(), HunterVerifier()]


# --- the doctrine --------------------------------------------------------------------------------

def _apply_history(row: ContactEmail) -> bool:
    if row.replied_count > 0 or row.delivered_count > 0:
        row.confidence = "proven"
        return True
    return False


def verify_candidates(session: Session, http: PoliteClient, rows: list[ContactEmail], *,
                      pool: list[EmailVerifier] | None = None, resolver=lookup_mx, smtp=smtp_check) -> ContactEmail | None:
    """Check addresses at ONE domain, best guess first; return the first one proven/verified.

    Candidates are alternative guesses for the same person (or a single real address), so we stop
    at the first verified one and leave the rest unchecked."""
    if not rows:
        return None
    rules = get_config().outreach.contacts.verification
    pool = default_pool() if pool is None else pool
    domain = rows[0].email.split("@", 1)[1]
    md = mail_domain(session, domain, resolver)
    now = _now()

    for row in rows:
        if _apply_history(row):
            return row
    if md.has_mx is False:
        for row in rows:
            row.confidence, row.checks, row.verified_at = "invalid", {"mx": "none"}, now
        return None
    if md.catch_all:
        # Permanently low-confidence: spending credits can't tell real from made-up addresses here.
        rows[0].confidence = "low"
        rows[0].checks = {**(rows[0].checks or {}), "catch_all": True}
        rows[0].verified_at = now
        return None

    # 1 — local SMTP handshake, all candidates in one conversation.
    if rules.smtp_handshake and md.mx_hosts and not smtp_blocked():
        result = smtp([*md.mx_hosts], [r.email for r in rows], timeout=rules.smtp_timeout_seconds)
        if result is None:
            _mark_smtp_blocked(rules.smtp_blocked_retry_hours)
        else:
            catch = result.pop("__catch_all__", "unknown")
            if catch == "yes":
                md.catch_all, md.catch_all_checked_at = True, now
                rows[0].confidence, rows[0].checks, rows[0].verified_at = "low", {"smtp": "catch_all"}, now
                return None
            if catch == "no":
                md.catch_all, md.catch_all_checked_at = False, now
            for row in rows:
                status = result.get(row.email, "unknown")
                row.checks = {**(row.checks or {}), "smtp": status}
                if status == "invalid":
                    row.confidence, row.verified_at = "invalid", now
                elif status == "valid" and catch == "no":
                    row.confidence, row.verified_at = "verified", now
                    return row
            rows = [r for r in rows if r.confidence not in ("invalid",)]

    # 2 — verification-API pool, one address at a time, stopping at the first verified.
    for row in rows[: rules.max_candidates_per_person]:
        verdict = None
        for provider in pool:
            if not provider.available():
                continue
            try:
                verdict = provider.check(http, row.email)
            except Exception as e:  # provider trouble → next provider
                log.info("%s failed for %s: %s", provider.name, row.email, redact(str(e))[:200])
                continue
            if verdict is None:
                continue  # budget spent
            row.checks = {**(row.checks or {}), provider.name: verdict.status}
            break
        if verdict is None:
            break  # nobody left to ask today
        row.verified_at = now
        if verdict.status == "valid":
            row.confidence = "verified"
            md.catch_all, md.catch_all_checked_at = False, now
            return row
        if verdict.status == "catch_all":
            md.catch_all, md.catch_all_checked_at = True, now
            row.confidence = "low"
            return None
        if verdict.status == "invalid":
            row.confidence = "invalid"
            continue
        row.confidence = "low"  # unknown: can't prove it either way; try the next guess

    # Nothing proved: construction-only mode — the best untested guess stays low-confidence.
    for row in rows:
        if row.confidence == "unchecked":
            row.confidence = "low"
            row.checks = {**(row.checks or {}), "unverified": "no checker available"}
            break
    return None
