#!/usr/bin/env python3
"""Generate every plugin's SKILL.md from the one source: `src/treg/web/skill.md`.

Five shop windows, one source:

- **codex**  -> `plugin/skills/treg/SKILL.md` — ships an MCP connector, so its bootstrap points at
  the MCP tools and tells the reader NOT to reach for a terminal.
- **skills** -> `skills/treg/SKILL.md`: the bare skill that `npx skills add` and ClawHub publish:
  the served page unchanged, since its first section already covers an agent with neither the tools
  nor the CLI. It gets a `version:` stamped into its frontmatter, which ClawHub requires.
- **plugin** -> `plugins/treg/skills/treg/SKILL.md`: the one plugin folder Claude Code and Cursor
  both install. It declares the remote MCP server (OAuth), so its bootstrap checks for the tools
  first and sends the human to sign in.
- **dsh**    -> `dsh/skills/treg/SKILL.md` — DeepSeek Harness, the only surface that ships the
  connector AND the CLI path in one install: the MCP row is disabled until `TREG_TOKEN` exists, so
  its bootstrap has to cover both states and steer away from `treg mcp install`, which cannot write
  a dsh profile. See docs/DSH-PLUGIN.md.
- **minimax** -> `plugins/minimax/skills/treg/SKILL.md`: skills-only, the served page unchanged.
  See docs/MINIMAX-PLUGIN.md.

The bare skill sits at the REPO ROOT because that is what `npx skills add` resolves and what
`clawhub skill publish` takes. The Claude Code plugin is NOT the repo root: a root plugin ships the
whole repository and every workflow skill under `skills/`.

Each plugin is a shop window, and what it ships is the SAME skill `treg skill bootstrap` already
writes into every agent's skills dir. Copying that file by hand would be a second source of truth for
the product's most-read page, and it would rot: the served copy changes whenever the product does,
and nothing would notice a plugin drifting behind it. So all of them are generated, and
`tests/test_plugin.py` fails if any checked-in copy is stale.

Three transformations, each for a reason the served file does not have:

1. `{BASE}` is a placeholder the SERVER substitutes per request (`api.py` `/skill.md`). Nothing
   substitutes it inside an installed plugin, so a raw copy would ship the literal string `{BASE}`
   and every URL in it would be broken. It is baked to the public deployment here.

2. A short section may be prepended, and it is the ONLY thing that differs between the variants.
   The served page's first section branches on what the agent has (MCP tools, the CLI, neither), so
   a bootstrap says only what that host adds: a plugin's tools waiting for sign-in, or dsh's
   token-gated row.

3. The bare skill and the plugin get `version:` stamped into their frontmatter. ClawHub requires it
   and the plugin hosts ignore it.

Usage:  python3 scripts/build_plugin.py [--check]
        --check exits 1 if any generated file differs from what is checked in (used by the test).
"""

from __future__ import annotations

import functools
import json
import re
import shutil
import sys
from pathlib import Path

ROOT_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ROOT_SRC))
from treg.domain.catalog import store as catalog_store  # noqa: E402  (needs the [server] extra: run via `uv run`)

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src" / "treg" / "web" / "skill.md"
PUBLIC_BASE = "https://treg.to"

CODEX_TARGET = ROOT / "plugin" / "skills" / "treg" / "SKILL.md"
SKILLS_TARGET = ROOT / "skills" / "treg" / "SKILL.md"
# Cursor's layout is prescribed by github.com/cursor/plugin-template: a listing manifest at the
# repo root, and every plugin under `plugins/<name>/` with its OWN `.cursor-plugin/plugin.json`.
# Claude Code installs the same folder through its own `.claude-plugin/plugin.json`.
PLUGIN_TARGET = ROOT / "plugins" / "treg" / "skills" / "treg" / "SKILL.md"
# DeepSeek Harness installs an npm package, not a plugin manifest: `package.json` declares
# `dsh.bundle`, and `dsh/cordis.patch.yml` mounts `dsh/index.js` as a `ctx.skills` provider over
# this tree. See docs/DSH-PLUGIN.md.
DSH_TARGET = ROOT / "dsh" / "skills" / "treg" / "SKILL.md"
# MiniMax (MiniMax Code desktop + MiniMax Agent cloud) prescribes `.minimax-plugin/plugin.json` at
# the plugin root and forbids credentials anywhere in the package, so it ships skills-only from its
# own root — submitted as the GitHub subdirectory `plugins/minimax/`, or zipped from inside it. See
# docs/MINIMAX-PLUGIN.md.
MINIMAX_TARGET = ROOT / "plugins" / "minimax" / "skills" / "treg" / "SKILL.md"
CLAUDE_MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
CLAUDE_MANIFEST = ROOT / "plugins" / "treg" / ".claude-plugin" / "plugin.json"
WORKFLOWS = ROOT / "plugins" / "workflows"
WORKFLOW_SOURCES = ROOT / "src" / "treg" / "web" / "skills"

