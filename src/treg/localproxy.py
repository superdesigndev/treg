"""treg local proxy — catch the agent's OWN outgoing calls, inject the credential on the SERVER.

Today treg only works when the agent is told to use it (`treg call …`, or the `/call/` URL). The
moment an agent writes its own script that talks to `api.stripe.com` directly, treg is invisible and
the script has no key. This module closes that gap: the agent makes an ordinary HTTPS call, we catch
it on the way out (`HTTPS_PROXY` + a certificate authority generated on this machine), and the
**registry server** adds the credential. No vendor key ever lands here. Spec:
docs/LOCAL-PROXY-PLAN.md.

**Not `infra/upstream/relay.py`.** That one is the SERVER relay (`relay()`), the thing that injects.
This one is the local catcher that feeds it. They are two ends of the same call and the names are close on purpose —
the module is named for `localrun.py`, its neighbour in "code that runs on the member's machine".

Shipped here: **P0** (listen, authenticate, blind-tunnel every byte), **P1** (the certificate
authority, the trust bundle, leaf certificates) and **P2** (intercept an allow-listed host and
re-address it to treg's `/call/` passthrough). Still to come: P3 fetches the allow-list from
`GET /tools` and refreshes it — until then `ProxyConfig.hosts` is whatever the caller passes, and an
empty set means the proxy decrypts nothing at all.

Because an intercepted call lands on the ordinary `/call/` path, everything already built keeps
working with no second copy: the per-member tool list, project scope, deny rules, daily caps, the
audit record, OAuth refresh. That is the reason this module is small.

The rules this file exists to keep (plan §"The non-negotiables"):

1. The CA private key is generated **per machine**, mode 0600, never shipped or shared. One CA handed
   to every user would let any holder impersonate any site to all of them.
2. **Never touch the system trust store.** Trust is scoped by environment variables
   (`NODE_EXTRA_CA_CERTS`, `SSL_CERT_FILE`, …), so only the agent's process tree trusts us — not the
   browser, not the operating system.
3. **Allow-list only** (P2): intercept a host only when it is a registered tool; everything else stays
   a blind tunnel we cannot read. This is also what stops us reading the agent's own
   `api.anthropic.com` traffic.
4. Bind `127.0.0.1` only, never `0.0.0.0`.
5. A random proxy token per session, carried in the proxy URL, so another local process cannot quietly
   spend the member's quota through us.
6. Never log bodies, headers or the token — host and status only.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import os
import secrets
import socket
import ssl
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

# The proxy listens here. 18791 sits next to the dev server's 18790 so the pair reads as one family.
DEFAULT_PORT = 18791
LISTEN_HOST = "127.0.0.1"  # never 0.0.0.0 — this proxy speaks for the member's quota

# Certificate lifetimes. Two years for the CA: long enough that nobody meets an expiry in normal use,
# short enough that a key stolen off a laptop stops being useful. `ensure_ca(renew=True)` regenerates
# it early. Leaves are 30 days and live only in memory, so their lifetime barely matters.
CA_DAYS = 730
LEAF_DAYS = 30
_RENEW_WITHIN_DAYS = 30  # a CA this close to expiry is regenerated on the next start

# How often the allow-list is re-read from `GET /tools`, so a tool registered mid-session starts
# working without restarting the shell.
HOSTS_REFRESH_SECONDS = 300

_HEAD_LIMIT = 64 * 1024   # cap on the request head we buffer before deciding what to do with it
_CHUNK = 64 * 1024
_CONNECT_TIMEOUT = 30.0

_CA_KEY_NAME = "ca-key.pem"
_CA_CERT_NAME = "ca-cert.pem"
_BUNDLE_NAME = "ca-bundle.pem"


class ProxyDependencyError(RuntimeError):
    """`cryptography` is missing. It is a compiled package, so it stays OUT of the base install —
    `pip install tools-registry` must remain the light CLI (httpx + questionary). The local proxy asks
    for it explicitly via the `[proxy]` extra."""


def _require_cryptography():
    """Import the certificate library on demand, or explain exactly how to get it."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised by hand, not in CI
        raise ProxyDependencyError(
            "the local proxy needs the certificate library, which is not part of the light CLI.\n"
            "  Run this from a terminal and treg offers to install it the right way for your install,\n"
            '  or add it by hand:  pip install "tools-registry[proxy]"'
        ) from exc
    return x509, hashes, serialization, ec, NameOID


# ---- where state lives --------------------------------------------------------------------
def proxy_dir() -> Path:
    """`~/.treg/proxy/` — the CA and the trust bundle. Follows `TREG_CONFIG` when set, so tests and
    agents that redirect the CLI's config also redirect the CA instead of writing into the real one."""
    cfg = os.environ.get("TREG_CONFIG")
    base = Path(cfg).expanduser().parent if cfg else Path.home() / ".treg"
    return base / "proxy"


