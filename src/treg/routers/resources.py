"""Secret, tool, skill, and bundle HTTP routes."""

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .. import convert as _convert
from .. import crypto, health, sandbox as demo_sandbox
from .. import providers as _providers
from .. import skills as _skills
from ..config import get_settings
from ..infra.db import get_session
from ..domain.governance import access as access_policy
from ..domain.governance import sandbox as sandbox_policy
from ..domain.tools import SecretOwnershipError, ToolConfigError
from ..domain.tools import bindings as binding_rules
from ..domain.tools import bundles as bundle_rules
from ..domain.identity.access import (
    Caller,
    _can_manage,
    _require_can_register,
    _role_at_least,
    require_member,
)
from ..infra.upstream import injectors
from ..models import Bundle, Secret, Tool
from .orgs import _resolve_project


# The app alias preserves the moved handlers' original decorator text byte-for-byte.
app = APIRouter()
crud_router = app

# Moved helpers retain their api.py-relative provider import while Stage 3 preserves source bytes.
sys.modules.setdefault("treg.routers.providers", _providers)
sys.modules.setdefault("treg.routers.convert", _convert)
sys.modules.setdefault("treg.routers.skills", _skills)


def _require_tool_use_http(caller: Caller, tool: Tool) -> None:
    try:
        access_policy._require_tool_use(caller, tool)
    except access_policy.AccessPolicyError as exc:
        raise HTTPException(status_code=403, detail=exc.detail) from exc


class SecretIn(BaseModel):
    name: str
    value: str
    kind: str = "env"
    bundle_id: int | None = None


class SecretUpdate(BaseModel):
    name: str | None = None
    value: str | None = None
    kind: str | None = None


async def _visible_secret_ids(caller: Caller, db: AsyncSession) -> set[int] | None:
    """The secret ids a tool-restricted member may SEE: the ones wired into their allowed tools
    (HTTP bindings + cli.inject). None = unrestricted (owner / NULL tool_access) — show all. The
    ACL isn't just a call gate: listings must not reveal credentials the member can't use."""
    if caller.role == "owner" or caller.membership.tool_access is None:
        return None
    tools = (await db.execute(select(Tool).where(Tool.org_id == caller.org_id))).scalars().all()
    ids: set[int] = set()
    for t in tools:
        if not access_policy._tool_usable(caller, t):
            continue
        ids |= {b.get("secret_id") for b in (t.bindings or []) if b.get("secret_id") is not None}
        ids |= {e.get("secret_id") for e in ((t.cli or {}).get("inject") or []) if e.get("secret_id") is not None}
    return ids


