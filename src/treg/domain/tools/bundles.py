"""Skill bundle rules: org scoping and shipped-file hygiene."""

import re

from sqlalchemy.ext.asyncio import AsyncSession

from ...models import Bundle
from . import ToolConfigError

_SECRET_DIR_RE = re.compile(r"(^|/)\.secrets?(/|$)")


async def require_bundle_in_org(bundle_id: int | None, org_id: int, db: AsyncSession) -> None:
    """A resource may only attach to a bundle in its OWN org — else it'd be counted by, rendered in,
    and swept up by a foreign org's bundle view/delete (org-scoping leak)."""
    if bundle_id is None:
        return
    bundle = await db.get(Bundle, bundle_id)
    if bundle is None or bundle.org_id != org_id:
        raise ToolConfigError(f"bundle_id {bundle_id} not found in this org")


def sanitize_bundle_files(files: dict) -> dict:
    """Defense-in-depth before persisting companion files (the CLI/dashboard already exclude these):
    drop path-traversal / absolute paths, SKILL.md (that's `recipe`), and anything under a secret dir —
    a secret must NEVER live in the shipped file blob. `skill install` re-checks on the way out too."""
    clean: dict[str, str] = {}
    for raw, content in (files or {}).items():
        p = str(raw).replace("\\", "/")
        if not p or p.startswith("/") or ".." in p.split("/"):   # absolute or traversal → drop
            continue
        if p == "SKILL.md" or _SECRET_DIR_RE.search(p):
            continue
        if not isinstance(content, str):
            continue
        clean[p] = content
    return clean