# ---- P1 · the certificate authority ---------------------------------------------------------
@dataclass
class CertAuthority:
    """The machine's own certificate authority plus the leaf certificates signed from it.

    One ECDSA key is reused for every leaf: it never leaves this process, and generating a fresh key
    per host would only add latency to the first call to each host. Leaves are cached in memory, so
    they vanish with the session — nothing to clean up and nothing to leak."""

    dir: Path
    key_path: Path
    cert_path: Path
    bundle_path: Path
    cert: Any                         # cryptography x509.Certificate
    key: Any                          # the CA private key
    _leaf_key: Any = None             # one leaf key, reused for every host
    _contexts: dict = field(default_factory=dict)   # host → ssl.SSLContext
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def expires(self) -> datetime:
        return self.cert.not_valid_after_utc

    def leaf_pem(self, host: str) -> tuple[bytes, bytes]:
        """Sign a server certificate for `host`, returning `(cert_pem, key_pem)`. The name goes in the
        subject-alternative-name extension — modern clients ignore the common name entirely — as an IP
        entry when the host is a literal address, otherwise as a DNS entry."""
        x509, hashes, serialization, ec, NameOID = _require_cryptography()
        if self._leaf_key is None:
            self._leaf_key = ec.generate_private_key(ec.SECP256R1())
        try:
            san = x509.IPAddress(ipaddress.ip_address(host))
        except ValueError:
            san = x509.DNSName(host)
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host[:64])]))
            .issuer_name(self.cert.subject)
            .public_key(self._leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))   # tolerate a skewed clock
            .not_valid_after(now + timedelta(days=LEAF_DAYS))
            .add_extension(x509.SubjectAlternativeName([san]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, key_encipherment=True, content_commitment=False,
                    data_encipherment=False, key_agreement=False, key_cert_sign=False,
                    crl_sign=False, encipher_only=False, decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self.cert.public_key()), critical=False
            )
            .sign(self.key, hashes.SHA256())
        )
        key_pem = self._leaf_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return cert.public_bytes(serialization.Encoding.PEM), key_pem

    def context_for(self, host: str) -> ssl.SSLContext:
        """A server-side TLS context presenting a leaf for `host`, cached per host (a handshake per
        call would otherwise pay for a signature every time).

        `load_cert_chain` only reads from disk — Python has no in-memory equivalent — so the leaf is
        written to a private 0600 file that is deleted the moment it is loaded. The key exists on disk
        for microseconds and is this machine's own throwaway leaf key, never a vendor credential."""
        with self._lock:
            ctx = self._contexts.get(host)
            if ctx is not None:
                return ctx
            cert_pem, key_pem = self.leaf_pem(host)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            fd, path = tempfile.mkstemp(prefix="treg-leaf-", suffix=".pem", dir=self.dir)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(cert_pem)
                    fh.write(key_pem)
                ctx.load_cert_chain(path)
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            self._contexts[host] = ctx
            return ctx


def _system_ca_pem() -> bytes:
    """The trust anchors the machine already uses. `certifi` ships with httpx (our one hard runtime
    dependency) and is what httpx itself verifies against, so preferring it keeps the agent's view of
    the internet identical to treg's. Falls back to OpenSSL's configured file."""
    try:
        import certifi

        return Path(certifi.where()).read_bytes()
    except (ModuleNotFoundError, OSError):
        cafile = ssl.get_default_verify_paths().cafile
        if cafile and os.path.exists(cafile):
            try:
                return Path(cafile).read_bytes()
            except OSError:
                pass
        return b""


def build_bundle(ca_cert_pem: bytes, out_path: Path) -> Path:
    """Write `ca-bundle.pem` = **the system roots plus ours**, in that order.

    Appending is the whole point. `SSL_CERT_FILE` and friends REPLACE the trust list rather than adding
    to it, so a bundle holding only our CA would leave the agent unable to verify the real internet —
    every site except the ones we intercept would fail."""
    system = _system_ca_pem()
    body = bytearray(system)
    if body and not body.endswith(b"\n"):
        body += b"\n"
    body += b"# treg local proxy CA (this machine only)\n"
    body += ca_cert_pem
    out_path.write_bytes(bytes(body))
    os.chmod(out_path, 0o644)
    return out_path


def ensure_ca(directory: Path | None = None, *, days: int = CA_DAYS, renew: bool = False) -> CertAuthority:
    """Load this machine's certificate authority, generating it on first use.

    Regenerates when `renew=True`, when the files are missing or unreadable, or when the certificate is
    inside its last 30 days — an expiry that surfaces as an unexplained TLS error mid-session is a bad
    way to learn about it. The private key is written 0600; the certificate and the bundle are 0644
    because other processes in the agent's tree must read them to trust us."""
    x509, hashes, serialization, ec, NameOID = _require_cryptography()
    d = Path(directory) if directory else proxy_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    key_path, cert_path, bundle_path = d / _CA_KEY_NAME, d / _CA_CERT_NAME, d / _BUNDLE_NAME

    if not renew and key_path.exists() and cert_path.exists():
        try:
            key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            fresh = cert.not_valid_after_utc - datetime.now(timezone.utc) > timedelta(days=_RENEW_WITHIN_DAYS)
            if fresh:
                if not bundle_path.exists():
                    build_bundle(cert.public_bytes(serialization.Encoding.PEM), bundle_path)
                return CertAuthority(d, key_path, cert_path, bundle_path, cert, key)
        except (ValueError, TypeError, OSError):
            pass  # unreadable or corrupt → generate a new one rather than dying on start

    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, f"treg local proxy CA ({_machine_label()})"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "treg (local, not a public authority)"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)                       # self-signed: it is its own root
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    # Create the key file 0600 BEFORE any bytes go in — writing first and chmod-ing after leaves a
    # window in which a private key sits world-readable.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ))
    os.chmod(key_path, 0o600)  # an existing file keeps its old mode through O_CREAT
    cert_path.write_bytes(cert_pem)
    os.chmod(cert_path, 0o644)
    build_bundle(cert_pem, bundle_path)
    return CertAuthority(d, key_path, cert_path, bundle_path, cert, key)


def _machine_label() -> str:
    """A readable hint of where this CA came from, for anyone who inspects the certificate. Kept to
    plain characters — it goes into a certificate subject."""
    try:
        node = socket.gethostname() or "local"
    except OSError:
        node = "local"
    safe = "".join(ch for ch in node if ch.isalnum() or ch in "-._")
    return safe[:40] or "local"


