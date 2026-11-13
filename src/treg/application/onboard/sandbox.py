"""Landing sandbox provisioning, export, sample generation, and garbage collection."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from secrets import token_hex

from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from ... import crypto
from ...domain.governance.teams import cascade_delete_org
from ...domain.identity.access import SANDBOX_DOMAIN
from ...models import Membership, Org, Secret, Tool, User
from ...sandbox_identity import visitor_name

SANDBOX_TTL_MIN = 60
MAX_TOOLS = 3                              # a sandbox visitor may register at most this many endpoints
MAX_SECRETS = 3

# Believable starters (real-brand names, OBVIOUSLY-FAKE placeholder values) so the console is alive on
# arrival and the demo reads instantly. Each value's only job is to appear in the "injected" output; it is
# a credential to nothing, and sandbox calls never leave treg. The visitor edits/removes these and adds
# their own. The placeholders spell DEMO on purpose so no scanner mistakes them for a real key.
#
# ONE live endpoint on arrival (Stripe) keeps the story clean: it auto-runs so the "no key" aha shows
# immediately. POSTHOG_KEY is seeded as a vault secret WITHOUT a tool, so the "add your own" row is
# prefilled with the real PostHog API + this placeholder and the visitor's first action is a single Add.
DEFAULTS = [
    {"secret": "STRIPE_KEY",  "value": "sk_live_DEMO0000PLACEHOLDER", "tool": "stripe",
     "base": "https://api.stripe.com/v1/charges",  "host": "api.stripe.com",
     "example": {"method": "GET", "path": "", "note": "list charges"}},
    {"secret": "POSTHOG_KEY", "value": "phx_DEMO0000PLACEHOLDER"},  # vault-only (no tool); the add-row uses it
]

def is_sandbox_user(user) -> bool:
    """A login-free sandbox visitor (`visitor-…@sandbox.treg.local`). It may act ONLY inside its own
    sandbox org — never create a real team or graduate to a real account (that needs a real sign-in door)."""
    return bool(user) and (getattr(user, "email", "") or "").endswith(f"@{SANDBOX_DOMAIN}")


async def mint(db: AsyncSession) -> dict:
    """Create a fresh login-free sandbox team (with a starter secret + endpoint) and RETURN its token
    + the facts the studio needs. Caller need not commit - we commit here."""
    rid = token_hex(6)
    user = User(email=f"visitor-{rid}@{SANDBOX_DOMAIN}", demo=True, onboarded=True)
    db.add(user)
    await db.flush()

    org = Org(name=f"Sandbox {rid}", slug=f"sbx-{rid}", demo=True)
    db.add(org)
    await db.flush()

    token = crypto.new_token()  # the ONE thing we surface (the visitor drives everything with it)
    db.add(Membership(user_id=user.id, org_id=org.id, role="member", token_hash=crypto.hash_token(token)))

    for d in DEFAULTS:  # seed the vault secret; only entries with a "tool" also get a live endpoint
        secret = Secret(org_id=org.id, name=d["secret"], owner=user.email, kind="env",
                        value=crypto.encrypt(d["value"]))
        db.add(secret)
        await db.flush()
        if d.get("tool"):
            db.add(Tool(
                org_id=org.id, name=d["tool"], owner=user.email, base_url=d["base"], host=d["host"],
                bindings=[{"secret_id": secret.id, "injector": "env", "location": "header",
                           "name": "Authorization", "format": "Bearer {secret}", "secret_field": "access_token"}],
                examples=[d["example"]],
            ))

    await db.commit()
    return {
        "token": token,
        "org_slug": org.slug,
        "visitor": visitor_name(org.slug),  # the identity live charges carry (metadata[visitor])
        "max_tools": MAX_TOOLS,
        "max_secrets": MAX_SECRETS,
        "ttl_min": SANDBOX_TTL_MIN,
    }


async def export_skill(db: AsyncSession, org: Org) -> dict:
    """Turn whatever the visitor built (secrets + endpoints) into a shareable **skill** - the
    `POST /skills`-shaped manifest (values redacted to placeholders, since real skills carry values
    in `.secret` files, not the manifest) + a generated SKILL.md recipe + install commands."""
    secrets = (await db.execute(select(Secret).where(Secret.org_id == org.id))).scalars().all()
    tools = (await db.execute(select(Tool).where(Tool.org_id == org.id))).scalars().all()
    id_to_name = {s.id: s.name for s in secrets}

    manifest_secrets = [{"local_name": s.name, "kind": s.kind, "value": f"<paste your {s.name}>"}
                        for s in secrets]
    manifest_tools = []
    for t in tools:
        binds = []
        for b in t.bindings:
            nb = {k: v for k, v in b.items() if k != "secret_id"}
            nb["secret"] = id_to_name.get(b.get("secret_id"), "SECRET")  # skills reference by local_name
            binds.append(nb)
        manifest_tools.append({"name": t.name, "base_url": t.base_url, "bindings": binds,
                               "examples": t.examples or []})

    name = "my-api-skill"
    manifest = {"name": name, "recipe": f"# {name}\n\nCall these endpoints without holding the key.",
                "secrets": manifest_secrets, "tools": manifest_tools}

    lines = [f"# {name}", "",
             "A treg **skill**: a recipe + its endpoints + the secrets they need, bundled so a",
             "teammate installs the capability **without ever holding your keys**.", "",
             "## Endpoints"]
    for t in tools:
        ex = (t.examples or [{}])[0]
        lines.append(f"- `{t.name}` → {t.base_url}"
                     + (f"  (e.g. `{ex.get('method','GET')} {ex.get('path','')}`)" if ex else ""))
    lines += ["", "## Use", "```", "# an agent or you, after installing the skill:",
              f"treg call {tools[0].base_url}/… " if tools else "treg call <url>", "```",
              "", "The key is injected server-side by treg - it never lands on the caller's machine."]
    skill_md = "\n".join(lines)

    return {
        "manifest": manifest,
        "treg_json": json.dumps(manifest, indent=2),
        "skill_md": skill_md,
        "install": [
            "# save the manifest as treg.json in a skill folder, then:",
            "treg skill add ./my-api-skill",
            "# - or paste the manifest into the dashboard's Skills composer.",
        ],
    }


async def gc(db: AsyncSession) -> int:
    """Reap sandboxes older than the TTL - the throwaway user, its org, and every org-scoped row.
    Runs opportunistically each time a new sandbox is minted. Commits itself.

    Only landing sandbox orgs (`is_sandbox`) are reaped. A visitor that also belongs to any other
    org is left whole, user and every org, for a person to look at: that should not exist, and
    deleting it would erase the only record of how it came to."""
    from ... import sandbox as sandbox_runtime

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=SANDBOX_TTL_MIN)
    visitors = (await db.execute(
        select(User).where(User.email.like(f"visitor-%@{SANDBOX_DOMAIN}")))).scalars().all()
    n = 0
    for u in visitors:
        ca = u.created_at
        ca = ca.replace(tzinfo=None) if (ca is not None and ca.tzinfo) else ca
        if ca is not None and ca >= cutoff:
            continue
        # One savepoint per visitor: a sandbox that will not delete is logged and skipped, and every
        # other expired one still goes. gc runs only from mint_sandbox, so one bad row must not stop
        # all reaping.
        nested = await db.begin_nested()
        try:
            mems = (await db.execute(select(Membership).where(Membership.user_id == u.id))).scalars().all()
            orgs = [org for m in mems if (org := await db.get(Org, m.org_id)) is not None]
            if not all(sandbox_runtime.is_sandbox(org) for org in orgs):
                await nested.rollback()
                logging.getLogger("treg").warning(
                    "sandbox user %s belongs to a non-sandbox org; kept for review", u.id)
                continue
            for org in orgs:
                # The shared cascade, never a private table list.
                await cascade_delete_org(org, db)
            await db.delete(u)
            await db.flush()
        except Exception:
            await nested.rollback()
            logging.getLogger("treg").exception("could not reap sandbox user %s; skipping", u.id)
            continue
        n += 1
    if n:
        await db.commit()
    return n
