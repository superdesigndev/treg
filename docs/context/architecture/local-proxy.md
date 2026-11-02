---
title: Local proxy - catch a program's own outgoing calls (`treg <command>`)
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

A program that writes its own request to `api.stripe.com` does not know treg exists and has no key.
The local proxy (`localproxy.py`, the member-side catcher; `infra/upstream/relay.py` is the server
relay it feeds) catches the call on the way out via `HTTPS_PROXY` and re-addresses it to
`POST {base}/call/https://host/path`, keeping method, path, query and body. The server injects the
credential, so every `/call/` policy (tool list, project scope, deny rules, caps, audit, OAuth
refresh) applies with no second policy engine ([proxy-model](proxy-model.md)). Unregistered hosts
are blind-tunnelled: bytes copied, never decrypted, no certificate signed.

**The proxy carries the member's treg token and nothing else.** Decrypting or injecting vendor keys
on the laptop is out of scope; it is the situation treg exists to prevent.

## Front doors

- **`treg <command>`** (`cmd_with`): the normal case. treg is the parent, so only that process tree is
  captured (`treg claude` uses team access, plain `claude` does not) and nothing is written to any
  config. `main()` falls through to it only when the first word is **not** a treg subcommand **and**
  is on `PATH`: otherwise a stray `call` binary would shadow `treg call`, or a typo would become an
  exec attempt. It reuses a running `serve` daemon or starts a private proxy on port 0.
- **`treg shell start --proxy`**: one subshell ([shell](../interface/shell.md)).
- **`treg serve`**: a daemon other terminals opt into with `eval "$(treg serve env)"`.

**Do not add a global hook** (for example writing `BASH_ENV` into an agent's settings file). It
captures every session of that agent forever and leaves no easy way to use a personal key.

## Rules a change must keep

| Rule | What it prevents |
|---|---|
| CA generated per machine, private key 0600 from creation, never shipped | one shared CA would impersonate any site to every user; chmod-after-write leaves a readable window |
| Never touch the system trust store; trust is scoped by environment variables | interception reaching the browser and the OS |
| Intercept only allow-listed hosts, and only with a CA and a treg token | reading the caller's own LLM traffic; terminating TLS with no way to finish the call |
| Bind `127.0.0.1` | anyone on the network spending the member's quota |
| Random session token; caller `x-treg-*` headers are dropped | another process, or the caller's headers, speaking as someone else |
| `trust_env=False` on the proxy's own httpx client | httpx reads `HTTPS_PROXY` and the first call loops back into us |
| Log host and status only | a body, header or token in a log file |
| A failed host fetch yields an empty set, and the refresher never applies an empty answer over a working list | "intercept everything", or silently stopping injection |
| Refuse CONNECT to our own address | a loop eating a connection per attempt |

The allow-list comes from `GET /tools`, already filtered to what this member may use.

Response framing is re-derived for our hop, never copied. `Accept-Encoding` becomes `identity` when
the caller sent none (as in `relay()`), or httpx would request gzip and hand the caller compressed
bytes it never asked for.

## Certificates and trust

The CA renews itself when unreadable or inside `_RENEW_WITHIN_DAYS` of expiry, so expiry never shows up
mid-session as an unexplained TLS error. The trust bundle is **the system roots plus ours**:
`SSL_CERT_FILE` replaces the trust list, so a bundle with only our CA breaks verification of the real
internet. Python's `load_cert_chain` reads only from a file, so each leaf goes through a 0600 temp file
deleted once loaded.

`proxy_env` sets proxy variables in both letter cases (curl reads lowercase) and the bundle under every
runtime's CA variable. **Node's built-in `fetch` ignores proxy variables before Node 24**, where
`NODE_USE_ENV_PROXY=1` turns them on; on older Node a plain `fetch()` goes out uncredentialed, which
looks like the feature being broken. Tools that read the environment themselves are captured at any
version. `examples/proxy-demo/server.js` speaks CONNECT by hand for this reason.

## Errors

treg's own refusals carry `X-Treg-Error`; only those are replaced with a message naming treg and the
next action, because a raw 404 reads as "no such vendor endpoint" and an agent then rewrites a good URL.
A vendor's own error is never rewritten. treg unreachable answers `502` saying the vendor call was not
made.

## The `serve` daemon

`~/.treg/proxy/proxy.json` (0600) holds port, pid and the session proxy token, never a vendor key. A
state file whose pid is gone counts as not running and is deleted. `pid_alive` rejects a non-positive
pid before `os.kill`, since `os.kill(0, ...)` signals the whole process group. The detached child runs
via `sys.executable`, never the `treg` on `PATH`, which may be an older copy without the command.

Certificate generation needs `cryptography`, so it lives in the `[proxy]` extra (included in
`[server]`); the base install stays light.

## Known limits

Certificate-pinning clients refuse our leaf. Hosted remote MCP servers are not covered (a local stdio
one is). SSH and database protocols are not covered; WebSockets are tunnelled, not intercepted. Every
intercepted call fails if treg is down. Some CLIs (`gh`) refuse before any network call and need
`treg run`.
