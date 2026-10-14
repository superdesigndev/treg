"""Pure identity checks for the normalized output of a routed work-email lookup."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit


_LOCAL_PART = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+\Z")
_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def _dns_domain(value: str) -> str | None:
    """Canonical DNS spelling, without inferring employer aliases or parent domains."""
    try:
        domain = value.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    labels = domain.split(".")
    if len(domain) > 253 or len(labels) < 2:
        return None
    if not all(_DOMAIN_LABEL.fullmatch(label) for label in labels):
        return None
    return domain


def _requested_domain(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        parsed = urlsplit(value if "://" in value else "//" + value)
        if parsed.username is not None or parsed.password is not None:
            return None
        host = parsed.hostname
    except ValueError:
        return None
    # An absolute website URL is a permitted way to specify the company domain. DNS's
    # terminal root dot is insignificant, but `www` and other subdomains are identities.
    return _dns_domain(host.removesuffix(".")) if host else None


def _name(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    return "".join(char for char in decomposed if char.isalnum())


def email_identity_mismatch(identity: dict, output: dict) -> str | None:
    """Return a PII-free reason when a found address contradicts the requested identity.

    Call after the adapter's ordinary miss/required-output checks. Missing name echoes
    are allowed; present names are compared only with corresponding requested names.
    This is an identity guard, not a mailbox deliverability check. It accepts DNS work
    addresses with an ASCII dot-atom local part, without claiming support for quoted
    local parts, SMTPUTF8 local parts, employer aliases, or nicknames.
    """
    email = output.get("email")
    if not isinstance(email, str) or email.count("@") != 1:
        return "email_invalid"
    local, domain = email.rsplit("@", 1)
    email_domain = _dns_domain(domain)
    if (
        not local
        or len(local) > 64
        or not _LOCAL_PART.fullmatch(local)
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or email_domain is None
        or len(local) + len(email_domain) + 1 > 254
    ):
        return "email_invalid"

    requested_domain = identity.get("domain")
    if requested_domain not in (None, ""):
        if _requested_domain(requested_domain) != email_domain:
            return "email_domain_mismatch"

    for field in ("full_name", "first_name", "last_name"):
        actual = output.get(field)
        if actual in (None, ""):
            continue
        if not isinstance(actual, str) or not _name(actual):
            return f"{field}_mismatch"
        expected = identity.get(field)
        if field == "full_name" and expected in (None, ""):
            first, last = identity.get("first_name"), identity.get("last_name")
            if isinstance(first, str) and isinstance(last, str) and first.strip() and last.strip():
                expected = f"{first} {last}"
        if isinstance(expected, str) and _name(expected) and _name(actual) != _name(expected):
            return f"{field}_mismatch"
    return None
