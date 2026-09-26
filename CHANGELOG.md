# Changelog

Notable changes to treg. Dates are release dates; anything under **Unreleased** is on `main` but not
yet published to PyPI or Homebrew.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and treg uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- **The tool hub — publish a tool made of other tools.** A team can publish its own tool and let
  other people's agents call it. A hub tool is one folder of four files: `recipe.json` (the
  contract), either a JSON steps recipe or a `run.js` script, `check.json` (sample inputs, run once
  for real at publish), and `README.md`. A fifth file, `data.csv`, travels with the tool and a
  script reads it as `ctx.data`, so a spreadsheet becomes a callable tool with no database.

  Every step goes out through treg under its own hold: a catalog step runs as the caller, a step on
  one of the maker's own tools runs as the maker and is never metered. A caller therefore never
  sees the maker's keys, and `uses` in the manifest names every host a tool may reach. A script
  runs in a separate process with no network of its own; `ctx.call` is its only road out.

  **Three ways to price it**, one `pricing` block per version, in the maker's words: `per_call` (a
  fixed price per successful run), `per_result` (a price times the integer `results` the run
  returns, bounded by a `results_from` input), and `percent` (a percent of the run's provider fees).
  The provider fees are billed to the caller on top. A variable price holds the most the maker can
  earn on that run (derived from the caller's ceiling or the results input, no declared cap needed)
  and settles the real amount, refunding the difference. Callers never see the mode: every surface
  leads with what recent successful runs cost, fees and price together, as one number or a
  low–high range. The price settles to the maker as `earned` credit, spendable at once.

  **Two switches the maker controls**, neither bumping the version: `listed` puts the newest live
  version into catalog search, and `public_log` shows a run log on the tool's share page. A hub
  tool is out of search until its maker lists it. The run log shows outcomes only — time, result,
  duration, steps, units and the price paid — and never who called, the inputs, or the output.

  New commands: `treg hub init | run | publish | ls | earnings | price | list | unlist | log |
  retire`. New MCP verbs: `hub_create`, `hub_update`, `hub_mine`. Every hub tool gets a share page
  at `/hub/<id>`, with an agent twin at `/hub/<id>.md`.

  The whole feature sits behind `TREG_HUB_ENABLED` and is **off by default**. While it is off every
  `/hub/...` route answers `404`, the agent-facing files carry no hub text, and catalog search
  returns no hub row.

### Changed

- The agent plugins (Claude Code, Codex, Cursor, DeepSeek Harness, MiniMax) now always carry the
  hub section. A plugin is a static file installed against any registry, including a self-hosted
  one that may have the hub switched on, so it ships the knowledge and lets the agent find out at
  run time. The section tells an agent to read a `404` as "this registry does not offer the hub"
  rather than retrying.
