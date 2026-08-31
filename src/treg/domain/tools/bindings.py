"""Credential binding and local-run inject rules for team-owned tools.

The interface layer supplies the caller facts (email, admin verdict) and the injector
vocabulary, and translates the raised rule errors into HTTP shapes.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from ... import localrun
from ...models import Secret
from . import SecretOwnershipError, ToolConfigError


async def require_secret_ownership(secret: Secret, *, caller_email: str,
                                   caller_is_admin: bool) -> None:
    """A member may bind/inject only a secret they OWN; admins/owners may use any team secret (they set
    up shared tools). Without this, a member could attach a teammate's key to a tool they control and
    exfiltrate it — via the proxy (an attacker `base_url`) or `/grant` on a local-run tool."""
    if not (secret.owner == caller_email or caller_is_admin):
        raise SecretOwnershipError(
            f"you can only bind a secret you own — secret {secret.id} belongs to another member "
            "(ask an org admin to wire up a shared-key tool)")


async def validate_bindings(bindings: list[dict], *, org_id: int, caller_email: str,
                            caller_is_admin: bool, known_injectors: frozenset,
                            db: AsyncSession, grandfather: frozenset = frozenset()) -> None:
    for b in bindings:
        # A `platform_setting` binding injects one of TREG's own credentials (a tier-4 provider key, the
        # Google Ads developer token) — relay resolves it from settings and never looks at secret_id, so
        # a caller-supplied one would be a straight read of our key through any tool they register.
        # Only the server builds these (_provider_bindings / _platform_bindings); user input never may.
        if b.get("platform_setting"):
            raise ToolConfigError(
                "a binding may not name a platform_setting — treg's own credentials are server-managed "
                "(they are attached by `connections connect`, or injected by the marketplace ladder)")
        injector = b.get("injector", "env")
        if injector not in known_injectors:  # unknown injector 500s the proxy at call time — reject now
            raise ToolConfigError(f"unknown injector {injector!r}")
        fmt = b.get("format", "{secret}")  # rendered as fmt.format(secret=…) on the hot path
        if not isinstance(fmt, str):
            raise ToolConfigError("binding format must be a string")
        try:
            fmt.format(secret="x")  # an unexpected placeholder / literal brace would KeyError/ValueError → 500
        except (KeyError, IndexError, ValueError):
            raise ToolConfigError(f"invalid binding format {fmt!r} — use only {{secret}}")
        # name/secret_field, if present, feed httpx header/param setters and the JSON extractor —
        # a null or non-string there AttributeErrors on the hot path; location must be header|query.
        for key in ("name", "secret_field"):
            if key in b and not (isinstance(b[key], str) and b[key]):
                raise ToolConfigError(f"binding {key} must be a non-empty string")
        loc = b.get("location", "header")
        if loc not in ("header", "query"):
            raise ToolConfigError("binding location must be 'header' or 'query'")
        sid = b.get("secret_id")
        secret = await db.get(Secret, sid) if sid is not None else None
        if secret is None or secret.org_id != org_id:
            raise ToolConfigError(f"binding secret_id {sid} not found")
        if sid not in grandfather:  # a binding already on the tool is grandfathered (don't lock the owner out on edit)
            await require_secret_ownership(  # can't ADD a teammate's secret
                secret, caller_email=caller_email, caller_is_admin=caller_is_admin)
    # Two bindings with the same target name silently overwrite each other at call time (the first
    # credential is dropped) — reject the collision at registration, for BOTH query and header
    # (header names are case-insensitive; `httpx.Headers[name]=…` overwrites just like a query param).
    qnames = [b.get("name", "Authorization") for b in bindings if b.get("location", "header") == "query"]
    qdupes = sorted({n for n in qnames if qnames.count(n) > 1})
    if qdupes:
        raise ToolConfigError(f"duplicate query binding name(s): {qdupes}")
    hnames = [b.get("name", "Authorization").lower() for b in bindings if b.get("location", "header") == "header"]
    hdupes = sorted({n for n in hnames if hnames.count(n) > 1})
    if hdupes:
        raise ToolConfigError(f"duplicate header binding name(s): {hdupes}")


def validate_cli_profile(cli: dict | None) -> None:
    """A malformed local-run profile is a config error (not a write-through) — a bad deny regex or
    inject shape must fail HERE, never at grant time (localrun.check_deny skips uncompilable legacy
    patterns)."""
    if cli is None:
        return
    try:
        localrun.validate_cli_profile(cli)
    except ValueError as exc:
        raise ToolConfigError(str(exc))


async def validate_cli_secrets(cli: dict | None, *, org_id: int, caller_email: str,
                               caller_is_admin: bool, db: AsyncSession,
                               grandfather: frozenset = frozenset()) -> None:
    """Ownership check for secrets a cli.inject entry names by secret_id — same rule as bindings, so a
    member can't launder a teammate's secret into a local-run tool and extract it via /grant."""
    if not cli:
        return
    for e in cli.get("inject") or []:
        sid = e.get("secret_id")
        if sid is None:
            continue
        secret = await db.get(Secret, sid)
        if secret is None or secret.org_id != org_id:
            raise ToolConfigError(f"cli.inject secret_id {sid} not found")
        if sid not in grandfather:
            await require_secret_ownership(
                secret, caller_email=caller_email, caller_is_admin=caller_is_admin)
