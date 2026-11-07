"""What a caller is told when treg's own provider account refused a call for want of credit or
quota. Shared by `/call/` (which sets the header), the light CLI and MCP (which render it).

The caller cannot fix this one: it is not their balance and not their key. So the notice names no
vendor wallet, top-up page or internal mechanism; it says the tool is out on treg's side, that the
team knows (the capacity events behind it alert), and what the caller can do meanwhile."""

CAPACITY_HEADER = "X-Treg-Capacity"
EXHAUSTED = "exhausted"


def is_balance_refusal(headers, body) -> bool:
    """treg itself refused for the team's balance (`error: insufficient_balance`). Not a provider's
    402, not a replay (which marks every stored error `X-Treg-Error`), not `route_max_cost`."""
    detail = body.get("detail") if isinstance(body, dict) else None
    return (bool(headers.get("X-Treg-Error")) and not headers.get("X-Treg-Idempotent-Replay")
            and isinstance(detail, dict) and detail.get("error") == "insufficient_balance")


# treg's refusals for the whole team's money, which no other item of a batch escapes until a person
# acts, with what that person does. A tag's limits are left out: the next item may carry another tag.
STOP_REFUSALS = {
    "insufficient_balance": "the team's balance is below this call's cost; top it up (`treg topup`)",
    "platform_daily_cap_reached": "the team reached its daily limit for calls on treg's keys; "
                                  "it resets at 00:00 UTC",
}


def stop_refusal(headers, body) -> str | None:
    """The kind of a treg refusal for the team's money (`STOP_REFUSALS`), else None. A replay marks
    every stored error `X-Treg-Error`, so it never counts."""
    detail = body.get("detail") if isinstance(body, dict) else None
    if not headers.get("X-Treg-Error") or headers.get("X-Treg-Idempotent-Replay") \
            or not isinstance(detail, dict):
        return None
    kind = detail.get("error")
    return kind if kind in STOP_REFUSALS else None


def account_out_notice(headers) -> str | None:
    """The one-line notice for a `/call/` answer's headers, or None when treg's account is not out.
    "Not charged" is said only when the call's cost header says 0."""
    if headers.get(CAPACITY_HEADER) != EXHAUSTED:
        return None
    not_charged = " You were not charged." if headers.get("X-Treg-Cost-Micro") == "0" else ""
    return ("Sorry, this tool is temporarily unavailable on treg's side. We've been notified and "
            f"will restore it shortly.{not_charged} You can try another tool meanwhile.")