# ---- the environment we hand to the agent ---------------------------------------------------
def proxy_env(port: int, token: str, bundle_path: Path | str, treg_host: str | None = None) -> dict[str, str]:
    """The variables that point a process at this proxy and make it trust our certificates.

    Two of these are the difference between "works" and "mysteriously does nothing":

    - **`NODE_USE_ENV_PROXY`** — since Node 18 the built-in `fetch` silently IGNORES `HTTPS_PROXY`.
      Without this flag every Node agent walks straight past the proxy and the whole feature looks
      intermittently broken.
    - **`NO_PROXY`** — loopback and the registry itself must never come back through us. The proxy's
      own call to treg would otherwise arrive at the proxy.

    Both letter cases are set: curl reads lowercase, most other tools read uppercase."""
    token_url = quote(token, safe="")
    url = f"http://treg:{token_url}@{LISTEN_HOST}:{port}"
    bundle = str(bundle_path)
    skip = ["localhost", "127.0.0.1", "::1"]
    if treg_host:
        skip.append(treg_host)
    no_proxy = ",".join(skip)
    env = {
        "HTTPS_PROXY": url,
        "HTTP_PROXY": url,
        "https_proxy": url,
        "http_proxy": url,
        "NO_PROXY": no_proxy,
        "no_proxy": no_proxy,
        "NODE_USE_ENV_PROXY": "1",
        "NODE_EXTRA_CA_CERTS": bundle,
        "SSL_CERT_FILE": bundle,
        "REQUESTS_CA_BUNDLE": bundle,
        "CURL_CA_BUNDLE": bundle,
        "GIT_SSL_CAINFO": bundle,
        "DENO_CERT": bundle,
        "AWS_CA_BUNDLE": bundle,
    }
    return env


# ---- the daemon's state file (`treg serve`) --------------------------------------------------
# `treg shell --proxy` needs none of this: the proxy lives and dies with one subshell, and its token
# is passed straight into that shell's environment. A DAEMON is different — another terminal has to
# find it — so the port and token go to disk. That file is the whole extra risk of `treg serve`, which
# is why it is 0600 and holds nothing but this session's own proxy token (never a vendor key).
_STATE_NAME = "proxy.json"


def state_path() -> Path:
    return proxy_dir() / _STATE_NAME


def write_state(port: int, token: str, pid: int, base_url: str, org: str, hosts: list[str]) -> Path:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"port": port, "token": token, "pid": pid, "base_url": base_url,
                          "org": org, "hosts": sorted(hosts)}, indent=2)
    # 0600 from the moment the file exists — it carries the token that spends the member's quota.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(payload)
    os.chmod(path, 0o600)
    return path


def read_state() -> dict | None:
    try:
        return json.loads(state_path().read_text())
    except (OSError, ValueError):
        return None


def clear_state() -> None:
    try:
        state_path().unlink()
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    """Signal 0 asks the operating system 'does this process exist and may I signal it?' without
    sending anything.

    Zero and negative values are rejected first, and not out of tidiness: `os.kill(0, …)` targets the
    caller's whole PROCESS GROUP and a negative pid targets a group by id. A truncated or missing pid
    in the state file would therefore read as "alive" — and `treg serve stop` would signal everything
    in this terminal."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # it exists, it just is not ours
    except (OSError, TypeError, ValueError):
        return False
    return True


def running() -> dict | None:
    """The live daemon's state, or None. A state file left behind by a killed process is removed
    rather than reported — otherwise `status` would insist a proxy is running forever, and `start`
    would refuse to replace it."""
    state = read_state()
    if not state:
        return None
    if not pid_alive(int(state.get("pid", 0) or 0)):
        clear_state()
        return None
    return state


# ---- P0 · the proxy itself ---------------------------------------------------------------
def mint_token() -> str:
    """A fresh proxy token per session. It is what stops any other process on the machine from
    pointing itself at us and spending the member's quota."""
    return secrets.token_urlsafe(24)


@dataclass
class ProxyConfig:
    """One session's settings. `port=0` asks the operating system for a free port (tests do this);
    the real port is on the handle after start.

    `hosts` is the allow-list — **only** these are intercepted, everything else is tunnelled blind
    (non-negotiable #3). It stays empty unless a treg identity is configured, so a proxy with no
    credentials cannot decrypt anything at all."""

    token: str
    port: int = DEFAULT_PORT
    ca: CertAuthority | None = None
    verbose: bool = False
    # Where an intercepted call is sent, and who it is sent as. No vendor key here — just the
    # member's own treg token, exactly what the CLI carries.
    base_url: str = ""
    treg_token: str = ""
    org: str = ""
    client_name: str = "local-proxy"
    hosts: frozenset[str] = frozenset()
    refresh_seconds: int = HOSTS_REFRESH_SECONDS   # 0 turns the periodic re-read off

    def intercepts(self, host: str) -> bool:
        """Allow-list only, and only when we could actually complete the call. Missing a CA or a
        treg token means we would terminate TLS and then fail — worse than not intercepting, because
        the agent's own call would break for a reason it cannot see."""
        if not (self.ca and self.base_url and self.treg_token):
            return False
        return host.lower() in self.hosts


def _log(cfg: ProxyConfig, message: str) -> None:
    """Host and status only. Never a body, never a header, never the token (rule 7)."""
    if cfg.verbose:
        print(f"▚ treg proxy: {message}", file=sys.stderr)


