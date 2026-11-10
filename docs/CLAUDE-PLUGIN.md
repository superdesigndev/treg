# The Claude Code plugin, and where it gets listed

treg is a Claude Code plugin served from this repo as its own marketplace, live on merge:

```
/plugin marketplace add superdesigndev/treg
/plugin install treg@treg
```

Then `/mcp`, pick `plugin:treg:treg`, and Authenticate. Every plugin variant, its layout and the
host constraints behind it are in [docs/context/interface/skill.md](context/interface/skill.md).
The plugin path never asks an agent to run a downloaded script, which Claude Code's auto mode
blocks by default.

The marketplace `source` must stay `./plugins/treg`: `./` would make the repo root the plugin and
ship the whole repository. Never edit a generated `SKILL.md`; change `src/treg/web/skill.md` and
run `uv run python scripts/build_plugin.py` (`--check` is also a test). Bump `version` in
`pyproject.toml` and every manifest together (`test_plugin_version_tracks_the_package`).

## Listing runbook

Self-serve listings first, review queues last.

### ClawHub (clawhub.ai)

`https://clawhub.ai/submit` 404s; publish through the npm CLI (`npm i -g clawhub`, `clawhub login`):

```bash
clawhub skill publish "$PWD/skills/treg" \
  --slug treg --name "treg" --version <version> --changelog "..." \
  --topics "api,seo,serp,backlinks,enrichment,scraping,agent-tools,web-search" \
  --source-repo superdesigndev/treg --source-commit "$(git rev-parse HEAD)" \
  --source-ref main --source-path skills/treg \
  --dry-run                       # drop --dry-run to publish for real
clawhub scan --slug treg --version <version> --update    # completes publication; can take a while
```

- **Publishing is two phases, and the second is undocumented.** `skill publish` only submits: the
  version sits at `pending.publication`, hidden everywhere, until `scan --update` attaches a scan.
  `skill publish` has no `--wait` and no progress endpoint. Check
  `GET https://clawhub.ai/api/v1/skills/treg/moderation` (bearer `clawhub token`):
  `legacyReason` moves from `pending.publication` to `scanner.llm.review`.
- **Pass an absolute path.** Paths resolve against `--workdir` + `--dir` (default `skills`), so a
  relative `./skills/treg` looks for `skills/skills/treg`.
- Fill the `--source-*` flags: they are the only provenance signal on the listing.
- `--categories` takes slugs the public API does not enumerate; use free-form `--topics`.
- Moderation is what hides a skill (only `isMalwareBlocked` hard-blocks). The `clawscan` rating is
  an advisory on the listing and does not change visibility.
- ClawHub forces MIT-0 on every skill. Licensing the `SKILL.md` prose permissively is deliberate;
  it says nothing about the server code's licence.
- After publishing, install it back into a scratch dir and confirm the delivered `SKILL.md` is
  byte-identical to `skills/treg/SKILL.md`. ClawHub feeds Hermes' Skills Hub at `community` trust.

### Skill Store and skills.sh

Do not open a PR against `aiskillstore/marketplace` (its README says such PRs are closed). Submit
`https://github.com/superdesigndev/treg/tree/main/skills/treg` at <https://skillstore.io/submit>;
its audit is report-only. It is one of Hermes' `KNOWN_MARKETPLACES`.

Every published `npx skills` line must carry `-s treg`: the bare form offers every repo skill
without `metadata.internal: true`, and `skills.sh.json` does not prevent that.

### anthropics/claude-plugins-official

Submit at <https://claude.ai/settings/plugins/submit>; it is a review queue. Entries use
`git-subdir` with a pinned `ref` and `sha`, so tag a release (`claude plugin tag` creates
`treg--v{version}` and checks `plugin.json` against the marketplace entry) and give them the tag.

## Before you submit anywhere

- `uv run --frozen python -m pytest tests/test_plugin.py tests/test_skill_md.py -q`
- `claude plugin validate .` catches shape errors the JSON schema does not.
- Install from a scratch clone (`claude plugin marketplace add <clone>`, `claude plugin install
  treg@treg`). `claude plugin details` counts a server Claude Code later rejects; `claude mcp list`
  must show `plugin:treg:treg` as HTTP, needing authentication.
- The listing copy must not overstate provider choice (AGENTS.md non-negotiable 4); a store
  listing is much harder to correct than the landing page.
