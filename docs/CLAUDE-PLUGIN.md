# The Claude Code plugin, and where it gets listed

treg ships as a Claude Code plugin served from this repo as its own marketplace: live the moment it
merges, with no reviewer in the loop. It installs the treg skill and treg's remote MCP server.

## Install

```
/plugin marketplace add superdesigndev/treg
/plugin install treg@treg
```

Then `/mcp`, pick `plugin:treg:treg`, and Authenticate. The server is declared URL-only with
`type: "http"`; Claude Code runs OAuth itself, so the install needs **no token and no
configuration**. Until sign-in the server is listed as "needs authentication" and contributes no
tools, so nothing 401s in the meantime.

The skill checks for the tools first and sends the human to `/mcp` when they are missing. The CLI
(`curl … install.sh | sh`, `treg login`) stays optional: the plugin path never asks an agent to run
a downloaded script, which Claude Code's auto mode blocks by default.

## What is where

```
.claude-plugin/marketplace.json           makes this repo its own marketplace: treg plus the workflow plugins
plugins/treg/.claude-plugin/plugin.json   the manifest Claude Code reads, MCP server inline
plugins/treg/.cursor-plugin/plugin.json   the manifest Cursor (and Grok Bot) read
plugins/treg/mcp.json                     Cursor's URL-only MCP config
plugins/treg/skills/treg/SKILL.md         GENERATED - the plugin's skill
plugins/workflows/<name>/                 GENERATED - one optional plugin per workflow skill
skills/treg/SKILL.md                      GENERATED - the bare skill for `npx skills add` and ClawHub
skills.sh.json                            category grouping for skills.sh / Hermes' Skills Hub
```

Claude Code and Cursor install **the same folder**, `plugins/treg/`, through their own manifests.
The marketplace `source` must stay `./plugins/treg`: a source of `./` makes the repo root the plugin,
which ships the whole repository and every workflow skill under `skills/`.

The workflow skills (`lead-signals`, `recruit`, `make-ugc`, `web-access`, `jev-memory`) are separate
plugins in the same marketplace, generated with their manifests and marketplace entries by
`scripts/build_plugin.py`, so `/plugin` offers them one by one instead of installing them with
treg. The plugin's skill tells the agent to offer one when the task matches it.

The MCP server is inline in the Claude manifest rather than pointing at `mcp.json` because Claude
Code rejects a server with a `url` and no `type` (it logs "Invalid MCP server config" and registers
nothing), while `mcp.json` stays URL-only for the hosts that read it.

The served page's first section branches on what the agent has: treg's MCP tools, the `treg` CLI,
or neither. Only the last installs anything, and only when the human asked for setup. So the bare
skill at the repo root is the served page unchanged, and the plugin's prepended note says only that
its tools are waiting for `/mcp` sign-in.

**Never edit `skills/treg/SKILL.md`.** Change `src/treg/web/skill.md` and regenerate:

```bash
uv run python scripts/build_plugin.py            # regenerate every plugin
uv run python scripts/build_plugin.py --check    # fail if any is stale (also a test)
```

## Listing runbook

Ordered so that everything self-serve ships before anything with a queue.

### 1. Own marketplace — live on merge

Nothing to submit. Merging to `main` publishes it; the install lines above start working.

Bump `version` in `pyproject.toml` and every manifest together: `test_plugin_version_tracks_the_package`
pins all of them to one value.

### 2. ClawHub (clawhub.ai) — ✅ LIVE, self-serve, CLI-based

