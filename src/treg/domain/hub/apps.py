"""A hub tool's web page: the rules that need no database (docs/context/architecture/hub-apps.md).

The URL is `/apps/<team slug>/<name>`. The team slug already carries the identity (team names are
checked when a team is made), so a name only has to be a clean URL part, unique within its team.
The password is optional; it is stored only as a slow hash.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

from .manifest import ManifestError

NAME_MAX = 48
PASSWORD_MIN = 8
PASSWORD_MAX = 128
# scrypt at these costs takes tens of milliseconds and 16 MB: slow for a guesser, fine for a person.
_N, _R, _P = 2 ** 14, 8, 1
_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


def validate_app_name(name: object) -> str:
    """Lower-case letters, digits and hyphens, no hyphen at either end."""
    if not isinstance(name, str) or not name:
        raise ManifestError("name", "required: lower-case letters, digits and hyphens")
    if len(name) > NAME_MAX:
        raise ManifestError("name", f"at most {NAME_MAX} characters")
    if not _NAME.match(name):
        raise ManifestError("name", "lower-case letters, digits and hyphens, no hyphen at either end")
    return name


def default_app_name(tool_id: str) -> str:
    """The hub tool's own name: `acme.leads-db` -> `leads-db`."""
    return tool_id.split("@", 1)[0].split(".", 1)[-1]


def validate_password(password: object) -> str:
    if not isinstance(password, str) or not (PASSWORD_MIN <= len(password) <= PASSWORD_MAX):
        raise ManifestError("password", f"{PASSWORD_MIN} to {PASSWORD_MAX} characters")
    return password


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    """`scrypt$n$r$p$salt$hash`, a fresh salt each time."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time; a malformed or missing hash never matches."""
    if not stored or not isinstance(password, str) or len(password) > PASSWORD_MAX:
        return False
    try:
        kind, n, r, p, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        want = _unb64(digest)
        got = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=len(want))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)
