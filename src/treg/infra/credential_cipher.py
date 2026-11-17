"""Stored-credential encryption: AWS KMS envelope encryption when configured, Fernet otherwise.

A KMS-sealed value is `kms1:<wrapped data key>.<nonce + AES-256-GCM ciphertext>`, both parts
urlsafe base64. Every value has its own data key; KMS wraps it under `kms_key_id` with the
encryption context `{"treg:purpose": "credential", "treg:org": "<org id>"}`, which also authenticates
the ciphertext, so a value moved to another team's row will not open. Any other value is a legacy
Fernet token from `crypto.py`. Both read paths stay until no Fernet credential remains.

KMS calls are network I/O: `seal` and `unseal` run them in a worker thread, and the call path opens
each credential once per call (`open_for_call`) after its database phase has ended.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .. import crypto
from ..config import get_settings

if TYPE_CHECKING:
    from ..models import Secret

PREFIX = "kms1:"
_NONCE_BYTES = 12


class CredentialCipherError(RuntimeError):
    pass


def is_kms_sealed(value: str) -> bool:
    return value.startswith(PREFIX)


def _context(org_id: int | None) -> dict[str, str]:
    return {"treg:purpose": "credential", "treg:org": "" if org_id is None else str(org_id)}


def _aad(context: dict[str, str]) -> bytes:
    return json.dumps(context, sort_keys=True, separators=(",", ":")).encode()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode()


@lru_cache
def _client(key_id: str):
    import boto3
    from botocore.config import Config

    # A key or alias ARN names its region: arn:aws:kms:<region>:<account>:... botocore's default
    # 60-second timeouts would let a degraded KMS stall a call for minutes.
    return boto3.client("kms", region_name=key_id.split(":")[3], config=Config(
        connect_timeout=2, read_timeout=5, retries={"mode": "standard", "max_attempts": 3}))


def _key_id() -> str:
    key_id = get_settings().kms_key_id
    if not key_id:
        raise CredentialCipherError("a KMS-sealed credential needs TREG_KMS_KEY_ID on this server")
    return key_id


def _seal_kms(plaintext: str, org_id: int | None) -> str:
    key_id = _key_id()
    context = _context(org_id)
    issued = _client(key_id).generate_data_key(KeyId=key_id, KeySpec="AES_256", EncryptionContext=context)
    nonce = os.urandom(_NONCE_BYTES)
    body = AESGCM(issued["Plaintext"]).encrypt(nonce, plaintext.encode(), _aad(context))
    return f"{PREFIX}{_b64(issued['CiphertextBlob'])}.{_b64(nonce + body)}"


def _unseal_kms(value: str, org_id: int | None) -> str:
    key_id = _key_id()
    context = _context(org_id)
    try:
        wrapped, sealed = value[len(PREFIX):].split(".", 1)
        wrapped_key, raw = base64.urlsafe_b64decode(wrapped), base64.urlsafe_b64decode(sealed)
    except ValueError as exc:
        raise CredentialCipherError("malformed KMS-sealed credential") from exc
    data_key = _client(key_id).decrypt(
        CiphertextBlob=wrapped_key, KeyId=key_id, EncryptionContext=context)["Plaintext"]
    nonce, body = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
    return AESGCM(data_key).decrypt(nonce, body, _aad(context)).decode()


def kms_writes() -> bool:
    return get_settings().kms_encrypt_new


async def seal(plaintext: str, *, org_id: int | None) -> str:
    if not kms_writes():
        return crypto.encrypt(plaintext)
    return await asyncio.to_thread(_seal_kms, plaintext, org_id)


async def unseal(value: str, *, org_id: int | None) -> str:
    if not is_kms_sealed(value):
        return crypto.decrypt(value)
    return await asyncio.to_thread(_unseal_kms, value, org_id)


# The credentials one call has opened, keyed by (secret id, stored value) so a value refreshed
# mid-call is never answered from the old one.
_OPENED: ContextVar[dict[tuple[int | None, str], str] | None] = ContextVar("opened_credentials", default=None)


@contextmanager
def call_scope() -> Iterator[None]:
    """Bound what a call opens to that call; a routed child call gets its own scope."""
    token = _OPENED.set({})
    try:
        yield
    finally:
        _OPENED.reset(token)


async def open_for_call(secrets: Iterable[Secret]) -> None:
    """Open every credential the current call may inject or mask, once, before any of it is used.
    Run after the call's database phase: KMS-sealed values are opened over the network."""
    opened_now = _OPENED.get()
    if opened_now is None:
        raise CredentialCipherError("open_for_call needs a call_scope")
    pending = {(s.id, s.value): s for s in secrets if (s.id, s.value) not in opened_now}
    if pending:
        values = await asyncio.gather(*(unseal(s.value, org_id=s.org_id) for s in pending.values()))
        opened_now.update(zip(pending, values))


async def open_one(secret: Secret) -> str:
    found = (_OPENED.get() or {}).get((secret.id, secret.value))
    return found if found is not None else await unseal(secret.value, org_id=secret.org_id)


def opened(secret: Secret) -> str:
    """The plaintext of a credential `open_for_call` opened for this call. A legacy Fernet value is
    opened locally on demand; a KMS-sealed one that was not opened is a programming error."""
    found = (_OPENED.get() or {}).get((secret.id, secret.value))
    if found is not None:
        return found
    if not is_kms_sealed(secret.value):
        return crypto.decrypt(secret.value)
    raise CredentialCipherError(f"credential {secret.id} was not opened for this call")


class Credentials:
    """The connections domain's CredentialPort."""

    async def open(self, secret: Secret) -> str:
        return await open_one(secret)

    async def seal(self, plaintext: str, org_id: int | None) -> str:
        return await seal(plaintext, org_id=org_id)
