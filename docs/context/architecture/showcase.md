---
title: Showcase leaderboard and pictures - applications, review, votes, avatars
status: shipped
sources:
  - src/treg/domain/showcase/__init__.py
  - src/treg/application/showcase.py
  - src/treg/routers/showcase.py
  - src/treg/domain/images/__init__.py
  - src/treg/application/images.py
  - src/treg/routers/images.py
  - src/treg/infra/imaging.py
  - src/treg/infra/media_store.py
  - src/treg/alembic/versions/0076_showcase_and_avatars.py
  - src/treg/web/showcase.html
  - tests/test_showcase_leaderboard.py
  - tests/test_images.py
related:
  - interface/seo.md
  - architecture/media.md
---

# Showcase leaderboard and pictures

`/showcase` has an editorial gallery written in the page itself ([seo](../interface/seo.md)) and a
database-driven leaderboard: teams apply, staff review, people upvote, and approved apps show activity
badges their makers agreed to. Writer ownership is in AGENTS.md. Every showcase route, like the page,
404s on a self-hosted registry.

## Applying and review

A team owner or admin applies (on /showcase or the Team page); the proposal sits in
`ShowcaseApp.pending` and nothing is public before staff approve it. An edit to a live app is a new
proposal while the live version stays. Not reviewed, applied at once because they can only reduce or
re-scope what is shown: the show flags (`SHOW_FLAGS`), the usage source, and taking a live app off
(status `hidden`; only staff show it again). Staff act through `/admin/showcase/*`
(`require_superadmin`), never the tables. Every change appends a `ShowcaseAppEvent` under the next
version; actions carry an expected version and a stale one is refused.

## Where an app's activity comes from

`ShowcaseApp.usage_source` is `team` (every call the team makes) or `keys` (only calls made with the
keys in `ShowcaseAppKey`). A key belongs to at most one app, must be the team's and not revoked; a
rotated key (`ApiKey.replacement_key_id`) keeps counting through its replacement. Calls with no key
count only for a whole-team app. Staff see the source but cannot change it.

## Weekly rows, counted incrementally

Monthly numbers cannot be maintained from a running total (the week leaving the window must be
subtracted, and distinct provider counts do not add), so `refresh_activity` (`treg-worker showcase
activity`) stores one `ShowcaseWeek` per source (a team or one key) per UTC week; the leaderboard sums
an app's sources over the last complete `WINDOW_WEEKS` (the running week never counts) and walks
active-day bits for the streak. Refused calls never reached a provider and are not counted. Rows
belong to sources, not apps, so switching an app's source only changes which rows it reads.

Each source has a `ShowcaseCursor`, the last `callrecord.id` counted. A run reads only later calls over
the `(org_id, id)` or `(org_id, api_key_id, id)` index in small batches, each a short transaction that
adds to the week rows and moves the cursor with the cursor row locked (inserted before locking), so
overlapping runs never count a call twice. Calls younger than `LAG` wait, so a lower id still being
committed is not skipped. Sources take turns a batch at a time so a busy one never starves the others.
Do not replace this with a per-week query: the `(org_id, created_at)` index does not hold provider or
tool, so a busy team's week reads a very large number of table rows. Approval counts nothing; an app
joins the activity boards after the next run, so schedule the command frequently. Upvotes are live.

## What leaves the server

`domain.showcase.public_entry` is the only shape activity leaves in: upvotes, badges and, per consent
flag, a banded call tier, Rising rounded to ten percent, streak days, a provider count, top
categories; never a team, an exact count or money. Boards rank only apps whose maker consented to that
board's metric. `GET /showcase/leaderboard` is `no-cache` with an ETag so an approval or upvote shows
at once. Upvotes need the browser session, a verified person (no agent, demo or suspended identity),
the same origin and a rate limit; there is no downvote. App links carry `rel="noopener ugc"`: makers
write the entries, and the referrer stays so they see visits from treg.

## Pictures

One pipeline serves every kind (`domain.images.KINDS`). An upload is size-capped while it streams, its
leading bytes must be PNG, JPEG or WebP, it is decoded under a pixel cap and re-drawn from its pixels
in a worker thread, and files are named by the SHA-256 of the re-drawn primary under `img/<kind>/`.
Screenshots keep their shape (nothing cropped or stretched).

Everything lands in the private staging bucket; publishing copies it to the public bucket through treg
(the SDK cannot copy across buckets). Avatars publish when set; showcase pictures on approval, before
the row points at them, and an approval whose staged pictures expired fails with "upload it again". A
replaced picture is deleted once no row names it; `treg-worker media sweep` deletes unnamed public
pictures older than a day (approval publishes before the row is saved). Same-named files are one file,
so a picture two teams use stays while either names it.

Bucket requirements: the public bucket may be shared, so treg touches only `img/` and the bucket must
carry no lifecycle rule not limited to its own prefix. Staging is treg's alone and needs a lifecycle
rule expiring objects after about 30 days; the admin queue's `pending_since` exists to warn before a
waiting proposal's pictures expire.

URLs come from `TREG_MEDIA_BASE_URL` or, while empty, treg's own `/media-files/`; the database stores
only keys. With no media settings, or an endpoint that is neither https nor localhost, the feature is
off (uploads 503, pages show initials), never breaking sign-in. Uploads refuse a cross-site Origin. No
database connection is held during storage I/O.