@app.post("/secrets")
async def create_secret(
    body: SecretIn, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    _require_can_register(caller)
    try:
        await sandbox_policy.enforce_sandbox_cap(
            sandbox=demo_sandbox.is_sandbox(caller.org), org_id=caller.org_id,
            model=Secret, cap=demo_sandbox.MAX_SECRETS, noun="secrets", db=db)
    except sandbox_policy.SandboxLimitError as exc:
        raise HTTPException(status_code=422, detail=(
            f"the sandbox is limited to {exc.cap} {exc.noun} — sign up for more")) from exc
    await _validate_bundle_id(body.bundle_id, caller.org_id, db)
    secret = Secret(
        org_id=caller.org_id, name=body.name, owner=caller.email, kind=body.kind,
        value=crypto.encrypt(body.value), bundle_id=body.bundle_id,
    )
    db.add(secret)
    await db.commit()
    return _secret_view(secret)


@app.get("/secrets")
async def list_secrets(
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    rows = (await db.execute(select(Secret).where(Secret.org_id == caller.org_id))).scalars().all()
    visible = await _visible_secret_ids(caller, db)
    if visible is not None:  # tool-restricted member: only the keys wired into their allowed tools
        rows = [s for s in rows if s.id in visible]
    return [_secret_view(s) for s in rows]


@app.patch("/secrets/{secret_id}")
async def update_secret(
    secret_id: int,
    body: SecretUpdate,
    caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> dict:
    secret = await db.get(Secret, secret_id)
    if secret is None or secret.org_id != caller.org_id:
        raise HTTPException(status_code=404, detail="secret not found")
    if not _can_manage(caller, secret):
        raise HTTPException(status_code=403, detail="only the creator or an admin can edit this secret")
    _require_not_live_demo_secret(caller, secret)
    fields = body.model_dump(exclude_unset=True)
    for k in ("name", "value", "kind"):  # these map to NOT-NULL columns; explicit null is a 422, not a 500
        if k in fields and fields[k] is None:
            raise HTTPException(status_code=422, detail=f"{k} cannot be null")
    # A kind change drives refresh + health + extraction shape; validate a JSON-kind actually has a
    # JSON value (else the tool silently 502s later) and reset the now-meaningless health verdict.
    if "kind" in fields and fields["kind"] != secret.kind:
        if fields["kind"] in ("oauth", "secret_file"):
            raw = fields["value"] if "value" in fields else crypto.decrypt(secret.value)
            try:
                json.loads(raw)
            except (ValueError, TypeError):
                raise HTTPException(status_code=422, detail=f"kind {fields['kind']!r} needs a JSON value")
        secret.health_status, secret.health_detail, secret.health_checked_at = "unknown", "", None
    if "value" in fields:
        fields["value"] = crypto.encrypt(fields["value"])  # re-encrypt on rotate
        # The value is exactly what health measures — a rotation invalidates the prior verdict.
        secret.health_status, secret.health_detail, secret.health_checked_at = "unknown", "", None
    for k, v in fields.items():
        setattr(secret, k, v)
    await db.commit()
    return _secret_view(secret)


@app.delete("/secrets/{secret_id}")
async def delete_secret(
    secret_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    secret = await db.get(Secret, secret_id)
    if secret is None or secret.org_id != caller.org_id:
        raise HTTPException(status_code=404, detail="secret not found")
    if not _can_manage(caller, secret):
        raise HTTPException(status_code=403, detail="only the creator or an admin can delete this secret")
    _require_not_live_demo_secret(caller, secret)
    # bindings live in a JSON column — scan tools IN THIS ORG (registry-scale N is small).
    tools = (await db.execute(select(Tool).where(Tool.org_id == caller.org_id))).scalars().all()
    if any(b.get("secret_id") == secret_id for t in tools for b in t.bindings):
        raise HTTPException(status_code=409, detail="secret is referenced by a tool binding")
    # a secret used only by a local-run inject (not an HTTP binding) would otherwise be silently
    # deletable, breaking `treg run` — guard those references too.
    if any((e.get("secret_id") == secret_id) for t in tools for e in ((t.cli or {}).get("inject") or [])):
        raise HTTPException(status_code=409, detail="secret is referenced by a tool's local-run (cli) profile")
    await db.delete(secret)
    await db.commit()
    return {"deleted": secret_id}


def _require_not_live_demo_secret(caller: Caller, secret: Secret) -> None:
    """Companion guard for the seeded STRIPE_KEY the live tool is bound to."""
    if (demo_sandbox.is_sandbox(caller.org) and get_settings().demo_stripe_key
            and secret.name == "STRIPE_KEY"):
        raise HTTPException(status_code=403, detail=(
            "STRIPE_KEY powers the live stripe demo — add your own keys instead"))


async def _validate_bundle_id(bundle_id: int | None, org_id: int, db: AsyncSession) -> None:
    try:
        await bundle_rules.require_bundle_in_org(bundle_id, org_id, db)
    except ToolConfigError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc


def _secret_view(s: Secret) -> dict:
    return {"id": s.id, "name": s.name, "kind": s.kind, "owner": s.owner, "bundle_id": s.bundle_id}


class ToolIn(BaseModel):
    name: str
    base_url: str
    bundle_id: int | None = None
    # Multi-binding (explicit) — each: {secret_id, injector, location, name, format, secret_field}
    bindings: list[dict] | None = None
    # Single-binding sugar (the common case): provide secret_id + placement, get one binding.
    secret_id: int | None = None
    injector: str = "env"
    auth_in: str = "header"
    auth_name: str = "Authorization"
    auth_format: str = "Bearer {secret}"
    secret_field: str = "access_token"
    health_check: dict | None = None  # {method, path, expect_status}
    examples: list[dict] | None = None  # [{method, path, note}]
    cli: dict | None = None  # local-run profile for `treg run` (docs/CLI-RUN-PLAN.md)
    project: str | int | None = None  # project slug or id; None = org-wide (the default)


class ToolUpdate(BaseModel):
    base_url: str | None = None
    bindings: list[dict] | None = None
    health_check: dict | None = None
    examples: list[dict] | None = None
    cli: dict | None = None  # set/replace the local-run profile; explicit null clears it
    project: str | int | None = None  # move between projects; explicit null makes it org-wide


def _host_of(url: str) -> str:
    try:
        return urlsplit(url).netloc.lower()
    except ValueError:  # e.g. unbalanced IPv6 brackets "http://[::1" → don't 500, reject the input
        raise HTTPException(status_code=422, detail="base_url is not a valid URL")


def _normalize_scheme(rest: str) -> str:
    """A path param collapses `https://` to `https:/`; restore it."""
    for sch in ("https:/", "http:/"):
        if rest.startswith(sch) and not rest.startswith(sch + "/"):
            return sch + "/" + rest[len(sch):]
    return rest


def _flat_binding(body: ToolIn) -> dict:
    return {
        "secret_id": body.secret_id,
        "injector": body.injector,
        "location": body.auth_in,
        "name": body.auth_name,
        "format": body.auth_format,
        "secret_field": body.secret_field,
    }


def _require_not_live_demo_tool(caller: Caller, tool: Tool) -> None:
    """The sandbox's seeded live-wire tool (`stripe`, pinned base) is the demo's centerpiece —
    editing or removing it would break the visitor's own live pane, so refuse. Only the seeded
    name is frozen; visitor-created tools stay fully editable. No-op outside sandboxes / with
    the wire off."""
    if (demo_sandbox.is_sandbox(caller.org) and get_settings().demo_stripe_key
            and tool.name == "stripe" and demo_sandbox.is_live_tool(tool)):
        raise HTTPException(status_code=403, detail=(
            "the live stripe demo endpoint is part of the sandbox — add your own endpoints instead"))


def _require_public_base_url(base_url: str) -> None:
    """A tool's base_url is fetched server-side by the proxy — reject internal / loopback / cloud-metadata
    targets so a member can't turn `treg call` into an SSRF (e.g. base_url=169.254.169.254). Reuses the
    same block-list the webhook path already uses. DNS names are allowed (best-effort)."""
    if not health.safe_webhook_url(base_url):
        raise HTTPException(status_code=422, detail=(
            "base_url must be a public http(s) address — loopback, private, link-local, and cloud-"
            "metadata hosts are refused"))


async def _require_secret_ownership(secret: Secret, caller: Caller) -> None:
    try:
        await binding_rules.require_secret_ownership(
            secret, caller_email=caller.email,
            caller_is_admin=_role_at_least(caller.role, "admin"))
    except SecretOwnershipError as exc:
        raise HTTPException(status_code=403, detail=exc.detail) from exc


async def _validate_bindings(bindings: list[dict], caller: Caller, db: AsyncSession,
                             grandfather: frozenset = frozenset()) -> None:
    try:
        await binding_rules.validate_bindings(
            bindings, org_id=caller.org_id, caller_email=caller.email,
            caller_is_admin=_role_at_least(caller.role, "admin"),
            known_injectors=frozenset(injectors.INJECTORS), db=db, grandfather=grandfather)
    except SecretOwnershipError as exc:
        raise HTTPException(status_code=403, detail=exc.detail) from exc
    except ToolConfigError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc


def _validate_cli_profile(cli: dict | None) -> None:
    try:
        binding_rules.validate_cli_profile(cli)
    except ToolConfigError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc


async def _validate_cli_secrets(cli: dict | None, caller: Caller, db: AsyncSession,
                                grandfather: frozenset = frozenset()) -> None:
    try:
        await binding_rules.validate_cli_secrets(
            cli, org_id=caller.org_id, caller_email=caller.email,
            caller_is_admin=_role_at_least(caller.role, "admin"), db=db, grandfather=grandfather)
    except SecretOwnershipError as exc:
        raise HTTPException(status_code=403, detail=exc.detail) from exc
    except ToolConfigError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc


def _allowed_server_bins() -> set[str]:
    """The commands `treg run --server` may execute: catalog-known CLIs + an admin allow-list. Blocks a
    member naming `bash`/`python` to run arbitrary code as the server user (docs/CLI-RUN-PLAN.md Option A)."""
    from . import providers as prov
    bins = {(e.get("cli") or {}).get("bin") for e in prov.CATALOG}
    bins.discard(None)
    extra = get_settings().run_allowed_bins
    bins |= {b.strip() for b in extra.split(",") if b.strip()}
    return bins  # type: ignore[return-value]


@app.post("/tools")
async def create_tool(
    body: ToolIn, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    _require_can_register(caller)
    try:
        await sandbox_policy.enforce_sandbox_cap(
            sandbox=demo_sandbox.is_sandbox(caller.org), org_id=caller.org_id,
            model=Tool, cap=demo_sandbox.MAX_TOOLS, noun="endpoints", db=db)
    except sandbox_policy.SandboxLimitError as exc:
        raise HTTPException(status_code=422, detail=(
            f"the sandbox is limited to {exc.cap} {exc.noun} — sign up for more")) from exc
    if body.bindings is not None:
        bindings = body.bindings
    elif body.secret_id is not None:
        bindings = [_flat_binding(body)]
    else:
        bindings = []  # a public upstream needing no credential is allowed
    _require_public_base_url(body.base_url)  # no SSRF to internal/metadata hosts via the proxy
    await _validate_bindings(bindings, caller, db)
    await _validate_bundle_id(body.bundle_id, caller.org_id, db)
    _validate_cli_profile(body.cli)
    await _validate_cli_secrets(body.cli, caller, db)
    project = await _resolve_project(body.project, caller.org_id, db)
    tool = Tool(
        org_id=caller.org_id, name=body.name, owner=caller.email, base_url=body.base_url,
        host=_host_of(body.base_url), bindings=bindings, health_check=body.health_check,
        examples=body.examples or [], cli=body.cli, bundle_id=body.bundle_id,
        project_id=project.id if project else None,
    )
    db.add(tool)
    try:
        await db.commit()
    except IntegrityError:
        raise HTTPException(status_code=409, detail=f"tool name {body.name!r} already exists in this org")
    return _tool_view(tool)


@app.get("/tools")
async def list_tools(
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    rows = (await db.execute(select(Tool).where(Tool.org_id == caller.org_id))).scalars().all()
    # The per-member tool ACL hides what it gates: a restricted member's listing shows only their tools.
    return [_tool_view(t) for t in rows if access_policy._tool_usable(caller, t)]


@app.get("/tools/by-name/{name}")
async def get_tool_by_name(
    name: str, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Name-keyed lookup so shareable detail URLs (/app/tools/<name>) resolve without an id."""
    tool = (await db.execute(
        select(Tool).where(Tool.org_id == caller.org_id, Tool.name == name)
    )).scalars().first()
    if tool is None:
        raise HTTPException(status_code=404, detail="tool not found")
    _require_tool_use_http(caller, tool)  # a 403 names the fix (ask an admin) — clearer than a fake 404
    return _tool_view(tool)


@app.patch("/tools/{tool_id}")
async def update_tool(
    tool_id: int,
    body: ToolUpdate,
    caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> dict:
    tool = await db.get(Tool, tool_id)
    if tool is None or tool.org_id != caller.org_id:
        raise HTTPException(status_code=404, detail="tool not found")
    if not _can_manage(caller, tool):
        raise HTTPException(status_code=403, detail="only the creator or an admin can edit this tool")
    _require_not_live_demo_tool(caller, tool)
    fields = body.model_dump(exclude_unset=True)
    if "base_url" in fields and fields["base_url"] is None:  # NOT-NULL column + feeds _host_of — 422, not 500
        raise HTTPException(status_code=422, detail="base_url cannot be null")
    if fields.get("base_url"):
        _require_public_base_url(fields["base_url"])  # no SSRF to internal/metadata hosts
    # Secrets ALREADY on the tool are grandfathered on edit — only a NEWLY-added binding/inject must be
    # owned by the caller. Otherwise re-saving a tool an admin wired with a shared key locks its owner out.
    grandfather = frozenset(
        {b.get("secret_id") for b in tool.bindings if b.get("secret_id") is not None}
        | {e.get("secret_id") for e in ((tool.cli or {}).get("inject") or []) if e.get("secret_id") is not None}
    )
    if "bindings" in fields:
        await _validate_bindings(fields["bindings"], caller, db, grandfather)
    if "cli" in fields:  # explicit null clears the profile (turns local runs off entirely)
        _validate_cli_profile(fields["cli"])
        await _validate_cli_secrets(fields["cli"], caller, db, grandfather)
    if "project" in fields:  # slug/id in, column out; explicit null = back to org-wide
        project = await _resolve_project(fields.pop("project"), caller.org_id, db)
        tool.project_id = project.id if project else None
    for k, v in fields.items():
        setattr(tool, k, v)
    if "base_url" in fields:
        tool.host = _host_of(tool.base_url)  # keep the resolution index in sync
    await db.commit()
    return _tool_view(tool)


@app.delete("/tools/{tool_id}")
async def delete_tool(
    tool_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    tool = await db.get(Tool, tool_id)
    if tool is None or tool.org_id != caller.org_id:
        raise HTTPException(status_code=404, detail="tool not found")
    if not _can_manage(caller, tool):
        raise HTTPException(status_code=403, detail="only the creator or an admin can delete this tool")
    _require_not_live_demo_tool(caller, tool)
    await db.delete(tool)
    await db.commit()
    return {"deleted": tool_id}


def _tool_view(t: Tool) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "owner": t.owner,
        "base_url": t.base_url,
        "host": t.host,
        "bindings": t.bindings,
        "health_check": t.health_check,
        "examples": t.examples or [],
        "cli": t.cli,
        # Server-computed so the dashboard never guesses: a run needs a cli profile, an allow-listed bin
        # (server config the client can't see), AND a server-injectable auth mechanism — a config_file /
        # device CLI authenticates from the member's own machine, so it's local-only (default "env" keeps
        # every pre-auth_mechanism tool server-runnable as before).
        "server_runnable": (bool(t.cli) and (t.cli.get("bin") or t.name) in _allowed_server_bins()
                            and (t.cli.get("auth_mechanism") or "env") in ("env", "argv")),
        "project_id": t.project_id,  # None = org-wide
        "bundle_id": t.bundle_id,
    }


class BundleUpdate(BaseModel):
    recipe: str | None = None  # edit the SKILL.md text of a recipe/skill bundle
    # (Run metadata moved to Tool.cli — a tool with a cli profile is runnable.)


class SkillSecretIn(BaseModel):
    local_name: str  # name within the skill; bindings reference it by this
    value: str
    kind: str = "env"


class SkillToolIn(BaseModel):
    name: str
    base_url: str
    bindings: list[dict] = []  # each binding's "secret" is a local_name, resolved server-side
    health_check: dict | None = None  # optional {method, path, expect_status}
    examples: list[dict] = []  # optional [{method, path, note}]
    cli: dict | None = None  # optional local-run profile; inject entries may reference local_names


class SkillIn(BaseModel):
    name: str
    recipe: str = ""  # the SKILL.md text
    files: dict[str, str] = {}  # companion files {relpath: content} — the rest of the skill folder
    secrets: list[SkillSecretIn] = []
    tools: list[SkillToolIn] = []
    # (Execution config — both run tiers — lives in each tool's `cli` block: bin/server/enabled/inject.)


class SkillFileIn(BaseModel):
    path: str      # the file's path relative to the picked folder (webkitRelativePath)
    content: str


class SkillAnalyzeIn(BaseModel):
    files: list[SkillFileIn] = []


class SkillImportIn(BaseModel):
    files: list[SkillFileIn] = []
    select: list[str] = []           # skill names to register (empty = every ready one)
    env_values: dict[str, str] = {}  # user-filled values for env secrets missing from the upload


# A second router keeps local grant/report between CRUD and skills in the legacy route order.
app = APIRouter()
skill_router = app


@app.post("/skills")
async def register_skill(
    body: SkillIn, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Register a skill from a raw payload (recipe + secrets + tools). The dashboard's folder importer
    and the CLI build this same payload; the shared core is `_register_skill_bundle`."""
    return await _register_skill_bundle(body, caller, db)


_SECRET_DIR_RE = bundle_rules._SECRET_DIR_RE
_sanitize_bundle_files = bundle_rules.sanitize_bundle_files


async def _register_skill_bundle(body: SkillIn, caller: Caller, db: AsyncSession) -> dict:
    _require_can_register(caller)
    if demo_sandbox.is_sandbox(caller.org):  # a skill import would create unlimited tools/secrets, past the cap
        raise HTTPException(status_code=403, detail="skill import is disabled in the sandbox")
    names = [s.local_name for s in body.secrets]  # bindings reference secrets by local_name
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:  # a duplicate would silently orphan the first secret (only the last id is kept)
        raise HTTPException(status_code=422, detail=f"duplicate secret local_name(s): {dupes}")
    files = _sanitize_bundle_files(body.files)  # drop unsafe paths / secrets before persisting
    bundle = Bundle(org_id=caller.org_id, name=body.name, owner=caller.email, recipe=body.recipe, files=files)
    db.add(bundle)
    await db.flush()  # assign bundle.id without committing yet

    local_to_id: dict[str, int] = {}
    for s in body.secrets:
        secret = Secret(
            org_id=caller.org_id, name=s.local_name, owner=caller.email, kind=s.kind,
            value=crypto.encrypt(s.value), bundle_id=bundle.id,
        )
        db.add(secret)
        await db.flush()
        local_to_id[s.local_name] = secret.id

    for t in body.tools:
        _require_public_base_url(t.base_url)  # no SSRF to internal/metadata hosts via an imported skill
        resolved: list[dict] = []
        for raw in t.bindings:
            b = dict(raw)
            local = b.pop("secret", None)  # bindings reference secrets by local_name
            if local is not None:
                if local not in local_to_id:
                    raise HTTPException(status_code=422, detail=f"binding references unknown secret {local!r}")
                b["secret_id"] = local_to_id[local]
            resolved.append(b)
        # Same gate as POST /tools: reject unknown injectors / dangling secret_ids here, or the
        # skill door persists a poison tool (missing secret_id → KeyError → 500 on every call).
        await _validate_bindings(resolved, caller, db)
        cli = dict(t.cli) if t.cli else None
        if cli:  # inject entries reference secrets by local_name too — resolve like bindings
            cli["inject"] = [dict(e) for e in cli.get("inject") or []]
            for e in cli["inject"]:
                local = e.pop("secret", None)
                if local is not None:
                    if local not in local_to_id:
                        raise HTTPException(status_code=422, detail=f"cli.inject references unknown secret {local!r}")
                    e["secret_id"] = local_to_id[local]
            _validate_cli_profile(cli)
            await _validate_cli_secrets(cli, caller, db)  # a raw secret_id in the upload must be owned too
        db.add(Tool(
            org_id=caller.org_id, name=t.name, owner=caller.email, base_url=t.base_url,
            host=_host_of(t.base_url), bindings=resolved, health_check=t.health_check,
            examples=t.examples, cli=cli, bundle_id=bundle.id,
        ))

    try:
        await db.commit()
    except IntegrityError:
        raise HTTPException(status_code=409, detail="a tool name in this skill already exists in this org")
    return await _bundle_view(bundle.id, db)


_SKILL_UPLOAD_MAX_FILES = 600


_SKILL_UPLOAD_MAX_BYTES = 2 * 1024 * 1024  # per file


_SKILL_UPLOAD_MAX_TOTAL_BYTES = 20 * 1024 * 1024  # whole upload — cap BEFORE materializing to disk


def _check_upload_size(files: list) -> None:
    """Reject an oversized folder upload early (before writing anything to disk), so a member can't
    exhaust the server with a huge `/skills/analyze|import` body. Per-file cap still applies later."""
    if len(files) > _SKILL_UPLOAD_MAX_FILES:
        raise HTTPException(status_code=413, detail=f"too many files (max {_SKILL_UPLOAD_MAX_FILES})")
    total = sum(len((getattr(f, "content", "") or "").encode("utf-8", "ignore")) for f in files)
    if total > _SKILL_UPLOAD_MAX_TOTAL_BYTES:
        raise HTTPException(status_code=413, detail="upload too large (max 20 MB total)")


def _materialize_skill_files(files: list) -> str:
    """Write uploaded skill files into a fresh temp dir so the SAME disk-based scanner the CLI uses
    (skills.scan_skills / _classify) can run on them unchanged. Paths are sanitized against traversal;
    the caller must rmtree the returned dir."""
    root = Path(tempfile.mkdtemp(prefix="treg-skill-")).resolve()
    for f in files[:_SKILL_UPLOAD_MAX_FILES]:
        rel = f.path.replace("\\", "/").lstrip("/")
        dest = (root / rel).resolve()
        if root not in dest.parents:      # a '..' path escaping the temp root — drop it
            continue
        if len(f.content.encode("utf-8", "ignore")) > _SKILL_UPLOAD_MAX_BYTES:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            dest.write_text(f.content)
        except OSError:
            continue
    return str(root)


def _scan_uploaded_skills(root: str, catalog: list, env_names: set) -> list:
    """Find every skill dir (a dir with a SKILL.md) at any depth under root and classify each with the
    CLI's own `skills._classify` — so the dashboard verdict is identical to `treg upload skills`."""
    from . import skills as sk
    dets = []
    for dirpath, _dirs, filenames in os.walk(root):
        if any(m in filenames for m in ("SKILL.md", "skill.md")):
            dets.append(sk._classify(Path(dirpath), catalog, env_names))
    dets.sort(key=lambda d: d.name)
    return dets


@app.post("/skills/analyze")
async def analyze_skill_folder(
    body: SkillAnalyzeIn, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Classify an uploaded skill folder WITHOUT registering — the dashboard's verify step. Same
    classifier as `treg upload skills`: recipe-only vs contract vs generated, plus readiness gaps."""
    _require_can_register(caller)
    if demo_sandbox.is_sandbox(caller.org):
        raise HTTPException(status_code=403, detail="skill import is disabled in the sandbox")
    _check_upload_size(body.files)
    from . import providers as prov, convert, skills as sk_mod
    root = _materialize_skill_files(body.files)
    try:
        env_path = Path(root) / ".env"
        env_names = set(prov.var_names(str(env_path))) if env_path.is_file() else set()
        dets = _scan_uploaded_skills(root, prov.CATALOG, env_names)
        existing = {b.name for b in (await db.execute(
            select(Bundle).where(Bundle.org_id == caller.org_id))).scalars().all()}
        out = []
        for d in dets:
            secs = []
            for s in d.secrets:
                if s.get("file"):
                    secs.append({"name": s["name"], "source": "file", "ref": s["file"],
                                 "present": (Path(d.path) / s["file"]).is_file()})
                elif s.get("env"):
                    secs.append({"name": s["name"], "source": "env", "ref": s["env"],
                                 "present": s["env"] in env_names})
            out.append({"name": d.name, "kind": d.kind, "base_url": d.base_url,
                        "secrets": secs, "gaps": d.gaps, "ready": d.ready,
                        "already": d.name in existing,
                        "cli": sk_mod.cli_preview(d, prov.CATALOG),
                        "recipe_chars": len(convert._read_recipe(Path(d.path)))})
        return {"skills": out}
    finally:
        shutil.rmtree(root, ignore_errors=True)


@app.post("/skills/import")
async def import_skill_folder(
    body: SkillImportIn, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Register selected skills from an uploaded folder: scan → build the payload (secret VALUES from
    the uploaded files / provided env values) → register each as a bundle. Mirrors `treg upload skills`."""
    _require_can_register(caller)
    if demo_sandbox.is_sandbox(caller.org):
        raise HTTPException(status_code=403, detail="skill import is disabled in the sandbox")
    _check_upload_size(body.files)
    from . import skills as sk, providers as prov
    root = _materialize_skill_files(body.files)
    try:
        env_path = Path(root) / ".env"
        env_names = set(prov.var_names(str(env_path))) if env_path.is_file() else set()
        env_names |= set(body.env_values or {})  # a value the user typed in the dashboard counts as present
        dets = _scan_uploaded_skills(root, prov.CATALOG, env_names)
        want = set(body.select) if body.select else {d.name for d in dets if d.ready}
        chosen = [d for d in dets if d.name in want]
        values: dict[str, str] = {}
        need = sk.env_needs(chosen)
        if need and env_path.is_file():
            values.update(prov.env_values(str(env_path), need))
        values.update(body.env_values or {})
        # Idempotent + crash-proof (like the CLI): skip anything already registered, and never let one
        # skill 500 the whole batch. A name clash on the bundle/tool/secret would otherwise raise an
        # IntegrityError on flush (not on commit, so it escaped the register helper's guard).
        existing_bundles = {b.name for b in (await db.execute(
            select(Bundle).where(Bundle.org_id == caller.org_id))).scalars().all()}
        existing_tools = {t.name for t in (await db.execute(
            select(Tool).where(Tool.org_id == caller.org_id))).scalars().all()}
        existing_secrets = {s.name for s in (await db.execute(
            select(Secret).where(Secret.org_id == caller.org_id))).scalars().all()}
        from ..infra.db import session_maker
        results = []
        for d in chosen:
            if d.gaps:
                results.append({"name": d.name, "ok": False, "error": "; ".join(d.gaps)}); continue
            secret_names = {s["name"] for s in d.secrets}
            if d.name in existing_bundles or d.name in existing_tools or (secret_names & existing_secrets):
                results.append({"name": d.name, "ok": False, "skipped": True, "error": "already registered"}); continue
            try:
                payload = sk.build_payload(d, values)
                # Each skill registers in its OWN session so a failure (bad binding, IntegrityError…)
                # can't poison the shared session for the rest of the batch (greenlet_spawn errors).
                async with session_maker() as sk_db:
                    await _register_skill_bundle(SkillIn(**payload), caller, sk_db)
                existing_bundles.add(d.name); existing_tools.add(d.name); existing_secrets |= secret_names
                results.append({"name": d.name, "ok": True, "kind": d.kind})
            except HTTPException as exc:
                results.append({"name": d.name, "ok": False, "error": str(exc.detail)})
            except Exception:  # noqa: BLE001 -- report per-skill, never 500 the batch
                # A generic message — a raw exception string could echo a fragment of an uploaded secret.
                results.append({"name": d.name, "ok": False, "error": "registration failed"})
        return {"results": results}
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def _bundle_allowed(caller: Caller, bundle: Bundle, db: AsyncSession) -> bool:
    """Skill visibility for a tool-restricted member: the access list may grant a bundle by its own
    name (recipe-only skills) or via any of its tools. Owner / NULL access see everything."""
    if caller.role == "owner" or caller.membership.tool_access is None:
        return True
    access = set(caller.membership.tool_access)
    if bundle.name in access:
        return True
    tools = (await db.execute(select(Tool.name).where(Tool.bundle_id == bundle.id))).all()
    return any(r[0] in access for r in tools)


@app.get("/bundles")
async def list_bundles(
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    rows = (await db.execute(select(Bundle).where(Bundle.org_id == caller.org_id))).scalars().all()
    return [{"id": b.id, "name": b.name, "owner": b.owner}
            for b in rows if await _bundle_allowed(caller, b, db)]


@app.get("/bundles/by-name/{name}")
async def get_bundle_by_name(
    name: str, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Name-keyed lookup so shareable detail URLs (/app/skills/<name>) resolve without an id."""
    bundle = (await db.execute(
        select(Bundle).where(Bundle.org_id == caller.org_id, Bundle.name == name)
    )).scalars().first()
    if bundle is None:
        raise HTTPException(status_code=404, detail="bundle not found")
    if not await _bundle_allowed(caller, bundle, db):
        raise HTTPException(status_code=403, detail=(
            f"you don't have access to the skill {name!r} in this team — an admin can grant it"))
    return await _bundle_view(bundle.id, db)


@app.get("/bundles/{bundle_id}")
async def get_bundle(
    bundle_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    bundle = await db.get(Bundle, bundle_id)
    if bundle is None or bundle.org_id != caller.org_id:
        raise HTTPException(status_code=404, detail="bundle not found")
    if not await _bundle_allowed(caller, bundle, db):  # `treg skill install` uses this route too
        raise HTTPException(status_code=403, detail=(
            f"you don't have access to the skill {bundle.name!r} in this team — an admin can grant it"))
    return await _bundle_view(bundle_id, db)


@app.patch("/bundles/{bundle_id}")
async def update_bundle(
    bundle_id: int, body: BundleUpdate,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Edit a bundle's SKILL.md text. Only its creator or an admin may. (Execution config lives on
    the tool's cli profile, not here.)"""
    bundle = await db.get(Bundle, bundle_id)
    if bundle is None or bundle.org_id != caller.org_id:
        raise HTTPException(status_code=404, detail="bundle not found")
    if not _can_manage(caller, bundle):
        raise HTTPException(status_code=403, detail="only the creator or an admin can edit this recipe")
    fields = body.model_dump(exclude_unset=True)  # exclude_unset so a field left out is untouched
    if fields.get("recipe") is not None:
        bundle.recipe = fields["recipe"]
    await db.commit()
    return await _bundle_view(bundle_id, db)


@app.delete("/bundles/{bundle_id}")
async def delete_bundle(
    bundle_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    bundle = await db.get(Bundle, bundle_id)
    if bundle is None or bundle.org_id != caller.org_id:
        raise HTTPException(status_code=404, detail="bundle not found")
    if not _can_manage(caller, bundle):
        raise HTTPException(status_code=403, detail="only the creator or an admin can delete this bundle")
    bundle_tools = (await db.execute(select(Tool).where(Tool.bundle_id == bundle_id))).scalars().all()
    bundle_tool_ids = {t.id for t in bundle_tools}
    bundle_secrets = (await db.execute(select(Secret).where(Secret.bundle_id == bundle_id))).scalars().all()
    # A bundle secret may be bound by a tool OUTSIDE the bundle (use-without-hold). Deleting it would
    # dangle that binding — the same invariant delete_secret guards with a 409, enforced here too.
    org_tools = (await db.execute(select(Tool).where(Tool.org_id == bundle.org_id))).scalars().all()
    outside = [t for t in org_tools if t.id not in bundle_tool_ids]
    # A bundle secret may be referenced by an outside tool's HTTP binding OR its local-run cli.inject —
    # guard BOTH (delete_secret does), else a local-run tool would dangle a missing secret_id.
    referenced = {b.get("secret_id") for t in outside for b in t.bindings}
    referenced |= {e.get("secret_id") for t in outside for e in ((t.cli or {}).get("inject") or [])}
    if any(s.id in referenced for s in bundle_secrets):
        raise HTTPException(status_code=409, detail="a bundle secret is referenced by a tool outside this bundle")
    for t in bundle_tools:
        await db.delete(t)
    for s in bundle_secrets:
        await db.delete(s)
    await db.delete(bundle)
    await db.commit()
    return {"deleted": bundle_id}


async def _bundle_view(bundle_id: int, db: AsyncSession) -> dict:
    bundle = await db.get(Bundle, bundle_id)
    tools = (await db.execute(select(Tool).where(Tool.bundle_id == bundle_id))).scalars().all()
    secrets = (await db.execute(select(Secret).where(Secret.bundle_id == bundle_id))).scalars().all()
    return {
        "id": bundle.id,
        "name": bundle.name,
        "owner": bundle.owner,
        "recipe": bundle.recipe,
        "files": bundle.files or {},   # companion files {relpath: content} — `skill install` writes these
        "tools": [_tool_view(t) for t in tools],
        "secrets": [_secret_view(s) for s in secrets],
    }