**Published 2026-08-14: [clawhub.ai/superdesigndev](https://clawhub.ai/superdesigndev) · `treg@0.11.0`
· owner `@superdesigndev` · MIT-0.** Verified by installing it back out of the registry into a scratch
dir — the delivered `SKILL.md` is byte-identical to `skills/treg/SKILL.md` in this repo.

The publisher org was created with
`clawhub publisher create superdesigndev --display-name "Superdesign dev, Inc."`. Publishing under a
personal handle would also have worked — `--owner <handle> --migrate-owner` moves a skill later — but
a company handle reads as a product listing rather than a side project.


`https://clawhub.ai/submit` **404s** (Hermes' CLI still points there; its `--to clawhub` is a stub).
The live path is the npm CLI:

```bash
npm i -g clawhub
clawhub login                     # device flow; opens a browser (--no-browser prints a URL + code)
clawhub whoami                    # confirm before publishing

clawhub skill publish "$PWD/skills/treg" \
  --slug treg --name "treg" --version 0.11.0 \
  --changelog "..." \
  --topics "api,seo,serp,backlinks,enrichment,scraping,agent-tools,web-search" \
  --source-repo superdesigndev/treg \
  --source-commit "$(git rev-parse HEAD)" \
  --source-ref main --source-path skills/treg \
  --dry-run                       # drop --dry-run to publish for real

# Publishing is TWO PHASES. The command above only SUBMITS — the version sits at
# `pending.publication`, invisible on every public surface, until a scan is attached to it:
clawhub scan --slug treg --version 0.11.0 --update    # took ~37 minutes; let it run
```

**Do not skip that second command, and do not conclude the publish failed while it runs.** This is
the single least obvious thing about ClawHub and it is not in their docs. `clawhub skill publish`
reports `"status": "pending-publication"` and exits; nothing else happens on its own. `scan --update`
("write published scan results back to the selected version") is not a convenience — it is the step
that completes publication. Watch the moderation record flip:

```bash
curl -s -H "Authorization: Bearer $(clawhub token)" \
  https://clawhub.ai/api/v1/skills/treg/moderation
```

| | before `scan --update` | after |
|---|---|---|
| `legacyReason` | `pending.publication` | `scanner.llm.review` |
| `reasonCodes` | `[]` | `["review.llm_review"]` |
| public `GET /api/v1/skills/treg` | 403, hidden | returns the skill |

Note `package publish` has a `--wait` flag that polls to a definitive state; **`skill publish` does
not**, and `/api/v1/publish/attempts/<attemptId>` 404s for skill attempts. So for skills there is no
progress indicator at all — run `scan --update` and wait it out.

Verified against clawhub CLI **v0.23.3** — the published docs are thinner than the real `--help`,
so read the CLI:

- **Pass an ABSOLUTE path.** A relative `./skills/treg` fails with `Path must be a folder`, because
  paths resolve against `--workdir` + `--dir` (which defaults to `skills`), so it looks for
  `skills/skills/treg`. `"$PWD/skills/treg"` is the reliable form.
- **The `--source-*` flags are worth filling in.** They link the listing back to a GitHub repo,
  commit and path. That is the one provenance signal available on a registry whose own vetting
  history is the reason Hermes marks every ClawHub skill `community` trust.
- **`--categories` takes slugs the public API does not enumerate** (`/api/v1/categories` 404s and
  listings come back with empty `categories`). Do not invent one — use `--topics`, which is
  free-form, until a real category slug can be read off a live listing.
- `--dry-run --json` prints the resolved slug, version, file count and fingerprint without
  publishing. `latestVersion: null` means the slug is unclaimed.
- Publishing is not irreversible: `clawhub delete` soft-deletes a skill or withdraws one version,
  and `clawhub hide` / `undelete` exist too. Ownership can move later with
  `--owner <handle> --migrate-owner`, so publishing under a personal handle now does not lock the
  skill out of a `superdesigndev` org later.

Requirements the generated skill already satisfies: frontmatter `name` + `description` + semver
`version`, with `name` matching the parent directory (`treg`). A security scan runs post-publish;
flagged releases are hidden from the public catalog but stay in your dashboard.

⚠️ **ClawHub forces MIT-0 on every skill, with no per-skill override.** This repo is Apache-2.0 plus
a hosted-service restriction. `SKILL.md` is prose — it documents a hosted service rather than
implementing one — so licensing that one file permissively is consistent with wanting it copied
everywhere. It is a deliberate choice, not an oversight; do not let it drift into a claim about the
server code.

Listing here also feeds Hermes' Skills Hub automatically. Note Hermes hard-codes every ClawHub skill
to `community` trust after the Feb 2026 "ClawHavoc" incident (341 malicious skills), so this is a
discovery channel, not a trust signal.

#### Two verdicts, and only one of them gates visibility

Do not confuse these — one costs a day if you do:

- **Moderation** (`/api/v1/skills/treg/moderation`) is what hides or shows a skill. Ours is
  `verdict: "clean"`, `isSuspicious: false`, `isMalwareBlocked: false`, empty `reasonCodes`. Reading
  the CLI source: `isSuspicious` only prints an install-time warning that `--force` bypasses; only
  `isMalwareBlocked` hard-blocks.
- **`clawscan`** (`clawhub scan`) is an LLM review published as an **advisory on the listing**. It
  rated treg `suspicious` — and that changed nothing about visibility.

The v0.11.0 advisory raised four findings against `SKILL.md`. Three restate deliberate product
decisions; **one is worth weighing on its own merits, independent of ClawHub**:

| id | sev | what it says |
|---|---|---|
| `PE3` | **HIGH** | `cli_auth` consumes material from a CLI's keychain and relays it — "expands the agent's credential-access surface … without a strong, explicit consent boundary". Remediation suggested: per-tool opt-in before using keychain-derived credentials, and prefer scoped service tokens. |
| `SQP-1` | MEDIUM | the frontmatter `description` is broad enough to get the skill selected for loosely-related tasks |
| `SQP-2` | MEDIUM | `curl … \| sh` executes remote code with no checksum or signature |
| `EA2` | MEDIUM | the "ask once, not per command" guidance discourages per-command approval |

`EA2` and `SQP-2` are the trade-offs treg made knowingly. **`PE3` is a real description of a real
feature** and deserves a decision rather than a dismissal — see `docs/context/architecture/` for how
`cli_auth` binds. Static analysis and VirusTotal both came back clean.

### 3. Skill Store (aiskillstore/marketplace) + skills.sh

**Do NOT open a pull request against `aiskillstore/marketplace`.** Its README is explicit — *"PRs
adding skills will be closed"*; the repo is written to by the platform's own review pipeline, not by
contributors. Submit through the form instead:

> <https://skillstore.io/submit> — one required field, and it accepts a **directory**, so give it:
> `https://github.com/superdesigndev/treg/tree/main/skills/treg`

GitHub auth is optional (it only adds review notifications); anonymous submissions are allowed. The
audit there is **report-only** — a risk finding does not block publication. This is still worth doing
because `aiskillstore/marketplace` is one of only two repos in Hermes' `KNOWN_MARKETPLACES`, so it is
the cheapest route into Hermes' `claude-marketplace` source. Note `npx skillstore` is install-only —
there is no CLI submit path.

**skills.sh / `npx skills` — pass `-s treg`, always:**

```bash
npx skills add superdesigndev/treg -s treg     # ✅ just the treg skill
npx skills add superdesigndev/treg             # ❌ installs NINE skills
```

Verified on a scratch machine: the bare form scans the whole repo, finds `.agents/skills/*` as well
as `skills/treg/`, and installs this repo's **internal development skills** (`dev-local`,
`tools-registry-context`, `write-provider-skill`, `add-oauth-provider`, `vendor-listing`, the three
Google-provider skills) into the user's agent alongside treg. That is our layout leaking into a
product install. `skills.sh.json` does not prevent it — it only supplies category labels.

`-s treg` scopes it to one skill and is what every install line we publish must carry. With it, that
one command still covers 70+ coding agents beyond Claude Code.

### 4. anthropics/claude-plugins-official — the queue

Submit at <https://claude.ai/settings/plugins/submit>. Highest-trust placement, and it also feeds
Hermes. It is a review queue — the same "takes too long" risk as the MCP track — which is exactly why
it is last: everything above is already shipping by the time it is filed.

Entries in that directory use `git-subdir` with a pinned `ref` **and** `sha`, so being inside this
monorepo is not a blocker. **Tag a release and give them the tag.**

## The fourth door: treg.to itself

`GET /.well-known/skills/index.json` + `/.well-known/skills/treg/SKILL.md` (in `src/treg/api.py`)
make this host a first-class skill source under the agentskills.io convention — no registry, no
review, no third party. Hermes reads it directly. Because it goes through the same `_serve_md` as
`/skill.md`, `{BASE}` is templated to the **serving** host, so a self-hosted registry advertises
itself rather than treg.to.

## Before you submit anywhere

- `uv run --frozen python -m pytest tests/test_plugin.py tests/test_skill_md.py -q`
- `claude plugin validate .` — validates the marketplace manifest the way Claude Code itself parses
  it, which catches shape errors the JSON schema does not.
- Install it for real from a scratch clone, because markup that reads correctly still breaks:

  ```bash
  git clone --depth 1 https://github.com/superdesigndev/treg /tmp/treg-check
  claude plugin marketplace add /tmp/treg-check
  claude plugin install treg@treg
  claude plugin details treg@treg     # then: uninstall + marketplace remove
  ```

  `details` must report **Skills (1) treg** and **MCP servers (1) treg**, but it counts a server
  Claude Code later rejects. `claude mcp list` is the real check: it must list
  `plugin:treg:treg: https://treg.to/mcp/ (HTTP) - ! Needs authentication`.
- `claude plugin tag` creates the `treg--v{version}` release tag and validates that `plugin.json`
  and the enclosing marketplace entry agree — worth running before handing a tag to Anthropic.
- Confirm the copy still says treg **compares** providers and the caller chooses. treg does not route
  and does not fail over; the landing page had to be corrected for that claim once already, and a
  store listing is much harder to correct. `test_the_listing_does_not_promise_routing` guards it.
- Confirm the one-line positioning is identical in all three places (`test_one_product_one_sentence`).
