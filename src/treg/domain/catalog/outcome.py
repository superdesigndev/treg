"""The outcome of one answer to a v2 row: hit, miss or error, judged from the row's one outcome
declaration (`expect`, `error`, `miss`). Billing, routing and stats all read it here."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from .. import plugins
from .schema.paths import holds, leaf_paths, relative, select


@dataclass(frozen=True)
class Outcome:
    state: Literal["hit", "miss", "error"]
    # Which rule decided: the status, the `expect` rule, the `error` rule, the `miss` rule, or a
    # 204, which carries no answer. Empty for a hit.
    cause: Literal["", "status", "expect", "error", "miss", "no_content"] = ""


def _holds(rule: dict, endpoint: dict, doc: object, request: plugins.Request | None,
           answer: plugins.Answer | None) -> bool:
    """An outcome rule's `when` or `plugin`; a rule with neither (a status miss) holds."""
    if "plugin" in rule:
        return plugins.call(rule["plugin"], endpoint["provider"], request, answer) is True
    return "when" not in rule or holds(rule["when"], {"response": doc})


def declares_miss(endpoint: dict) -> bool:
    """The row says what a 2xx that found nothing looks like."""
    rule = endpoint.get("miss")
    return isinstance(rule, dict) and "status" not in rule


def miss_holds(endpoint: dict, doc: object, request: plugins.Request | None = None,
               answer: plugins.Answer | None = None) -> bool:
    """The row's 2xx miss rule holds on this answer."""
    return declares_miss(endpoint) and _holds(endpoint["miss"], endpoint, doc, request, answer)


def status_miss(endpoint: dict, status: int, doc: object, request: plugins.Request | None = None,
                answer: plugins.Answer | None = None) -> bool:
    """A non-2xx answer the row declares a miss: its status is listed and its `when` holds."""
    rule = endpoint.get("miss")
    return (isinstance(rule, dict) and status in (rule.get("status") or ())
            and _holds(rule, endpoint, doc, request, answer))


def not_found(endpoint: dict, status: int, doc: object) -> bool:
    """Routing only: the answer says the target itself does not exist."""
    rule = endpoint.get("not_found")
    return isinstance(rule, dict) and status in rule["status"] and _holds(rule, endpoint, doc, None, None)


def expect_failure(endpoint: dict, doc: object) -> str | None:
    """`path=value` of the first `expect` condition when the rule does not hold, else None."""
    rule = endpoint.get("expect")
    if rule is None or holds(rule, {"response": doc}):
        return None
    path = leaf_paths(rule)[0]
    found = select(path, {"response": doc})
    shown = json.dumps(found[0] if found else None, ensure_ascii=True, default=str)[:80]
    return f"{relative(path) or '.'}={shown}"


def judge(endpoint: dict, status: int, doc: object, *, request: plugins.Request | None = None,
          answer: plugins.Answer | None = None) -> Outcome:
    """The classification of one answer with status `status` and parsed body `doc` (None when the
    body is not JSON). For an async row the answer is the terminal document."""
    if not 200 <= status < 300:
        return Outcome("miss", "status") if status_miss(endpoint, status, doc, request, answer) \
            else Outcome("error", "status")
    if expect_failure(endpoint, doc) is not None:
        return Outcome("error", "expect")
    if isinstance(endpoint.get("error"), dict) and _holds(endpoint["error"], endpoint, doc, request, answer):
        return Outcome("error", "error")
    if status == 204:
        return Outcome("miss", "no_content")
    if miss_holds(endpoint, doc, request, answer):
        return Outcome("miss", "miss")
    return Outcome("hit")
