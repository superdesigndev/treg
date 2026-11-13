# Checking a comparison page before you commit

## Run it
The pages render only when the registry thinks it is treg.to. Run a preview on a spare port:

    set -a && . scripts/.dev-home/dev-keys.env && set +a
    TREG_DATABASE_URL=sqlite+aiosqlite:///$PWD/treg-dev.db TREG_PUBLIC_URL=https://treg.to \
      TREG_PORT=18797 uv run python -m treg --port 18797

Then open http://localhost:18797/compare and the page. (`scripts/dev-local.sh up` runs on
localhost, where /compare is a 404 by design.)

## Tests
    uv run --with pytest-xdist pytest -n auto -q tests/test_compare_pages.py

They check: every row and card filled, each competitor fact sourced on its own site, structured
data equal to the visible text, the hub linking every page, a markdown twin kept out of the index,
nofollow on every outbound link, and a share image for every comparison.

## Look at it
- Desktop at 1280 and 1512 px: the on-this-page outline appears after the hero (1400 px and up).
- Phone at 390 and 320 px: no side scroll, the navbar shows "treg / vs X" with an ellipsis for a
  long name, the date chip on one line, the table shows a greyed logo before each product's name.
- Hub cards: 3 lines of text each, no two opening alike.

## Scan the copy
Search the record for treg-negative words in prose: only, just, a few, lacks, limited, smaller,
has no, does not offer, cannot. "No plan", "no seats" and "no subscription" are fine.

## Share image
    uv run --frozen python scripts/og_images.py /compare/treg-vs-<slug>
Open the JPEG and check the name fits, the logo is the product's own, and the `sub` reads well.
