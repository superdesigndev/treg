"""Credential connection workflows and transaction boundaries."""

import asyncio
import base64
from dataclasses import dataclass
from datetime import timedelta
import json
import logging
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .. import crypto, health, oauth, oauth_providers
from ..config import get_settings
from ..domain.catalog import store as catalog_store
from ..infra.db import session_maker
from ..models import PendingOAuth, Secret, Tool
from ..timeutil import as_naive as _as_naive
from ..timeutil import utcnow_naive as _utcnow_naive


def _host_of(url: str) -> str:
    return urlsplit(url).netloc.lower()


async def _free_connection_name(base: str, org_id: int, db: AsyncSession) -> str:
    """First connection for a provider keeps the bare service name; later ones get -2, -3.

    The bare name matters: every skill and doc says `treg call google-search-console`, and a tool
    name is unique per org, so the first account must own it or all of that breaks. Suffixing only
    the extras means adding a second account can never change how the first one is called.
    """
    taken = set((await db.execute(
        select(Tool.name).where(Tool.org_id == org_id)
    )).scalars().all()) | set((await db.execute(
        select(Secret.name).where(Secret.org_id == org_id)
    )).scalars().all())
    if base not in taken:
        return base
    return next(f"{base}-{n}" for n in range(2, 1000) if f"{base}-{n}" not in taken)


def _provider_bindings(provider, secret: Secret) -> list[dict]:
    """The binding list that injects `secret` the way this registry provider authenticates.

    A pasted-secret provider's value is a plain string, not an oauth blob — injecting it with
    secret_field="access_token" would try to read a JSON field that isn't there. A key may ride in
    a header (default) or a query param (Semrush's ?key=…). A provider needing a second credential
    that TREG holds (Google Ads' developer token) gets it as a platform binding — read from settings
    at call time, never copied into the org's secrets."""
    if provider.uses_pasted_secret:
        if provider.token_location == "query":
            bindings = [{
                "secret_id": secret.id, "injector": "env", "location": "query",
                "name": provider.token_param, "format": provider.token_format,
            }]
        else:
            bindings = [{
                "secret_id": secret.id, "injector": "env", "location": "header",
                "name": provider.token_header, "format": provider.token_format,
            }]
    else:
        bindings = [{
            "secret_id": secret.id, "injector": "oauth", "location": "header",
            "name": "Authorization", "format": "Bearer {secret}", "secret_field": "access_token",
        }]
    # A provider-required protocol header is a constant-format binding over the same encrypted
    # secret reference. `format` deliberately contains no {secret}: the existing injector stamps
    # the literal value after caller headers are copied, so a caller cannot accidentally select a
    # different API version. The relay remains provider-blind.
    source = {k: v for k, v in bindings[0].items()
              if k in ("secret_id", "platform_setting", "injector", "secret_field")}
    bindings.extend({**source, "location": "header", "name": name, "format": value}
                    for name, value in provider.required_headers)
    if provider.needs_extra_credential and provider.extra_credential_is_platform:
        bindings.append({
            "platform_setting": provider.extra_credential_setting, "injector": "env",
            "location": "header", "name": provider.extra_credential_header, "format": "{secret}",
        })
    return bindings


async def _autoprovision_provider_tool(
    provider, secret: Secret, pending: PendingOAuth, db: AsyncSession
) -> None:
    """Bind the freshly-connected credential to the provider's API as a callable tool.

    Named after the CONNECTION, not the provider — a tool name is unique per org, so two accounts
    on one provider need two tools. The first account's connection is named for the service, so it
    still gets the bare `google-search-console` every skill and doc refers to.

    Idempotent by (org, name): reconnecting rebinds the existing tool to the new credential rather
    than piling up duplicates."""
    tool_name = secret.name or provider.service
    existing = (
        await db.execute(
            select(Tool).where(Tool.org_id == secret.org_id, Tool.name == tool_name)
        )
    ).scalars().first()
    bindings = _provider_bindings(provider, secret)
    # A registry tool with a probe can self-validate on `health --run` instead of sitting at
    # "unchecked" until something happens to call it.
    health_check = (
        {"method": "GET", "path": provider.probe_path, "expect_status": 200}
        if provider.probe_path else None
    )
    examples = _provider_tool_examples(provider)
    if existing is not None:
        existing.bindings = bindings
        existing.base_url = provider.base_url
        existing.host = _host_of(provider.base_url)
        # Reconnecting is how an already-provisioned tool picks up a probe — or examples — added
        # to the registry since it was made.
        existing.health_check = health_check or existing.health_check
        if examples and not existing.examples:
            existing.examples = examples
    else:
        db.add(Tool(
            org_id=secret.org_id, name=tool_name, owner=pending.owner,
            base_url=provider.base_url, host=_host_of(provider.base_url),
            bindings=bindings, health_check=health_check,
            examples=examples,
        ))
    await _upsert_provider_extra_tools(provider, secret, tool_name, pending.owner, db, bindings)


