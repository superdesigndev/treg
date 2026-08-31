"""Tool, project, and upstream deny policy shared by call, run, and resource surfaces."""

from urllib.parse import urlsplit

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ...models import DenyRule, Tool
from ..identity.access import Caller


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


def _deny_match(rules: list[DenyRule], host: str, path: str, method: str) -> DenyRule | None:
    """The FIRST rule that matches — pure, so it unit-tests without a DB (like `localrun.check_deny`).

    An empty field on a rule means "any", so `{method: "DELETE"}` blocks every delete and
    `{host: "api.stripe.com"}` blocks that upstream entirely. Host is compared case-insensitively;
    the path match is a prefix, anchored at `/` so `/v1/charges` cannot be dodged by `/v1/chargesX`.
    """
    host, method = host.lower(), method.upper()
    path = path or "/"
    for r in rules:
        if r.host and r.host.lower() != host:
            continue
        if r.method and r.method.upper() != method:
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
    caller: Caller, url: str, method: str, db: AsyncSession, tool_project_id: int | None = None
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
    rule = _deny_match(rules, parts.netloc, parts.path, method)
    if rule is None:
        return
    why = f" ({rule.note})" if rule.note else ""
    scope = "this team" if rule.user_id is None else "you"
    in_proj = " in this project" if rule.project_id is not None else ""
    raise AccessPolicyError(
        f"blocked by a policy rule on {scope}{in_proj}{why} — "
        f"{rule.method or 'any'} {rule.host or 'any host'}{rule.path_prefix or ''}")
