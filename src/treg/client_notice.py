"""What a CLI is told about its own version. The registry decides and words it from the version the
CLI sends (`X-Treg-Client-Version`) and the operator's `cli_latest_version` / `cli_min_version`;
the CLI only prints the header, so a client never needs its own release logic."""

import re

NOTICE_HEADER = "X-Treg-Client-Notice"
_RELEASE = re.compile(r"[0-9]{1,9}(?:\.[0-9]{1,9}){0,2}")


def _parse(version: str) -> tuple[int, ...] | None:
    version = (version or "").strip()
    if not _RELEASE.fullmatch(version):
        return None
    parts = version.split(".")
    return tuple(int(p) for p in parts) + (0,) * (3 - len(parts))


def notice(client_version: str, *, latest: str, minimum: str) -> str | None:
    """The one-line notice for a CLI at `client_version`, or None when it is current, when its
    version is not a plain release number, or when the operator set neither version."""
    current = _parse(client_version)
    if current is None:
        return None
    newest, oldest = _parse(latest), _parse(minimum)
    if oldest and current < oldest:
        return (f"treg {client_version} is no longer supported and some commands may fail. "
                f"Run `treg update` to install {latest if newest else minimum}.")
    if newest and current < newest:
        return f"treg {latest} is available (you have {client_version}). Run `treg update`."
    return None
