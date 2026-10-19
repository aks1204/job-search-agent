"""Your own Gmail via Google sign-in (spec §0): send + read scopes; the app reads only threads it started
(tracked by message/thread IDs). You create the Google Cloud credentials yourself (README: Connect Gmail).
Never a relay service: mail goes out from your account and shows in your Sent folder.
"""

import base64
import logging
import secrets
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.crypto import decrypt, encrypt
from app.core.redis_client import get_redis
from app.models.outreach import MailAccount
from app.settings import get_settings, redact

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPES = ["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly"]
ACCESS_KEY = "m3:gmail:access"
STATE_KEY = "m3:gmail:state:{}"


class GmailError(RuntimeError):
    pass


class GmailNotConnected(GmailError):
    pass


def configured() -> bool:
    s = get_settings()
    return bool(s.google_client_id and s.google_client_secret and s.app_secret_key)


def redirect_uri() -> str:
    return get_settings().public_base_url.rstrip("/") + "/api/gmail/callback"


def auth_url() -> str:
    """Google's consent page. `state` guards the callback against forged requests."""
    if not configured():
        raise GmailError("set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and APP_SECRET_KEY in .env first")
    state = secrets.token_urlsafe(24)
    get_redis().set(STATE_KEY.format(state), "1", ex=900)
    return AUTH_URL + "?" + urlencode({
        "client_id": get_settings().google_client_id, "redirect_uri": redirect_uri(), "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent", "state": state,
        "include_granted_scopes": "true",
    })


def _token_request(data: dict) -> dict:
    s = get_settings()
    resp = httpx.post(TOKEN_URL, data={**data, "client_id": s.google_client_id, "client_secret": s.google_client_secret},
                      timeout=20)
    if resp.status_code >= 400:
        raise GmailError(f"Google sign-in failed: {redact(resp.text)[:300]}")
    return resp.json()


def _cache_access(token: str, expires_in: int) -> None:
    get_redis().set(ACCESS_KEY, encrypt(token), ex=max(60, int(expires_in) - 120))


def finish_connect(session: Session, code: str, state: str) -> MailAccount:
    """The callback: swap the one-time code for a long-lived sign-in, store it encrypted."""
    if not state or not get_redis().delete(STATE_KEY.format(state)):
        raise GmailError("this sign-in link has expired or was already used; start again")
    tokens = _token_request({"code": code, "redirect_uri": redirect_uri(), "grant_type": "authorization_code"})
    if "refresh_token" not in tokens:
        raise GmailError("Google didn't return a long-lived sign-in; remove the app's access at "
                         "myaccount.google.com/permissions and connect again")
    granted = set(tokens.get("scope", "").split())
    missing = [s for s in SCOPES if s not in granted]
    if missing:
        raise GmailError("please tick both Gmail permissions (send, and read) on Google's consent page")
    _cache_access(tokens["access_token"], tokens.get("expires_in", 3600))
    profile = httpx.get(f"{API}/profile", headers={"Authorization": f"Bearer {tokens['access_token']}"}, timeout=20)
    profile.raise_for_status()
    email = profile.json()["emailAddress"].lower()
    for old in session.scalars(select(MailAccount)).all():  # one mailbox per install
        session.delete(old)
    account = MailAccount(provider="gmail", email=email, refresh_token_enc=encrypt(tokens["refresh_token"]),
                          scopes=" ".join(sorted(granted)), status="ok")
    session.add(account)
    session.flush()
    return account


def account(session: Session) -> MailAccount | None:
    return session.scalar(select(MailAccount).order_by(MailAccount.id.desc()))


def access_token(session: Session) -> str:
    acct = account(session)
    if acct is None:
        raise GmailNotConnected("Gmail isn't connected yet (python -m app.cli gmail connect)")
    if acct.status != "ok":
        raise GmailNotConnected("Gmail needs reconnecting (python -m app.cli gmail connect)")
    cached = get_redis().get(ACCESS_KEY)
    if cached:
        return decrypt(cached.decode() if isinstance(cached, bytes) else cached)
    try:
        tokens = _token_request({"refresh_token": decrypt(acct.refresh_token_enc), "grant_type": "refresh_token"})
    except GmailError as e:
        if "invalid_grant" in str(e):  # revoked, password changed, or expired
            acct.status = "reconnect"
        raise
    _cache_access(tokens["access_token"], tokens.get("expires_in", 3600))
    return tokens["access_token"]


