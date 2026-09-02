---
title: Landing sandbox backend - front-end entry removed
status: shipped
sources:
  - src/treg/sandbox.py
  - src/treg/sandbox_identity.py
  - src/treg/application/onboard/pubfeed.py
  - src/treg/application/onboard/sandbox.py
  - src/treg/application/onboard/__init__.py
  - src/treg/domain/governance/sandbox.py
  - src/treg/api.py
  - src/treg/routers/onboard.py
  - src/treg/routers/web.py
  - src/treg/web/index.html
  - src/treg/web/install.sh
related:
  - interface/dashboard.md
  - interface/onboarding.md
  - interface/api.md
---

# Landing sandbox backend

> **Front-end entry removed.** The logged-out SPA no longer renders the sandbox studio or its coach
> tour, stores `localStorage['treg-sbx']`, or calls `POST /demo/sandbox`. A use-case CTA arriving at
> `/app?ref=<page>` keeps the parameter for attribution, strips it as a one-shot parameter, and opens
> the sign-in modal. Plain logged-out `/app` still redirects to `/`. The logged-out hero, key-leak
> explanation, footer CTA, invite and share gates, OAuth entry, and sign-in modal remain.

The sections below document backend behavior that is still shipped but has no visitor-facing mint
path in `index.html`. Provisioning, export, samples, and garbage collection remain in
`application/onboard/sandbox.py`; the call-side sandbox engine remains in `sandbox.py`; and the routes
remain in `routers/onboard.py`. Their removal is intentionally deferred to the backend follow-up.

