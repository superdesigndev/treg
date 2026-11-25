"""An email as the hash each ad platform's customer-list upload matches on.

Every platform takes SHA-256 of the normalized address as 64 lowercase hex characters:
- LinkedIn Matched Audiences: lowercase, no whitespace
  (learn.microsoft.com/linkedin/marketing/matched-audiences/create-and-manage-segment-users).
- Meta Custom Audiences: trim, lowercase
  (developers.facebook.com/docs/marketing-api/audiences/guides/custom-audiences).
- Google Customer Match: no whitespace, lowercase, and for gmail.com and googlemail.com only, no dots
  and no `+suffix` in the local part
  (developers.google.com/data-manager/api/devguides/concepts/formatting).
"""

from __future__ import annotations

import hashlib
import re

PLATFORMS = ("linkedin", "meta", "google")
_GOOGLE_MAIL = frozenset({"gmail.com", "googlemail.com"})
_SPACE = re.compile(r"\s+")


def normalize(email: str, platform: str) -> str | None:
    """The address as `platform` hashes it, or None when it is not one `local@domain`."""
    if platform not in PLATFORMS:
        raise ValueError(f"unknown ad platform {platform!r}")
    if not isinstance(email, str):
        return None
    address = _SPACE.sub("", email).lower()
    local, at, domain = address.rpartition("@")
    if not at or not local or not domain:
        return None
    if platform == "google" and domain in _GOOGLE_MAIL:
        local = local.split("+", 1)[0].replace(".", "")
        if not local:
            return None
    return f"{local}@{domain}"


def hashes(email: str) -> dict[str, str] | None:
    """`{platform: sha256 hex}` for every platform, or None when `email` is not an address."""
    out = {}
    for platform in PLATFORMS:
        normalized = normalize(email, platform)
        if normalized is None:
            return None
        out[platform] = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return out
