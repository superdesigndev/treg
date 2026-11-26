"""The shape of a name a team chooses for its tools, secrets and skills."""

import unicodedata

MAX_LENGTH = 128


def check_name(name: str) -> str:
    if not 1 <= len(name) <= MAX_LENGTH:
        raise ValueError(f"a name is 1-{MAX_LENGTH} characters")
    if any(unicodedata.category(c).startswith("C") for c in name):
        raise ValueError("a name cannot contain control or invisible characters")
    return name