## The throwaway team (`application/onboard/sandbox.py`)
`mint(db)` creates a login-free team: a `visitor-<hex>@sandbox.treg.local` `User` (can never sign in),
a `demo` `Org` slugged `sbx-<hex>`, a member `Membership` whose **token is returned** (unlike
onboarding's `demo.py`, which discards it), plus seeded starters from `DEFAULTS` — real-brand names,
**fake keys**. To keep the story clean the seed leaves **one** working endpoint on arrival:
`STRIPE_KEY`→`stripe`, whose base is pinned to the full charges resource
(`https://api.stripe.com/v1/charges`, host `api.stripe.com`, an `env` `Authorization: Bearer {secret}`
binding), which auto-runs so the "no key" aha shows immediately. This exact seeded tool is also the
**live wire** (see below): when the server is configured for it, a call to it is the sandbox's one real
upstream request. `POSTHOG_KEY` is seeded **vault-only** (an entry with no `tool`
key, so `mint` creates the secret but no Tool); the removed studio used it for its prefilled
"add your own" row. `is_sandbox(org)` =
`org.demo && _SANDBOX_SLUG_RE.match(org.slug)` — it matches the **exact mint slug format**
(`^sbx-[0-9a-f]{12}$`, i.e. `sbx-<token_hex(6)>`), NOT a loose `startswith("sbx-")`, so a real team a
user happens to name "sbx …" (slug `sbx-…`) is not misread as a sandbox. It also stays distinct from
onboarding demo teams (also `demo`, but team-named).

`is_sandbox_user(user)` is the companion check on the **visitor** (email ends in `@sandbox.treg.local`,
`SANDBOX_DOMAIN`). Such a login-free visitor may act ONLY inside its own sandbox org — it can **never
create a real team** (`POST /orgs` → `create_org` returns 403: "sign in with GitHub, Google, or email")
nor otherwise graduate to a real account. Escaping the sandbox requires a real sign-in door.

## Safety: sandbox calls never touch the network (except the one live wire)
`application.call.service` checks `demo_sandbox.is_sandbox(caller.org)` and, for a sandbox, short-circuits to
`sandbox.synthesize(...)` instead of `relay()`. `synthesize` runs the **real** `injectors.inject` to
compute exactly what treg would send upstream (the injected header/query), then returns a **labelled
dummy** response — brand-shaped via `SAMPLE_BODIES` (Stripe charge list / PostHog events). So the
injected credential shown is 100% real, but no outbound request is ever made: no SSRF, no open relay,
regardless of the (arbitrary) base_url the visitor typed. Org-scoped tool resolution already prevents a
sandbox token from reaching any tool it didn't register.

## The one live wire (real Stripe test charges)
There is a single deliberate exception, gated on env `TREG_DEMO_STRIPE_KEY` (`settings.demo_stripe_key`,
a Stripe **restricted test key** limited to Charges). When it is set, a sandbox call to the exact seeded
stripe tool relays for real. The call use case matches the tool with `demo_sandbox.is_live_tool(tool)` — a strict
fingerprint (`LIVE_HOST == "api.stripe.com"` and `base_url.rstrip("/") == LIVE_BASE
== "https://api.stripe.com/v1/charges"`) — and, for `GET`/`POST` only, calls `_relay_live_demo(...)`. That
helper is intentionally narrower than `relay()`: form-encoded only, the `Authorization: Bearer` header is
built from the **env key** (never from any sandbox secret), and `metadata[visitor]` in the POST body is
**stripped and re-set server-side** to `demo_sandbox.visitor_name(org.slug)` so the identity on the public
feed is always ours. Because the match is exact, editing the tool (base_url, bindings, a lookalike) makes it
stop matching and **fall through to `synthesize`** — there is no key in the sandbox org to exfiltrate.
Two guards keep the demo intact: `_require_not_live_demo_tool` / `_require_not_live_demo_secret` refuse edits
or deletes of the seeded `stripe` tool and its `STRIPE_KEY` while the wire is on (visitor-created tools stay
fully editable). `is_live_tool` lives in `sandbox.py`; `visitor_name` and its wordlists (`ADJECTIVES`/
`ANIMALS`) live in the neutral `sandbox_identity.py` leaf. `mint()` returns the visitor name;
`POST /demo/sandbox` adds `"live"` and `GET /demo/sandbox/live` (`demo_sandbox_live`) reports `{live, visitor}`.
Both routes remain pending backend removal, but `index.html` no longer calls either one or holds a
sandbox token.

## The public payments feed (`application/onboard/pubfeed.py`)
The feed is the landing page's **live payments ticker**: a stranger's live-wire charge appears on the
page within seconds, no refresh, as skeptic-proof that the proxy really injected a real key. The path:
visitor `curl` → live wire relays a Stripe test charge → Stripe fires `charge.succeeded` at
`POST /stripe/webhook` (`stripe_webhook`) → `pubfeed.push_charge(...)` → `GET /landing/stripe-feed`
(`landing_stripe_feed`) streams it over Server-Sent Events via `pubfeed.stream()`. The webhook verifies the
`Stripe-Signature` with `pubfeed.verify_signature` (constant-time HMAC-SHA256 over `{t}.{body}`, timestamps
older than `SIG_TOLERANCE_S` rejected as replays, any of several `v1` signatures accepted during rotation);
it returns **404 when `TREG_DEMO_STRIPE_WEBHOOK_SECRET` (`settings.demo_stripe_webhook_secret`) is unset**, so
a deploy without the secret exposes no unauthenticated POST surface. Design points:
- **In-memory + tiny.** A `deque(maxlen=FEED_MAX)` ring buffer plus a set of per-subscriber `asyncio.Queue`s;
  it is a marketing surface, not a system of record. A dropped event on restart costs nothing (Stripe retries),
  and each instance of a multi-instance deploy streams only the deliveries its own webhook received.
- **No visitor-controlled text can reach the page.** `push_charge` copies **only server-chosen fields**
  (amount/currency/created, a 6-char `id_suffix`, and a `receipt_url` only if it starts with
  `https://pay.stripe.com/`) — never `description`. The display **name** (`_display_name`) is accepted only when
  it passes `_is_wordlist_name` (adjective-animal-nnn, both words from the exact `ADJECTIVES`/`ANIMALS` lists,
  number ≤ 999); anything else falls back to `_derived_name`, a deterministic wordlist name hashed from the
  charge id. This is the "graffiti lesson": hand-typed strings can never deface the shared feed.
- **`stream()`** replays the ring buffer to a fresh subscriber, then live events, with a `: ping` keepalive every
  `KEEPALIVE_S`; a subscriber that lags past `_MAX_SUBSCRIBER_LAG` is dropped rather than buffered forever. The
  SSE response sets `X-Accel-Buffering: no` so a reverse proxy does not buffer it. `reset()` is a test hook.

Bounds: `MAX_TOOLS`/`MAX_SECRETS` (3) are enforced by `domain/governance/sandbox.py` on
`POST /tools|/secrets`; `SANDBOX_TTL_MIN` (60) + `gc(db)` reaps expired visitors (their org + all
org-scoped rows), run opportunistically on each mint. The reaper deletes the org through the ONE shared
`cascade_delete_org` (`domain/governance/teams.py`) - it must never keep its own list of org-scoped
tables. It once did, the copy never learned about `IdempotentCall`, and on 2026-09-02 Postgres refused
the membership delete on every mint. `mint_sandbox` now also isolates the reaper: a gc failure is
rolled back and logged, and the visitor still gets a sandbox. A DB-backed `ratestore` window keyed by client
IP and `SANDBOX_RATE_MAX` guards `POST /demo/sandbox`. The browser no longer mints or reuses a sandbox.
**Skill import is disabled in a sandbox org** - `POST /skills` (register),
`POST /skills/analyze`, and `POST /skills/import` all check `is_sandbox(caller.org)` and 403 ("skill
import is disabled in the sandbox"), because a skill package could register unlimited tools/secrets past
the `MAX_TOOLS`/`MAX_SECRETS` cap.

`export_skill(db, org)` → `GET /demo/sandbox/skill` turns whatever the visitor built into a shareable
skill manifest (treg.json + SKILL.md, secret values redacted to placeholders).

## Hosted sample skills + the "Run in Claude Code" flow
`SAMPLE_SKILLS` (`posthog-insights`, `stripe-billing`) each mirror a seeded tool so an installed skill's
proxied calls resolve against the visitor's sandbox. `skill_files(name, base, token)` builds the three
files a skill folder is — `SKILL.md` (agent recipe: call the treg proxy, key injected server-side),
`treg.json` (wiring: base_url + which secret by name), `.secret` (empty; value stays in the vault).
- `GET /skills/samples` (`skill_samples`) — public JSON of each sample + its files (for the landing).
- `GET /skills/{name}/install.sh?token=` (`skill_install`) — `install_script(name, base, token)` emits a
  POSIX `sh` that `mkdir -p ./.claude/skills/<name>` and writes the files (token baked in via quoted
  heredocs), so `curl … | sh` from a project dir installs the skill for Claude Code to load. Its recipe
  calls `{BASE}/call/…` with the caller's treg token — the API key never lands on the machine. The
  `token` query param is **charset-restricted** (`re.fullmatch(r"[A-Za-z0-9_\-]{1,200}")`, else 422)
  because it is interpolated into that shell script — a crafted value can't inject a newline + extra
  commands into the generated `curl … | sh`.

## CLI installer
`GET /install.sh` (`install_sh`) serves `src/treg/web/install.sh`, `{BASE}`-templated like `llms.txt`
(so it targets whatever host is live — a dev box or the real domain). The script now installs from
**PyPI** (`SRC="tools-registry"`, the light CLI package; the FastAPI/DB server stack is the separate
`tools-registry[server]` extra for self-hosters) via `uv tool install --force` → `pipx install --force` →
`pip3 install --user --upgrade`, then runs `treg config --base-url {BASE}`. The uv/pipx paths pin the
supported interpreter range (`PYREQ`, kept in sync with `requires-python` in `pyproject.toml`): without
it uv resolves against its *default* interpreter — its own managed Python first — and a machine whose
default falls outside the range fails resolution instead of picking (or auto-downloading) a compatible
one. It also installs the official
**tools-registry skill** into every detected agent via `treg skill bootstrap` (Claude Code, Cursor, Codex,
Gemini, Copilot, OpenCode, Windsurf …), falling back on older CLIs to a Claude-only drop that curls
`{BASE}/skill.md` into `~/.claude/skills/treg`. Because the package is public on PyPI,
`curl … /install.sh | sh` now works for anyone with no repo/git access needed. The **Getting started**
dashboard view (`view==='start'`) surfaces this install command + `treg login`/`onboard`/`add`/`call` and
links to the tutorial and `/llms.txt`; `llms.txt` has a matching **Install the CLI** section.

## Backend follow-up
Remove the now-unreachable sandbox mint, export, sample, live-wire, feed, governance, and call-pipeline
branches in a separate backend change. This front-end change deliberately leaves those routes and
guards intact.
