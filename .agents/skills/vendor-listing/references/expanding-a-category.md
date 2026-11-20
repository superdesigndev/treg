# Key-provider verification toolbox and curation

Reference for Steps 1, 2 and 5 of the vendor-listing skill: which `OAuthProvider` fields make the
connect probe reject a bad key, the traps real providers set, and how to choose providers when a
category needs more. Mechanics live in `src/treg/oauth_providers.py` (field docs on the dataclass)
and `application/connect.py` (`connect_with_token`, `POST /connections/token`). OAuth providers
are a different path: see the `add-oauth-provider` skill.

## Provider kinds

- **API key** (`auth_kind="key"`): the user pastes a key; self-serve; research, implement and
  live-test in one session. Almost all catalog growth happens here.
- **OAuth** (`auth_kind="oauth"`): treg holds its own registered app. Without its client
  credentials it lists as `configured:false`, so it is safe to add unconfigured.
- **Token** (Slack): a bring-your-own bot token on the pasted-secret path (`uses_pasted_secret`).

## Verify toolbox: which field for which bad-key behavior

| Provider behavior | Field(s) | Example |
|---|---|---|
| Key in a header | `token_header` + `token_format` (`Bearer {secret}`, `{secret}`, `Token {secret}`) | TikHub, Apollo |
| Key in the query string | `token_location="query"` + `token_param` | Semrush, SpyFu |
| Key in the JSON body | `token_location="json"` + `token_param` | Adyntel `api_key` |
| HTTP Basic from `login:password` | `token_format="Basic {secret}"` + `token_encode="base64"` | DataForSEO, Moz |
| HTTP Basic with a raw token | `token_format="Basic {secret}"`, no `token_encode` | The Companies API |
| Cheapest check on another host | `probe_url` (absolute) | Semrush, Diffbot |
| Probe needs a POST body | `probe_method="POST"` + `probe_json` | Serpstat, Coresignal |
| No free route; the cheapest paid call is the probe | `probe_method`/`probe_json` on the cheapest call; state the price in `setup_note` | Exa `/contents` |
| 200 on a bad key; a truthy field means valid | `token_verify_field` | Slack `ok`, Apollo `is_logged_in` |
| 200 on a bad key; a field equals a value when valid | `token_ok_field` + `token_ok_value` | Majestic `Code=="OK"` |
| 200 on a bad key; an error object means invalid | `token_reject_field` | Serpstat `error` |
| 200 on a bad key with an `ERROR ...` text body | automatic (text-error guard) | Semrush |
| Valid key 400s on an empty body, invalid 401s | `probe_reject_statuses=(401, 403)` | Coresignal |
| The vendor's own "test auth" route answers 200 to a bad key | probe a data endpoint instead | Tiingo |
| Bad key 302-redirects to an HTML login page | `probe_reject_statuses=(302, 401, 403)` plus `token_verify_field` on a field the redirect lacks | Findymail |
| Two per-user header credentials; a free probe needs only the key | `extra_credential_label`/`extra_credential_header`, `extra_credential_setting` empty (user adds the second via `POST /connections/{id}/extra-credential`) | Tomba |
| Two credentials, not in headers | `extra_credential_location` + `extra_credential_param` | Adyntel key + account email |
| Two JSON-body credentials and the key cannot be verified alone | both `location="json"` plus `probe_deferred_statuses` on the "incomplete pair" status; connect stores `health_status="unknown"` until the second arrives | Adyntel `POST /facebook` 422 |
| Two credentials accepted as HTTP Basic `a:b` | one pasted `key:token`, Basic + base64; no second slot | PredictLeads |
| A demo host answers 200 to anything | pin the host that rejects | CoinGecko |
| Accepts any key on every endpoint | drop the provider | Alpha Vantage, ScrapeCreators |
| Ongoing tool health check | `probe_path` (cheap GET on `base_url`) | most |

The connect probe tries to parse every body as JSON whatever its content type (some vendors label
JSON `text/plain`); a non-JSON body leaves the payload empty and falls through to the text-error
guard. By default any status >= 400 rejects.

## Traps

- **200 for a bad key.** The status lies; read a body field.
- **Trailing-slash redirects.** Redirects are not followed with the key attached, so a slash-less
  path that 307s or 301s never reaches the clean 401. Put the slash in `probe_path`, and in
  every catalog `path` (Django and similar frameworks drop the body on the redirect).
- **Key in the URL path** (`/v3/{key}/...`): injectors only do header, query and JSON; skip it.
- **A gateway 504 that still bills.** A slow backend behind a proxy cutoff can return a 504 page
  and charge anyway. Record it in the catalog file's header note instead of stamping the route
  unverified, and rerun `catalog_verify.py` for timed-out routes rather than widening its timeout.
- **A cross-platform provider** (one endpoint covering many networks via a body field) needs its
  own platform slug. Platforms cannot be proposed from a provider file: add the slug to
  `capabilities.yaml` and propose only the capabilities.

## Selection heuristics

- **Self-serve API key first.** Anything sales-gated breaks the fast path.
- **Distinct value.** Fill a gap the category lacks; skip near-duplicates.
- **Reliability and legal.** Reject with a recorded reason: legal or shutdown risk, deprecated or
  absorbed products, UI-only tools with no API, enterprise-sales-gated APIs.
- **When the self-serve field is thin, say so.** Fill with an adjacent clean pick or an
  unconfigured OAuth platform rather than a bad fit.
