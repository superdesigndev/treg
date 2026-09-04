"""Pure runtime semantics and state transitions for deferred async settlement."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit


PENDING = "pending"
SETTLED = "settled"
RELEASED = "released"
TIMED_OUT = "timed_out"
TERMINAL_STATUSES = frozenset({SETTLED, RELEASED, TIMED_OUT})
MAX_AGE = timedelta(hours=24)


class ExtractionError(ValueError):
    """A provider submission no longer matches its frozen async descriptor."""


@dataclass(frozen=True)
class Submission:
    task_id: str
    poll_url: str | None


def json_path(document: object, dotted: str) -> object:
    current = document
    for part in dotted.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


def extract_submission(descriptor: dict, response: object) -> Submission:
    task_id = json_path(response, str(descriptor.get("id_from") or ""))
    if task_id in (None, ""):
        raise ExtractionError("submission response does not contain the async task id")
    poll = descriptor.get("poll") or {}
    poll_url = None
    if poll.get("url_from"):
        value = json_path(response, str(poll["url_from"]))
        if not isinstance(value, str) or not value:
            raise ExtractionError("submission response does not contain the async poll URL")
        parsed = urlsplit(value)
        hosts = {str(host).lower() for host in poll.get("url_hosts") or []}
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in hosts:
            raise ExtractionError("submission poll URL is not on the descriptor allow-list")
        poll_url = value
    return Submission(task_id=str(task_id), poll_url=poll_url)


def classify_terminal(descriptor: dict, response: object) -> str:
    status_rule = descriptor.get("status") or {}
    value = json_path(response, str(status_rule.get("path") or ""))
    if value is None:
        return "progress"
    status = str(value)
    if status in {str(item) for item in status_rule.get("success", [])}:
        return "success"
    if status in {str(item) for item in status_rule.get("failure", [])}:
        return "failure"
    return "progress"


def artifact(descriptor: dict, terminal: object) -> dict:
    """What a successful terminal response lets the caller retrieve, read per the descriptor.

    `result.path` mode yields the raw value plus the first URL-shaped string in it; `result.fetch`
    mode yields the retrieval target as `{endpoint, name, value}` (the artifact lives behind one
    more call and treg never downloads media) for the caller to format. `ttl_note` passes through
    so every display can say how long the address stays valid. Shared by the CLI awaiter, the
    settlement worker's views and the dashboard - one reading of the descriptor, not three.
    """
    rule = (descriptor or {}).get("result") or {}
    view: dict = {"result": None, "result_url": None, "fetch": None,
                  "ttl_note": str(rule["ttl_note"]) if rule.get("ttl_note") else None}
    if rule.get("path"):
        value = json_path(terminal, str(rule["path"]))
        view["result"] = value
        candidates = value if isinstance(value, list) else [value]
        view["result_url"] = next(
            (item for item in candidates
             if isinstance(item, str) and item.startswith(("https://", "http://"))), None)
        return view
    fetch_rule = rule.get("fetch_param") or {}
    value = json_path(terminal, str(fetch_rule.get("value_from") or ""))
    if rule.get("fetch") and fetch_rule.get("name") and value not in (None, ""):
        view["fetch"] = {"endpoint": str(rule["fetch"]), "name": str(fetch_rule["name"]),
                         "value": str(value)}
    return view


def fetch_command(fetch: dict) -> str:
    """The `treg call` line that retrieves a fetch-mode artifact, shell-safe: the value is the
    provider's, so it is quoted wherever a human might paste the line (CLI stderr, the dashboard)."""
    return f"treg call {shlex.quote(str(fetch['endpoint']))} -p " \
           f"{shlex.quote(str(fetch['name']) + '=' + str(fetch['value']))}"


def shown(value: object, limit: int = 120) -> str:
    """A provider string made safe to print on a terminal: control characters and escape bytes
    are shown as escapes so a task id or status value cannot forge lines or drive the terminal."""
    text = str(value)
    text = "".join(ch if ch.isprintable() and ch not in "\x7f" else ch.encode("unicode_escape").decode()
                   for ch in text)
    return text if len(text) <= limit else text[:limit] + "…"


def next_check(now: datetime, attempts: int) -> datetime:
    """First retry is 30 seconds; later retries grow to the frozen 60-second ceiling."""
    return now + timedelta(seconds=min(60, 30 + max(0, attempts) * 10))


def expired(created_at: datetime, now: datetime) -> bool:
    return now - created_at >= MAX_AGE
