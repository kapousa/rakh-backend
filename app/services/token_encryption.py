"""
Token encryption service.

OAuth access/refresh tokens for connected ad accounts are encrypted at the
application layer before they ever touch the database — the DB should
never hold a usable token even if it were somehow read directly (backup
leak, misconfigured RLS, etc). Uses Fernet (AES-128-CBC + HMAC), which is
the right tool here: symmetric, authenticated, and simple to rotate.

The encryption key must never be committed or exposed to the frontend.
Generate one with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
and set it as TOKEN_ENCRYPTION_KEY in the backend's .env only.
"""
from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings


class TokenEncryptionNotConfigured(RuntimeError):
    pass


@lru_cache
def _get_fernet() -> Fernet:
    settings = get_settings()
    if not settings.TOKEN_ENCRYPTION_KEY:
        raise TokenEncryptionNotConfigured(
            "TOKEN_ENCRYPTION_KEY is not set. Ad platform connections cannot be "
            "created or read until this is configured — see token_encryption.py "
            "docstring for how to generate one."
        )
    return Fernet(settings.TOKEN_ENCRYPTION_KEY.encode())


def encrypt_token(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise ValueError(
            "Failed to decrypt stored token — it may have been encrypted with a "
            "different TOKEN_ENCRYPTION_KEY, or the key rotated without "
            "re-encrypting existing connections."
        ) from exc