async def _upsert_provider_extra_tools(
    provider, secret: Secret, tool_name: str, owner: str, db: AsyncSession,
    bindings: list[dict] | None = None,
) -> int:
    """Upsert a split-host provider's companion tools; return the number newly created.

    Connect and startup backfill deliberately share this exact write path. A new provider registry
    `extra_tools` entry therefore heals old connections on their next boot without a one-off migration.
    """
    bindings = bindings or _provider_bindings(provider, secret)
    created = 0
    for extra in getattr(provider, "extra_tools", ()) or ():
        extra_name = f"{tool_name}-{extra['suffix']}"
        extra_probe = (
            {"method": "GET", "path": extra["probe_path"], "expect_status": 200}
            if extra.get("probe_path") else None
        )
        prior = (await db.execute(
            select(Tool).where(Tool.org_id == secret.org_id, Tool.name == extra_name)
        )).scalars().first()
        if prior is not None:
            prior.bindings = bindings
            prior.base_url = extra["base_url"]
            prior.host = _host_of(extra["base_url"])
            prior.health_check = extra_probe or prior.health_check
            if extra.get("examples") and not prior.examples:
                prior.examples = extra["examples"]
        else:
            db.add(Tool(
                org_id=secret.org_id, name=extra_name, owner=owner,
                base_url=extra["base_url"], host=_host_of(extra["base_url"]),
                bindings=bindings, health_check=extra_probe,
                examples=extra.get("examples") or [],
            ))
            created += 1
    return created


async def _backfill_provider_extra_tools() -> int:
    """Heal provider connections created before their registry entry gained companion tools.

    A connection qualifies only when its provider-attributed Secret still has the expected main
    Tool and that Tool is bound to the same Secret. This avoids creating orphan companions for a
    partially-deleted connection while keeping the scan generic across all future `extra_tools`.
    """
    async with session_maker() as db:
        provider_secrets = (await db.execute(
            select(Secret).where(Secret.provider != "")
        )).scalars().all()
        candidates = [
            (secret, provider)
            for secret in provider_secrets
            if (provider := oauth_providers.get(secret.provider)) is not None
            and (getattr(provider, "extra_tools", ()) or ())
        ]
        if not candidates:
            return 0

        org_ids = {secret.org_id for secret, _ in candidates}
        tools = (await db.execute(select(Tool).where(Tool.org_id.in_(org_ids)))).scalars().all()
        by_org_name = {(tool.org_id, tool.name): tool for tool in tools}
        created = 0
        for secret, provider in candidates:
            tool_name = secret.name or provider.service
            main = by_org_name.get((secret.org_id, tool_name))
            if main is None or not any(
                binding.get("secret_id") == secret.id for binding in (main.bindings or [])
            ):
                continue
            created += await _upsert_provider_extra_tools(
                provider, secret, tool_name, main.owner or secret.owner, db)
        await db.commit()
        if created:
            logging.getLogger("treg").info("backfilled %d provider companion tool(s)", created)
        return created


CATALOG_STAMP_CAP = 12  # a tool's examples are read by a human/agent scanning, not a full API doc


def _provider_tool_examples(provider) -> list[dict]:
    """The provisioned tool's `examples`: the registry's hand-written ones first, then the endpoint
    catalog's verified core operations for the same provider.

    This is what makes a fresh connection immediately useful — the agent gets real paths with the
    inputs they need instead of guessing them from the provider's docs and burning paid calls."""
    out = [dict(e) for e in provider.examples]
    seen = {(e.get("method", "").upper(), (e.get("path") or "").lstrip("/")) for e in out}
    for ex in catalog_store.tool_examples(provider.service):
        if len(out) >= CATALOG_STAMP_CAP:
            break
        key = (ex["method"], ex["path"].lstrip("/"))
        if key in seen:
            continue
        seen.add(key)
        out.append(ex)
    return out


async def _record_connected_identity(provider, secret: Secret, blob: dict, client) -> None:
    """Ask the provider who just connected, and remember it.

    Providers with nothing to choose between (LinkedIn acts as the one member who consented) would
    otherwise show a connection with no indication of WHICH account it is. This also captures the
    id the API actually needs — LinkedIn's person URN — so the agent doesn't re-fetch it on every
    call. Best-effort: a failed lookup must never fail the connect."""
    try:
        resp = await client.get(
            f"{provider.base_url.rstrip('/')}{provider.identity_path}",
            headers={"Authorization": f"Bearer {blob.get('access_token')}"},
        )
        if resp.status_code != 200:
            return
        data = resp.json()
        ident = _dig(data, provider.identity_id_path)
        if not ident:
            return
        secret.resource_ref = provider.identity_ref_format.format(id=ident)
        label = _dig(data, provider.identity_label_path) if provider.identity_label_path else None
        secret.resource_name = str(label) if label else str(ident)
    except Exception as exc:  # noqa: BLE001
        print(f"[oauth] identity lookup failed for {provider.service}: {exc}")


