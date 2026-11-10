"""The two picture buckets: private STAGING (every upload lands here) and PUBLIC (what pages show).

S3 API through obstore, like the archive's store, but separate settings, keys and buckets: public
pictures never share a bucket or a credential with the archive's customer data. Publishing is a read
from staging and a write to public through treg (the files are small; the SDK cannot copy across
buckets). Callers hold no database connection while these run (AGENTS.md rule 3).
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol
from urllib.parse import urlsplit

Bucket = Literal["staging", "public"]
CACHE_FOREVER = "public, max-age=31536000, immutable"


class MediaStoreError(Exception):
    """A storage call failed; the message never carries keys, credentials or bodies."""


class MediaStore(Protocol):
    async def put(self, bucket: Bucket, key: str, body: bytes, content_type: str) -> None: ...
    async def get(self, bucket: Bucket, key: str) -> bytes | None: ...
    async def delete(self, bucket: Bucket, key: str) -> None: ...
    async def listing(self, bucket: Bucket, prefix: str) -> list[tuple[str, datetime]]: ...


def configured(settings) -> bool:
    return bool(settings.media_store_endpoint and settings.media_store_access_key_id
                and settings.media_store_secret_access_key and settings.media_staging_bucket
                and settings.media_public_bucket)


def _local_http(endpoint: str) -> bool:
    parts = urlsplit(endpoint)
    return parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1")


class S3MediaStore:
    def __init__(self, clients: dict[str, object]):
        self._clients = clients

    async def put(self, bucket: Bucket, key: str, body: bytes, content_type: str) -> None:
        import obstore
        try:
            await obstore.put_async(self._clients[bucket], key, body,
                                    attributes={"Content-Type": content_type, "Cache-Control": CACHE_FOREVER})
        except Exception as exc:  # noqa: BLE001 - wrap every storage failure; from None keeps keys out
            raise MediaStoreError(f"put failed ({type(exc).__name__})") from None

    async def get(self, bucket: Bucket, key: str) -> bytes | None:
        import obstore
        from obstore.exceptions import NotFoundError
        try:
            result = await obstore.get_async(self._clients[bucket], key)
            return bytes(await result.bytes_async())
        except (NotFoundError, FileNotFoundError):   # obstore raises the builtin for a missing key
            return None
        except Exception as exc:  # noqa: BLE001 - wrap every storage failure; from None keeps keys out
            raise MediaStoreError(f"get failed ({type(exc).__name__})") from None

    async def delete(self, bucket: Bucket, key: str) -> None:
        import obstore
        try:
            await obstore.delete_async(self._clients[bucket], key)
        except Exception as exc:  # noqa: BLE001 - wrap every storage failure; from None keeps keys out
            raise MediaStoreError(f"delete failed ({type(exc).__name__})") from None

    async def listing(self, bucket: Bucket, prefix: str) -> list[tuple[str, datetime]]:
        """Every (key, last modified) under `prefix`."""
        import obstore
        out: list[tuple[str, datetime]] = []
        try:
            async for batch in obstore.list(self._clients[bucket], prefix=prefix, chunk_size=1000):
                out.extend((item["path"], item["last_modified"]) for item in batch)
        except Exception as exc:  # noqa: BLE001 - wrap every storage failure; from None keeps keys out
            raise MediaStoreError(f"list failed ({type(exc).__name__})") from None
        return out


def open_media_store(settings) -> S3MediaStore | None:
    """The store, or None while the feature is not configured. Raises ValueError on an endpoint that
    is neither https nor a local MinIO, so a typo cannot send pictures over plain HTTP."""
    if not configured(settings):
        return None
    endpoint = settings.media_store_endpoint.rstrip("/")
    local = _local_http(endpoint)
    if not (endpoint.startswith("https://") or local):
        raise ValueError("TREG_MEDIA_STORE_ENDPOINT must be https (or http://localhost for MinIO)")
    from obstore.store import S3Store

    timeout = f"{settings.media_store_timeout_s}s"
    options = {"timeout": timeout, "connect_timeout": timeout}
    if local:
        options["allow_http"] = "true"

    def client(bucket: str):
        return S3Store(bucket, config={
            "endpoint": endpoint, "region": "auto",
            "access_key_id": settings.media_store_access_key_id,
            "secret_access_key": settings.media_store_secret_access_key,
            "virtual_hosted_style_request": "false",
        }, client_options=options, retry_config={"max_retries": 1})

    return S3MediaStore({"staging": client(settings.media_staging_bucket),
                         "public": client(settings.media_public_bucket)})