def _parse_head(head: bytes) -> tuple[str, str, dict[str, str]]:
    """Split a request head into `(method, target, headers)`. Header names are lowercased; a repeated
    header keeps the first value, which is all the two we care about (`proxy-authorization`, `host`)
    can meaningfully have."""
    lines = head.split(b"\r\n")
    parts = lines[0].decode("latin-1").split()
    if len(parts) < 2:
        raise ValueError("malformed request line")
    method, target = parts[0].upper(), parts[1]
    headers: dict[str, str] = {}
    for raw in lines[1:]:
        if not raw:
            continue
        name, sep, value = raw.decode("latin-1").partition(":")
        if sep:
            headers.setdefault(name.strip().lower(), value.strip())
    return method, target, headers


def authorized(headers: dict[str, str], token: str) -> bool:
    """Check `Proxy-Authorization: Basic base64(treg:<token>)`. Compared with `compare_digest` so a
    local process cannot learn the token one character at a time from response timing."""
    raw = headers.get("proxy-authorization", "")
    scheme, _, encoded = raw.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return False
    _, _, supplied = decoded.partition(":")
    return secrets.compare_digest(supplied, token)


def split_hostport(target: str, default_port: int) -> tuple[str, int]:
    """`host:port` → `(host, port)`, understanding the bracket form IPv6 uses (`[::1]:443`)."""
    if target.startswith("["):
        host, _, rest = target[1:].partition("]")
        port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else default_port
        return host, port
    host, sep, port_s = target.rpartition(":")
    if sep and port_s.isdigit():
        return host, int(port_s)
    return target, default_port


def _origin_form(head: bytes, target: str) -> bytes:
    """Rewrite a proxied plain-HTTP head for the origin server: an origin-form request line, and the
    `proxy-*` headers removed. Dropping `Proxy-Authorization` is not tidiness — leaving it on would
    hand our session token to every upstream the agent talks to."""
    split = urlsplit(target)
    path = split.path or "/"
    if split.query:
        path = f"{path}?{split.query}"
    lines = head.split(b"\r\n")
    first = lines[0].decode("latin-1").split()
    rebuilt = [f"{first[0]} {path} {first[2] if len(first) > 2 else 'HTTP/1.1'}".encode("latin-1")]
    for raw in lines[1:]:
        if raw.lower().startswith((b"proxy-authorization:", b"proxy-connection:")):
            continue
        rebuilt.append(raw)
    return b"\r\n".join(rebuilt)


async def _respond(writer: asyncio.StreamWriter, status: int, reason: str, body: str = "",
                   extra: dict[str, str] | None = None) -> None:
    """A short plain-text answer straight from the proxy. Kept explicit so a treg-side failure never
    reads like the vendor being down."""
    payload = (body or reason).encode("utf-8")
    head = [f"HTTP/1.1 {status} {reason}", "Content-Type: text/plain; charset=utf-8",
            f"Content-Length: {len(payload)}", "Connection: close"]
    for k, v in (extra or {}).items():
        head.append(f"{k}: {v}")
    try:
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + payload)
        await writer.drain()
    except (ConnectionError, OSError):
        pass


def _close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
    except (ConnectionError, OSError, RuntimeError):
        pass


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    """Copy one direction until the source ends. Errors are expected here — either side may hang up
    mid-transfer, and that is a normal end of a tunnel, not a fault to report."""
    try:
        while True:
            data = await src.read(_CHUNK)
            if not data:
                break
            dst.write(data)
            await dst.drain()
    except (ConnectionError, OSError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            if dst.can_write_eof():
                dst.write_eof()
        except (ConnectionError, OSError, RuntimeError):
            pass


async def _pump(client_r, client_w, up_r, up_w) -> None:
    """Run both directions until both finish, then close both ends."""
    await asyncio.gather(_pipe(client_r, up_w), _pipe(up_r, client_w), return_exceptions=True)
    _close(up_w)
    _close(client_w)


async def _open_upstream(host: str, port: int):
    return await asyncio.wait_for(asyncio.open_connection(host, port), timeout=_CONNECT_TIMEOUT)


async def _blind_tunnel(cfg: ProxyConfig, client_r, client_w, host: str, port: int) -> None:
    """Connect to the real host and copy bytes both ways, encrypted end to end.

    This is what a non-registered host gets, forever: we never see inside it and never generate a
    certificate for it. It is also the whole of P0 — a proxy that is provably neutral before any
    interception exists."""
    try:
        up_r, up_w = await _open_upstream(host, port)
    except (OSError, asyncio.TimeoutError) as exc:
        _log(cfg, f"{host}:{port} unreachable ({type(exc).__name__})")
        await _respond(client_w, 502, "Bad Gateway", f"treg proxy: cannot reach {host}:{port}")
        _close(client_w)
        return
    client_w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
    await client_w.drain()
    _log(cfg, f"{host}:{port} tunnel")
    await _pump(client_r, client_w, up_r, up_w)


async def _forward_plain(cfg: ProxyConfig, client_r, client_w, head: bytes, target: str) -> None:
    """Plain `http://` through the proxy. Not the interesting path — real API calls are HTTPS — but
    `HTTP_PROXY` is set too, so an http caller must not simply break."""
    split = urlsplit(target)
    host, port = split.hostname, split.port or 80
    if not host:
        await _respond(client_w, 400, "Bad Request", "treg proxy: no host in the request URL")
        _close(client_w)
        return
    try:
        up_r, up_w = await _open_upstream(host, port)
    except (OSError, asyncio.TimeoutError):
        await _respond(client_w, 502, "Bad Gateway", f"treg proxy: cannot reach {host}:{port}")
        _close(client_w)
        return
    up_w.write(_origin_form(head, target) + b"\r\n\r\n")
    await up_w.drain()
    _log(cfg, f"{host}:{port} forward (http)")
    await _pump(client_r, client_w, up_r, up_w)


# ---- P2 · intercept an allow-listed host and forward it to treg -----------------------------
# Rebuilt per hop or the stream corrupts — the same list `infra.upstream.relay.relay()` keeps on the server side.
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
})
# A caller must not be able to name its own identity or org by setting these on the intercepted
# request — the proxy's own treg token is the only identity in play.
_CALLER_MUST_NOT_SET = frozenset({"x-treg-token", "x-treg-org", "x-treg-client", "x-treg-body-encoding"})

