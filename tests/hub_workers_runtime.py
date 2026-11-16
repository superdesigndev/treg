"""Start the hub loader under miniflare for tests. Not a test module."""

from __future__ import annotations

import fcntl
import os
import select
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "workers" / "hub-sandbox"


def _npm_ci() -> None:
    lock = open("/tmp/treg-hub-sandbox-npm.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (ROOT / "node_modules" / "miniflare").exists():
            return
        subprocess.run(["npm", "ci"], cwd=ROOT, check=True)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _ready(proc: subprocess.Popen, err: list[bytes], timeout: float = 60) -> str:
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = max(0, deadline - time.monotonic())
        readable, _, _ = select.select([proc.stdout], [], [], remaining)
        if proc.poll() is not None and not readable:
            text = b"".join(err).decode("utf-8", "replace")
            raise RuntimeError(f"miniflare exited {proc.returncode}: {text[-2000:]}")
        if not readable:
            break
        line = proc.stdout.readline().decode()
        if line.startswith("READY "):
            return line.split(None, 1)[1].strip()
    _kill(proc)
    text = b"".join(err).decode("utf-8", "replace")
    raise RuntimeError(f"miniflare did not become ready: {text[-2000:]}")


@contextmanager
def running(secret: str):
    _npm_ci()
    env = os.environ.copy()
    env["AUTH_SECRET"] = secret
    proc = subprocess.Popen(
        ["node", "dev-server.mjs"], cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    err: list[bytes] = []

    def _drain() -> None:
        assert proc.stderr is not None
        while True:
            chunk = proc.stderr.read(4096)
            if not chunk:
                break
            err.append(chunk)

    threading.Thread(target=_drain, daemon=True).start()
    try:
        yield _ready(proc, err)
    finally:
        _kill(proc)
