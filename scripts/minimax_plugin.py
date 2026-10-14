#!/usr/bin/env python3
"""Validate the MiniMax plugin at `plugins/minimax/` against the marketplace's intake rules, and zip it.

The rules are MiniMax's (docs/MINIMAX-PLUGIN.md carries the source): `.minimax-plugin/plugin.json`
at the package root with no wrapper directory; ASCII-only paths; no symlinks, hardlinks, install
scripts, executables or native binaries; UTF-8 without BOM; JSON objects without duplicate keys;
every capability file referenced by the manifest and every referenced file present; no credential
anywhere in the package. Their validator runs after submission and reports back over Feishu —
this one runs before, in the test suite.

Usage:  python3 scripts/minimax_plugin.py --check           # validate only (the test)
        python3 scripts/minimax_plugin.py --zip OUT.zip     # validate, then write a submission ZIP
"""

from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "minimax"
MANIFEST = PLUGIN / ".minimax-plugin" / "plugin.json"

CATEGORIES = {"Office", "Studio", "Design & Sites", "Code", "Business", "Sales", "Productivity",
              "Science & Healthcare", "Education", "Other"}
NAME_RE = re.compile(r"^[a-z][a-z0-9._-]{0,79}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
PATH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
ICON_EXT = {".png", ".jpg", ".jpeg", ".webp"}
# Things that must never ship. The frontmatter and prose talk ABOUT tokens, so match value shapes.
SECRET_RE = re.compile(r"(sk-[A-Za-z0-9]{20,}|treg_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|"
                       r"gh[pousr]_[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY)")
INSTALL_SCRIPT_EXT = {".sh", ".bash", ".zsh", ".bat", ".cmd", ".ps1", ".exe", ".dll", ".so", ".dylib"}


def package_version() -> str:
    for line in (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version = "):
            return line.split('"')[1]
    raise SystemExit("pyproject.toml has no version")


def no_dup_keys(pairs):
    seen = set()
    for k, _ in pairs:
        if k in seen:
            raise ValueError(f"duplicate key {k!r}")
        seen.add(k)
    return dict(pairs)


def files() -> list[Path]:
    return sorted(p for p in PLUGIN.rglob("*") if not p.is_dir() or p.is_symlink())


def validate() -> list[str]:
    errors: list[str] = []
    e = errors.append
    if not MANIFEST.is_file():
        return [f"missing {MANIFEST.relative_to(ROOT)}"]

    try:
        m = json.loads(MANIFEST.read_text(encoding="utf-8"), object_pairs_hook=no_dup_keys)
    except ValueError as exc:
        return [f"plugin.json: {exc}"]
    if not isinstance(m, dict):
        return ["plugin.json must be a JSON object"]

    if m.get("schemaVersion") != 1:
        e("schemaVersion must be 1")
    if not NAME_RE.match(str(m.get("name", ""))):
        e(f"name {m.get('name')!r} must be lowercase, start with a letter, <=80 chars of [a-z0-9._-]")
    if not SEMVER_RE.match(str(m.get("version", ""))):
        e("version must be SemVer")
    elif m["version"] != package_version():
        e(f"version {m['version']} != pyproject {package_version()}")
    for field in ("description", "author", "icon", "category"):
        if not m.get(field):
            e(f"{field} is required")
    if m.get("category") not in CATEGORIES:
        e(f"category {m.get('category')!r} not one of {sorted(CATEGORIES)}")
    eq = m.get("exampleQueries", [])
    if not isinstance(eq, list) or len(eq) > 3 or any(not isinstance(q, str) or not q.strip()
                                                       or len(q) > 4096 for q in eq):
        e("exampleQueries must be 0-3 non-empty strings of <=4096 chars")
    for cap in ("apps", "mcpServers", "skills"):
        if not isinstance(m.get(cap), list):
            e(f"{cap} must be present (an empty array when unused)")
    if not any(m.get(cap) for cap in ("apps", "mcpServers", "skills")):
        e("a plugin must contain at least one capability")

    icon = m.get("icon", "")
    if Path(icon).suffix not in ICON_EXT or Path(icon).suffix != Path(icon).suffix.lower():
        e(f"icon {icon!r} must be a lowercase .png/.jpg/.jpeg/.webp")

    referenced = set()
    for rel in [icon, *m.get("apps", []), *m.get("mcpServers", []), *m.get("skills", [])]:
        if not isinstance(rel, str) or rel.startswith("/") or ".." in rel.split("/"):
            e(f"{rel!r} must be a relative path inside the package")
            continue
        referenced.add(rel)
        if not (PLUGIN / rel).is_file():
            e(f"manifest references {rel}, which does not exist")

    for rel in m.get("skills", []):
        p = PLUGIN / rel
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8")
        parts = text.split("---", 2)
        if not text.startswith("---\n") or len(parts) < 3:
            e(f"{rel}: must begin with YAML frontmatter")
            continue
        fm = {k.strip(): v.strip() for k, _, v in
              (line.partition(":") for line in parts[1].strip().splitlines())}
        if fm.get("name") != p.parent.name:
            e(f"{rel}: frontmatter name {fm.get('name')!r} != directory {p.parent.name!r}")
        if not fm.get("description"):
            e(f"{rel}: frontmatter description is empty")
        if not re.fullmatch(r"skills/[A-Za-z0-9._-]+/SKILL\.md", rel):
            e(f"{rel}: skills live at skills/<name>/SKILL.md")

    # Every capability-shaped file must be referenced, and every file must obey the package rules.
    for p in files():
        rel = p.relative_to(PLUGIN).as_posix()
        if p.is_symlink():
            e(f"{rel}: symlinks are rejected")
            continue
        if not PATH_RE.match(rel):
            e(f"{rel}: paths may only use ASCII letters, digits, . _ - and /")
        if len(rel.encode()) > 512 or any(len(seg.encode()) > 128 for seg in rel.split("/")) \
                or rel.count("/") + 1 > 16:
            e(f"{rel}: path too long or too deep")
        if p.stat().st_nlink > 1:
            e(f"{rel}: hardlinks are rejected")
        if p.stat().st_mode & 0o111:
            e(f"{rel}: executable bit set")
        if p.suffix.lower() in INSTALL_SCRIPT_EXT:
            e(f"{rel}: install scripts / binaries are rejected")
        if p.stat().st_size > 16 * 1024 * 1024:
            e(f"{rel}: over 16 MiB")
        if p.name.endswith((".mcp.json", ".app.json")) or p.name == "SKILL.md":
            if rel not in referenced:
                e(f"{rel}: capability file not referenced by the manifest")
        if p.suffix in {".md", ".json", ".txt", ".yml", ".yaml", ".py", ".js"}:
            raw = p.read_bytes()
            if raw.startswith(b"\xef\xbb\xbf"):
                e(f"{rel}: UTF-8 BOM")
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                e(f"{rel}: not UTF-8")
                continue
            if SECRET_RE.search(text):
                e(f"{rel}: looks like it contains a credential")
            if p.suffix == ".json":
                try:
                    if not isinstance(json.loads(text, object_pairs_hook=no_dup_keys), dict):
                        e(f"{rel}: JSON must be an object")
                except ValueError as exc:
                    e(f"{rel}: {exc}")

    regular = [p for p in files() if not p.is_symlink()]
    if len(regular) > 1024:
        e("more than 1,024 files")
    if sum(p.stat().st_size for p in regular) > 64 * 1024 * 1024:
        e("more than 64 MiB uncompressed")
    return errors


def write_zip(out: Path) -> None:
    # No wrapper directory: entries are relative to the plugin root, so
    # `.minimax-plugin/plugin.json` is a top-level entry. Deterministic order and timestamps.
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files():
            rel = p.relative_to(PLUGIN).as_posix()
            info = zipfile.ZipInfo(rel, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, p.read_bytes())
    print(f"wrote {out} ({out.stat().st_size} bytes, {len(files())} files)")


def main() -> int:
    errors = validate()
    for err in errors:
        print(f"FAIL — {err}", file=sys.stderr)
    if errors:
        return 1
    print(f"OK — {PLUGIN.relative_to(ROOT)}/ passes the MiniMax package rules")
    if "--zip" in sys.argv:
        write_zip(Path(sys.argv[sys.argv.index("--zip") + 1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
