# The treg MCP server, and the plugin that ships it

**Status:** shipped; kept as the design record. The MCP server is live at `/mcp/` and `/mcp/v2/`;
the current design is `docs/context/architecture/mcp-oauth.md`.

The goal is the plugin directory's traffic. A skill-only plugin gets that traffic and then tells the
visitor to install a CLI — the dead end is the conversion. An MCP connector **works the moment it is
installed**, and it is the only shape that reaches ChatGPT, where there is no terminal at all.

Not one tool per provider. The catalog stays *data*; the MCP server exposes **treg's own operations**
— the same handful the CLI has. Five tools, not 2,600.

## What I verified first, on this machine (codex-cli 0.145.0)

Three facts, each proven rather than read, because they decide the design:

| Question | Answer | How |
|---|---|---|
| Can a plugin declare a **remote** MCP server, or only an OpenAI-registered connector? | **Yes, remote works** | Built a throwaway plugin whose `.mcp.json` declared a `url`; installed it; `codex mcp list` showed the server registered and enabled |
| What config shape? | `{"<name>": {"url": …, "bearer_token_env_var": …}}` | `codex mcp add --url` writes exactly `[mcp_servers.x] url/bearer_token_env_var` |
| Is OAuth supported, or only a bearer token? | Both — `codex mcp login <name>` authenticates by OAuth | `codex mcp login --help` |

And the constraint that shapes phase 2: OpenAI's own `github` and `gmail` plugins use `.app.json`
with an OpenAI-issued `connector_…` id. **That id comes from the submission process**, so the ChatGPT
path is gated on review in a way the Codex path is not.

## Spike results — all four questions answered, on this machine

Throwaway code, deleted. What it proved:

**1. It mounts inside treg's FastAPI app — but there is a trap.** `app.mount()` does **not** run the
mounted app's lifespan, and the streamable-HTTP session manager initialises its task group there.
Every request 500s with `Task group is not initialized`. treg's `api.py` already has a lifespan
(database setup), so the real implementation must **compose** the two:

```python
mcp_app = mcp.streamable_http_app(streamable_http_path="/", stateless_http=True, json_response=True)

@asynccontextmanager
async def lifespan(app):
    async with mcp_app.router.lifespan_context(mcp_app):   # treg's own lifespan wraps this
        yield
```

Finding this in a spike rather than on Render is the whole point of the exercise.

**2. `stateless_http=True` works**, which matters more than it looks: production is Render and can run
more than one instance. A session-based transport would need sticky routing to be reliable. Stateless
plus `json_response=True` also skips SSE framing, which is where the speed comes from.

**3. It is FAST.** Measured over 12 calls each, against the real 2,590-endpoint catalog:

| call | p50 | min | max | of which real work |
|---|---|---|---|---|
| `tools/list` | 1.5 ms | — | — | — |
| `catalog_search("backlinks")` | **1.4 ms** | 1.0 | 1.9 | 0.18 ms |
| `catalog_search("work email")` | **2.1 ms** | 1.9 | 2.6 | 1.01 ms |
| `catalog_get(<id>)` | **1.4 ms** | 1.0 | 1.8 | — |

Transport overhead is ~1.2 ms. The catalog is already parsed once at import, so search is a scan over
memory. Nothing here needs a cache yet.

**4. A tool CAN read the caller's bearer token** — `Context.headers`, populated by HTTP transports.
Verified both ways: with a token the tool saw `authorization`, without one it saw the same headers
minus that. Org isolation has something to key on.

The SDK's own docstring carries the warning to honour: *"Headers are client-supplied input — never
treat one as an identity assertion."* The token must be validated against the database on every
call, exactly as the HTTP API does.

**5. End to end from Codex, which is the real test.** Registered the spike with `codex mcp add --url`
and asked Codex to search for backlinks endpoints. It called the tool and answered with real data —
endpoint ids, providers, `$0.02436` per call, and "no API key needed" — in a 4 ms round trip.

That is the "wow": a question in, a priced answer out, no key and no CLI.

## The tool surface

Five tools, each mirroring a CLI command that already exists and is tested:

