"""Every doc path named anywhere in the repository exists.

A deleted or renamed doc otherwise leaves pointers in code comments, fragments and agent-facing
files that send the next reader to nothing.
"""
from __future__ import annotations

import posixpath
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = re.compile(r"(?<![\w./-])docs/[\w./-]*\w")
MD_LINK = re.compile(r"\]\(([^)\s]+)\)")
TEXT_SUFFIXES = {
    ".md", ".py", ".js", ".ts", ".vue", ".html", ".txt", ".yaml", ".yml", ".toml", ".json", ".sh",
}


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [p for p in out.split("\0") if p and (ROOT / p).is_file()]


def _known(tracked: list[str]) -> set[str]:
    known = set(tracked)
    for path in tracked:
        parent = posixpath.dirname(path)
        while parent:
            known.add(parent)
            parent = posixpath.dirname(parent)
    return known


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def test_every_docs_path_named_in_the_repo_exists():
    tracked = _tracked()
    known = _known(tracked)
    missing = []
    for rel in tracked:
        # pyproject.toml names docs/ paths only to keep them out of the published sdist.
        if rel == "pyproject.toml" or Path(rel).suffix not in TEXT_SUFFIXES:
            continue
        text = (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        for match in DOC_PATH.finditer(text):
            ref = match.group(0).rstrip(".")
            if ref not in known:
                missing.append(f"{rel}:{_line(text, match.start())}: {ref}")
    assert not missing, "dangling docs/ references:\n" + "\n".join(missing)


def test_every_relative_markdown_link_resolves():
    tracked = _tracked()
    known = _known(tracked)
    missing = []
    for rel in tracked:
        if not rel.endswith(".md"):
            continue
        text = (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        for match in MD_LINK.finditer(text):
            target = match.group(1).split("#")[0]
            if not target or re.match(r"[a-z]+:", target) or target.startswith("/") or "{" in target:
                continue
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
            if resolved not in known:
                missing.append(f"{rel}:{_line(text, match.start())}: {target}")
    assert not missing, "dangling relative links:\n" + "\n".join(missing)
