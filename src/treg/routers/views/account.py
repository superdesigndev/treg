"""The account views: bare `treg` (home), `treg balance` and `treg feedback get`."""

from __future__ import annotations

import json

from .catalog import GET_HELP


def usd(micro) -> str:
    """micro-USD as money, sign outside the dollar mark; under a dollar, four decimals, so a
    sub-cent charge never reads as free."""
    try:
        v = int(micro) / 1_000_000
    except (TypeError, ValueError):
        return "-"
    sign, v = ("-" if v < 0 else ""), abs(v)
    return f"{sign}${v:,.2f}" if v >= 1 else f"{sign}${v:.4f}"


def home_description(catalog_tools: str | None = None) -> str:
    return f"The tool catalog for your agent. Search and call {catalog_tools or 'its'} tools"


def home_help(state: dict) -> list[str]:
    """Next steps as runnable commands, most useful first. Runtime values stay `<placeholders>`."""
    out: list[str] = []
    if state.get("unavailable"):
        out.append("treg config   # which registry this CLI talks to")
    elif state.get("registry_error"):
        out.append("treg   # again in a minute, the registry answered with a server error")
    elif not state.get("account"):
        out.append("treg login   # or treg login --token <key> with a team key")
    elif not state.get("team") and state.get("teams"):
        out.append("treg org use <team>   # pick a team; treg org ls lists them")
    elif not state.get("team"):
        out.append("treg org create \"<team name>\"   # or treg invites to see invites")
    elif state.get("balance_micro") is not None and state["balance_micro"] <= 0:
        out.append("treg topup   # catalog calls on treg's key spend the balance")
    out += ["treg catalog search \"<task>\"   # find a tool for a job, with prices",
            GET_HELP]
    if state.get("own_tools"):
        out.append("treg call <tool>/<path>   # an own_tools entry; <path> is the upstream API's own")
    out.append("treg --help   # every command")
    return out


def credential_refused(status: int, body: dict) -> dict:
    """Home state for a 401 (the token is not valid) or 403 (valid, but refused), with the
    registry's reason."""
    detail = body.get("detail")
    reason = detail if isinstance(detail, str) and detail else None
    if status == 401:
        return {"account": None, "account_error": "token rejected" + (f": {reason}" if reason else "")}
    return {"account": None, "account_error": "refused" + (f": {reason}" if reason else "")}


def home(state: dict, *, treg: str | None = None, registry: str | None = None) -> tuple[dict, list[str]]:
    """The home view. `treg` (version and path) and `registry` (when it is not the default one) are
    the client's own lines; the registry renders the rest."""
    team = state.get("team")
    if team and state.get("role"):
        team += f" ({state['role']}" + (f"; 1 of {state['teams']} teams)" if (state.get("teams") or 0) > 1 else ")")
    problem = state.get("unavailable") or state.get("registry_error")
    return ({"treg": treg, "about": home_description(state.get("catalog_tools")),
             "registry": registry,
             "account": None if problem else state.get("account") or state.get("account_error") or "not signed in",
             "team": (team or "none") if state.get("account") else None,
             "balance": usd(state["balance_micro"]) if "balance_micro" in state else None,
             "own_tools": state.get("own_tools")},
            home_help(state))


def balance(b: dict, *, team: str, autotopup: dict | None) -> str:
    """`treg balance` as the CLI prints it without colour. `autotopup` is the team's policy when
    billing is configured and the caller may read it."""
    lines = ["", f"  Balance  {usd(b['balance_micro'])}   ({b['balance_micro']} micro-USD)"
             + (f"   team {team}" if team else "")]
    blocks = b.get("blocks") or []
    if blocks:
        lines += ["", "  credit"]
        earned = [blk for blk in blocks if blk["kind"] == "earned"]
        for blk in blocks:
            if blk["kind"] == "earned" and len(earned) > 1:
                continue
            lines.append(f"    {blk['kind']:<12} {usd(blk['remaining_micro']):>10} left  "
                         f"of {usd(blk['amount_micro'])} granted {(blk.get('created_at') or '')[:10]}")
        if len(earned) > 1:
            lines.append(f"    {'earned':<12} {usd(sum(x['remaining_micro'] for x in earned)):>10} left  "
                         f"of {usd(sum(x['amount_micro'] for x in earned))} from {len(earned)} sales")
    holds = b.get("holds") or []
    if holds:
        lines += ["", "  in flight (held)"]
        for h in holds:
            lines.append(f"    {usd(h['amount_micro']):>10}  {h.get('endpoint_id') or '-'}")
    if autotopup and autotopup.get("enabled"):
        lines.append(f"  auto top-up  on - add {usd(autotopup['amount_micro'])} when it drops "
                     f"below {usd(autotopup['threshold_micro'])}  (cap {usd(autotopup['monthly_cap_micro'])}/mo, "
                     f"{usd(autotopup['month_spend_micro'])} used)")
    elif autotopup and autotopup.get("disabled_reason"):
        lines.append(f"  auto top-up off - {autotopup['disabled_reason']}. Run `treg topup --auto on` after "
                     f"fixing your card.")
    entries = b.get("entries") or {}
    items = entries.get("items") or []
    if not items and entries.get("withheld"):
        return "\n".join([*lines, "", "  the ledger is shown to team admins - `treg calls` lists this team's calls.", "", ""])
    if not items:
        return "\n".join([*lines, "", "  no ledger activity yet - nothing has been spent.", "", ""])
    lines += ["", "  recent ledger"]
    for e in items:
        when = (e.get("created_at") or "")[:19].replace("T", " ")
        what = e.get("endpoint_id") or (e.get("meta") or {}).get("reason") or ""
        lines.append(f"    {when}  {e['kind']:<9} {usd(e['amount_micro']):>10}  {what}")
    return "\n".join([*lines, "", ""])


FEEDBACK_REFUSALS = {
    401: ("authentication_required", "Sign in with `treg login`, or check the configured token."),
    403: ("access_denied", "Check the active team and your token's permissions."),
    404: ("not_found", "No feedback is available with this ID in the active team. Check the ID and team."),
    422: ("invalid_feedback", "Check the fields with `treg feedback submit --help`. "
          "Messages must contain 1-2000 characters; at most 100 call IDs are allowed."),
    429: ("rate_limited", "This team has reached its feedback submission limit. Try again later."),
}


def feedback(status: int, body) -> str:
    """`treg feedback get`: the report, or a refusal that never echoes the registry's body."""
    if status >= 400:
        code, message = FEEDBACK_REFUSALS.get(status, ("request_failed", "Could not retrieve feedback. Try again later."))
        return json.dumps({"error": code, "message": message, "http_status": status}) + "\n"
    return as_json(body)


def as_json(body) -> str:
    """A body as the CLI prints a registry answer it does not render: indented JSON."""
    return json.dumps(body, indent=2) + "\n"
