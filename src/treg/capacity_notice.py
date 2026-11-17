"""What a caller is told when treg's own provider account refused a call for want of credit or
quota. Shared by `/call/` (which sets the header), the light CLI and MCP (which render it).

The caller cannot fix this one: it is not their balance and not their key. So the notice names no
vendor wallet, top-up page or internal mechanism; it says the tool is out on treg's side, that the
team knows (the capacity events behind it alert), and what the caller can do meanwhile."""

CAPACITY_HEADER = "X-Treg-Capacity"
EXHAUSTED = "exhausted"


def _treg_refusal(headers, body, errors: frozenset[str]) -> bool:
    """treg itself refused with one of `errors`: not a provider's answer and not a replay (which
    marks every stored error `X-Treg-Error`)."""
    detail = body.get("detail") if isinstance(body, dict) else None
    return (bool(headers.get("X-Treg-Error")) and not headers.get("X-Treg-Idempotent-Replay")
            and isinstance(detail, dict) and detail.get("error") in errors)


def is_balance_refusal(headers, body) -> bool:
    """treg itself refused for the team's balance (`error: insufficient_balance`), not `route_max_cost`."""
    return _treg_refusal(headers, body, frozenset({"insufficient_balance"}))


def is_admission_refusal(headers, body) -> bool:
    """treg's policy refused its own money for this call (`funds_required`, `own_key_required`)."""
    return _treg_refusal(headers, body, frozenset({"funds_required", "own_key_required"}))


def account_out_notice(headers) -> str | None:
    """The one-line notice for a `/call/` answer's headers, or None when treg's account is not out.
    "Not charged" is said only when the call's cost header says 0."""
    if headers.get(CAPACITY_HEADER) != EXHAUSTED:
        return None
    not_charged = " You were not charged." if headers.get("X-Treg-Cost-Micro") == "0" else ""
    return ("Sorry, this tool is temporarily unavailable on treg's side. We've been notified and "
            f"will restore it shortly.{not_charged} You can try another tool meanwhile.")
