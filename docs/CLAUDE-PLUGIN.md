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

The file layout of every plugin variant, and why the MCP server is inline in the Claude manifest,
is in [docs/context/interface/skill.md](context/interface/skill.md). Two rules matter here:

The marketplace `source` must stay `./plugins/treg`: a source of `./` makes the repo root the plugin,
which ships the whole repository and every workflow skill under `skills/`. Each workflow skill is
its own optional plugin in the same marketplace (`plugins/workflows/<name>/`, generated with its
manifest and marketplace entry by `scripts/build_plugin.py`), so `/plugin` offers them one by one.

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

### 2. ClawHub (clawhub.ai) - self-serve, CLI-based

Published as [clawhub.ai/superdesigndev](https://clawhub.ai/superdesigndev), owner
`@superdesigndev`, MIT-0. After a publish, install it back out of the registry into a scratch dir
and confirm the delivered `SKILL.md` is byte-identical to `skills/treg/SKILL.md`.

`https://clawhub.ai/submit` **404s** (Hermes' CLI still points there; its `--to clawhub` is a stub).
The live path is the npm CLI:

```bash
npm i -g clawhub
clawhub login                     # device flow; opens a browser (--no-browser prints a URL + code)
clawhub whoami                    # confirm before publishing

clawhub skill publish "$PWD/skills/treg" \
  --slug treg --name "treg" --version <version> \
  --changelog "..." \
  --topics "api,seo,serp,backlinks,enrichment,scraping,agent-tools,web-search" \
  --source-repo superdesigndev/treg \
  --source-commit "$(git rev-parse HEAD)" \
  --source-ref main --source-path skills/treg \
  --dry-run                       # drop --dry-run to publish for real

# Publishing is TWO PHASES. The command above only SUBMITS — the version sits at
# `pending.publication`, invisible on every public surface, until a scan is attached to it:
clawhub scan --slug treg --version <version> --update    # can take tens of minutes; let it run
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

The published docs are thinner than the real `--help`, so read the CLI:

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
  `--owner <handle> --migrate-owner`.

Requirements the generated skill already satisfies: frontmatter `name` + `description` + semver
`version`, with `name` matching the parent directory (`treg`). A security scan runs post-publish;
flagged releases are hidden from the public catalog but stay in your dashboard.

⚠️ **ClawHub forces MIT-0 on every skill, with no per-skill override.** This repo is Apache-2.0 plus
a hosted-service restriction. `SKILL.md` is prose — it documents a hosted service rather than
implementing one — so licensing that one file permissively is consistent with wanting it copied
everywhere. It is a deliberate choice, not an oversight; do not let it drift into a claim about the
server code.

Listing here also feeds Hermes' Skills Hub automatically. Hermes hard-codes every ClawHub skill to
`community` trust, so this is a discovery channel, not a trust signal.

#### Two verdicts, and only one of them gates visibility

Do not confuse these — one costs a day if you do:

- **Moderation** (`/api/v1/skills/treg/moderation`) is what hides or shows a skill. Reading the CLI
  source: `isSuspicious` only prints an install-time warning that `--force` bypasses; only
  `isMalwareBlocked` hard-blocks.
- **`clawscan`** (`clawhub scan`) is an LLM review published as an **advisory on the listing**. A
  `suspicious` rating there changes nothing about visibility.

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
npx skills add superdesigndev/treg -s treg     # just the treg skill
npx skills add superdesigndev/treg             # every skill in the repo not marked internal
```

The bare form scans the whole repo, `.agents/skills/*` as well as `skills/`, and offers every skill
whose frontmatter lacks `metadata.internal: true` alongside treg. `skills.sh.json` does not prevent
it; it only supplies category labels. `-s treg` scopes it to one skill and is what every install
line we publish must carry.

### 4. anthropics/claude-plugins-official — the queue

Submit at <https://claude.ai/settings/plugins/submit>. Highest-trust placement, and it also feeds
Hermes. It is a review queue — the same "takes too long" risk as the MCP track — which is exactly why
it is last: everything above is already shipping by the time it is filed.

Entries in that directory use `git-subdir` with a pinned `ref` **and** `sha`, so being inside this
monorepo is not a blocker. **Tag a release and give them the tag.**

## The fourth door: treg.to itself

`GET /.well-known/skills/index.json` + `/.well-known/skills/treg/SKILL.md` (in `src/treg/routers/web.py`)
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
- Confirm the copy does not overstate provider choice: treg compares providers, and chooses only
  for routed endpoints and overflow, both disclosed in the response (AGENTS.md non-negotiable 4). A
  store listing is much harder to correct than the landing page.
