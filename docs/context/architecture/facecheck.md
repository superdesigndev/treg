---
title: FaceCheck — own-key face search over the native REST API
status: shipped
sources:
  - src/treg/catalog/facecheck.yaml
  - src/treg/web/logos/facecheck.svg
  - tests/test_facecheck.py
related:
  - architecture/catalog.md
  - architecture/auth-secrets.md
  - architecture/proxy-model.md
  - guides/expanding-a-category.md
---

# FaceCheck own-key face search

`FACECHECK` in `oauth_providers.py` registers FaceCheck.ID as an API-key provider on the
Enrichment shelf. A team connects its own token; the registry injects the raw value into
`Authorization` on `https://facecheck.id`. The Swagger security scheme is named `Bearer`, but
its type is `apiKey`: adding a `Bearer ` prefix would change the credential.

The connection check sends `POST /api/info`. A bogus token returned HTTP 200 with a non-empty
`error` and `code: EXCEPTION` on 2026-09-11; `token_reject_field="error"` rejects that envelope.
`remaining_credits=0`, `has_credits_to_search=false` and `is_online=false` are not invalid-token
signals. The absolute `probe_url` makes this a connection-only check: the existing generic tool
health-check builder takes `probe_path` as GET, which does not describe FaceCheck's POST API.
The provisioned tool instead includes a POST `api/info` example for its first call.

## Catalog surface

`facecheck.yaml` follows the official
[OpenAPI v1.02](https://facecheck.id/Swagger/v1/swagger.json). Four business operations are
listed; `/llms.txt` is documentation and is excluded.

| Tool | Native request | Purpose |
|---|---|---|
| `facecheck.web.face.upload` | POST `/api/upload_pic`, multipart | Upload images; returns `id_search` and input image IDs |
| `facecheck.web.face.search` | POST `/api/search`, JSON | Submit a search or query its progress/result |
| `facecheck.web.face.image.delete` | POST `/api/delete_pic`, query parameters | Remove an input image from a search request |
| `facecheck.account.usage` | POST `/api/info` | Read credits, engine availability and index size |

The upload endpoint needs binary multipart parts. Its input note explicitly directs callers to
CLI `--upload` or raw HTTP; JSON-only MCP requests cannot upload a local file. There is no URL-fetch
adapter, image conversion, result normalization or automatic search loop in the relay.

Example after connecting through `treg connections connect --provider facecheck`:

```bash
treg call facecheck.account.usage --method POST
treg call facecheck.web.face.upload --method POST --upload images=@/path/to/photo.jpg
```

Copy the returned `id_search` into the JSON below. The demo search does not deduct credits but only
covers the first 100,000 faces. It checks the protocol, not search quality.

```bash
treg call facecheck.web.face.search --method POST \
  --data '{"id_search":"YOUR_SEARCH_ID","demo":true,"with_progress":true,"status_only":false}'
treg call facecheck.web.face.search --method POST \
  --data '{"id_search":"YOUR_SEARCH_ID","demo":true,"with_progress":true,"status_only":true}'
```

Subsequent checks use `status_only=true`, which the spec defines as not submitting a new search.
Stop on a non-empty `error` or a populated `output`. `with_progress=false` asks the upstream to wait
for completion; its practical duration and timeout behavior have not been verified. Do not blindly
resubmit after a timeout. Use the existing search ID to check its state.

The official example treats `output.items[].url` as a string while OpenAPI declares an object
containing `value`. The actual paid response shape is unverified; the registry preserves either
shape. A match score is a similarity signal, not a verified identity or a LinkedIn match.

## Billing, isolation and caching

Every row explicitly carries `platform_blocked` and `cache: forbidden`. The provider has no new
platform-key setting, routed adapter or async descriptor. Own-key calls remain unmetered by treg
and always reach the upstream, including repeated status checks. The published full-search price
is three credits at $0.10 each; per-request costs stay unknown because one search spans multiple
requests and the accounting point, retries and utility costs have not been measured.

Shared-key operation needs a separate design: current async descriptors only poll GET utilities
using path/query parameters, whereas FaceCheck uses a POST JSON `id_search`. Uploads can also append
to an existing search via multipart `id_search`. All reads and mutations of shared-account search
objects would need org ownership checks, and one search must settle only once. The BYOK-only scope
avoids claiming those contracts exist; each team uses its own provider account.

## Verification

The native bogus-token response was checked live. The opt-in test below repeats it through the
actual `/connections/token` route using a throwaway test org and the isolated test database, and
asserts that the rejected token creates neither a connection nor a tool:

```bash
TREG_FACECHECK_LIVE_PROBE=1 uv run --frozen python -m pytest -q \
  tests/test_facecheck.py::test_live_bogus_token_is_rejected_by_connection_route
```

Normal tests use synthetic responses to cover zero-balance connection acceptance, raw token
injection, byte-preserving multipart upload, submission and repeated status polling, query-based
image removal, platform refusal, cache policy and absence of treg money entries.

No endpoint carries `verified` or a captured success example yet: a valid FaceCheck token was not
available. The catalog verifier can check account info with `--id facecheck.account.usage`; it cannot
build the upload's multipart file parts. Upload/search/delete require an authorized test image and
fresh IDs from that upload, not the placeholders in `test_request`. Before claiming live support,
verify the positive connection and all four operations, inspect actual result URLs, compare credits
before/after a full search, and scrub any captured account or image data.