# The Codex plugin is the one variant that is NOT the served page plus a bootstrap. It ships an MCP
# connector, and OpenAI's plugin review refuses a skill that "may download or execute code outside
# the reviewed package" - which the served page does (`curl ... install.sh | sh`, the CLI throughout).
# So it ships this connector-only skill whole: the tools, the flow, the rules. Nothing to install.
CODEX_SKILL = """---
name: treg
description: People and company enrichment, AEO and SEO, ads, social, web scraping, image and video generation through the treg connector. Use when a task needs external or live data; search the catalog by the task, check the endpoint, then call it.
---

# treg

treg is connected as an MCP connector. Use its tools; there is nothing to install and no API key to
ask the human for - treg adds the provider credential on its server.

| tool | use it for |
|---|---|
| `catalog_search` | find an endpoint by what you want to do - "work email", "backlinks for a domain", "tiktok comments" |
| `catalog_get` | one endpoint's parameters, price per call, and the other providers for the same job with measured success rate and speed |
| `call` | call a catalog endpoint by id, or one of the team's own tools from `my_tools` |
| `call_media` | call an audio endpoint (text-to-speech) and get the audio back |
| `resources_list` | durable provider resources the team created, such as voices |
| `balance` | the team's prepaid balance |
| `my_tools` | API accounts and connections the team registered |
| `catalog_request` | file a one-line request when the catalog has nothing for the task |
| `feedback` / `review` | report a problem with treg, or rate a call result when asked |

## Flow

1. `catalog_search` with the task in plain words, not a vendor name. Read `verdict`: `strong` means
   these do it, `closest` means check `catalog_get`, `none` means it is not in the catalog.
2. `catalog_get` the endpoint you picked. When several providers do the same job, compare their
   price, success rate and speed, pick one, and tell the human the price before a call that costs
   more than a cent.
3. `call(endpoint_id, params)`. If a call's answer never arrived (timeout, dropped connection),
   repeat it with the same `idempotency_key`; treg returns the stored answer instead of calling again.
4. Nothing fits: `catalog_request` one sentence saying what is missing, then tell the human.

## When something goes wrong

- **Not authenticated:** the connector has no sign-in yet. Tell the human to connect treg (sign in at
  {BASE}) and stop.
- **Refused for funds:** check `balance` and tell the human to add funds at {BASE}.
- **A provider error:** the response is the provider's own answer, relayed unchanged; read it before
  retrying, and try another provider from `catalog_get` if it keeps failing.
- `feedback` and `review` text goes to the treg team; keep it about the tool and leave out private
  data, credentials and raw responses.
"""

# The plugin folder (Claude Code, Cursor, Grok Bot) declares the MCP server URL-only and the host
# runs OAuth itself. A `${env:TREG_TOKEN}` header made Grok Bot demand the variable before sign-in
# could start, which no phone user can supply. The served page already branches on whether the tools
# are there; the plugin only adds that they come with it, waiting for sign-in.
PLUGIN_BOOTSTRAP = """
This plugin ships treg's MCP tools. If you cannot see them yet, they are waiting for sign-in: the
human connects treg in the app's MCP settings (in Claude Code: `/mcp`, pick treg, Authenticate). Do
not install the CLI for this.

In Claude Code, treg's marketplace also offers ready workflow skills as separate plugins, each
described in `/plugin`:

{WORKFLOWS}

When the human's task matches one, offer it once; they add it with `/plugin install <name>@treg`.
"""

