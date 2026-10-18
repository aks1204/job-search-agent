"""Encryption at rest for secrets the app must store (the Gmail sign-in). Key: APP_SECRET_KEY in .env."""

from cryptography.fernet import Fernet, InvalidToken

from app.settings import get_settings


class SecretKeyMissing(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = get_settings().app_secret_key
    if not key:
        raise SecretKeyMissing("APP_SECRET_KEY is not set in .env (see README: Connect Gmail)")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as e:
        raise SecretKeyMissing("APP_SECRET_KEY is not a valid key; generate one as the README shows") from e


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise SecretKeyMissing("the stored sign-in can't be read with this APP_SECRET_KEY; reconnect Gmail") from e
