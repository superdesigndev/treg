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

Durable objects created with a shared provider credential are assigned to one organization before
their upstream id is exposed. Every shared-key read, use, update, or delete checks that assignment
before contacting the provider; unknown and cross-organization ids return the same denial. If a
create succeeds upstream but ownership cannot be committed, treg attempts compensating deletion and
returns an error without exposing the unmanaged id. BYOK objects remain scoped by the customer's
provider account and bypass this platform ownership table.

- **The proxy never hands the key to the caller.** For an HTTP tool, the registry injects the credential
  server-side and makes the upstream call; the consumer's token only authorizes it.
- **Encryption at rest.** Stored secrets are Fernet-encrypted; the key is an environment variable, never
  in the repo or the database.
- **CLI pairing is shared and single-use.** `treg login` keeps its pairing and social-login return
  state in the database with ten-minute validity windows. Pairing codes and completed credentials
  are encrypted, approval attempts are serialized, and polling atomically consumes a result.
  Storage failures refuse the operation; there is no process-local fallback. Workers must share
  the database, encryption key and session-signing secret. A lost response after consumption requires
  starting a new login; credentials are never republished to make a retry succeed.
  Expired CLI rows are cleaned in bounded batches outside the pairing transaction. On rollout,
  an in-flight login from an old worker may need to restart once because its local state cannot
  be read by the new workers.
- **Tenant isolation.** Every secret, tool, and record is scoped to an org; access is gated by role and,
  per member, by an explicit tool allow-list. Pinned agent tags additionally scope call/run history,
  archived results and shared-provider async ownership; foreign and untagged records return 404.
  These scopes do not partition a team's BYOK account or authenticate public media URLs.
- **SSRF guard.** A tool's upstream host is re-resolved at call time and internal/metadata addresses are
  refused (defeats DNS-rebinding).
- **Local runs are sandboxed.** `treg run` on a member's machine executes the CLI as a locked-down
  `treg-run` user, with an egress allow-list and an optional filesystem jail, so a shared key can't be
  read, exfiltrated over the network, or written to a member-readable file.
- **Server runs are disabled.** `POST /run` unconditionally returns 503 before authentication,
  database access, secret decryption or subprocess execution. No role, CLI profile or executable
  allow-list can enable it.
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
- **Designated sign-in codes are a password, stored only as a hash.** `TREG_FIXED_LOGIN_CODES`
  (unset by default) maps an email with no inbox, such as an app directory reviewer's demo account,
  to the SHA-256 of a fixed code. The email-code door then sends nothing and accepts only that code,
  under the same five-guess and start limits as an emailed code. Use a long random code, give the
  account a dedicated team with sample data only, and rotate by changing the hash.

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
access credential. Upstream answers are retained under the existing archive policy. A provider
that echoes treg's PLATFORM credential in a 2xx body is not sanitized by these guards (judge that
provider's `cache` with it in mind); an answer that echoes a team's OWN credential is refused by
the recorder (`_echoes_own_credential`). Response headers are kept only from a fixed allowlist
(charge headers and the failure-evidence set, never auth or cookies), and a header value quoting
any injected credential is dropped (`_archived_headers`). An own-credential answer is stored under a cache key that
folds in the org (or the connection, on an `own_account` endpoint), so another team never computes
the hash that would find it; only an endpoint-level `cache.sharing: public` declaration puts such an
answer on the public key.

Object I/O never holds the calling task's database connection. Startup refuses incomplete R2
configuration when archive mode is enabled and any storage switch selects R2. The manual smoke
script accepts only `treg-archive-dev`, refuses CI, and skips absent credentials. It creates no
cloud resources apart from one small test object. Production configuration belongs in the private ops project.

## Picture storage (avatars and /showcase pictures)

Pictures use their own credentials and buckets (`TREG_MEDIA_*`), never the archive's: a private
STAGING bucket every upload lands in, and a PUBLIC bucket where treg publishes (an avatar once its
owner sets it, a /showcase picture once staff approve it). treg writes and deletes only under `img/`
in the public bucket, which may hold other files (page videos); never give that bucket a lifecycle
rule that is not limited to its own prefix. Scope one token to these two buckets with object
read/write access. The endpoint must be https; only `http://localhost`
(a local MinIO) is accepted over plain HTTP.

Every upload is checked before it is decoded (size cap, leading bytes must be PNG, JPEG or WebP; SVG
and GIF are refused), decoded under a pixel cap, and re-drawn from its pixels into fixed sizes, so
metadata and anything appended to the original are discarded. Files are named by the SHA-256 of the
re-drawn picture: clients never supply a path, bucket, host or URL, and a name never changes
meaning. Staged pictures are served only to signed-in people (`private, no-store`), the public
bucket through a media domain or treg's `/media-files/`; bytes from treg's origin carry `nosniff`
and `sandbox`. Picture storage I/O never holds a database connection. Give the staging bucket a
lifecycle rule that expires objects after about 30 days; nothing else cleans it. Public pictures no
row names any more are deleted by `treg-worker media sweep`.

## Known limitations (by design, documented on purpose)

**The server CLI runner lacks filesystem/process isolation.** A scrubbed child environment and
resource limits do not isolate it from the server's credentials. Server-side CLI execution remains
disabled until an independently reviewed isolation boundary is in place.

## Supported versions

This project is pre-1.0; security fixes land on `main`. Pin a commit if you need stability.