DSH_BOOTSTRAP = """
## First, check which treg you have

This bundle ships two things: the page you are reading, and a treg MCP connector that stays
**disabled until `TREG_TOKEN` is in dsh's environment** — a connector registered without a token is
MCP tools that return 401 on every call. So the first move depends on which one you got.

**If you can see `mcp__treg__catalog_search`, `mcp__treg__catalog_get`, `mcp__treg__call`,
`mcp__treg__balance` and `mcp__treg__my_tools`** — the token was there at boot and there is nothing
to install. The rest of this page is written around the `treg` command line, so read
`treg catalog search` as `mcp__treg__catalog_search`, `treg call` as `mcp__treg__call`, and so on.

**If you cannot see them**, this profile booted without a token. Everything here still works through
the CLI, which you set up once:

```bash
curl -fsSL {BASE}/install.sh | sh   # the CLI, pointed at treg
treg login                                   # sign in; first login registers you
```

A new team starts with **$1.00 of free balance**, so there is nothing to pay before the first call.
If sign-in is needed, say so plainly and stop — never ask the human for a provider's API key, which
is the thing treg exists to avoid.

To get the tools as well, the human exports that token in the environment dsh starts in and
**restarts dsh** — the row is then enabled automatically:

```bash
export TREG_TOKEN=<token from {BASE} → sign in → copy token>
```

Do **not** run `treg mcp install` for this: it writes configs for Claude Code, Cursor and opencode,
and a dsh profile is neither. This bundle already carries the dsh row.

One more thing worth mentioning to the human rather than silently fixing: `install.sh` always runs
`treg skill bootstrap`, which drops a second copy of this same page into `~/.agents/skills/treg/` —
a directory dsh also scans. Harmless, but redundant with this bundle.

---
"""

# variant -> (target, bootstrap, stamp_version). One source, five shop windows; None means the
# served page unchanged.
VARIANTS = {
    "codex": (CODEX_TARGET, None, False),
    "skills": (SKILLS_TARGET, None, True),
    "plugin": (PLUGIN_TARGET, PLUGIN_BOOTSTRAP, True),
    "dsh": (DSH_TARGET, DSH_BOOTSTRAP, False),
    "minimax": (MINIMAX_TARGET, None, True),
}


