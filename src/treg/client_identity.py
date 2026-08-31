"""Framework-neutral normalization for self-reported client identities."""

import re


def _norm_client(raw: str) -> str:
    """A runtime name as a short slug. One spelling for both ends of the roster: what an incoming
    header is stored as, and what `promoted_from` must match to hide a detected pair."""
    return re.sub(r"[^a-z0-9-]", "",
                  raw.strip().lower().split("/", 1)[0])[:32]  # "claude-code/1.2" → "claude-code"
