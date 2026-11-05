---
title: Media hosting - reference files a vendor can fetch (`treg host`)
status: shipped
sources:
  - src/treg/application/media.py
  - src/treg/routers/media.py
  - src/treg/models.py
  - src/treg/infra/object_store.py
  - src/treg/alembic/versions/0037_media_hosting.py
  - src/treg/alembic/versions/0092_media_object_key.py
  - tests/test_media.py
related:
  - architecture/archive.md
  - architecture/proxy-model.md
  - interface/cli.md
  - interface/api.md
---

# Media hosting

AIGC endpoints take reference images, voice clips and videos as public URLs that the vendor's
fetcher downloads. An agent on a laptop has no host, and public paste hosts fail vendors' pre-flight
probes unpredictably. `treg host <file>` gives it one.

## Shape

- `POST /media` (member+, raw body, `Content-Type` names the media type) stores the bytes (see
  below) with a metadata row in the `media` table and answers
  `{url, token, content_type, size, expires_at}`. The URL is `<public_url>/m/<token>`.
- `GET /m/{token}` is unauthenticated on purpose: the vendor's fetcher holds no treg token. The
  token is 192 random bits and the URL names nothing about the team, so the URL is the only
  capability. Served inline with the stored type, `Content-Security-Policy: sandbox` and
  `nosniff` (user bytes on treg's origin must stay inert), `Cache-Control: public, max-age=3600`
  and `X-Robots-Tag: noindex`. No listing endpoint exists.
- Bounds live in `application/media.py`: a per-file cap matching vendors' reference limits, a
  per-team rolling 24 h byte quota, a TTL, and only `image/*`, `audio/*`, `video/*`, with SVG
  denied (an image type that carries script). Sandbox orgs are refused. The router refuses on `Content-Length` and again mid-stream at the cap, so an
  oversized body never reaches worker RAM in full.
- Expiry is swept inline on every upload rather than by a cron: one bounded batch
  (oldest first, `SKIP LOCKED`) per upload in its own transaction; each upload adds one row, so
  the sweep keeps up. An expired row also 404s on read before the sweep reaches it.
- The daily quota is exact under concurrency: the new row is inserted and the team's 24 h sum
  (which includes it) is read in one transaction, serialized per team by a Postgres advisory lock
  (SQLite serializes the insert's write lock), and an upload past the quota rolls back.
- Not metered. Hosting is a courtesy like polling, so money's entries stay untouched and the
  call runtime never touches this table.

## Where the bytes live

`media_body_write` picks where a new upload's bytes go; reads follow each row, never the setting.

- `db` (the default, and what a self-hosted registry without object storage keeps): the bytes are
  the row's `body`; no bucket needed.
- `r2`: the bytes go to the archive's private bucket (`archive_object_store_*`, the same R2
  client as archive bodies, `infra.object_store.build_r2`) as a named object
  `hosted-media/v1/<64 random hex>`; the row keeps metadata and `object_key`, and `body` is NULL.
  The key is drawn apart from the URL's token and names no team or file. Startup refuses `r2`
  while that bucket is not configured. `media_r2_timeout_s` bounds one whole transfer.

A database body in the media table is live, short-lived, large bytes: every upload and every
expiry writes and frees whole files in the database, which is what the move to the bucket stops.
`GET /m/{token}` reads the row, closes the session, then streams the object (`open_named`) with the
row's size as `Content-Length`; an object whose size differs from the row's answers 503, a
missing one 404, an unreachable bucket 503 with `Retry-After`. A row with a `body` is served from
it in either mode, so switching to `r2` needs no backfill (database rows leave by TTL) and switching
back keeps serving the objects while the bucket stays configured.

Upload order in `r2`: the sweep's transaction; then the quota transaction, which inserts the row
with its `object_key` and no `body`; then the object PUT with no session open. The token is
returned only after the PUT, so no URL reaches the row in between; a failed PUT deletes the row.
No database connection is held while the bucket is read or written (AGENTS.md rule 3);
`tests/test_media.py` checks it at the SDK seam.

Swept rows' objects are deleted off the request path, by one background deleter per process,
bounded by queue length and time per run. In `db` mode the
upload path never touches the bucket: swept rows' objects are not deleted there. So objects no row
names are expected: overflow or a failed delete, a run past its budget, a sweep in `db` mode, a
cancelled upload, and team deletion (which removes a team's rows without touching the bucket).
Such an object is unreachable, since no row means no URL serves it. **The bucket's lifecycle rule on
`hosted-media/` is what guarantees deletion**: give the bucket one that deletes those objects
some days past the TTL; the deleter only makes them go sooner.

Once a deployment has switched, the table's disk space comes back only after the database rows
have expired and the table has been rewritten (a repack or `VACUUM FULL`); plain vacuum makes the
space reusable but does not return it.

## Not built

- A treg-side pre-flight fetch of the URLs an agent passes to a vendor. It would model the
  vendor's probe, which non-negotiable 4 forbids, and it cannot see host blocks that apply only to
  the vendor's fetcher.
- An MCP `host` tool. MCP clients that need it can `POST /media` directly; add the tool when an
  agent asks for it, and pin it in the MCP tool-set tests like the others.