@functools.cache
def package_version() -> str:
    """The one version, read from pyproject — the same value `tests/test_plugin.py` pins both
    manifests to. Read rather than duplicated so a release bump cannot leave the skill behind."""
    for line in (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version = "):
            return line.split('"')[1]
    raise SystemExit("pyproject.toml has no `version = \"…\"` line")


def workflow_names() -> list[str]:
    return sorted(p.parent.name for p in WORKFLOW_SOURCES.glob("*/SKILL.md"))


def workflow_description(skill: str) -> str:
    """The skill's own frontmatter description, which is also what `/plugin` shows for it."""
    for line in skill.split("---", 2)[1].splitlines():
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip()
    raise SystemExit("a workflow skill has no description in its frontmatter")


def render(variant: str) -> str:
    target, bootstrap, stamp_version = VARIANTS[variant]
    if variant == "codex":
        return CODEX_SKILL.replace("{BASE}", PUBLIC_BASE)
    text = SOURCE.read_text(encoding="utf-8")
    if "---" not in text:
        raise SystemExit(f"{SOURCE} has no frontmatter — refusing to guess where it ends")
    # Keep the frontmatter exactly as served (name + description drive discovery in BOTH surfaces),
    # and insert the bootstrap immediately after it, before the skill's own opening.
    _, fm, body = text.split("---", 2)
    if stamp_version:
        # ClawHub REQUIRES a semver `version` in frontmatter (and requires `name` to match the
        # parent directory, which `treg` already does). Claude Code ignores the extra key, so
        # stamping it here is what lets one generated file satisfy both registries.
        fm = f"{fm.rstrip()}\nversion: {package_version()}\n"
    if bootstrap:
        bootstrap = bootstrap.replace("{WORKFLOWS}", "\n".join(f"- `{n}`" for n in workflow_names()))
    out = f"---{fm}---\n" + (f"{bootstrap}\n" if bootstrap else "") + body.lstrip(chr(10))
    # 4. `<!--routed-->` / `<!--/routed-->` delimit the section the SERVER strips when a deployment
    #    sets TREG_ROUTED_DISCOVERY=off. A plugin copy is static and cannot know a deployment's
    #    setting, so it keeps the content — but the markers themselves must never ship, or the
    #    product's most-read page starts with visible HTML comments.
    out = out.replace("<!--routed-->\n", "").replace("\n<!--/routed-->", "")
    # 5. `<!--hub-->` / `<!--/hub-->` delimit the tool-hub section. A plugin is a static file that
    #    every user of every agent installs, while the hub is open only to the teams in
    #    TREG_HUB_TEAMS: so the plugin never carries it. A member of such a team gets the section
    #    from the server itself (`treg skill bootstrap` signed in, or /skill.md with their key).
    out = re.sub(r"<!--hub-->.*?<!--/hub-->\n?", "", out, flags=re.S)
    # `{ENDPOINTS}` / `{PROVIDERS}`: the server fills these per request from the loaded catalog; a
    # static plugin copy gets the numbers as of generation, refreshed with every release.
    endpoints, providers = catalog_store.headline_counts(catalog_store.load())
    out = out.replace("{ENDPOINTS}", endpoints).replace("{PROVIDERS}", str(providers))
    return out.replace("{BASE}", PUBLIC_BASE)


# Accepted and ignored: a plugin never ships the hub section (see the comment in the renderer).
WITH_HUB = "--with-hub" in sys.argv


def main() -> int:
    check = "--check" in sys.argv
    stale = False

    def emit(target: Path, generated: str, source: Path) -> None:
        nonlocal stale
        current = target.read_text(encoding="utf-8") if target.exists() else None
        rel = target.relative_to(ROOT)
        if check:
            if current != generated:
                print(f"STALE - {rel} does not match {source.relative_to(ROOT)}", file=sys.stderr)
                stale = True
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(generated, encoding="utf-8")
        print(f"wrote {rel}  ({len(generated.splitlines())} lines)")

    for variant, (target, _, _) in VARIANTS.items():
        emit(target, render(variant), SOURCE)

    # The workflow skills (make-ugc, lead-signals…) as real files under skills/: `npx skills add
    # superdesigndev/treg` (skills.sh) reads that folder and skips symlinks, so a link would hide them.
    # Each is also its own Claude Code plugin in this repo's marketplace, so `/plugin` offers them one
    # by one beside `treg` instead of installing all of them with it.
    workflows = []
    shared = json.loads(CLAUDE_MANIFEST.read_text(encoding="utf-8"))
    shared = {k: shared[k] for k in ("author", "homepage", "repository", "license")}
    for name in workflow_names():
        src = WORKFLOW_SOURCES / name / "SKILL.md"
        generated = src.read_text(encoding="utf-8").replace("{BASE}", PUBLIC_BASE)
        emit(ROOT / "skills" / name / "SKILL.md", generated, src)
        emit(WORKFLOWS / name / "skills" / name / "SKILL.md", generated, src)
        description = workflow_description(generated)
        emit(WORKFLOWS / name / ".claude-plugin" / "plugin.json", json.dumps(
            {"name": name, "version": package_version(), "description": description, **shared},
            indent=2, ensure_ascii=False) + "\n", src)
        workflows.append({"name": name, "source": f"./plugins/workflows/{name}",
                          "description": description, "category": "productivity"})

    # The marketplace lists the hand-written `treg` entry, then every workflow plugin (generated).
    market = json.loads(CLAUDE_MARKETPLACE.read_text(encoding="utf-8"))
    market["plugins"] = [p for p in market["plugins"] if p["name"] == "treg"] + workflows
    emit(CLAUDE_MARKETPLACE, json.dumps(market, indent=2, ensure_ascii=False) + "\n", WORKFLOW_SOURCES)

    # A workflow skill that was renamed or removed must not linger as a published plugin or skill.
    generated = {*workflow_names(), "treg"}
    for leftover in sorted({*WORKFLOWS.glob("*/"), *(ROOT / "skills").glob("*/")}):
        if leftover.name in generated:
            continue
        if check:
            print(f"STALE - {leftover.relative_to(ROOT)} has no source in "
                  f"{WORKFLOW_SOURCES.relative_to(ROOT)}", file=sys.stderr)
            stale = True
        else:
            shutil.rmtree(leftover)
            print(f"removed {leftover.relative_to(ROOT)}")

    if stale:
        print("  regenerate with: python3 scripts/build_plugin.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
