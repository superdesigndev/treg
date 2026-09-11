"""In-memory object storage for bootstrap injection; never opens a socket."""
import asyncio
import hashlib

from treg.infra.object_store import ObjectInfo, ObjectStoreError


class MemoryObjectStore:
    def __init__(self):
        self.objects = {}
        self.put_calls = 0
        self.get_calls = 0
        self.fail_puts = 0
        self.fail_gets = False
        self.gate = None
        self.entered = asyncio.Event()
        self.check_io = lambda: None

    async def put(self, body: bytes, *, content_hash=None) -> ObjectInfo:
        self.check_io()
        self.put_calls += 1
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_puts:
            self.fail_puts -= 1
            raise ObjectStoreError('store_error')
        key = content_hash or hashlib.sha256(body).hexdigest()
        self.objects[key] = body
        return ObjectInfo(key, len(body))

    async def get(self, content_hash: str) -> bytes | None:
        self.check_io()
        self.get_calls += 1
        if self.fail_gets:
            raise ObjectStoreError('store_error')
        body = self.objects.get(content_hash)
        if body is not None and hashlib.sha256(body).hexdigest() != content_hash:
            raise ObjectStoreError('hash_mismatch')
        return body

    async def head(self, content_hash: str) -> ObjectInfo | None:
        self.check_io()
        body = self.objects.get(content_hash)
        return ObjectInfo(content_hash, len(body)) if body is not None else None


class MemoryObstoreSDK:
    """The obstore methods used by our adapter, without Rust or network I/O."""
    def __init__(self):
        self.calls = []

    class Result:
        def __init__(self, body):
            self.body = body
            self.meta = {'size': len(body)}

        async def bytes_async(self):
            return self.body

    async def put_async(self, path, body, *, attributes, use_multipart):
        self.calls.append("put")
        self.path, self.body, self.attributes = path, body, attributes
        assert use_multipart is False

    async def head_async(self, path):
        self.calls.append('head')
        return {'size': len(self.body)}

    async def get_async(self, path):
        self.calls.append('get')
        return self.Result(self.body)
