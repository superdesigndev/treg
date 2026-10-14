# jev-memory

A Claude Code mod that remembers the preferences you state in your prompts, without forking an agent.

After each answered prompt, the mod splits it into sentences (up to 6) and asks
[Jev](https://treg.to/jev), a decision model reached through treg, one yes/no question per sentence in
a single request: does this sentence state a lasting preference, rule or fact about how you want work
done in this project, rather than a one-off task? Any sentence scoring 0.8 or higher is saved,
verbatim (trimmed to 300 characters, no duplicates), to `.claude/jev-memory.md` in the project. A toast
tells you ("jev saved: Always use pnpm here. (p=0.93)") and a short coin sound plays.

The saved lines go into the first message of every later session under "Preferences the user stated
in earlier sessions", unless the memory file is committed to the repo: a committed file is someone
else's text, so it is skipped. Turn on `takeOverMemory` to also drop Claude Code's own memory
instructions, so a preference is saved once, by the mod, instead of a second time into `CLAUDE.md`.

Above the prompt, a small panel:

    ◆ jev memory                      3 remembered · 14 turns judged · $0.00030
    ███░░░░░░█░░░░░░░░░░  ■ 4 saved  ░ 16 skipped
    ✓ 0.97 Never use npm in this project, always use pnpm.
    · 0.04 Fix the failing login test.

One cell per sentence Jev has judged (green saved, dim skipped, red failed call), and the last turn's
sentences with Jev's probability. The strip and counts are for the current session; "remembered" is
the number of lines in the memory file.

- `/jev-memory` lists what's saved.
- `/jev-memory forget <n>` removes line n.

The step-by-step install, and a prompt to build the same mod from scratch, are in the
[jev-memory skill](../../../skills/jev-memory/SKILL.md).

## Requirements

- Claude Code with mods (2.1.287 or later). The panel draws in the terminal; Claude Code Desktop's
  bundled CLI may be older.
- The `treg` CLI, signed in (`curl -fsSL https://treg.to/install.sh | sh`, then `treg login`). The mod
  looks for it at `$HOME/.local/bin/treg`; set the `tregPath` option if `command -v treg` says
  otherwise.

## Try it for one session

    claude --plugin-dir ./jev-memory

To install it for good, add this folder's parent as a local marketplace:

    claude plugin marketplace add ./examples/claude-code-mods
    claude plugin install jev-memory@treg-mods

## Options

| Option | Default | What it does |
| --- | --- | --- |
| `tregPath` | empty (`$HOME/.local/bin/treg`) | Where the `treg` CLI lives |
| `takeOverMemory` | `false` | Drop Claude Code's built-in memory instructions so only the mod saves preferences. It applies to every project the mod is installed for |
| `playSound` | `true` | Play the coin sound when a preference is saved |

Set one with `claude plugin configure jev-memory@treg-mods`, or at install time with
`--config tregPath=/path/to/treg`.

## Cost

One `openrouter.ai-judge.decide` call per answered prompt that has at least one sentence. A short
prompt settles around $0.00002; Jev bills by input tokens, so longer prompts cost a little more.
Listing and forgetting are free.

## Behavior notes

- Judging runs after the turn completes, off the turn's own dispatch, so a slow call never delays a
  turn. A failed call is skipped and counted in red on the panel.
- Only prompts you type are judged (not notifications, scheduled prompts, peers or other plugins'),
  and only turns that ended with an answer.
- Each typed prompt (up to 4,000 characters, 6 sentences) leaves your machine: treg sends it to
  OpenRouter and Jev's provider. Don't paste secrets into prompts with the mod on.
- Sentences are sent to Jev as quoted evidence with `& < >` escaped. The 0.8 threshold, counting and
  deduplication are in code, not in the model.

## Limitation

Jev judges each sentence on its own, with no view of the conversation, so a sentence like "Use the
same approach as before", or a one-off phrased as a rule ("Don't touch the auth module" while fixing
one bug), can be saved or missed. Check `/jev-memory` now and then and `forget` what doesn't belong.

## Tests

    claude plugin validate .
    claude plugin test .
