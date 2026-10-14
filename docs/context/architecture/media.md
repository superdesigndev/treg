---
title: Media hosting - reference files a vendor can fetch (`treg host`)
status: shipped
sources:
  - src/treg/application/media.py
  - src/treg/routers/media.py
  - src/treg/models.py
  - src/treg/alembic/versions/0037_media_hosting.py
  - tests/test_media.py
related:
  - architecture/proxy-model.md
  - interface/cli.md
  - interface/api.md
---

# Media hosting

AIGC endpoints (reAPI, PiAPI, OpenRouter video) take reference images, voice clips and videos as
public URLs that the vendor's fetcher downloads. An agent on a laptop has no host, and every paste
host tried failed a vendor's pre-flight probe at least once: catbox unreachable from reAPI's
fal-backed probe, tmpfiles answering HTML on its download link, uguu timing out. Five submissions
to land one 18 s Seedance task was the cost on 2026-09-14. `treg host <file>` removes that class.

## Shape

- `POST /media` (member+, raw body, `Content-Type` names the media type) stores the bytes in the
  `media` table and answers `{url, token, content_type, size, expires_at}`. The URL is
  `<public_url>/m/<token>`.
- `GET /m/{token}` is unauthenticated on purpose: the vendor's fetcher holds no treg token. The
  token is 192 random bits and the row names nothing about the org, so the URL is the only
  capability. Served inline with the stored type, `Cache-Control: public, max-age=3600` and
  `X-Robots-Tag: noindex`. No listing endpoint exists.
- Bounds, all in `application/media.py`: 30 MB per file (the vendors' own reference limit),
  300 MB per org per rolling 24 h, 7-day TTL (reAPI's output URLs live about as long), and only
  `image/*`, `audio/*`, `video/*`, with SVG denied (an image type that carries script). Sandbox
  orgs are refused. The router refuses on `Content-Length` and again mid-stream at the cap, so an
  oversized body never reaches worker RAM in full.
- Expiry is swept inline on every upload (`DELETE WHERE expires_at < now`) rather than by a cron;
  an expired row also 404s on read before the sweep reaches it.
- Not metered. Hosting is a courtesy like polling, so money's five entries stay untouched and the
  call runtime never touches this table.

## Why bytes in the database

The archive's object store is private and configured per deployment (`archive_body_write` is
`db` in production today). A row in Postgres serves a 30 MB reference correctly, keeps the feature
one table and one router, and needs no public bucket. Move bodies to the object store when the
media table's size, not its correctness, becomes the problem.

## Not built

- A treg-side pre-flight fetch of the URLs an agent passes to a vendor. It would model the
  vendor's probe, which non-negotiable 4 forbids, and it cannot see host blocks that apply only to
  the vendor's fetcher (catbox served us 200 while fal got nothing).
- An MCP `host` tool. MCP clients that need it can `POST /media` directly; add the tool when an
  agent asks for it, and pin it in the MCP tool-set tests like the others.
