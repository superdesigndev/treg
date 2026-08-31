---
title: Local proxy — catch a program's own outgoing calls (`treg <command>`)
status: shipped
sources:
  - src/treg/localproxy.py
  - examples/proxy-demo/server.js
related:
  - architecture/proxy-model.md
  - interface/shell.md
  - interface/cli.md
  - architecture/local-run.md
---

# The local proxy

Every other way into treg requires the caller to **know about treg** — `treg call`, `treg run`, the
`/call/` URL. The moment a program writes its own request to `api.stripe.com`, treg is invisible to it
and it has no key. The local proxy closes that gap: catch the call on the way out, and let the **server**
add the credential.

**Not `infra/upstream/relay.py`.** That is the SERVER relay (`relay()`), the thing that injects. This is the local
catcher that feeds it — two ends of one call. The module is named for `localrun.py`, its neighbour in
"code that runs on the member's machine". Plan + build log: `docs/LOCAL-PROXY-PLAN.md`.

## The whole thing in one picture

```
  a program that has never heard of treg
        │  HTTPS_PROXY=http://treg:<session-token>@127.0.0.1:<port>
        ▼
  local proxy  (src/treg/localproxy.py — this fragment)
        │
        ├─ host NOT registered  ──►  blind tunnel. Bytes copied, never decrypted,
        │                            no certificate ever signed for it.
        │
        └─ host IS registered   ──►  terminate TLS with a leaf we sign
                                     read the plaintext request
                                     re-address it, keeping method/path/query/body
                                          │
                                          ▼
                       treg server   POST /call/https://api.stripe.com/v1/charges
                                     + X-Treg-Token / X-Treg-Org / X-Treg-Client
                                          │  relay() injects the credential  ← proxy-model.md
                                          ▼
                                     api.stripe.com
                                          │
        ◄─────────────── response streamed back, unchanged ──────────────────┘
```

The proxy carries the member's **treg token** and nothing else. No vendor key ever reaches the machine.

Because the call lands on the ordinary `/call/` path, everything already built applies with no second
copy: the per-member tool list, project scope, deny rules, daily caps, the audit record, OAuth refresh.
That is why this module is small and there is no second policy engine —
see [proxy-model](proxy-model.md).

## What we took from oneCLI, and what we refused
Their design has two separable halves. We keep the capture mechanism (`HTTPS_PROXY` + own CA) and throw
away the injection half: their gateway holds `SECRET_ENCRYPTION_KEY` and decrypts secrets **on the
laptop** (`crypto.rs`, `connect.rs`). That is the exact situation treg exists to prevent. A future
"inject locally under `treg run`" mode is deliberately out of scope — deciding it later costs nothing,
getting it wrong now costs the product's promise.

## Three front doors, one engine

| Door | Scope of the change | Use it when |
|---|---|---|
| **`treg <command>`** (`cmd_with`) | that one process and its children | the normal case — `treg claude`, `treg node app.js` |
| **`treg shell start --proxy`** | one subshell | you want a session where the team's CLIs *and* raw calls both work |
| **`treg serve`** | any terminal that opts in with `eval "$(treg serve env)"` | you want it running across terminals |

