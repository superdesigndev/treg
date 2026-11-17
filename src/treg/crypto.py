"""Fernet encryption for short-lived state, Arena payloads and, without KMS, stored credentials.

TREG_SECRET_KEY is one key, or a comma-separated list during a rotation: the first key encrypts and
every listed key decrypts.
"""

from __future__ import annotations

import hashlib
import secrets as _secrets

from cryptography.fernet import Fernet, MultiFernet

from .config import get_settings

# Mint an ephemeral key once per process if none is configured (dev only — secrets
# won't survive a restart, which is the intended loud signal to set TREG_SECRET_KEY).
_EPHEMERAL = Fernet.generate_key()


def _fernet() -> MultiFernet:
    keys = [k.strip() for k in get_settings().secret_key.split(",") if k.strip()]
    return MultiFernet([Fernet(k.encode()) for k in keys] or [Fernet(_EPHEMERAL)])


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def rotate(token: str) -> str:
    """`token` re-encrypted under the first key; raises if no listed key opens it."""
    return _fernet().rotate(token.encode()).decode()


def is_current(token: str) -> bool:
    """Whether the first key alone opens `token`."""
    keys = [k.strip() for k in get_settings().secret_key.split(",") if k.strip()]
    try:
        Fernet((keys[0] if keys else _EPHEMERAL.decode()).encode()).decrypt(token.encode())
    except Exception:  # noqa: BLE001 — InvalidToken, or a value that is not a Fernet token at all
        return False
    return True


def new_key() -> str:
    """A fresh urlsafe key for TREG_SECRET_KEY."""
    return Fernet.generate_key().decode()


# A token is high-entropy random, so a plain SHA-256 (not a slow password hash) is the right
# store: we look up by hash, the token is shown to the user exactly once at registration.
def new_token() -> str:
    return _secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
