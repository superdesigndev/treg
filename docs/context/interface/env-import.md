---
title: Import - scan a .env, a skills dir or the machine's CLIs and register them as tools
status: shipped
sources:
  - src/treg/providers.py
  - src/treg/skills.py
related:
  - interface/cli.md
  - interface/skill.md
  - architecture/auth-secrets.md
  - architecture/data-model.md
---

# Import (`treg upload`; read-only preview `treg scan`)

`treg upload` scans a directory and registers what it finds: provider keys in a `.env`
(`providers.py`), skill folders (`skills.py`), or installed vendor CLIs (`clis`). `cmd_import` in
`cli.py` dispatches; `treg import` is a deprecated alias.

## The provider catalog (`providers.CATALOG`)

Curated and hand-verified, never bulk-ingested: public API directories carry wrong hosts and auth
schemes and miss modern providers, so an ingest may only feed suggestions a human confirms.

- `tokens` are matched against the **underscore-split** var name, so `TREG_RESEND_API_KEY` resolves
  via `RESEND`; the **longest** matching token wins (HUGGINGFACE over HF).
- `auth` is the shape `build_binding` turns into a treg binding: `bearer`, `api_key_header` (custom
  `header`/`format`), `query` (`param`; no param means it cannot be auto-bound), `basic` (the CLI
  base64s `id:secret` into one secret), `oauth2` (the connect flow, never a static binding).
- `required_headers` become constant-format header bindings reusing the credential's secret id.
- `probe` is a cheap authenticated GET stored as the tool's health check, so `treg health --run` and
  onboarding's test call hit a real endpoint instead of the base URL root, which 404s and looks like a
  bad credential.
- `skills: [...]` aliases let `match_skill` match a skill folder name. File-credential entries (OAuth
  token files) carry `tokens: []` so the env scanner never mistakes them for a bearer key. A
  skill-name match is authoritative for `base_url`.
- A `cli` block is the `treg run` profile ([local-run](../architecture/local-run.md)), validated by
  `localrun.validate_cli_profile`. `verified` records a real machine test (documentation is not
  proof); `beta` marks an untested entry; `unsupported: true` says why and what to do instead.
  `auth_mechanism` (`env|argv|config_file|device`) routes CLI auto-import: `env`/`argv` are
  injectable by treg, `config_file` is local-only, `device` report-only. `detect.config_paths` hints
  that the CLI is logged in; `login_cmd` is the fix printed in the gap report.

**Distribution.** `GET /providers.json` serves `{version, providers}` so the catalog grows without a
CLI release. `cli._load_catalog` caches it per base URL and uses the server's or cached copy only when
its `version` is at least the bundled `CATALOG_VERSION`; otherwise the bundled catalog wins, so a CLI
newer than its server never regresses. `--dry-run` uses the bundled catalog (no network). Bump
`CATALOG_VERSION` with any catalog change.

## `.env` side

**Detection reads names only** (`var_names` discards values); only `env_values` reads a value, and only
for vars being registered. Each var lands in one bucket, provider-first:

1. **matched**: a catalog provider, unless the var is clearly config (a `CONFIG_HINTS` component and no
   `SECRET_HINTS` component: `POSTHOG_HOST`, `SUPABASE_URL`). Duplicate tool names get a suffix.
2. **oauth_pair / basic_pair**: `CLIENT_ID`+`CLIENT_SECRET` or `ACCOUNT_SID`+`AUTH_TOKEN` grouped into
   one detection; a lone half is flagged incomplete. `*_AUTH_TOKEN` alone for a non-basic provider is a
   bearer token.
3. **app_internal**: the app's own secrets (`APP_INTERNAL`), never offered and never sent to an LLM.
   Checked after provider match, so `OPENAI_SECRET_KEY` is still an OpenAI key, except
   `ALWAYS_INTERNAL` suffixes (webhook and signing secrets), which are internal even on a provider match.
4. **unknown_secret**: a `SECRET_HINTS` component, no provider; candidate for `--llm`.
5. **config**: everything else.

`plan_actions` builds the actions; registration posts the secret, then the tool. A complete
`oauth_pair` whose provider has an `oauth` block is connected afterwards, one provider at a time, on a
TTY only (`--no-oauth` opts out).

**LLM fallback (`--llm`).** Sends only the names of `unknown_secret` vars to an OpenAI-compatible
endpoint, never on `--dry-run`. Every suggestion is confirmed by the user, and `_safe_base_url` rejects
non-https, loopback, private and link-local hosts before registering.

## CLI side (`treg scan clis` / `treg upload clis`)

Only the member's machine knows which CLIs are installed and logged in. For each catalog `cli` entry,
`classify_cli` (pure) turns installed / key present / logged in into `ready` (tier `server` or `local`),
`needs_key`, `needs_login`, `unsupported` or `not_installed`; every gap names its exact fix.

- **server tier** (key available): treg stores the key and binds it.
- **local tier** (CLI logged in, no key): secret-less, stored with an explicit `inject: []` so the
  catalog's inject cannot merge back in at grant time.
- `--add <bin>` registers an installed CLI absent from the catalog.

Secret kind **`param`** holds a non-secret value (a project id) injected like a secret; health checks
and invalid-marking skip it.

## Skill side (`treg upload skills`)

`scan_skills` keys on `SKILL.md` (skips dot-dirs) and classifies each folder:

- **contract**: a `treg.json` exists and is used verbatim; always wins. `convert.resolve_secret_path`
  tolerates the `.secret`/`.secrets` spelling difference between machines.
- **catalog CLI**: a secret-less skill whose name matches a catalog `cli` entry becomes a runnable cli
  tool, its env credentials read from the machine or asked once.
- **generated**: base URL from a catalog name match, else the script's `API="..."`, else a guess;
  credentials from local `.secret*` files or env vars the scripts read. A missing env var is a gap,
  not a broken tool. The `.env` is found by walking up from the skills dir, and `os.environ` is merged.
- **recipe_only**: published as a bundle with no tool or secret.

`convert.auto_bindings` (shared by CLI and dashboard so they agree) gives a multi-credential skill
distinct headers: the primary token on `Authorization: Bearer`, each other file on a header named from
its filename, and OAuth app config (`client_secret.json`, `credentials.json`) skipped. File secrets are
`.strip()`ed so a trailing newline cannot become an illegal header value. `collect_files` ships the
folder's companion files, never `.secret*`.

The dashboard importer (`POST /skills/analyze`, `POST /skills/import`) runs the same classifier and
registers each skill in its own session, so one failure cannot poison the rest of the batch.

## Guards

- **Never import silently.** Without a TTY and without `--all`, `--select` or `--dry-run`, import
  refuses (agents and CI must state intent).
- **Re-runs are idempotent.** Both sides skip by name (a recipe-only bundle has no tool for the
  server's 409 to catch); `--replace` deletes then recreates. The tool name is checked before the
  secret is posted, so a clash never leaves an orphan secret.
