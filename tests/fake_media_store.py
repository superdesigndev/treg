"""An in-memory stand-in for the two picture buckets (`infra.media_store.MediaStore`)."""
from __future__ import annotations

from datetime import datetime, timezone


class MemoryMediaStore:
    def __init__(self):
        self.buckets: dict[str, dict[str, tuple[bytes, str]]] = {"staging": {}, "public": {}}
        self.modified: dict[tuple[str, str], datetime] = {}

    async def put(self, bucket, key, body, content_type):
        self.buckets[bucket][key] = (bytes(body), content_type)
        self.modified[(bucket, key)] = datetime.now(timezone.utc)

    async def listing(self, bucket, prefix):
        return [(k, self.modified[(bucket, k)]) for k in sorted(self.buckets[bucket]) if k.startswith(prefix)]

    async def get(self, bucket, key):
        item = self.buckets[bucket].get(key)
        return item[0] if item else None

    async def delete(self, bucket, key):
        self.buckets[bucket].pop(key, None)

    def keys(self, bucket):
        return sorted(self.buckets[bucket])