def _api(session: Session, method: str, path: str, **kw) -> dict:
    resp = httpx.request(method, f"{API}{path}", headers={"Authorization": f"Bearer {access_token(session)}"},
                         timeout=30, **kw)
    if resp.status_code == 401:
        get_redis().delete(ACCESS_KEY)
        resp = httpx.request(method, f"{API}{path}", headers={"Authorization": f"Bearer {access_token(session)}"},
                             timeout=30, **kw)
    if resp.status_code >= 400:
        raise GmailError(f"Gmail {method} {path}: {resp.status_code} {redact(resp.text)[:300]}")
    return resp.json()


def build_message(*, sender: str, sender_name: str | None, to: str, subject: str, body: str,
                  attachment: Path | None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name or "", sender))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=sender.split("@", 1)[1])
    msg.set_content(body)
    if attachment is not None and attachment.is_file():
        msg.add_attachment(attachment.read_bytes(), maintype="application", subtype="pdf",
                           filename=attachment.name)
    return msg


def send(session: Session, msg: EmailMessage) -> tuple[str, str]:
    """Send from your Gmail. Returns (message id, thread id)."""
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    data = _api(session, "POST", "/messages/send", json={"raw": raw})
    return data["id"], data["threadId"]


def thread(session: Session, thread_id: str) -> list[dict]:
    """Messages in one thread the app started: [{id, from, subject, snippet, sent_by_me, date}]."""
    data = _api(session, "GET", f"/threads/{thread_id}",
                params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]})
    out = []
    for m in data.get("messages", []):
        headers = {h["name"].lower(): h["value"] for h in (m.get("payload") or {}).get("headers", [])}
        out.append({"id": m["id"], "from": headers.get("from", ""), "subject": headers.get("subject", ""),
                    "date": headers.get("date", ""), "snippet": m.get("snippet", ""),
                    "sent_by_me": "SENT" in (m.get("labelIds") or [])})
    return out


def _walk_text(part: dict) -> str:
    if part.get("mimeType") == "text/plain" and (part.get("body") or {}).get("data"):
        data = part["body"]["data"]
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
    for sub in part.get("parts") or []:
        if text := _walk_text(sub):
            return text
    return ""


def message_text(session: Session, message_id: str) -> str:
    """Plain text of one message in a thread the app started (for sorting a reply)."""
    data = _api(session, "GET", f"/messages/{message_id}", params={"format": "full"})
    return _walk_text(data.get("payload") or {})


def thread_link(thread_id: str) -> str:
    return f"https://mail.google.com/mail/#all/{thread_id}"


def search(session: Session, query: str, limit: int) -> list[dict]:
    """Message ids matching a Gmail search (Module 4 uses a narrow one: hiring-system senders only)."""
    out: list[dict] = []
    token = None
    while len(out) < limit:
        params = {"q": query, "maxResults": min(100, limit - len(out))}
        if token:
            params["pageToken"] = token
        data = _api(session, "GET", "/messages", params=params)
        out += [{"id": m["id"], "thread_id": m["threadId"]} for m in data.get("messages", [])]
        token = data.get("nextPageToken")
        if not token:
            break
    return out[:limit]


def message_meta(session: Session, message_id: str) -> dict:
    """From / Subject / Date / snippet of one message (no body)."""
    data = _api(session, "GET", f"/messages/{message_id}",
                params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]})
    headers = {h["name"].lower(): h["value"] for h in (data.get("payload") or {}).get("headers", [])}
    return {"id": data["id"], "thread_id": data.get("threadId"), "from": headers.get("from", ""),
            "subject": headers.get("subject", ""), "date": headers.get("date", ""),
            "snippet": data.get("snippet", ""), "internal_ms": int(data.get("internalDate") or 0)}
