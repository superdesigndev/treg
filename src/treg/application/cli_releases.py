"""Which binary CLI release a channel runs, and where its files are.

The operator names each channel's release (`cli_latest_version` for stable, `cli_beta_version` for
beta, which falls back to stable's). A value that is empty or below 1.0.0 (a Python CLI release,
which `client_notice` still words notices from) leaves the channel to the newest GitHub release of
`cli_release_repo` tagged `v<version>` from 1.0.0 up (the repository's 0.x tags are the registry's
own releases); stable skips pre-releases.
"""

from __future__ import annotations

import re

from ..config import get_settings
from ..infra import github_releases

PLATFORMS = frozenset({("darwin", "amd64"), ("darwin", "arm64"), ("linux", "amd64"), ("linux", "arm64"),
                       ("windows", "amd64"), ("windows", "arm64")})
TAG_PREFIX = "v"
_VERSION = re.compile(r"^[A-Za-z0-9._+-]{1,40}$")


def is_binary_release(version: str) -> bool:
    """The binary CLI starts at 1.0.0; a 0.x value names a Python CLI release, never an archive."""
    if not _VERSION.fullmatch(version or ""):
        return False
    major = version.split(".", 1)[0]
    return major.isdigit() and int(major) >= 1


def archive_name(version: str, os: str, arch: str) -> str:
    return f"treg_{version}_{os}_{arch}." + ("zip" if os == "windows" else "tar.gz")


async def current(channel: str) -> str:
    """The release `channel` runs now, or "" when it has none."""
    settings = get_settings()
    named = (settings.cli_beta_version or settings.cli_latest_version) if channel == "beta" \
        else settings.cli_latest_version
    if is_binary_release(named):
        return named
    for release in await github_releases.published(settings.cli_release_repo):
        version = release.tag.removeprefix(TAG_PREFIX)
        if (release.tag.startswith(TAG_PREFIX) and is_binary_release(version)
                and (channel == "beta" or not release.prerelease)):
            return version
    return ""


def manifest(version: str, channel: str, os: str, arch: str) -> dict:
    """Where a CLI on `os`/`arch` fetches `version` and the files that verify it."""
    base = get_settings().cli_download_base_url.replace("{version}", version).rstrip("/")
    archive = archive_name(version, os, arch)
    return {"version": version, "channel": channel, "os": os, "arch": arch, "archive": archive,
            "url": f"{base}/{archive}", "checksums_url": f"{base}/SHA256SUMS",
            "signature_url": f"{base}/SHA256SUMS.minisig"}