| Tool | Does | Mirrors |
|---|---|---|
| `catalog_search(query, limit)` | find endpoints by what you want to DO, with price, provider, and measured reliability | `treg catalog search` |
| `catalog_get(endpoint_id)` | one endpoint: parameters, price, auth tier, capability siblings, observed stats | `treg catalog get` |
| `call(endpoint_id, params)` | the metered call — treg injects the credential, relays the response | `treg call` |
| `balance()` | the team's prepaid balance and in-flight holds | `treg balance` |
| `my_tools()` | what the team registered: keys, OAuth connections, skills | `treg tool list` |

**The tool descriptions are the product.** They are what the model reads when deciding whether treg
is relevant, exactly as `skill.md`'s frontmatter is today. They get the same care.

`call` returns the upstream response **verbatim**, plus what it cost. The founding rule is unchanged:
treg relays, never models.

## Where it lives, and the rule it must not break

A new module, `src/treg/mcp.py`, mounted on the existing FastAPI app at `/mcp`. One deployment, one
database, one set of rules.

**It must route through `_resolve_marketplace_call`, not around it.** Org isolation, the balance
reserve, the $5/day platform cap, capability pins and deny rules all live on that path. A second
entrance that re-implements any of them is how the two drift and one of them stops being enforced.
Concretely: `call` builds the same request the HTTP proxy does and goes through the same resolution,
reservation and settle.

## Authentication, in two phases

**Phase 1 — Codex, works today.** The plugin's `.mcp.json` declares the URL and
`bearer_token_env_var: TREG_TOKEN`. The user exports a per-org token. That is one environment
variable instead of installing a CLI and signing in — and per-org tokens already exist, with roles
and audit.

**Phase 2 — ChatGPT, needs OAuth.** `codex mcp login` shows OAuth is supported, and MCP's spec uses
OAuth 2.1. treg already runs OAuth for GitHub and Google sign-in and has a hosted connect flow, so
there is a foundation — but this is genuine new work, and it is what the ChatGPT connector requires.

Phase 1 ships and can be tested end to end. Phase 2 is what the public submission needs.

## What the plugin contains

    plugin/
    ├── .codex-plugin/plugin.json     manifest + listing copy (reuse from the archived branch)
    ├── .mcp.json                     the remote server declaration
    ├── skills/treg/                  KEEP — generated from src/treg/web/skill.md
    └── assets/                       icon + logo (already made)

**Both**, exactly as OpenAI's `github` plugin ships `"skills"` and `"apps"` together. The connector
gives the model the operations; the skill gives it the judgement — when treg is the right move, how
to choose between providers, to state a price before spending. Tools alone would lose all of that.

## Risks and open decisions

- **`mcp` 2.0.0 depends on `httpx2`**, while treg's CLI uses `httpx`. It belongs in the `[server]`
  extra only — the base install must stay the light CLI. Needs checking for conflict before committing
  to the SDK.
- **A public MCP endpoint is new attack surface.** Unauthenticated requests must be refused before
  any work is done, and it needs its own rate limit — the existing per-user caps assume the HTTP API.
- **Streamable HTTP is session-based**, which is a different lifecycle from treg's stateless
  request/response. Worth a spike before committing.
- **Token in an environment variable** is weaker than OAuth: it sits in the user's shell profile. It
  is the pragmatic phase-1 answer, not the destination.
- **Cost visibility.** A model calling `call` can spend real money. The tool description must state
  the price, and `catalog_get` should be the encouraged step before `call`.

## Build order

1. **Spike** the MCP SDK against FastAPI — transport, session, dependency conflict. Throwaway.
2. **`src/treg/mcp.py`** with the five tools, routed through the existing enforcement path.
3. **Tests** — the enforcement ones matter most: a wrong-org token sees nothing, an over-cap call is
   refused, an unpriced endpoint is refused, a pinned capability rejects the wrong provider.
4. **Deploy** to the dev box, point Codex at it, exercise all five tools for real.
5. **Package** the plugin, install from a local marketplace, verify in `codex exec`.
6. **Then** the submission — with the connector, which is where phase 2 becomes required.

Stop after step 4 and look at it before packaging. That is the point where the tool surface either
reads well to a model or does not, and it is much cheaper to change then.
