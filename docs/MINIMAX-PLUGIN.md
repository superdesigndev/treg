# The MiniMax plugin, and how to submit it

treg's fifth shop window: the **MiniMax Plugin Marketplace**, which serves both **MiniMax Code**
(desktop) and **MiniMax Agent** (cloud). Like the Claude Code and Cursor plugins it is
**skills-only**, and for a harder reason than convenience: MiniMax rejects any package holding a
credential, and treg's MCP is bearer-authed per user. An authenticated MCP would have to go through
their **App/Connector** program (OAuth MCP server, PKCE, MiniMax-registered client) — see
"Later: an App" below.

Source of the rules: MiniMax's *Plugin Development and Submission Guide* (Feishu wiki
`QlTLwbAGNiACwLkWI85cFxHzn4L`, last updated 2026-08-17). It is a JS-rendered page behind a Feishu
login; the rules that matter are encoded in `scripts/minimax_plugin.py`.

## What is where

```
plugins/minimax/
├── .minimax-plugin/plugin.json   the manifest (schemaVersion 1)
├── icon.png                      512×512, the ▚ mark — same file as plugin/assets/icon.png
└── skills/treg/SKILL.md          GENERATED — never edit by hand
scripts/minimax_plugin.py         validator (--check) and ZIP builder (--zip OUT)
```

`plugins/minimax/` is the **plugin root** (a sibling of the Cursor root `plugins/treg/`; each store needs its own root because the manifests and generated bootstraps differ): `.minimax-plugin/plugin.json` sits directly inside it, which is
what MiniMax requires of a ZIP root or a GitHub subdirectory. Manifest `name` is `treg`; the skill is
exposed at runtime as `treg:treg`.

**Never edit `plugins/minimax/skills/treg/SKILL.md`.** Change `src/treg/web/skill.md` and regenerate:

```bash
python3 scripts/build_plugin.py            # regenerate ALL plugins
python3 scripts/build_plugin.py --check    # fail if any is stale (also a test)
python3 scripts/minimax_plugin.py --check  # MiniMax package rules (also a test)
```

### Why the bootstrap differs from Claude Code's

The Claude/Cursor bootstrap is three steps: install CLI → `treg login` → `treg mcp install`. The
MiniMax one has **two** and says explicitly not to run step 3. `treg mcp install` writes configs for
Claude Code, Cursor and opencode; there is no MiniMax target, so on MiniMax Code it is a silent
no-op an agent would walk straight past. Everything in the skill works through the CLI.

### What their validator enforces (and ours pre-checks)

- `.minimax-plugin/plugin.json` at the root, **no wrapper directory** in the ZIP.
- `name`: lowercase, starts with a letter, ≤80 chars of `[a-z0-9._-]`. `version`: SemVer, **must
  bump whenever package content changes** (region/target-only changes may keep it). Ours tracks
  `pyproject.toml` — `test_plugin_version_tracks_the_package` pins it.
- `category` from a fixed list (Office, Studio, Design & Sites, Code, Business, Sales,
  Productivity, Science & Healthcare, Education, Other). We use **Productivity**.
- `exampleQueries`: 0–3, non-empty, ≤4,096 chars, *production* content.
- `apps` / `mcpServers` / `skills`: relative paths; **unused ones stay as empty arrays**.
- `icon`: relative path, lowercase `.png/.jpg/.jpeg/.webp`, square.
- Skill: `skills/<name>/SKILL.md`, YAML frontmatter with `name` == directory and non-empty
  `description`; body must be executable instructions, not marketing.
- Paths ASCII-only, `/` separators. No symlinks, hardlinks, LFS pointers, submodules, install
  scripts, native binaries, executable bits. UTF-8 without BOM. JSON = object, no duplicate keys.
- Limits: ZIP ≤64 MiB, ≤2,048 entries, ≤1,024 files, ≤16 MiB per file, path ≤512 B / segment
  ≤128 B / depth ≤16.
- **No token, key, secret or personal data anywhere.**

The manifest carries no `license` key — their schema does not define one and unknown keys are a
validator risk. The licence is stated here instead: `SKILL.md` is prose about a hosted service and
ships under the repo's Apache-2.0 (see `LICENSE`).

## Submission runbook

Two sources are accepted and go through the same pipeline. **Prefer GitHub** — no artefact to
manage, and updates are a new `Ref`.

**Submit** (form linked from the guide: "Open the Plugin submission form"; needs a Feishu account —
use one the team keeps, the *same* account must query status later):

| field | value |
|---|---|
| Action | Submit a new Plugin |
| Region | **US** (and CN if wanted — separate dimension; treg.to is reachable from CN but billing is Stripe) |
| Delivery target | **Desktop (MiniMax Code) + Cloud (MiniMax Agent)** — the skill is CLI-driven, so cloud only helps if the Agent sandbox can run `install.sh`; pick both, they can disable one |
| Source | GitHub |
| Repo | `https://github.com/superdesigndev/treg` |
| Ref | `main` (or the release tag, e.g. `v0.12.1`) |
| Plugin subdirectory | `plugins/minimax` |
| Email | jason@superdesign.dev |

ZIP alternative — built deterministically, no wrapper directory:

```bash
python3 scripts/minimax_plugin.py --zip treg-minimax-$(python3 -c 'import tomllib;print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])').zip
```

**After submitting:** save the `submission_id`. Pipeline: source freeze → script validation
(failures come back as a Feishu message) → Release MR to the CN/US marketplace `main` → human
review of the MR diff, Markdown report, icon, CI → owner merges → pipeline packages → status
"Publishing" / "Published" / "Publishing failed" by Feishu message. Status query needs the
submitting Feishu account + `submission_id` + email.

**Updating:** `name` stays `treg`, bump `version` (the release bump does this), provide proof of
maintenance (the GitHub repo is the proof), resubmit with the new Ref. No change summary — they
diff the MR.

## Known review risks

1. **`curl … | sh` in the skill.** The package contains no install script, but the skill *tells the
   agent to run one*. ClawHub's LLM review flagged the same line (`SQP-2`, see
   `docs/CLAUDE-PLUGIN.md`). If a reviewer objects, the fallback is `pip install treg` /
   `uv tool install treg` — the CLI is on PyPI — and a one-line swap in `MINIMAX_BOOTSTRAP`.
2. **Cloud target.** On MiniMax Agent the skill only works if the sandbox has a shell with network
   egress and can open a browser for `treg login` (device-code login is what to point them at if
   not). If cloud fails their verification, resubmit desktop-only — that is a catalog-metadata change,
   no version bump.
3. **Description vs implementation.** Their rule: say what problem it solves, not how. The manifest
   description is capability-only; keep it that way.

## Later: an App

To get the five MCP tools into MiniMax natively, treg would enter their **App/Connector** track:
contact MiniMax *before* building, provide test + prod Streamable-HTTP MCP endpoints, an OAuth
issuer with discovery metadata, authorization/token/registration/revocation endpoints, scopes,
DCR/PKCE (S256) support, refresh tokens, callback allowlist needs, a test account, rate limits and
error codes. MiniMax then assigns a `provider` and the plugin gains `treg.app.json`
(`{"schemaVersion":1,"provider":"treg"}`). treg's MCP currently authenticates with a static bearer
token, which their guide says to raise with them *before* development — so this is a product
decision (an OAuth AS in front of `/mcp/`), not a packaging one.
