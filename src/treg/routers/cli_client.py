"""What the binary CLI asks the registry about itself: where its releases are (`/cli/latest`) and
where its anonymous usage events go (`/cli/events`).

Usage events carry no token and no identity: the relay accepts a fixed vocabulary per event, drops
anything else, forwards without the caller's address (no geolocation) and limits each address.
A registry without a PostHog key accepts and drops them, so a self-hosted CLI sends nowhere else.
"""

from __future__ import annotations

import json
import re
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response

from .. import analytics, client_ip
from ..config import get_settings

app = APIRouter()

_MAX_BODY = 64 * 1024
_MAX_EVENTS = 50
_RATE_WINDOW_S = 60.0
_RATE_MAX = 30
_RATE_KEYS = 10_000
_SKEW = timedelta(days=7)

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_VERSION = re.compile(r"^[A-Za-z0-9._+-]{1,40}$")
_ENDPOINT_ID = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+$")
_OPTION = re.compile(r"^-{1,2}[A-Za-z][\w-]{0,40}$")


def _text(pattern: str):
    rx = re.compile(pattern)
    return lambda v: v if isinstance(v, str) and rx.fullmatch(v) else None


def _int(low: int, high: int):
    return lambda v: v if isinstance(v, int) and not isinstance(v, bool) and low <= v <= high else None


def _bool(v):
    return v if isinstance(v, bool) else None


def _options(v):
    if not isinstance(v, list) or len(v) > 10:
        return None
    kept = [o for o in v if isinstance(o, str) and _OPTION.fullmatch(o)]
    return kept or None


_COMMON = {
    "os": _text(r"[A-Za-z0-9_]{1,32}"),
    "arch": _text(r"[A-Za-z0-9_]{1,16}"),
    "channel": _text(r"stable|beta"),
}
_VOCABULARY: dict[str, dict[str, Any]] = {
    "cli_command_completed": _COMMON | {
        "command": _text(r"[a-z][a-z0-9_]{0,63}"),
        "exit_code": _int(-1000, 1000),
        "success": _bool,
        "duration_ms": _int(0, 86_400_000),
        "session": _text(r"[A-Za-z0-9._:-]{1,64}"),
        "agent": _text(r"[a-z0-9-]{1,32}"),
        "cli_version": lambda v: v if isinstance(v, str) and _VERSION.fullmatch(v) else None,
        "error": _text(r"[A-Za-z0-9_.:-]{1,64}"),
        "status_class": _text(r"[1-5]xx"),
        "tool": lambda v: v if isinstance(v, str) and len(v) <= 128 and _ENDPOINT_ID.fullmatch(v) else None,
        "options": _options,
    },
    "cli_install_step": _COMMON | {
        "install_step": _text(r"started|installed|failed"),
        "reason": _text(r"[a-z_]{0,32}"),
        "method": _text(r"[a-z0-9_-]{0,32}"),
        "prior_version": _text(r"[A-Za-z0-9._+-]{0,40}"),
        "version": _text(r"[A-Za-z0-9._+-]{0,40}"),
        "on_path": _bool,
        "ad_link": _bool,
        "with_token": _bool,
    },
}

_hits: OrderedDict[str, list[float]] = OrderedDict()


def _admit(address: str) -> bool:
    """A sliding window per address, in process memory: losing it on restart costs nothing."""
    now = time.monotonic()
    recent = [t for t in _hits.pop(address, []) if now - t < _RATE_WINDOW_S]
    if len(recent) >= _RATE_MAX:
        _hits[address] = recent
        return False
    recent.append(now)
    _hits[address] = recent
    while len(_hits) > _RATE_KEYS:
        _hits.popitem(last=False)
    return True


def _timestamp(value: Any) -> str:
    now = datetime.now(timezone.utc)
    if isinstance(value, str):
        try:
            when = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            when = None
        if when is not None and when.tzinfo is not None and abs(now - when) <= _SKEW:
            return when.astimezone(timezone.utc).isoformat()
    return now.isoformat()