# Bodies at or below this are buffered, which is what makes the WAF retry below possible. Anything
# larger streams straight through and simply cannot be retried.
_BUFFER_LIMIT = 1024 * 1024


def _header_pairs(head: bytes) -> list[tuple[str, str]]:
    """Every header in order, duplicates and original spelling kept. `_parse_head`'s dict is for
    decisions; this is for forwarding, where collapsing repeated `Cookie` headers would change the
    request we promised to relay faithfully."""
    pairs: list[tuple[str, str]] = []
    for raw in head.split(b"\r\n")[1:]:
        if not raw:
            continue
        name, sep, value = raw.decode("latin-1").partition(":")
        if sep:
            pairs.append((name.strip(), value.strip()))
    return pairs


async def _read_chunked(reader: asyncio.StreamReader) -> bytes:
    """Decode a chunked request body into bytes. httpx re-frames it for the hop to treg, so we hand
    it the decoded content rather than passing the framing through."""
    out = bytearray()
    while True:
        line = await reader.readuntil(b"\r\n")
        size = int(line.strip().split(b";")[0] or b"0", 16)
        if size == 0:
            while True:                        # trailers, then the final blank line
                trailer = await reader.readuntil(b"\r\n")
                if trailer == b"\r\n":
                    break
            return bytes(out)
        out += await reader.readexactly(size)
        await reader.readexactly(2)            # the CRLF after each chunk
        if len(out) > _BUFFER_LIMIT * 8:
            raise ValueError("chunked body too large to relay")


async def _request_body(reader: asyncio.StreamReader, headers: dict[str, str]):
    """The request body, as bytes when it is small enough to buffer and an async stream when it is
    not. `None` means the caller sent no body at all — which must stay distinct from an empty one,
    or a GET picks up a bogus `Content-Length: 0`."""
    if "chunked" in headers.get("transfer-encoding", "").lower():
        return await _read_chunked(reader)
    raw = headers.get("content-length")
    if raw is None:
        return None
    try:
        length = int(raw)
    except ValueError:
        return None
    if length <= 0:
        return b""
    if length <= _BUFFER_LIMIT:
        return await reader.readexactly(length)

    async def _stream():                       # a large upload flows through without being held
        left = length
        while left > 0:
            data = await reader.read(min(_CHUNK, left))
            if not data:
                return
            left -= len(data)
            yield data

    return _stream()


def _treg_url(base: str, host: str, target: str) -> str:
    """`https://api.stripe.com/v1/charges` → `{base}/call/https://api.stripe.com/v1/charges`.

    The URL-passthrough shape, the one the agent-native callers already use. The server resolves the
    tool by host and longest base-url prefix, so the proxy never has to know a tool's NAME — which is
    the whole point: the agent wrote `api.stripe.com` and knows nothing about treg."""
    return f"{base.rstrip('/')}/call/https://{host}{target if target.startswith('/') else '/' + target}"


