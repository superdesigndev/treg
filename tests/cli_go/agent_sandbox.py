"""A sandboxed machine for the commands that write outside treg: a fresh HOME with fake agent
installs, a PATH holding only fake executables (a `claude` that records its arguments), and a
snapshot of every file the run left in HOME."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .cli_go_harness import PYTHON_CLI, Run, as_json, normalize, write_config

SYSTEM_PATH = os.pathsep.join(p for p in ("/usr/bin", "/bin") if os.path.isdir(p))

RECORDER = """#!{python}
import json, os, sys
with open(os.path.join(os.environ["HOME"], "{name}-calls.jsonl"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
sys.exit(int(os.environ.get("FAKE_{upper}_EXIT", "0")))
"""


@dataclass
class Machine:
    home: Path
    bin: Path

    def env(self, **extra: str) -> dict[str, str]:
        env = {
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "PATH": os.pathsep.join([str(self.bin), SYSTEM_PATH]),
            "TREG_CONFIG": str(self.home / ".treg" / "config.json"),
            "TREG_TELEMETRY": "0",
            "PYTHONIOENCODING": "utf-8",
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            "CODEX_HOME": str(self.home / ".codex"),
            # httpx also reads the macOS system proxy, which Go does not; neither may use one here.
            "NO_PROXY": "*",
        }
        if os.environ.get("SYSTEMROOT"):
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        return env | extra

    def fake_bin(self, name: str) -> None:
        path = self.bin / name
        path.write_text(RECORDER.format(python=sys.executable, name=name, upper=name.upper().replace("-", "_")))
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def calls(self, name: str) -> list[list[str]]:
        log = self.home / f"{name}-calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def mkdirs(self, *rel: str) -> None:
        for r in rel:
            (self.home / r).mkdir(parents=True, exist_ok=True)

    def write(self, rel: str, text: str) -> Path:
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path


def machine(root: Path, label: str, *, registry: str, **config) -> Machine:
    home = root / label / "home"
    bin_dir = root / label / "bin"
    home.mkdir(parents=True)
    bin_dir.mkdir(parents=True)
    write_config(home, base_url=registry, **config)
    return Machine(home, bin_dir)


def pair(root: Path, *, registry: str, **config) -> tuple[Machine, Machine]:
    return machine(root, "py", registry=registry, **config), machine(root, "go", registry=registry, **config)


def run_in(m: Machine, cli: list[str], argv: list[str], *, env: dict[str, str] | None = None,
           cwd: Path | None = None, stdin: bytes | None = b"", timeout: float = 90) -> Run:
    proc = subprocess.run(cli + argv, input=stdin, capture_output=True, env=m.env(**(env or {})),
                          cwd=cwd or m.home, timeout=timeout)
    return Run(proc.returncode, proc.stdout, proc.stderr.decode("utf-8", "replace"))


def both(py: Machine, go: Machine, go_bin: Path, argv: list[str], **kw) -> tuple[Run, Run]:
    cwd = kw.pop("cwd", None)
    return (run_in(py, PYTHON_CLI, argv, cwd=cwd(py) if callable(cwd) else cwd, **kw),
            run_in(go, [str(go_bin)], argv, cwd=cwd(go) if callable(cwd) else cwd, **kw))


def scrub(text: str, *machines: Machine) -> str:
    """Each sandbox's own paths read as <HOME> and <BIN>, so two runs compare."""
    for m in machines:
        for path, label in ((m.home, "<HOME>"), (m.bin, "<BIN>")):
            for spelling in {str(path), str(path.resolve())}:
                text = text.replace(spelling, label)
    return normalize(text)


def same_run(py: Run, go: Run, py_m: Machine, go_m: Machine) -> None:
    assert py.code == go.code, (py, go)
    assert scrub(py.text, py_m) == scrub(go.text, go_m), (py.text, go.text)
    assert scrub(py.err, py_m) == scrub(go.err, go_m), (py.err, go.err)


SKIP_FILES = {"claude-calls.jsonl"}


def snapshot(m: Machine) -> dict[str, tuple[str, str]]:
    """Every path under HOME: its mode, and for a file its text with the sandbox paths scrubbed."""
    out = {}
    for p in sorted(m.home.rglob("*")):
        rel = p.relative_to(m.home).as_posix()
        if rel in SKIP_FILES or rel.startswith(".treg/config.json"):
            continue
        mode = oct(stat.S_IMODE(p.lstat().st_mode))
        if p.is_symlink():
            out[rel] = (mode, "-> " + scrub(os.readlink(p), m))
        elif p.is_dir():
            out[rel + "/"] = (mode, "")
        else:
            text = scrub(p.read_bytes().decode("utf-8", "replace"), m)
            out[rel] = (mode, as_json(text.encode()))
    return out


def same_files(py_m: Machine, go_m: Machine) -> None:
    py, go = snapshot(py_m), snapshot(go_m)
    assert sorted(py) == sorted(go), (sorted(set(py) ^ set(go)))
    for rel in py:
        assert py[rel] == go[rel], (rel, py[rel], go[rel])