def _dig(obj, dotted: str):
    """Walk a dotted path through dicts and list indices; None if any hop is missing."""
    for part in dotted.split("."):
        if isinstance(obj, list):
            try:
                obj = obj[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
        if obj is None:
            return None
    return obj


class ConnectError(Exception):
    """A framework-neutral connection refusal translated by the HTTP router."""

    def __init__(self, kind: str, detail: str):
        self.kind = kind
        self.detail = detail
        super().__init__(kind)


@dataclass(frozen=True)
class OAuthCallbackOutcome:
    kind: str


async def start_oauth_connection(
    *, org_id: int, owner: str, name: str, provider_name: str | None,
    capability: str | None, connection_id: int | None, client_id: str,
    client_secret: str, auth_uri: str, token_uri: str, scopes: list[str],
    redirect_uri: str | None,
) -> dict:
    async with session_maker() as db:
        code_verifier, auth_params, auth_method = "", "", "client_secret_post"
        cid_param, scope_sep = "client_id", " "
        long_lived = False

        if provider_name:
            provider = oauth_providers.get(provider_name)
            if provider is None:
                known = ", ".join(sorted(oauth_providers.REGISTRY))
                raise ConnectError(
                    "unknown_provider",
                    f"unknown provider {provider_name!r} (known: {known})",
                )
            chosen_capability = capability or provider.default_capability
            try:
                scopes = provider.scopes_for(chosen_capability)
                client_id, client_secret = oauth_providers.credentials(provider)
            except ValueError as exc:
                raise ConnectError("invalid_provider", str(exc)) from None
            auth_uri, token_uri = provider.auth_uri, provider.token_uri
            name = name or provider.service
            auth_method = provider.token_endpoint_auth_method
            cid_param, scope_sep = provider.client_id_param, provider.scope_separator
            long_lived = provider.long_lived_exchange
            if provider.auth_params is not None:
                auth_params = json.dumps(provider.auth_params)
            if provider.pkce:
                code_verifier = crypto.new_token()
        elif not (client_id and client_secret):
            raise ConnectError(
                "invalid_provider",
                "supply `provider` for a registry connect, or client_id + client_secret to bring your own app",
            )
        if not name:
            raise ConnectError("invalid_provider", "name is required")

        # Reconnecting targets ONE connection. Scoped to the caller's org so a guessed id can't aim a
        # consent at another org's credential, and matched to the provider so a Slack consent can't be
        # made to overwrite a Google one.
        replaces_id = None
        if connection_id is not None:
            target = (await db.execute(select(Secret).where(
                Secret.id == connection_id, Secret.org_id == org_id
            ))).scalars().first()
            if target is None:
                raise ConnectError("unknown_connection", "unknown connection")
            if provider_name and target.provider != provider_name:
                raise ConnectError(
                    "invalid_provider",
                    f"connection {connection_id} is {target.provider or 'not a provider connection'}, not {provider_name}",
                )
            replaces_id = target.id
            name = target.name

        state = crypto.new_token()
        treg_callback = f"{get_settings().public_url.rstrip('/')}/oauth/callback"
        # The code must come back to treg's OWN callback — a body-supplied redirect_uri pointing elsewhere
        # turns this into a consent-phishing URL builder (a legit provider link that routes the code away).
        if redirect_uri and redirect_uri.rstrip("/") != treg_callback:
            raise ConnectError("invalid_provider", "redirect_uri must be treg's own /oauth/callback")
        redirect_uri = redirect_uri or treg_callback
        pending = PendingOAuth(
            org_id=org_id, state=state, name=name, owner=owner,
            client_id=client_id, client_secret=crypto.encrypt(client_secret),
            auth_uri=auth_uri, token_uri=token_uri, scopes=scope_sep.join(scopes),
            redirect_uri=redirect_uri, provider=provider_name or "",
            code_verifier=code_verifier, auth_params=auth_params,
            token_endpoint_auth_method=auth_method, client_id_param=cid_param,
            scope_separator=scope_sep, long_lived_exchange=long_lived,
            replaces_secret_id=replaces_id,
        )
        db.add(pending)
        await db.commit()
        return {"state": state, "consent_url": oauth.consent_url(pending), "redirect_uri": redirect_uri}


async def complete_oauth_connection(
    *, state: str, code: str, error: str, client_factory,
) -> OAuthCallbackOutcome:
    async with session_maker() as db:
        # Hit by the BROWSER on redirect — no token; protected by the unguessable `state`.
        pending = (
            await db.execute(select(PendingOAuth).where(PendingOAuth.state == state))
        ).scalar_one_or_none()
        if pending is None:
            return OAuthCallbackOutcome("invalid")
        if pending.status != "pending":
            # A browser re-load re-hits this URL with a now-spent code; re-exchanging would fail and
            # flip a successful connect's status to "error". Return the terminal result without redoing it.
            return OAuthCallbackOutcome("done" if pending.status == "done" else "already_failed")
        if _as_naive(pending.created_at) < _utcnow_naive() - timedelta(minutes=health.OAUTH_PENDING_TTL_MIN):
            pending.status, pending.detail = "error", "expired"  # an old state must not stay redeemable
            await db.commit()
            return OAuthCallbackOutcome("expired")
        if error or not code:
            pending.status, pending.detail = "error", (error or "no authorization code")[:200]
            await db.commit()
            return OAuthCallbackOutcome("authorization_failed")

        try:
            client = client_factory()
            blob = await oauth.exchange_code(pending, code, client)
            provider = oauth_providers.get(pending.provider) if pending.provider else None
            # A consent either REPLACES one named connection or ADDS another. `replaces_secret_id` says
            # which, decided back at /oauth/start where the user's intent was known. This used to
            # blanket-replace by provider, which fixed the real bug — widening read→write silently made
            # a second google-search-console row — at the cost of banning a second account entirely.
            secret = None
            if pending.replaces_secret_id is not None:
                secret = (await db.execute(select(Secret).where(
                    Secret.id == pending.replaces_secret_id, Secret.org_id == pending.org_id
                ))).scalars().first()
                # Deleted between consent and callback: fall through and add it back rather than 500.
            if secret is None:
                secret = Secret(
                    org_id=pending.org_id,
                    name=await _free_connection_name(pending.name, pending.org_id, db),
                    owner=pending.owner,
                    kind="oauth", value=crypto.encrypt(json.dumps(blob)),
                )
                db.add(secret)
            else:
                secret.value = crypto.encrypt(json.dumps(blob))
                secret.last_error = ""
            secret.provider = pending.provider or ""
            # granted_scopes stays canonically SPACE-joined whatever dialect went over the wire, so the
            # readers (satisfied_capabilities, the health payload) can keep using a plain .split().
            # TikTok comma-joins its consent scopes; without this normalisation a whole grant would
            # come back as one bogus scope string and every capability would read as unsatisfied.
            separator = pending.scope_separator or " "
            secret.granted_scopes = " ".join(s for s in pending.scopes.split(separator) if s)
            secret.expires_at = oauth.expiry_of(blob)
            await db.flush()
            # A connect that yields no callable tool is a dead end — the user consented and got
            # nothing. Auto-provision the provider's tool bound to this credential so the very next
            # thing they can do is make a real proxied call.
            if provider and provider.can_autoprovision:
                await _autoprovision_provider_tool(provider, secret, pending, db)
            if provider and provider.has_identity:
                await _record_connected_identity(provider, secret, blob, client)
            pending.status, pending.secret_id, pending.detail = "done", secret.id, "connected"
            await db.commit()
        except Exception as exc:  # noqa: BLE001
            print(f"[oauth] token exchange failed for state {state}: {exc}")  # detail stays server-side
            pending.status, pending.detail = "error", "token exchange failed"
            await db.commit()
            return OAuthCallbackOutcome("exchange_failed")
        return OAuthCallbackOutcome("connected")


async def connect_with_pasted_secret(
    *, provider_name: str, raw_token: str, org_id: int, owner: str, client_factory,
) -> dict:
    provider = oauth_providers.get(provider_name)
    if provider is None or not provider.uses_pasted_secret:
        raise ConnectError("invalid_token_provider", "this provider is connected by consent, not a token")
    token = raw_token.strip()
    if not token:
        raise ConnectError("invalid_token", f"{provider.token_label or 'Token'} is required")
    # HTTP Basic providers (DataForSEO, Moz) take a pasted `login:password`; store the Base64 blob so
    # `Basic {secret}` renders the same at connect and on every proxy call. Both dashboards ALSO hand
    # out a ready-made Base64 credential, and users paste that at least as often as the raw pair —
    # encoding it again produced a double-encoded blob the provider 401'd. So: if the paste already IS
    # Base64 of a printable `login:password`, keep it. A raw pair can never be mistaken for one (":"
    # is not in the Base64 alphabet, so strict decoding refuses it), and a Base64 blob can never be
    # a working raw pair (it has no ":"), so the branch is unambiguous either way.
    if provider.token_encode == "base64":
        already = None
        try:
            decoded = base64.b64decode(token, validate=True).decode()
            if ":" in decoded and decoded.isprintable():
                already = token
        except Exception:  # noqa: BLE001 — not Base64, or not text: encode it below
            pass
        token = already or base64.b64encode(token.encode()).decode()

    # The credential rides in a header (default) or a query param (Semrush: ?key=…). The cheapest
    # check may also live on a different host than base_url, so honor an absolute probe_url override,
    # and a POST probe with a JSON body (Serpstat's JSON-RPC limits call).
    rendered = provider.token_format.format(secret=token)
    if provider.token_location == "query":
        headers, params = {}, {provider.token_param: rendered}
    else:
        headers, params = {provider.token_header: rendered}, {}
    headers.update(dict(provider.required_headers))
    probe_url = provider.probe_url or f"{provider.base_url.rstrip('/')}{provider.probe_path}"
    # httpx REPLACES a URL's own query string when `params=` is passed, so a probe_path like
    # `/autocomplete?field=title&text=data` (PDL, Akta, JustOneAPI, SpyFu) silently lost its required
    # params and the probe 400'd — rejecting a perfectly good key. Merge the path's query into params
    # ourselves (params, i.e. the credential for a query provider, wins on a key collision).
    split = urlsplit(probe_url)
    if split.query:
        params = {**dict(parse_qsl(split.query, keep_blank_values=True)), **params}
        probe_url = urlunsplit((split.scheme, split.netloc, split.path, "", split.fragment))
    try:
        client = client_factory()
        resp = await client.request(
            provider.probe_method or "GET", probe_url,
            headers=headers, params=params, json=provider.probe_json,
        )
    except Exception as exc:  # noqa: BLE001
        raise ConnectError(
            "provider_unreachable", f"could not reach {provider.display_name}: {exc}"
        ) from None
    # Try to parse the body as JSON regardless of the content-type header: ScrapeCreators returns a
    # real JSON body labelled `text/plain`, and gating on `application/json` left its payload empty so
    # `token_verify_field` (creditCount) read as false and a valid key was rejected. The parse is
    # defensive — a genuinely non-JSON key check (Semrush's CSV/number balance) simply throws and
    # leaves payload empty, falling through to the `text_error` branch exactly as before.
    ctype = resp.headers.get("content-type", "")
    payload: dict = {}
    if resp.status_code < 500:
        try:
            parsed = resp.json()
            payload = parsed if isinstance(parsed, dict) else {}
        except Exception:  # noqa: BLE001
            payload = {}
    # Some providers answer HTTP 200 even for a BAD key and signal validity only in the body: a JSON
    # field (Slack: "ok"; Apollo: "is_logged_in") or an "ERROR ..." text line (Semrush). An HTTP
    # status alone would happily accept a dead key, so check all three signals.
    field_bad = bool(provider.token_verify_field) and not payload.get(provider.token_verify_field)
    field_reject = bool(provider.token_reject_field) and bool(payload.get(provider.token_reject_field))
    equals_bad = bool(provider.token_ok_field) and str(payload.get(provider.token_ok_field)) != provider.token_ok_value
    # Usually any >=400 is a bad key; a provider with no free probe (Coresignal) POSTs an empty body so
    # a VALID key answers 400 — there only 401/403 mean the key itself is bad.
    status_reject = (
        resp.status_code in provider.probe_reject_statuses
        if provider.probe_reject_statuses else resp.status_code >= 400
    )
    text_error = (
        resp.status_code < 400
        and not ctype.startswith("application/json")
        and resp.text.lstrip().upper().startswith("ERROR")
    )
    if status_reject or field_bad or field_reject or equals_bad or text_error:
        why = (
            payload.get("error")
            or (payload.get("ErrorMessage") if equals_bad else None)
            or (f"{provider.token_verify_field}=false" if field_bad else None)
            or (resp.text.strip()[:80] if text_error else f"HTTP {resp.status_code}")
        )
        raise ConnectError(
            "invalid_token", f"{provider.display_name} rejected that token ({why})"
        )

    async with session_maker() as db:
        secret = (await db.execute(
            select(Secret).where(Secret.org_id == org_id, Secret.provider == provider.service)
        )).scalars().first()
        if secret is None:
            secret = Secret(
                org_id=org_id, name=provider.service, owner=owner, kind="env",
                value=crypto.encrypt(token), provider=provider.service,
            )
            db.add(secret)
        else:
            secret.value = crypto.encrypt(token)
        if provider.token_scopes_header:
            granted = resp.headers.get(provider.token_scopes_header, "")
            if granted:
                secret.granted_scopes = " ".join(x.strip() for x in granted.split(",") if x.strip())
        secret.last_error = ""
        secret.health_status, secret.health_detail = "ok", "token verified at connect"
        secret.health_checked_at = _utcnow_naive()
        if provider.has_identity:
            ident = _dig(payload, provider.identity_id_path)
            if ident:
                secret.resource_ref = provider.identity_ref_format.format(id=ident)
                label = _dig(payload, provider.identity_label_path) if provider.identity_label_path else None
                secret.resource_name = str(label) if label else str(ident)
        await db.flush()

        pending = PendingOAuth(
            org_id=org_id, state="", name=provider.service, owner=owner,
            client_id="", client_secret="", auth_uri="", token_uri="", redirect_uri="",
        )
        await _autoprovision_provider_tool(provider, secret, pending, db)
        await db.commit()
        await db.refresh(secret)
        return oauth.connection_view(secret)


async def get_oauth_status(*, state: str, org_id: int) -> dict:
    async with session_maker() as db:
        pending = (
            await db.execute(
                select(PendingOAuth).where(PendingOAuth.state == state, PendingOAuth.org_id == org_id)
            )
        ).scalar_one_or_none()
        if pending is None:
            raise ConnectError("unknown_state", "unknown oauth state")
        return {
            "status": pending.status,
            "secret_id": pending.secret_id,
            "detail": pending.detail,
            "name": pending.name,
        }


async def _owned_connection(secret_id: int, org_id: int, db: AsyncSession) -> Secret:
    secret = (
        await db.execute(
            select(Secret).where(Secret.id == secret_id, Secret.org_id == org_id)
        )
    ).scalars().first()
    if secret is None or (secret.kind != "oauth" and not secret.provider):
        raise ConnectError("unknown_connection", "unknown connection")
    return secret


async def _enrich_resource_labels(provider, resources: list[dict], token: str, client) -> None:
    """Replace id-only labels with the upstream's human name, in place.

    Runs the lookups concurrently — six sequential round-trips to Google would make the picker feel
    broken. A row whose lookup fails keeps its id: a partial list beats an error, since the user may
    not have access to every account the listing returned."""
    async def one(row: dict) -> None:
        bare = str(row["id"]).rsplit("/", 1)[-1]
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        if provider.needs_extra_credential:
            headers[provider.extra_credential_header] = provider.platform_extra_credential
        if provider.enrich_header_name:
            headers[provider.enrich_header_name] = provider.enrich_header_value.format(id=bare)
        try:
            resp = await client.post(
                f"{provider.discovery_base.rstrip('/')}{provider.enrich_path.format(id=bare)}",
                headers=headers, json=provider.enrich_body or {},
            )
            if resp.status_code == 200:
                label = _dig(resp.json(), provider.enrich_label_path)
                if label:
                    row["label"] = str(label)
        except Exception:  # noqa: BLE001 — a naming lookup must never break the picker
            pass

    await asyncio.gather(*(one(r) for r in resources if r.get("id")))


async def list_connections(*, org_id: int) -> list[dict]:
    async with session_maker() as db:
        rows = (
            await db.execute(
                select(Secret).where(
                    Secret.org_id == org_id,
                    # A connection is "something a registry connect produced", NOT "an oauth blob".
                    # Bring-your-own-token providers (Slack) store a plain string with kind "env", so a
                    # kind=="oauth" filter created them successfully and then hid them from the list.
                    or_(Secret.kind == "oauth", Secret.provider != ""),
                )
            )
        ).scalars().all()
        out = []
        for s in rows:
            view = oauth.connection_view(s)
            provider = oauth_providers.get(s.provider) if s.provider else None
            if provider is not None:
                granted = s.granted_scopes.split()
                have = provider.satisfied_capabilities(granted)
                view["capabilities"] = have
                # Providers don't backfill scopes onto an issued grant, so a capability the user never
                # consented to can only be added by re-consenting. Naming the gap here is what turns an
                # opaque upstream 403 into "reconnect to enable write".
                view["missing_capabilities"] = [c for c in provider.capabilities if c not in have]
                if not provider.extra_credential_is_platform:
                    view["extra_credential_note"] = provider.extra_credential_note
                view["extra_credential_label"] = provider.extra_credential_label
                # Outstanding only while no tool exists for this provider — once one does, the second
                # credential has been supplied and the connection is callable.
                if provider.needs_extra_credential and not provider.extra_credential_is_platform:
                    built = (await db.execute(
                        select(Tool).where(Tool.org_id == org_id, Tool.name == provider.service)
                    )).scalars().first()
                    view["needs_extra_credential"] = built is None
            out.append(view)
        out.sort(key=lambda c: (c["provider"] or "~", c["name"]))
        return out


async def list_connection_resources(
    *, secret_id: int, org_id: int, client_factory,
) -> dict:
    async with session_maker() as db:
        secret = await _owned_connection(secret_id, org_id, db)
        provider = oauth_providers.get(secret.provider)
        if provider is None or not provider.supports_discovery:
            raise ConnectError(
                "no_resource_discovery",
                f"{secret.provider or 'this provider'} has nothing to choose between — it acts on your whole account",
            )
        client = client_factory()
        await oauth.ensure_fresh(secret, db, client)  # no-op for a non-oauth secret
        # A pasted-secret (bot token / API key) secret is a PLAIN STRING, not an oauth blob — json.loads
        # on it throws. (Only header-auth pasted providers reach here; a query-key provider like Semrush
        # has nothing to discover, so supports_discovery is False and this endpoint 422s earlier.)
        raw = crypto.decrypt(secret.value)
        if provider.uses_pasted_secret:
            disc_headers = {provider.token_header: provider.token_format.format(secret=raw)}
        else:
            blob = json.loads(raw)
            token = blob.get("access_token") or blob.get("token")
            disc_headers = {"Authorization": f"Bearer {token}"}
        if provider.needs_extra_credential:  # Ads won't list accounts without the developer token
            disc_headers[provider.extra_credential_header] = provider.platform_extra_credential
        resp = await client.get(
            f"{provider.discovery_base.rstrip('/')}{provider.discover_path}",
            headers=disc_headers,
        )
        body = {}
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {}
        # Slack answers 200 with {"ok": false, "error": "missing_scope"} — status alone would report an
        # empty picker instead of naming the scope the bot is missing.
        if resp.status_code >= 400 or body.get("ok") is False:
            upstream = ""
            err = body.get("error")
            if isinstance(err, dict):
                upstream = err.get("message", "")
            elif isinstance(err, str):
                upstream = err
            if not upstream:
                upstream = (resp.text or "")[:200]
            raise ConnectError(
                "resource_discovery_failed",
                f"could not list {provider.resource_plural} ({resp.status_code}): {upstream}".strip(),
            )
        # A successful discovery call is a real authenticated request to the upstream — the strongest
        # evidence we get that this credential works. Recording it turns the connection's health from
        # "unknown" into something earned, instead of waiting for the next health sweep.
        if secret.health_status != "ok":
            secret.health_status, secret.health_detail = "ok", "listed upstream resources"
            secret.health_checked_at = _utcnow_naive()
            await db.commit()
        rows = body.get(provider.discover_key) or []
        if provider.discover_nested_key:  # e.g. GA4 properties nested inside each account summary
            rows = [n for r in rows if isinstance(r, dict) for n in (r.get(provider.discover_nested_key) or [])]
        # Business-owned assets (Meta): a second listing whose rows hold nested lists of
        # primary-shaped rows — an agency member sees [] from /me/accounts yet manages everything
        # through their Business portfolio. Best-effort by design: the primary listing has already
        # answered, and a connection that consented before business_management existed in our scopes
        # gets a clean permission error here, which must read as "no extra assets", not a 502.
        if provider.discover_extra_path:
            try:
                extra = await client.get(
                    f"{provider.discovery_base.rstrip('/')}{provider.discover_extra_path}",
                    headers=disc_headers,
                )
                if extra.status_code < 400:
                    for holder in (extra.json().get(provider.discover_key) or []):
                        for path in provider.discover_extra_list_paths:
                            rows.extend(n for n in (_dig(holder, path) or []) if isinstance(n, dict))
            except Exception:  # noqa: BLE001 — the extra listing must never break the picker
                pass
        label_field = provider.discover_label_field or provider.discover_id_field
        resources = [
            # A row is usually an object, but some providers return bare strings — Google Ads'
            # listAccessibleCustomers gives ["customers/6186675831", …]. Treat the string as both id
            # and label rather than silently dropping every row.
            {"id": r, "label": r.rsplit("/", 1)[-1], "raw": r} if isinstance(r, str)
            # _dig, not .get — YouTube's channel title is nested at snippet.title. A plain key is just
            # a one-hop path, so every existing provider walks the same code.
            else {"id": _dig(r, provider.discover_id_field), "label": _dig(r, label_field), "raw": r}
            for r in rows if isinstance(r, (dict, str))
        ]
        if provider.discover_extra_path:
            # A directly-managed Page is usually ALSO owned by a Business, so the two listings
            # overlap — keep the first sighting (the primary listing's). Id-less rows go too: a
            # Business-owned Page with no linked Instagram account digs to id None, and one None
            # would survive dedup as a phantom picker row.
            seen: set = set()
            resources = [x for x in resources if x["id"] and not (x["id"] in seen or seen.add(x["id"]))]
        if provider.supports_enrichment:
            await _enrich_resource_labels(provider, resources, token, client)
        # Self-heal a connection whose target was chosen before we stored labels (or via the API, which
        # has no label to give). We're already holding the upstream's own naming — resolving it here
        # spares the user a pointless re-pick just to make the row readable.
        if secret.resource_ref and not secret.resource_name:
            match = next((x for x in resources if x["id"] == secret.resource_ref), None)
            if match and match["label"]:
                secret.resource_name = match["label"]
                await db.commit()
        return {
            "provider": provider.service,
            "resource_label": provider.resource_label,
            "resource_plural": provider.resource_plural,
            "selected": secret.resource_ref,
            "resources": resources,
        }


async def select_connection_resource(
    *, secret_id: int, resource_ref: str, resource_name: str, org_id: int,
) -> dict:
    async with session_maker() as db:
        secret = await _owned_connection(secret_id, org_id, db)
        secret.resource_ref = resource_ref
        secret.resource_name = resource_name
        # Picking a property/site/account is the moment we finally KNOW the id every real call needs —
        # so render it straight into the provisioned tool's examples as a ready-made call. Before this,
        # agents went hunting for the id through the vendor's admin API mid-task (GA4: 13 calls/7 orgs
        # dead-ended there). Re-picking replaces the stamped example rather than piling them up.
        provider = oauth_providers.get(secret.provider) if secret.provider else None
        tmpl = getattr(provider, "resource_example", None) if provider else None
        if tmpl and resource_ref:
            rendered = {
                k: v.replace("{resource}", resource_ref)
                    .replace("{resource_name}", resource_name or resource_ref)
                if isinstance(v, str) else v
                for k, v in tmpl.items()
            }
            # The marker is what makes re-picking REPLACE: a stamp for property A and one for property B
            # share no path, so path-matching would let them pile up, one stale and confidently wrong.
            rendered["stamped"] = "resource"
            tool = (await db.execute(select(Tool).where(
                Tool.org_id == org_id, Tool.name == (secret.name or provider.service)
            ))).scalars().first()
            if tool is not None:
                others = [e for e in (tool.examples or [])
                          if e.get("stamped") != "resource" and e.get("path") != tmpl["path"]]
                tool.examples = [rendered] + others
        await db.commit()
        await db.refresh(secret)
        return oauth.connection_view(secret)


async def supply_extra_credential(
    *, secret_id: int, value: str, org_id: int, owner: str,
) -> dict:
    async with session_maker() as db:
        secret = await _owned_connection(secret_id, org_id, db)
        provider = oauth_providers.get(secret.provider)
        if provider is None or not provider.needs_extra_credential:
            raise ConnectError("no_extra_credential", "this provider needs no extra credential")
        value = value.strip()
        if not value:
            raise ConnectError("extra_credential_required", f"{provider.extra_credential_label} is required")

        name = f"{provider.service}-{provider.extra_credential_header}"
        extra = (await db.execute(
            select(Secret).where(Secret.org_id == org_id, Secret.name == name)
        )).scalars().first()
        if extra is None:
            extra = Secret(org_id=org_id, name=name, owner=owner, kind="env",
                           value=crypto.encrypt(value))
            db.add(extra)
            await db.flush()
        else:  # re-supplying replaces it — the usual reason is a rotated token
            extra.value = crypto.encrypt(value)

        # The primary binding must match how THIS provider authenticates — OAuth bearer for Google Ads,
        # but a pasted-key provider (Tomba's X-Tomba-Key + X-Tomba-Secret pair) injects a plain header.
        # Hardcoding the OAuth shape here gave a key provider a binding that JSON-parses a bare key and
        # fails on every call, so build the primary half with the same helper the connect flow uses.
        bindings = _provider_bindings(provider, secret) + [
            {"secret_id": extra.id, "injector": "env", "location": "header",
             "name": provider.extra_credential_header, "format": "{secret}"},
        ]
        tool = (await db.execute(
            select(Tool).where(Tool.org_id == org_id, Tool.name == provider.service)
        )).scalars().first()
        if tool is None:
            tool = Tool(org_id=org_id, name=provider.service, owner=owner,
                        base_url=provider.base_url, host=_host_of(provider.base_url), bindings=bindings)
            db.add(tool)
        else:
            tool.bindings = bindings
        await db.commit()
        await db.refresh(secret)
        return {**oauth.connection_view(secret), "tool": provider.service, "ready": True}


async def revoke_connection(*, secret_id: int, org_id: int) -> dict:
    async with session_maker() as db:
        secret = await _owned_connection(secret_id, org_id, db)
        provider_service = secret.provider
        removed_tools: list[str] = []

        tools = (await db.execute(select(Tool).where(Tool.org_id == org_id))).scalars().all()
        for tool in tools:
            bindings = [b for b in (tool.bindings or []) if b.get("secret_id") != secret_id]
            if len(bindings) == len(tool.bindings or []):
                continue  # this tool never used the credential
            if tool.name == provider_service or not bindings:
                await db.delete(tool)  # treg's own auto-provisioned tool, or nothing left to inject
                removed_tools.append(tool.name)
            else:
                tool.bindings = bindings  # a user-built tool keeps its other credentials

        await db.delete(secret)
        await db.commit()
        return {"deleted": secret_id, "removed_tools": removed_tools}


async def run_connection_health(
    *, all_orgs: bool, is_superadmin: bool, org_id: int, client_factory,
) -> dict:
    async with session_maker() as db:
        # On-demand + Render-Cron trigger. Refreshes oauth tokens, probes tools, alerts owners.
        # Scoped to the caller's org so a member only ever probes/sees their own org's credentials —
        # EXCEPT a super-admin may pass ?all_orgs=1 to sweep EVERY org (so a single Render Cron token can
        # validate the whole platform, not just its own org).
        if all_orgs:
            if not is_superadmin:
                raise ConnectError("all_orgs_forbidden", "all_orgs requires super-admin")
            return await health.run_all(db, client_factory(), org_id=None)
        return await health.run_all(db, client_factory(), org_id=org_id)


async def list_connection_health(*, org_id: int, visible_ids_for) -> list[dict]:
    async with session_maker() as db:
        rows = (await db.execute(select(Secret).where(Secret.org_id == org_id))).scalars().all()
        visible = await visible_ids_for(db)
        if visible is not None:  # same visibility rule as /secrets — health mustn't leak hidden keys
            rows = [s for s in rows if s.id in visible]
        # health.needs_reconnect rides along so a credential treg cannot renew announces itself BEFORE
        # it dies. Nothing else surfaces that: it probes green until the moment it stops working.
        return [{**health._view(s), "needs_reconnect": health.needs_reconnect(s)} for s in rows]