**`treg <command>` is the headline.** treg is the PARENT of what it launches, so the environment reaches
that process only: `treg claude` uses team access, plain `claude` is untouched and uses the member's own
keys. Nothing is written to any config file, so there is nothing to undo. `main()` falls through to it
via `_looks_like_a_program()` when the first word (after `with`'s own flags) is **not** a treg subcommand
**and** exists on `PATH`. Both conditions matter: without the first, a stray `call` binary would shadow
`treg call`; without the second, `treg toool ls` would become an exec attempt instead of an ordinary
argparse error. It attaches to a running `serve` daemon and leaves it up, or starts a private proxy on
**port 0** — the operating system picks, so parallel sessions never collide — and stops it in a
`finally`. The child runs via `shell._run_subshell`, which ignores SIGINT/SIGQUIT so an interactive agent
owns the terminal, and the child's exit code becomes treg's.

**Deleted on purpose: the global hook.** A `treg serve hook` that wrote `BASH_ENV` into
`~/.claude/settings.json` (as oneCLI's Claude plugin does) was built and removed the same day. It
captured every session of that agent on the machine, forever, whether or not the member wanted treg that
day, and left no easy way to use a personal key instead. Per-launch gives the same "no eval ever" result
with none of the reach. **Do not re-add it.**

## Component map

| Symbol | Job |
|---|---|
| `handle_client` | one connection: read head, authenticate, then intercept / tunnel / forward |
| `authorized` | `Proxy-Authorization: Basic treg:<token>`, `compare_digest` |
| `_blind_tunnel` | CONNECT for a non-registered host — copy bytes, decrypt nothing |
| `_intercept` | CONNECT for a registered host — TLS terminate, then relay each request to treg |
| `_forward_plain` | absolute-form `http://` (we set `HTTP_PROXY` too, so http callers must not break) |
| `_treg_url` | `https://host/path` → `{base}/call/https://host/path` (the URL-passthrough shape) |
| `_forward_headers` | drop transport + caller `x-treg-*`; add our token/org/client |
| `_send_to_treg` | the outbound request, including the edge-WAF base64 retry |
| `_write_response` / `_body_chunks` | stream the answer back, re-deriving the framing |
| `_explain_treg_error` / `treg_error_message` | turn treg's own refusal into an instruction |
| `CertAuthority` / `ensure_ca` / `build_bundle` | the machine's CA, its leaves, the trust bundle |
| `proxy_env` | the variables that point a process at us and make it trust us |
| `fetch_hosts` / `refresh_hosts_forever` | the allow-list, from `GET /tools` |
| `serve` / `start` / `ProxyHandle` | bind on this loop / in a thread; `.env()`, `.stop()` |
| `write_state` / `running` / `pid_alive` | the `treg serve` daemon's `proxy.json` |
| `treg_client` | the proxy's own httpx client — **`trust_env=False`** |

## The request lifecycle, in order

1. `handle_client` reads the head with `readuntil(b"\r\n\r\n")`, capped at `_HEAD_LIMIT` (64 KiB → `431`).
2. `authorized()` checks the session token. No/wrong token → `407`, and the message never prints the
   token itself.
3. `CONNECT` → `split_hostport`. `_is_self` refuses our own address (a loop that would eat a connection
   per attempt).
4. `cfg.intercepts(host)` decides. It requires the host to be allow-listed **and** a CA **and** a treg
   token: terminating TLS and then having no way to finish the call is worse than not intercepting,
   because the caller breaks for a reason it cannot see.
5. Intercept: `200 Connection established`, then `StreamWriter.start_tls(ca.context_for(host))`.
6. Per request on that connection (keep-alive is honoured): parse head, read body
   (`_request_body` — chunked decoded, ≤1 MiB buffered so a WAF retry is possible, larger streams),
   build the treg URL and headers, `_send_to_treg`, then `_explain_treg_error` if the answer carries
   `X-Treg-Error`, else `_write_response`.
7. Response framing is **re-derived, not copied**: `Content-Length` when treg gave one, chunked when it
   did not, because our hop must be self-consistent even if the hop into treg was framed differently.
   `Accept-Encoding` is normalised to `identity` when the caller asked for nothing — the same rule
   `relay()` applies — or httpx would request gzip on its own and hand the caller compressed bytes it
   never asked for.

## Certificates (`CertAuthority`, `ensure_ca`, `build_bundle`)
Generated **per machine** on first use: ECDSA P-256, 2 years, self-signed, `BasicConstraints(ca=True)`.
The private key file is created 0600 **before any bytes go in** — writing first and chmod-ing after
leaves a window where a private key is world-readable. It regenerates when the files are unreadable or
inside the last 30 days (`_RENEW_WITHIN_DAYS`), so an expiry never surfaces mid-session as an
unexplained TLS error; `--renew-ca` forces it.

`build_bundle` writes **the system roots plus ours** (`certifi`, which ships with httpx, falling back to
OpenSSL's configured file). Appending is the point: `SSL_CERT_FILE` REPLACES the trust list, so a bundle
holding only our CA would leave the caller unable to verify the real internet.

Leaves are signed on demand with one reused key and cached per host as an `ssl.SSLContext`. Python's
`load_cert_chain` only reads from a file, so a leaf is written to a 0600 temp file that is deleted the
moment it is loaded.

**The system trust store is never touched.** Trust is scoped by environment variables, so only the
launched process tree trusts us — not the browser, not the operating system. oneCLI does the same; there
is no `add-trusted-cert` anywhere in their repo.

## The environment (`proxy_env`)
`HTTPS_PROXY`/`HTTP_PROXY` in **both letter cases** (curl reads lowercase), `NO_PROXY` covering loopback
and the registry host, the bundle under `NODE_EXTRA_CA_CERTS` / `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` /
`CURL_CA_BUNDLE` / `GIT_SSL_CAINFO` / `DENO_CERT` / `AWS_CA_BUNDLE`, and `NODE_USE_ENV_PROXY=1`.

**The Node caveat — measured, not assumed.** Node's built-in `fetch` ignores proxy variables entirely
until **Node 24**, where `NODE_USE_ENV_PROXY` turns it on; the flag does not exist before that
(`node --use-env-proxy` is a "bad option" on 23.11, tested on the dev machine). So on Node 23 or older a
plain `fetch()` walks straight past the proxy and the call goes out uncredentialed — which looks exactly
like the feature being broken. Anything that reads the environment itself is captured at any version:
curl, git, python-requests/httpx, axios, got, undici's `ProxyAgent`, Deno.
`examples/proxy-demo/server.js` speaks CONNECT by hand for this reason and its README says why real apps
do not have to.

## The allow-list (`fetch_hosts`, `refresh_hosts_forever`)
Hosts come from `GET /tools`, which is already filtered to what **this member** may use — so the
allow-list inherits the per-member tool list and project scope for free, and a host they cannot use is
never even decrypted. Two failure modes are handled deliberately: a failed fetch returns an empty set
(no answer must never mean "intercept everything"), and the refresher never applies an empty answer over
a working list, which would silently stop injecting while calls kept going out. Both front doors seed it
from the tool listing they already fetched, so turning the proxy on costs no extra request.

## Errors a caller can act on (`treg_error_message`, `_explain_treg_error`)
A raw 404 from treg reads as "the vendor has no such endpoint", and an agent "fixes" it by rewriting a
perfectly good URL. treg's own refusals are therefore replaced with a message naming treg and the next
action (register the tool / ask an admin / `treg login` / the cap is used up).

This fires **only** on `X-Treg-Error`, the header `api.py`'s `_mark_treg_own_errors` puts on treg's own
`/call/` refusals — otherwise a genuine vendor 404 would be rewritten too. Against an older registry that
does not send it, replies pass through untouched. treg being unreachable answers `502` naming treg and
saying the vendor call was **not** made, so a caller does not blame a healthy vendor and retry forever.

## Security invariants — each with the thing that would go wrong

| # | Invariant | What it prevents |
|---|---|---|
| 1 | CA private key per machine, 0600, never shipped | one shared CA would let any holder impersonate any site to every user |
| 2 | Never touch the system trust store | interception would reach the browser and the whole OS, not just what was launched |
| 3 | Allow-list only | reading the caller's own `api.anthropic.com` / `api.openai.com` traffic |
| 4 | Bind `127.0.0.1` (`LISTEN_HOST`) | anyone on the network spending the member's quota |
| 5 | Random session token (`mint_token`) + `_CALLER_MUST_NOT_SET` | another local process, or the caller's own headers, speaking as someone else |
| 6 | `trust_env=False` on `treg_client` | httpx reads `HTTPS_PROXY` from its own environment — the first call would loop back into us |
| 7 | Log host and status only (`_log`) | a body, a header or the token in a log file |

## The daemon's state (`treg serve` only)
A service must be findable by other terminals, so `write_state` records port, pid, token, registry and
captured hosts in `~/.treg/proxy/proxy.json` at mode **0600**, created before any bytes go in. That file
is the whole extra risk of `serve` versus the other two doors: it holds the session's proxy token — never
a vendor key — but it is on disk. `running()` treats a state file whose pid is gone as *not running* and
deletes it, or `status` would insist forever and `start` would refuse to replace a dead daemon.
`pid_alive` rejects a non-positive pid **before** calling `os.kill`, because `os.kill(0, …)` signals the
caller's whole process group — a truncated pid would read as alive and `stop` would signal the terminal.

The detached child is launched as `sys.executable -c "from treg.cli import main; main()" serve start
--foreground`, never the `treg` on `PATH`: a Homebrew copy one version behind would be started instead
and would not have the command at all. Output goes to `~/.treg/proxy/serve.log`, and the parent prints
that log's last line when the child fails to appear rather than telling the user to go read a file.

## Packaging
Certificate generation needs `cryptography`, which is compiled, so it sits in a **`[proxy]` extra** and
not the base install — `pip install tools-registry` must stay the light CLI (`httpx` + `questionary`).
`_require_cryptography` raises `ProxyDependencyError` with the exact install line. `[server]` already
includes it, so a self-hoster gets it free.

## What the tests actually prove
`tests/test_localproxy.py` (unit + integration, no network) and the proxy sections of
`tests/test_shell.py` (the front doors). The ones worth knowing: a real TLS handshake against a leaf we
signed **and** its rejection by a client trusting only the system roots (invariant 2); no leaf is ever
generated for a tunnelled host (invariant 3); the caller cannot spoof `x-treg-*`; `trust_env` is off;
a vendor's own error is never rewritten; and the end-to-end
`test_the_agent_calls_the_vendor_and_treg_injects_the_key`, which drives a plain `httpx.Client` through
the proxy against the **real** FastAPI app.

## Known limits — say these out loud
Certificate **pinning**: a client that accepts only its own certificate refuses ours. It fails alone, and
would need a per-host never-intercept list (not built). **Remote MCP servers** are not covered — a hosted
one calls from someone else's machine; a local stdio one inherits the environment and is captured. Not
HTTPS, not covered: SSH and database wire protocols; WebSockets are tunnelled, not intercepted. Every
intercepted call adds a hop to treg and back and **fails if treg is down** — the price of the key never
landing. Large uploads flow through us. Some vendor CLIs still need `treg run`: `gh` refuses before it
makes any network call, so there is nothing to intercept.
