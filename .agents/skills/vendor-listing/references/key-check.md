# Key-check fields

How treg checks a key: when a team connects one, treg calls the registry entry's probe with it and
expects a valid key to pass and an invalid one to fail. This table says which `OAuthProvider` fields
fit which API behavior. The fields are documented on the dataclass in `src/treg/oauth_providers.py`.

## Which field for which behavior

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
| 200 on an invalid key; a truthy field means valid | `token_verify_field` | Slack `ok`, Apollo `is_logged_in` |
| 200 on an invalid key; a field equals a value when valid | `token_ok_field` + `token_ok_value` | Majestic `Code=="OK"` |
| 200 on an invalid key; an error object means invalid | `token_reject_field` | Serpstat `error` |
| 200 on an invalid key with an `ERROR ...` text body | automatic (text-error guard) | Semrush |
| Valid key 400s on an empty body, invalid 401s | `probe_reject_statuses=(401, 403)` | Coresignal |
| The vendor's own "test auth" endpoint answers 200 to an invalid key | probe a data endpoint instead | Tiingo |
| Bad key 302-redirects to an HTML login page | `probe_reject_statuses=(302, 401, 403)` plus `token_verify_field` on a field the redirect lacks | Findymail |
| Two per-user header credentials; a free probe needs only the key | `extra_credential_label`/`extra_credential_header`, `extra_credential_setting` empty (user adds the second via `POST /connections/{id}/extra-credential`) | Tomba |
| Two credentials, not in headers | `extra_credential_location` + `extra_credential_param` | Adyntel key + account email |
| Two JSON-body credentials and the key cannot be verified alone | both `location="json"` plus `probe_deferred_statuses` on the "incomplete pair" status; connect stores `health_status="unknown"` until the second arrives | Adyntel `POST /facebook` 422 |
| Two credentials accepted as HTTP Basic `a:b` | one pasted `key:token`, Basic + base64; no second slot | PredictLeads |
| A demo host answers 200 to anything | pin the host that rejects | CoinGecko |
| An invalid key and a bad request share a status, told apart only by an error code | no field reads a code's value yet: probe with the cheapest paid call and state its price in `setup_note` | Brave Search |
| Accepts any key on every endpoint | drop the provider | Alpha Vantage, ScrapeCreators |
| Ongoing tool health check | `probe_path` (cheap GET on `base_url`) | most |

The connect probe tries to parse every body as JSON whatever its content type (some vendors label
JSON `text/plain`); a non-JSON body leaves the payload empty and falls through to the text-error
guard. By default any status >= 400 rejects.

## Traps

- **200 for an invalid key.** The status lies; read a body field.
- **Trailing-slash redirects.** Redirects are not followed with the key attached, so a slash-less
  path that 307s or 301s never reaches the clean 401. Put the slash in `probe_path`, and in
  every catalog `path` (Django and similar frameworks drop the body on the redirect).
- **Key in the URL path** (`/v3/{key}/...`): injectors only do header, query and JSON; skip it.