def _clean(raw: Any) -> tuple[str, str, dict, str, str | None] | None:
    if not isinstance(raw, dict):
        return None
    event, distinct_id = raw.get("event"), raw.get("distinct_id")
    rules = _VOCABULARY.get(event) if isinstance(event, str) else None
    if rules is None or not isinstance(distinct_id, str) or not _UUID.fullmatch(distinct_id):
        return None
    given = raw.get("properties") if isinstance(raw.get("properties"), dict) else {}
    props: dict[str, Any] = {}
    for key, check in rules.items():
        if key in given and (value := check(given[key])) is not None:
            props[key] = value
    props |= {"$process_person_profile": False, "$geoip_disable": True}
    uuid = raw.get("uuid") if isinstance(raw.get("uuid"), str) and _UUID.fullmatch(raw["uuid"]) else None
    return event, distinct_id, props, _timestamp(raw.get("timestamp")), uuid


@app.post("/cli/events", status_code=204)
async def cli_events(request: Request) -> Response:
    """Anonymous CLI usage, batched by the CLI. Always 204 for a well-formed request: an event the
    vocabulary does not know is dropped, never reported back."""
    if not _admit(client_ip.of(request)):
        raise HTTPException(status_code=429, detail="too many telemetry requests")
    body = await request.body()
    if len(body) > _MAX_BODY:
        raise HTTPException(status_code=413, detail="telemetry batch too large")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="telemetry batch is not JSON") from exc
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list) or len(events) > _MAX_EVENTS:
        raise HTTPException(status_code=400, detail=f"send {{\"events\": [...]}} with at most {_MAX_EVENTS} events")
    for raw in events:
        if cleaned := _clean(raw):
            event, distinct_id, props, timestamp, uuid = cleaned
            analytics.relay(distinct_id, event, props, timestamp=timestamp, uuid=uuid)
    return Response(status_code=204)


_PLATFORMS = {("darwin", "amd64"), ("darwin", "arm64"), ("linux", "amd64"), ("linux", "arm64"),
              ("windows", "amd64"), ("windows", "arm64")}
_GITHUB_TTL_S = 600.0
_github_cache: dict[str, tuple[float, str]] = {}


def _binary_release(version: str) -> bool:
    """The binary CLI starts at 1.0.0; a 0.x value names a Python CLI release, never an archive."""
    if not _VERSION.fullmatch(version or ""):
        return False
    major = version.split(".", 1)[0]
    return major.isdigit() and int(major) >= 1


def archive_name(version: str, os: str, arch: str) -> str:
    return f"treg_{version}_{os}_{arch}." + ("zip" if os == "windows" else "tar.gz")


async def _github_latest(repo: str, channel: str) -> str:
    """The newest `cli-v*` release on GitHub: a stable channel skips pre-releases."""
    key = f"{repo}:{channel}"
    cached = _github_cache.get(key)
    if cached and time.monotonic() - cached[0] < _GITHUB_TTL_S:
        return cached[1]
    version = ""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"https://api.github.com/repos/{repo}/releases",
                                 params={"per_page": 30},
                                 headers={"Accept": "application/vnd.github+json"})
        if r.status_code == 200:
            for release in r.json():
                tag = release.get("tag_name") or ""
                if release.get("draft") or not tag.startswith("cli-v"):
                    continue
                if channel == "stable" and release.get("prerelease"):
                    continue
                version = tag.removeprefix("cli-v")
                break
    except (httpx.HTTPError, ValueError):
        version = ""
    _github_cache[key] = (time.monotonic(), version)
    return version


@app.get("/cli/latest")
async def cli_latest(
    channel: str = Query("stable", pattern="^(stable|beta)$"),
    os: str = Query(..., pattern="^[a-z]{1,16}$"),
    arch: str = Query(..., pattern="^[a-z0-9]{1,16}$"),
) -> dict:
    """The release a CLI on this channel and platform should run, and where to fetch and verify it."""
    if (os, arch) not in _PLATFORMS:
        raise HTTPException(status_code=404, detail=f"no treg build for {os}/{arch}")
    settings = get_settings()
    version = (settings.cli_beta_version or settings.cli_latest_version) if channel == "beta" \
        else settings.cli_latest_version
    if not _binary_release(version):
        version = await _github_latest(settings.cli_release_repo, channel)
    if not _binary_release(version):
        raise HTTPException(status_code=404, detail=f"no treg release on the {channel} channel")
    base = settings.cli_download_base_url.replace("{version}", version).rstrip("/")
    archive = archive_name(version, os, arch)
    return {"version": version, "channel": channel, "os": os, "arch": arch, "archive": archive,
            "url": f"{base}/{archive}", "checksums_url": f"{base}/SHA256SUMS",
            "signature_url": f"{base}/SHA256SUMS.minisig"}
