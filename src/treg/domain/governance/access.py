"""Tool, project, and upstream deny policy shared by call, run, and resource surfaces."""

from collections.abc import Iterable
from urllib.parse import urlsplit

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ...models import DenyRule, Tool
from ..identity.access import Caller


def pinned_tag_predicates(column, pinned_tags: dict | None) -> list:
    """Match every server-enforced pin in SQL, before pagination or loading result bodies.

    Missing/NULL tags fail closed. An unpinned reader retains the enclosing org scope.
    JSON string extraction is portable across the supported SQLite and PostgreSQL engines.
    """
    return [column[key].as_string() == value for key, value in (pinned_tags or {}).items()]


class AccessPolicyError(Exception):
    """A tool or project ACL refusal translated by the calling interface."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _tool_allowed(caller: Caller, tool_name: str) -> bool:
    """Per-member tool ACL: allowed if the member's `tool_access` is unset (NULL = ALL tools) or names
    this tool. The OWNER is never restricted (the org's authority); admins/members can be."""
    if caller.role == "owner":
        return True
    access = caller.membership.tool_access
    return access is None or tool_name in access


def _require_tool_access(caller: Caller, tool_name: str) -> None:
    """Gate any use of a tool (proxy call + both run tiers) on the member's tool ACL."""
    if not _tool_allowed(caller, tool_name):
        raise AccessPolicyError(
            f"you don't have access to the tool {tool_name!r} in this team — an admin can grant it "
            "(dashboard → Team, or `treg org access <you> --tools …`)")


def _project_allowed(caller: Caller, tool: Tool) -> bool:
    """Per-member PROJECT scope, the coarse dial above the per-tool one.

    NULL `project_access` = the whole org (the default, so nothing changed when projects landed), and a
    tool with NULL `project_id` is ORG-WIDE and always in scope — which is every tool that existed
    before projects. Owner is never restricted, matching `_tool_allowed`. Pure: `project_access` holds
    project IDs, so this is a set test with no query, even on the proxy's hot path."""
    if caller.role == "owner":
        return True
    access = caller.membership.project_access
    return access is None or tool.project_id is None or tool.project_id in access


def usable_tools(caller: Caller, tools) -> list[Tool]:
    """The team's tools this caller may use: what `/tools` lists and bare `treg` names."""
    return [t for t in tools if _tool_usable(caller, t)]


def _tool_usable(caller: Caller, tool: Tool) -> bool:
    """The two ACL axes compose as AND: the project scope AND the per-tool list must both allow it.
    `project_access=[X]` with `tool_access=NULL` therefore means "every tool in project X, including
    ones added later" — the composition that makes the coarse dial useful on its own."""
    return _tool_allowed(caller, tool.name) and _project_allowed(caller, tool)


def _require_tool_use(caller: Caller, tool: Tool) -> None:
    """Gate any use of a tool (proxy call + both run tiers) on BOTH ACL axes."""
    _require_tool_access(caller, tool.name)
    if not _project_allowed(caller, tool):
        raise AccessPolicyError(
            f"the tool {tool.name!r} belongs to a project you're not scoped to — an admin can grant it "
            "(dashboard → Team, or `treg org access <you> --projects …`)")


# Request parts an upstream may read as the method to run instead of the one the request was sent
# with: Meta's Graph API runs `GET ?method=post` (or `delete`) as that method; Rails, Laravel,
# Symfony, Spring and Express apps take `_method`; `X-HTTP-Method-Override` is the header Google's
# APIs and most web frameworks honour, `X-Method-Override` a common variant, and `X-HTTP-Method`
# the OData one.
_OVERRIDE_PARAMS = frozenset({"method", "_method"})
_OVERRIDE_HEADERS = frozenset({b"x-http-method-override", b"x-method-override", b"x-http-method"})


def method_overrides(
    headers: Iterable[tuple[bytes, bytes]], query: Iterable[tuple[str, str]],
) -> tuple[str, ...]:
    """Every method a request asks its upstream to run in place of the one it was sent with,
    uppercased: each override header and query parameter, names and values case-insensitive, a
    comma list read as several. Nothing here changes what is relayed."""
    values = [value.decode("latin-1") for name, value in headers if name.lower() in _OVERRIDE_HEADERS]
    values += [value for name, value in query if name.lower() in _OVERRIDE_PARAMS]
    return tuple(dict.fromkeys(
        part.strip().upper() for value in values for part in value.split(",") if part.strip()))


def _deny_match(rules: list[DenyRule], host: str, path: str, method: str,
                overrides: Iterable[str] = ()) -> DenyRule | None:
    """The FIRST rule that matches — pure, so it unit-tests without a DB (like `localrun.check_deny`).

    An empty field on a rule means "any", so `{method: "DELETE"}` blocks every delete and
    `{host: "api.stripe.com"}` blocks that upstream entirely. Host is compared case-insensitively;
    the path match is a prefix, anchored at `/` so `/v1/charges` cannot be dodged by `/v1/chargesX`.
    A method rule matches the method the request was sent with and each of its `overrides`
    (`method_overrides`), so an override can add a refusal and never remove one.
    """
    host = host.lower()
    methods = {m.upper() for m in (method, *overrides)}
    path = path or "/"
    for r in rules:
        if r.host and r.host.lower() != host:
            continue
        if r.method and r.method.upper() not in methods:
            continue
        if r.path_prefix:
            p = (r.path_prefix if r.path_prefix.startswith("/") else "/" + r.path_prefix).rstrip("/")
            # Anchored at a segment boundary: `/v1/charges` must NOT match `/v1/chargesX`.
            if not (path == p or path.startswith(p + "/")):
                continue
        return r
    return None


async def _org_deny_rules(caller: Caller, db: AsyncSession) -> list[DenyRule]:
    """This caller's applicable rules: the org-wide ones plus the ones aimed at them specifically."""
    return list((await db.execute(select(DenyRule).where(
        DenyRule.org_id == caller.org_id,
        or_(DenyRule.user_id.is_(None), DenyRule.user_id == caller.membership.user_id),
    ))).scalars().all())


async def enforce_deny(
    caller: Caller, url: str, method: str, db: AsyncSession, tool_project_id: int | None = None,
    *, overrides: tuple[str, ...] = (),
) -> None:
    """Block a call the org's policy forbids. Deliberately applies to EVERY role including owner: a
    deny rule is a guardrail, not a permission tier — an owner who disagrees deletes the rule rather
    than quietly bypassing it. The refusal names the rule, mirroring `localrun.check_deny`'s
    "a refusal can name its source".

    `tool_project_id` = the project of the tool this call goes through (every enforcement point has
    resolved a Tool by then). A project-scoped rule (`project_id` set) fires only on that project's
    tools; an org-wide-tool call (`tool_project_id` None) is never caught by one."""
    rules = [r for r in await _org_deny_rules(caller, db)
             if r.project_id is None or r.project_id == tool_project_id]
    if not rules:
        return  # the common path costs one indexed query and nothing else
    try:
        parts = urlsplit(url)
    except ValueError:
        return
    rule = _deny_match(rules, parts.netloc, parts.path, method, overrides)
    if rule is None:
        return
    why = f" ({rule.note})" if rule.note else ""
    scope = "this team" if rule.user_id is None else "you"
    in_proj = " in this project" if rule.project_id is not None else ""
    asked = (f" (this {method.upper()} asks the upstream to run it as {rule.method.upper()})"
             if rule.method and rule.method.upper() != method.upper() else "")
    raise AccessPolicyError(
        f"blocked by a policy rule on {scope}{in_proj}{why} — "
        f"{rule.method or 'any'} {rule.host or 'any host'}{rule.path_prefix or ''}{asked}")