def _forward_headers(cfg: ProxyConfig, pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The caller's headers, minus transport and minus anything that would let it speak as someone
    else, plus treg's control headers. `Accept-Encoding: identity` is added when the caller asked for
    nothing, because we relay the raw bytes back — httpx would otherwise request gzip on its own and
    hand the agent compressed content it never asked for (the same rule `relay()` applies)."""
    out = [(k, v) for k, v in pairs
           if k.lower() not in _HOP_BY_HOP and k.lower() not in _CALLER_MUST_NOT_SET]
    if not any(k.lower() == "accept-encoding" for k, _ in out):
        out.append(("Accept-Encoding", "identity"))
    out.append(("X-Treg-Token", cfg.treg_token))
    if cfg.org:
        out.append(("X-Treg-Org", cfg.org))
    out.append(("X-Treg-Client", cfg.client_name))
    out.append(("ngrok-skip-browser-warning", "1"))
    return out


# ---- P3 · the allow-list, and errors an agent can act on -------------------------------------
async def fetch_hosts(client, base_url: str, token: str, org: str = "") -> frozenset[str]:
    """The hosts to intercept, from `GET /tools`.

    The listing is already filtered to what THIS member may use, so the allow-list inherits the
    per-member tool list and project scope for free — a host the caller has no access to is never
    even decrypted. A failure returns an empty set: no answer must never mean "intercept
    everything"."""
    headers = {"X-Treg-Token": token, "X-Treg-Client": "local-proxy", "ngrok-skip-browser-warning": "1"}
    if org:
        headers["X-Treg-Org"] = org
    try:
        r = await client.get(f"{base_url.rstrip('/')}/tools", headers=headers)
        if r.status_code != 200:
            return frozenset()
        return frozenset(t["host"].lower() for t in r.json() if isinstance(t, dict) and t.get("host"))
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return frozenset()


async def refresh_hosts_forever(cfg: ProxyConfig, client, interval: int = HOSTS_REFRESH_SECONDS) -> None:
    """Keep the allow-list current while the session runs. A tool registered ten minutes into a shell
    session should start working without restarting the shell.

    An empty answer is never applied over a working list: a transient failure would otherwise silently
    turn interception off for five minutes, and the agent's calls would go out with no credential."""
    while True:
        await asyncio.sleep(interval)
        hosts = await fetch_hosts(client, cfg.base_url, cfg.treg_token, cfg.org)
        if hosts and hosts != cfg.hosts:
            _log(cfg, f"allow-list now {len(hosts)} host(s)")
            cfg.hosts = hosts


def treg_error_message(status: int, host: str, detail: str) -> str:
    """Turn treg's refusal into something the agent can act on.

    The raw status is misleading here: a 404 from treg means "no tool registered for this host",
    which an agent reads as "the vendor has no such endpoint" and works around by rewriting a
    perfectly good URL. Saying who refused, and what would fix it, is the whole point."""
    if status == 404:
        return (f"treg: no tool is registered for {host} in this team, so there is no credential to "
                f"inject. An admin can add one with `treg tools add`. (This is treg's answer, not "
                f"{host}'s.)")
    if status == 403:
        return (f"treg: you do not have access to {host} through this registry — either the tool is "
                f"not on your list or a policy rule blocks it. Ask an admin of your team. "
                f"(treg's answer, not {host}'s.)")
    if status == 401:
        return ("treg: this shell's treg token is not valid any more. Run `treg login` and start a "
                "new `treg shell`.")
    if status == 429:
        return (f"treg: your daily call limit for this team is used up, so the call to {host} was not "
                f"made. An admin can raise it.")
    if status == 409:
        return (f"treg: more than one registered tool matches {host} and treg cannot tell which "
                f"credential you meant. An admin should narrow one tool's base URL.")
    return f"treg refused this call ({status}): {detail}" if detail else f"treg refused this call ({status})."


def _is_waf_block(status: int, content_type: str) -> bool:
    """A hosting edge (Render's, Cloudflare's) 403s a request whose body looks like an injection —
    a legitimate SQL or HTML payload, for instance. treg's own 403s are JSON, so an HTML 403 did not
    come from treg. Same test the CLI's `_RegistryClient` makes."""
    return status == 403 and "html" in content_type.lower()


async def _send_to_treg(client, method: str, url: str, headers: list[tuple[str, str]], body):
    """One request to the registry, with the edge-WAF escape hatch. A buffered body that comes back
    403-as-HTML is re-sent base64-encoded under `X-Treg-Body-Encoding`, which the server decodes to
    the real bytes before the relay ever sees them."""
    req = client.build_request(method, url, headers=headers, content=body)
    resp = await client.send(req, stream=True)
    if not isinstance(body, bytes) or not body:
        return resp
    if not _is_waf_block(resp.status_code, resp.headers.get("content-type", "")):
        return resp
    await resp.aclose()
    retry = client.build_request(method, url, headers=headers, content=base64.b64encode(body))
    retry.headers["x-treg-body-encoding"] = "base64"
    return await client.send(retry, stream=True)


async def _explain_treg_error(writer: asyncio.StreamWriter, resp, host: str, keep_alive: bool) -> bool:
    """Replace treg's own refusal with an explanation, keeping the status code.

    Only ever runs when the answer carries `X-Treg-Error`, the marker the server puts on its own
    `/call/` refusals — so a genuine 404 or 403 from the VENDOR is never rewritten. Against an older
    registry that does not send the marker, nothing here fires and the reply passes through
    untouched."""
    detail = ""
    try:
        await resp.aread()
        payload = resp.json()
        detail = payload.get("detail", "") if isinstance(payload, dict) else ""
    except (ValueError, httpx.HTTPError, TypeError):
        pass
    finally:
        await resp.aclose()
    body = json.dumps({
        "error": treg_error_message(resp.status_code, host, str(detail)),
        "source": "treg", "host": host, "treg_detail": detail,
    }).encode()
    lines = [
        f"HTTP/1.1 {resp.status_code} {resp.reason_phrase or ''}".rstrip(),
        "Content-Type: application/json",
        f"Content-Length: {len(body)}",
        "X-Treg-Error: 1",
        "Connection: " + ("keep-alive" if keep_alive else "close"),
    ]
    try:
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
        await writer.drain()
    except (ConnectionError, OSError):
        return False
    return keep_alive


async def _body_chunks(resp):
    """The response body as raw bytes, whether treg's answer is still streaming or already in memory.

    `aiter_raw()` raises `StreamConsumed` on a response whose content a transport already loaded, so
    asking for the stream unconditionally would turn a perfectly good reply into a dropped body."""
    if getattr(resp, "is_stream_consumed", False):
        yield resp.content
        return
    async for chunk in resp.aiter_raw():
        yield chunk


async def _write_response(writer: asyncio.StreamWriter, resp, method: str, keep_alive: bool) -> bool:
    """Stream treg's answer back to the agent inside the intercepted TLS connection.

    Framing is re-derived rather than copied: we keep `Content-Length` when the upstream gave one and
    use chunked encoding when it did not, because our hop must be self-consistent even if the hop
    into treg was framed differently. The body bytes themselves are untouched — including a
    `Content-Encoding` the caller asked for."""
    bodyless = method == "HEAD" or resp.status_code in (204, 304)
    length = resp.headers.get("content-length")
    lines = [f"HTTP/1.1 {resp.status_code} {resp.reason_phrase or ''}".rstrip()]
    for name, value in resp.headers.multi_items():
        if name.lower() in _HOP_BY_HOP:
            continue
        lines.append(f"{name}: {value}")
    chunked = not bodyless and length is None
    if bodyless:
        if length is not None:
            lines.append(f"Content-Length: {length}")   # a HEAD keeps the length it describes
    elif chunked:
        lines.append("Transfer-Encoding: chunked")
    else:
        lines.append(f"Content-Length: {length}")
    lines.append("Connection: " + ("keep-alive" if keep_alive else "close"))
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    try:
        if not bodyless:
            async for chunk in _body_chunks(resp):
                if not chunk:
                    continue
                writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n" if chunked else chunk)
                await writer.drain()
            if chunked:
                writer.write(b"0\r\n\r\n")
        await writer.drain()
    except (ConnectionError, OSError):
        return False
    finally:
        await resp.aclose()
    return keep_alive


async def _intercept(cfg: ProxyConfig, client_r, client_w, host: str, client) -> None:
    """An allow-listed host: terminate TLS with a leaf we sign, then relay each request to treg's
    `/call/` passthrough, which injects the credential SERVER-SIDE and streams the answer back.

    This is the whole product seen from the agent's side. The agent believes it is talking to
    `api.stripe.com`; it never learns treg exists, and no vendor key is anywhere on this machine."""
    ca = cfg.ca
    if ca is None:                               # unreachable via `intercepts()`; kept explicit
        await _blind_tunnel(cfg, client_r, client_w, host, 443)
        return
    client_w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
    await client_w.drain()
    try:
        await client_w.start_tls(ca.context_for(host))
    except (ssl.SSLError, OSError, ValueError) as exc:
        # A certificate-pinned client refuses a certificate it did not expect. Nothing to answer —
        # the handshake failed, so there is no HTTP connection to write an explanation into.
        _log(cfg, f"{host} refused our certificate ({type(exc).__name__}) — needs a never-intercept entry")
        _close(client_w)
        return

    while True:                                  # one TLS connection may carry many requests
        try:
            head = await client_r.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, OSError):
            break
        try:
            method, target, headers = _parse_head(head[:-4])
            body = await _request_body(client_r, headers)
        except (ValueError, UnicodeDecodeError, asyncio.IncompleteReadError, OSError):
            await _respond(client_w, 400, "Bad Request", "treg proxy: could not read the request")
            break

        wants_alive = "close" not in headers.get("connection", "").lower()
        url = _treg_url(cfg.base_url, host, target)
        out_headers = _forward_headers(cfg, _header_pairs(head[:-4]))
        try:
            resp = await _send_to_treg(client, method, url, out_headers, body)
        except httpx.HTTPError as exc:
            # treg itself is unreachable. Say so plainly — an agent that reads "502 Bad Gateway" here
            # would blame the vendor and retry forever against a service that is perfectly healthy.
            _log(cfg, f"{host} → treg unreachable ({type(exc).__name__})")
            await _respond(client_w, 502, "Bad Gateway",
                           f"treg proxy: cannot reach the registry at {cfg.base_url} "
                           f"({type(exc).__name__}). The call to {host} was NOT made.")
            break
        _log(cfg, f"{host} {method} → treg {resp.status_code}")
        if resp.headers.get("x-treg-error"):
            alive = await _explain_treg_error(client_w, resp, host, wants_alive)
        else:
            alive = await _write_response(client_w, resp, method, wants_alive)
        if not alive:
            break
    _close(client_w)


