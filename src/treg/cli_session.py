"""Which agent task a CLI invocation belongs to: a hashed session id and an agent label."""
from __future__ import annotations

import hashlib
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

MAX_HOPS = 8
IDLE_WINDOW_SECONDS = 30 * 60
# Variables an agent sets for the processes it runs. Children inherit them, so one is read only once
# the process tree has shown that agent to be the nearest.
_SESSION_VARS = {"claude-code": ("CLAUDE_CODE_SESSION_ID",),
                 "codex": ("CODEX_THREAD_ID", "CODEX_SESSION_ID")}
_PRESENCE_VARS = {"claude-code": ("CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "CLAUDE_PID"),
                  "codex": ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_CI"),
                  "gemini-cli": ("GEMINI_CLI",)}
_SAFE_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")


@dataclass(frozen=True)
class Proc:
    pid: int
    name: str
    started: int


@dataclass(frozen=True)
class Session:
    id: str
    agent: str


_current: Session | None = None


def current(config_path: Path) -> Session:
    """This invocation's session, resolved once. Never raises."""
    global _current
    if _current is None:
        try:
            _current = resolve(config_path, dict(os.environ), _ancestors())
        except Exception:  # noqa: BLE001 - telemetry must never change command behavior
            _current = Session(_hash("unknown", str(uuid.uuid4())), "unknown")
    return _current


def reset() -> None:
    global _current
    _current = None


def resolve(config_path: Path, env: dict[str, str], ancestors: list[Proc]) -> Session:
    agent, process = next(((a, p) for p in ancestors if (a := _agent_of(p, env))), (None, None))
    if agent is None and not ancestors:
        present = [name for name, keys in _PRESENCE_VARS.items() if any(env.get(k) for k in keys)]
        agent = present[0] if len(present) == 1 else None
    label = agent or "unknown"
    if given := env.get("TREG_SESSION_ID", "").strip():
        return Session(given if _SAFE_ID.fullmatch(given) else _hash("env", given), label)
    for key in _SESSION_VARS.get(agent or "", ()):
        if raw := env.get(key):
            return Session(_hash(label, raw), label)
    if process is not None:
        return Session(_hash(label, f"{process.pid}:{process.started}"), label)
    return Session(_hash(label, _idle_window_id(config_path)), label)


def _agent_of(proc: Proc, env: dict[str, str]) -> str | None:
    # A native Claude Code install runs as an executable named after its version, so its own
    # CLAUDE_PID is the reliable mark.
    if proc.name == "claude" or env.get("CLAUDE_PID") == str(proc.pid):
        return "claude-code"
    if proc.name.startswith("codex"):
        return "codex"
    if proc.name == "opencode":
        return "opencode"
    if proc.name == "gemini" or (proc.name == "node" and env.get("GEMINI_CLI") == "1"):
        return "gemini-cli"
    return None


def _hash(agent: str, raw: str) -> str:
    return hashlib.sha256(f"{agent}:{raw}".encode()).hexdigest()[:16]


def _idle_window_id(config_path: Path) -> str:
    path = config_path.with_name("session-id")
    try:
        if time.time() - path.stat().st_mtime < IDLE_WINDOW_SECONDS:
            identifier = str(uuid.UUID(path.read_text().strip()))
            path.touch()
            return identifier
    except (OSError, ValueError):
        pass
    identifier = str(uuid.uuid4())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(identifier)
    except OSError:
        pass
    return identifier


def _ancestors() -> list[Proc]:
    """Up to MAX_HOPS parents, nearest first; empty where the platform offers no cheap walk."""
    try:
        read = _read_proc_linux if sys.platform.startswith("linux") else \
            _read_proc_darwin() if sys.platform == "darwin" else None
        if read is None:
            return []
        found, pid = [], os.getppid()
        while pid > 1 and len(found) < MAX_HOPS:
            entry = read(pid)
            if entry is None:
                break
            proc, pid = entry
            found.append(proc)
        return found
    except Exception:  # noqa: BLE001 - an unreadable tree only loses the agent label
        return []


def _normal_name(raw: str) -> str:
    return os.path.basename(raw.strip()).lstrip("-").lower()


def _read_proc_linux(pid: int) -> tuple[Proc, int] | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # The name sits in parentheses and may itself contain spaces or parentheses.
    name, rest = stat[stat.index("(") + 1:stat.rindex(")")], stat[stat.rindex(")") + 2:].split()
    return Proc(pid, _normal_name(name), int(rest[19])), int(rest[1])


def _read_proc_darwin():
    import ctypes

    class BsdInfo(ctypes.Structure):  # struct proc_bsdinfo, <sys/proc_info.h>
        _fields_ = [("flags", ctypes.c_uint32), ("status", ctypes.c_uint32),
                    ("xstatus", ctypes.c_uint32), ("pid", ctypes.c_uint32),
                    ("ppid", ctypes.c_uint32), ("ids", ctypes.c_uint32 * 7),
                    ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
                    ("nfiles", ctypes.c_uint32), ("pgid", ctypes.c_uint32),
                    ("pjobc", ctypes.c_uint32), ("tdev", ctypes.c_uint32),
                    ("tpgid", ctypes.c_uint32), ("nice", ctypes.c_int32),
                    ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]

    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    proc_pidtbsdinfo = 3

    def read(pid: int) -> tuple[Proc, int] | None:
        info = BsdInfo()
        size = libproc.proc_pidinfo(pid, proc_pidtbsdinfo, ctypes.c_uint64(0),
                                    ctypes.byref(info), ctypes.sizeof(info))
        if size != ctypes.sizeof(info):
            return None
        name = (info.name or info.comm).decode("utf-8", "replace")
        return Proc(pid, _normal_name(name), int(info.start_sec)), int(info.ppid)

    return read
