---
title: /follower-analysis - who follows an X account, and the board everyone sees
status: shipped
sources:
  - src/treg/web/follower-analysis.html
  - src/treg/web/media/follower-analysis/examples.json
  - src/treg/application/follower_board.py
  - src/treg/routers/web.py
  - src/treg/bootstrap.py
  - tests/test_follower_board.py
related:
  - interface/seo.md
  - architecture/archive.md
---

# /follower-analysis

A visitor types an X handle; the page reads the account's follower list, sorts a random 100 into
groups with Jev, and draws a ring of their faces plus "Followed by" (the biggest accounts read). Every
step is an ordinary `/call/` on the visitor's own team, run from the browser.

## The scan (browser)

- **Reading the list.** The follower cursor is `<follow time in ms × 2^20>|<anchor>`, and a cursor built
  for any date is accepted, so pages are read from across the whole history in parallel instead of
  only the newest. Each built page is checked (nobody on it joined X after the date asked for); when
  the check fails the scan falls back to plain paging from the top. X documents none of this.
- **Picking.** 100 non-empty followers are drawn at random, each weighted by how many followers its
  page stands for. Accounts up to `FULL_AUTO` followers are read whole.
- **Sorting.** One Jev call per follower answers category (the visitor's editable groups), seniority
  and person-or-organisation. Other, or under 0.6 sure, sends the follower's posts for a second call.
  An organisation leaves the groups unless it is an investment firm; in "Followed by" it shows as
  "Company" or "VC firm". The account itself is classified person or company for the table's tabs.
- **Facts.** Shares (on X 10+ years, a bio word, verified, places) hold from a sample. Combined reach,
  the 10k+ count and "followed this year" are shown only when the whole list was read: a handful of
  very large accounts decides them, and a sample cannot.

## The board (server)

- `POST /follower-analysis/board` keeps a signed-in visitor's finished scan (`follower_board.clean`
  caps every text and keeps only X's own image host). One entry per handle in the shared key-value
  store, plus one index row the table reads. A later save replaces the stored one only when it read
  more of the list, or when the same visitor finishes their own full read. Per-user and global
  windows bound the writes.
- `GET /follower-analysis/board.json` is the table; `GET /follower-analysis/board/{handle}.json` one
  result in full, without who saved it.
- **The stored numbers are the visitor's.** The scan runs in their browser, so the server checks the
  shape, not the truth, of what is saved.

## The default list

`media/follower-analysis/examples.json` is a small index of row summaries; each full result is
`media/follower-analysis/examples/<handle>.json`, fetched only when its row opens. The table shows
the board first, then these, each handle once. Typing a handle already on either opens it instead of
scanning again.

## Sharing

`/follower-analysis?u=<handle>` opens that result. The server fills the page's title and `og:`
description from the board row (or the default list), so a shared link previews the account and its
biggest groups. "Copy link" copies a ready post that tags @treg_ai with the link.