def _is_self(host: str, port: int, listen_port: int) -> bool:
    """Guard against the proxy being asked to talk to itself — a loop that would otherwise eat a
    connection slot per attempt."""
    return port == listen_port and host in ("127.0.0.1", "::1", "localhost", LISTEN_HOST)


def treg_client(timeout: float = 120.0):
    """The proxy's own connection to the registry.

    **`trust_env=False` is not optional.** httpx reads `HTTPS_PROXY` from its own environment, and
    inside a `treg shell` that variable points at THIS proxy — the first intercepted call would come
    straight back to us and loop. It also means the bundle and `NO_PROXY` are irrelevant here: we
    talk to treg over the ordinary system trust, as any client would."""
    return httpx.AsyncClient(trust_env=False, timeout=timeout, follow_redirects=False)


async def handle_client(cfg: ProxyConfig, listen_port: int,
                        reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                        client=None) -> None:
    """One client connection: read the head, authenticate, then intercept, tunnel or forward."""
    try:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=_CONNECT_TIMEOUT)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, OSError):
            _close(writer)
            return
        except asyncio.LimitOverrunError:
            await _respond(writer, 431, "Request Header Fields Too Large", "treg proxy: request head too large")
            _close(writer)
            return

        try:
            method, target, headers = _parse_head(head[:-4])
        except (ValueError, UnicodeDecodeError):
            await _respond(writer, 400, "Bad Request", "treg proxy: malformed request")
            _close(writer)
            return

        if not authorized(headers, cfg.token):
            # 407 is the correct answer and the one an HTTP client knows how to act on. The message
            # says how to get the right URL without ever printing the token itself.
            await _respond(
                writer, 407, "Proxy Authentication Required",
                "treg proxy: this proxy is private to one treg shell session.\n"
                "  Use the HTTPS_PROXY value from that session (it carries the session token).",
                extra={"Proxy-Authenticate": 'Basic realm="treg"'},
            )
            _close(writer)
            return

        if method == "CONNECT":
            host, port = split_hostport(target, 443)
            if _is_self(host, port, listen_port):
                await _respond(writer, 400, "Bad Request", "treg proxy: refusing to tunnel to myself")
                _close(writer)
                return
            # The allow-list decision, and the only place it is made: a registered tool's host is
            # intercepted so treg can add the credential; everything else stays a tunnel we cannot
            # read — including the agent's own api.anthropic.com / api.openai.com traffic.
            if client is not None and cfg.intercepts(host):
                await _intercept(cfg, reader, writer, host, client)
            else:
                await _blind_tunnel(cfg, reader, writer, host, port)
            return

        if target.startswith("http://"):
            await _forward_plain(cfg, reader, writer, head[:-4], target)
            return

        await _respond(writer, 400, "Bad Request",
                       "treg proxy: only CONNECT and absolute-form http:// requests are proxied")
        _close(writer)
    except asyncio.CancelledError:
        _close(writer)
        raise
    except Exception as exc:  # noqa: BLE001 — one bad connection must never take the proxy down
        _log(cfg, f"connection failed ({type(exc).__name__})")
        _close(writer)


