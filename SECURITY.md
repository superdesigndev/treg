# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities **privately** — do not open a public issue.

Use GitHub's **[Private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)**
(the "Report a vulnerability" button on the Security tab), or email **<jason@superdesign.dev>**.

We aim to acknowledge a report within a few business days, agree on a disclosure timeline, and credit
reporters who wish to be named.

## The security model (what protects your keys)

tools-registry is built so that **reading the source does not help an attacker** — nothing load-bearing
is hidden in the code. Secrets live in the database (encrypted with a Fernet key held only in the server
environment) and enforcement happens server-side and in the operating system.

- **The proxy never hands the key to the caller.** For an HTTP tool, the registry injects the credential
  server-side and makes the upstream call; the consumer's token only authorizes it.
- **Encryption at rest.** Stored secrets are Fernet-encrypted; the key is an environment variable, never
  in the repo or the database.
- **Tenant isolation.** Every secret, tool, and record is scoped to an org; access is gated by role and,
  per member, by an explicit tool allow-list.
- **SSRF guard.** A tool's upstream host is re-resolved at call time and internal/metadata addresses are
  refused (defeats DNS-rebinding).
- **Local runs are sandboxed.** `treg run` on a member's machine executes the CLI as a locked-down
  `treg-run` user, with an egress allow-list and an optional filesystem jail, so a shared key can't be
  read, exfiltrated over the network, or written to a member-readable file.
- **Server runs are resource-limited.** `treg run --server` executes each CLI with a scrubbed environment
  (treg's own secrets removed), a per-run throwaway home, an allow-list of runnable commands, output
  redaction, and POSIX resource limits (CPU, file size, no core dumps).
- **Signup credit belongs to a verified account, once.** Only an email OTP, provider-verified
  Google/GitHub email, or inbox-only invitation link marks an identity verified. Legacy `POST /users`
  and admin-visible invitation codes do not. `claim_signup_promo` atomically consumes the user's
  eligibility in the same transaction as the money grant; team deletion never restores it. Accounts
  predating revision `0033` receive no new automatic grant; their existing balances are unchanged.
  New identities can still be farmed through verified inboxes, so this is not a one-human guarantee.
  `TREG_PROMO_GRANT_MICRO=0` stops new automatic grants after deployment. The domain blocklist
  (`TREG_BLOCKED_EMAIL_DOMAINS`, unset means no blocks) remains a configurable speed bump at every
  sign-in/sign-up door and both team-creating endpoints; it fails open on classifier errors.
  Suspend abusive users and teams separately, retaining their records for investigation.

## Archive object storage credentials

R2 credentials are server environment settings (`TREG_ARCHIVE_OBJECT_STORE_ACCESS_KEY_ID` and
`TREG_ARCHIVE_OBJECT_STORE_SECRET_ACCESS_KEY`), never catalog credentials or caller input. Use a token scoped
to the private archive bucket with object read/write access; bucket administration is unnecessary.
The configured endpoint must be an HTTPS Cloudflare R2 account endpoint. Object names are always
SHA-256 hashes computed by treg; clients cannot supply an object path, bucket, host or URL.
The obstore compiled wheel exists only in the server extra and is imported lazily by the
server factory, initialized by bootstrap. Its shared Rust HTTP client uses configured connect and
request timeouts and zero SDK retries; archive_bodies owns retry policy. SDK exceptions, signed
requests, credentials and response bodies are not included in archive telemetry.

obstore S3Store uses `checksum_algorithm=SHA256` and single-part PUT. R2 validates the transmitted
SHA-256 checksum before PUT succeeds; only then can a DB pointer be committed. PUT performs no
follow-up HEAD. HEAD makes one request for object size. No custom digest metadata is written or
required. GET enforces the size limit and checks downloaded bytes against the object's SHA-256
hash. The bucket is not public and no object URL is exposed: call-history authorization checks
the team's CallRecord before resolving its archive reference. A hash is an identity, not an
access credential. Upstream answers are retained under the existing archive policy; these guards
do not sanitize an upstream provider that echoes a credential in its response.

Object I/O never holds the calling task's database connection. Startup refuses incomplete R2
configuration when archive mode is enabled and any storage switch selects R2. The manual smoke
script accepts only `treg-archive-dev`, refuses CI, and skips absent credentials. It creates no
cloud resources apart from one small test object. Production configuration belongs in the private ops project.

## Known limitations (by design, documented on purpose)

Honesty is part of the model. Two items are deliberately deferred:

1. **Server runs do not yet have filesystem/network isolation.** The resource limits above cap denial of
   service, but a full jail (a locked-down user + egress allow-list, like the local sandbox) requires a
   container deployment and is planned. On the reference deployment there is no on-disk secret file to
   read (the encryption key is an environment variable), and only allow-listed CLIs may run.
2. **The CLI-login handshake is in-process.** The short-lived pairing state for `treg login` lives in the
   server process (it self-heals on retry and carries no rate-limit value). Running more than one server
   instance requires sticky routing for that one flow, or moving it to shared storage.

## Supported versions

This project is pre-1.0; security fixes land on `main`. Pin a commit if you need stability.