@dataclass
class ProxyHandle:
    """A running proxy. `treg shell` is synchronous, so the event loop lives in a daemon thread and
    this is the only object the rest of the CLI needs to hold."""

    port: int
    token: str
    ca: CertAuthority | None
    _loop: asyncio.AbstractEventLoop
    _server: asyncio.AbstractServer
    _thread: threading.Thread
    _client: Any = None       # the httpx client the intercept path uses; owned by the loop

    def stop(self, timeout: float = 5.0) -> None:
        """Close the listener, end the connections still open, then stop the loop. Safe to call twice.

        Cancelling the in-flight handlers first is what makes shutdown quiet: a tunnel is a long-lived
        task, and stopping the loop out from under one leaves a live connection dangling and prints an
        `asyncio` complaint the user would have no way to act on."""
        if not self._thread.is_alive():
            return

        async def _shutdown() -> None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except (OSError, RuntimeError):
                pass
            if self._client is not None:
                try:
                    await self._client.aclose()
                except (httpx.HTTPError, RuntimeError, OSError):
                    pass
            here = asyncio.current_task()
            rest = [t for t in asyncio.all_tasks() if t is not here]
            for task in rest:
                task.cancel()
            if rest:
                await asyncio.gather(*rest, return_exceptions=True)

        try:
            asyncio.run_coroutine_threadsafe(_shutdown(), self._loop).result(timeout=timeout)
        except (RuntimeError, TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
            pass  # the loop is already going down; the join below is the real guarantee
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=timeout)

    def env(self, treg_host: str | None = None) -> dict[str, str]:
        """The variables to publish into the agent's process tree (see `proxy_env`)."""
        bundle = self.ca.bundle_path if self.ca else ""
        return proxy_env(self.port, self.token, bundle, treg_host)


def listening_port(server: asyncio.AbstractServer, fallback: int) -> int:
    """The port actually bound — the real one when `port=0` asked the operating system to choose."""
    sockets = getattr(server, "sockets", None) or ()
    return sockets[0].getsockname()[1] if sockets else fallback


async def serve(cfg: ProxyConfig, client=None) -> asyncio.AbstractServer:
    """Bind the listener on the CURRENT event loop and return the server.

    `start()` wraps this in a thread for the synchronous CLI; a caller already inside a loop (the
    integration tests, which drive the registry's own ASGI app) uses it directly."""
    port_holder: dict = {}
    server = await asyncio.start_server(
        lambda r, w: handle_client(cfg, port_holder.get("port", cfg.port), r, w, client),
        host=LISTEN_HOST, port=cfg.port, limit=_HEAD_LIMIT,
    )
    port_holder["port"] = listening_port(server, cfg.port)
    return server


def start(cfg: ProxyConfig, client=None) -> ProxyHandle:
    """Start the proxy in a background thread and return once it is accepting connections.

    `client` overrides the connection to the registry — the test suite passes one wired to the ASGI
    app so the whole intercept path can be exercised without a server on a socket."""
    loop = asyncio.new_event_loop()
    ready: dict = {}
    error: list[BaseException] = []
    started = threading.Event()

    def _run() -> None:
        asyncio.set_event_loop(loop)
        try:
            # Created INSIDE the loop: an httpx.AsyncClient binds to the loop that first uses it.
            ready["client"] = client if client is not None else treg_client()
            have_identity = bool(cfg.base_url and cfg.treg_token)
            if have_identity and not cfg.hosts:
                # Nobody pre-seeded the allow-list (the shell does, from the tools it already
                # fetched for its shims), so read it once before accepting any connection —
                # otherwise the first calls of the session would slip out uncaptured.
                cfg.hosts = loop.run_until_complete(
                    fetch_hosts(ready["client"], cfg.base_url, cfg.treg_token, cfg.org))
            server = loop.run_until_complete(serve(cfg, ready["client"]))
            if have_identity and cfg.refresh_seconds:
                loop.call_soon(
                    lambda: loop.create_task(refresh_hosts_forever(cfg, ready["client"], cfg.refresh_seconds))
                )
            ready["server"] = server
            ready["port"] = listening_port(server, cfg.port)
        except BaseException as exc:  # noqa: BLE001 — hand the failure to the caller's thread
            error.append(exc)
            started.set()
            return
        started.set()
        try:
            loop.run_forever()
        finally:
            server.close()
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    thread = threading.Thread(target=_run, name="treg-localproxy", daemon=True)
    thread.start()
    started.wait(timeout=10)
    if error:
        raise error[0]
    if "server" not in ready:
        raise RuntimeError("treg proxy: the listener did not start")
    return ProxyHandle(ready["port"], cfg.token, cfg.ca, loop, ready["server"], thread, ready.get("client"))
