"""Register treg's MCP server into the coding agents on this machine — headless, header-authed.

The sibling of `treg skill bootstrap`. Bootstrap drops a SKILL.md into each agent's skills dir (a
uniform file-drop). MCP is not uniform: each agent keeps its server list in its OWN file and schema,
so this writes each format rather than one shared thing.

**Why a static `Authorization` header and not OAuth here.** treg IS an OAuth authorization server,
and a browser "connect" flow exists — but the whole point of this command is the *headless* path: an
agent (or install.sh) is handed a team-scoped token and wires every agent up with no browser. A
client only falls back to OAuth discovery when it gets a 401; treg returns 200 for a valid header, so
the header is honoured and OAuth never triggers (verified against Claude Code, which otherwise prefers
OAuth — the determinant is "does the server 200 a valid header", which treg does).

**Kept deliberately small and correct over broad.** Only agents whose MCP format is verified are
written: Claude Code via its own `claude mcp add` (it owns its format, and redacts the token in its
output), Cursor and opencode via their documented JSON, Codex as one `[mcp_servers.treg]` table in
its TOML with an inline `http_headers` map (verified live: the tools load with no env var). CLI-light:
stdlib only — no yaml (server-only dep), which is why Hermes is reported as manual rather than
half-written wrong. There is no TOML writer in stdlib either, so the Codex block is appended as text
after any previous treg table is cut out, and `tomllib` proves the result parses before it lands.

The Codex entry once said "set TREG_TOKEN in its environment and use `bearer_token_env_var`". A user
followed that, restarted the Codex app, the variable was gone, treg 401'd and Codex silently dropped
every treg tool; their agent then drove the dashboard through Codex's browser and burnt their daily
Codex allowance on a $0.13 job. Never hand Codex a token by env-var reference.

DeepSeek Harness is manual for a different reason: it has no MCP config file at all. A server there
is a row in a profile's composition layer, and the answer is to install treg's own bundle, which
carries that row — see docs/DSH-PLUGIN.md.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

HOME = Path.home()


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config"))


# name -> how to register. `marker` mirrors agents.py (existence = installed). `kind` picks the writer.
MCP_AGENTS: dict[str, dict] = {
    "claude": {"display": "Claude Code", "kind": "claude_cli",
               "marker": lambda: (HOME / ".claude").exists() or bool(shutil.which("claude"))},
    "cursor": {"display": "Cursor", "kind": "json",
               "path": lambda: HOME / ".cursor" / "mcp.json", "root": "mcpServers",
               "entry": lambda url, tok: {"url": url, "headers": {"Authorization": f"Bearer {tok}"}},
               "marker": lambda: (HOME / ".cursor").exists()},
    "codex": {"display": "Codex", "kind": "toml",
              "path": lambda: HOME / ".codex" / "config.toml",
              "marker": lambda: (HOME / ".codex").exists() or bool(shutil.which("codex"))},
    "opencode": {"display": "opencode", "kind": "json",
                 "path": lambda: _config_home() / "opencode" / "opencode.json", "root": "mcp",
                 "entry": lambda url, tok: {"type": "remote", "url": url, "enabled": True,
                                            "headers": {"Authorization": f"Bearer {tok}"}},
                 "marker": lambda: (_config_home() / "opencode").exists()},
}

# Detected but not auto-written — the format isn't safely expressible from the light CLI. We tell the
# user exactly how instead of writing a config we haven't runtime-verified.
MANUAL_AGENTS: dict[str, dict] = {
    "hermes": {"display": "Hermes", "marker": lambda: (HOME / ".hermes").exists(),
               "how": "add to ~/.hermes/config.yaml under mcp_servers: a `url` + `headers: "
                      "{Authorization: \"Bearer <token>\"}` entry."},
    "dsh": {"display": "DeepSeek Harness", "marker": lambda: (HOME / ".dsh").exists()
            or bool(shutil.which("dsh")),
            "how": "dsh has no MCP config file to write — a server is a row in a PROFILE's config "
                   "layer. Install the bundle instead, which carries that row: `dsh plugin "
                   "--profile <name> add github:superdesigndev/treg`. The row stays disabled until "
                   "TREG_TOKEN is in the environment dsh starts in, so export it and restart dsh."},
    "openclaw": {"display": "OpenClaw", "marker": lambda: (HOME / ".openclaw").exists()
                 or bool(shutil.which("openclaw")),
                 "how": "run `openclaw mcp add --transport http treg <url> --header "
                        "\"Authorization: Bearer <token>\"`."},
}


def _installed(meta: dict) -> bool:
    try:
        return bool(meta["marker"]())
    except Exception:  # noqa: BLE001 — a marker that can't be evaluated is "not installed"
        return False


def _write_private(path: Path, text: str) -> None:
    """Atomically replace `path` with `text`, 0600 from the first byte — the file holds a bearer token.
    mkstemp creates a random, exclusive same-directory file; fchmod makes it 0600 on POSIX even under
    a maximally restrictive umask, before any token bytes are written. Keeping it beside the
    destination preserves os.replace's atomic same-filesystem guarantee and avoids the fixed `.tmp`
    name's symlink and concurrent-writer hazards. Windows has no POSIX mode guarantee; its ACL is
    inherited from the config directory."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd: int | None = None
    tmp_path: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        tmp_path = Path(tmp_name)
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        handle = os.fdopen(fd, "w", encoding="utf-8")
        fd = None
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)


def _write_json_agent(meta: dict, name: str, url: str, token: str) -> tuple[str, str]:
    """Merge one server entry into an agent's JSON config, preserving everything else. Returns
    (status, detail). Idempotent: re-running overwrites just our entry."""
    path: Path = meta["path"]()
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(data, dict):
            return "error", f"{path} is not a JSON object"
        root = meta["root"]
        servers = data.get(root)
        if not isinstance(servers, dict):
            servers = {}
        servers[name] = meta["entry"](url, token)
        data[root] = servers
        _write_private(path, json.dumps(data, indent=2) + "\n")
        return "ok", str(path)
    except Exception as exc:  # noqa: BLE001 — report, never crash the whole install
        return "error", f"{path}: {exc}"


def _write_toml_agent(meta: dict, name: str, url: str, token: str) -> tuple[str, str]:
    """Codex: one `[mcp_servers.<name>]` table with an inline `http_headers` map. Stdlib has a TOML
    reader but no writer, so the previous `[mcp_servers.<name>]` table (ours, or a hand-written
    `bearer_token_env_var` one) is cut out as text — header line through the line before the next
    table header — and a fresh table appended. `tomllib` must parse the result and show exactly our
    entry, or nothing is written: a file we cannot prove correct is worse than the how-to."""
    path: Path = meta["path"]()
    try:
        import tomllib
        old = path.read_text() if path.exists() else ""
        tomllib.loads(old)  # a config Codex can't read is not ours to edit
        header = f"[mcp_servers.{name}]"
        # Cut our table, and any other table pointing at this same MCP url under another name — an
        # agent wiring Codex by hand tends to leave a `treg-to_mcp` twin with the token in the wrong
        # field, which Codex then shows as a dead server beside the working one.
        stale = {name} | {k for k, v in tomllib.loads(old).get("mcp_servers", {}).items()
                          if isinstance(v, dict) and v.get("url") == url}
        kept = old
        for k in stale:
            kept = re.sub(rf"(?ms)^\[mcp_servers\.{re.escape(k)}\]\n.*?(?=^\[|\Z)", "", kept)
        kept = kept.rstrip()
        block = (f"{header}\nurl = {json.dumps(url)}\n"
                 f"http_headers = {{ \"Authorization\" = {json.dumps('Bearer ' + token)} }}\n")
        new = (kept + "\n\n" if kept else "") + block
        servers = tomllib.loads(new)["mcp_servers"]
        if (servers[name] != {"url": url, "http_headers": {"Authorization": f"Bearer {token}"}}
                or any(k != name and isinstance(v, dict) and v.get("url") == url
                       for k, v in servers.items())):
            return "error", f"{path}: a sub-table of {header} survived; edit it by hand"
        _write_private(path, new)
        return "ok", str(path)
    except Exception as exc:  # noqa: BLE001 — report, never crash the whole install
        return "error", f"{path}: {exc}"


def _write_claude(name: str, url: str, token: str) -> tuple[str, str]:
    """Register with Claude Code through its OWN CLI — it owns its config format and redacts the token
    in its output. Remove-then-add makes it idempotent (a plain add errors if the name exists)."""
    claude = shutil.which("claude")
    if not claude:
        return "skipped", "the `claude` CLI is not on PATH"
    try:
        # `--scope user` = available across ALL the user's projects, not the default `local` (which
        # pins it to the current directory). This is a per-machine setup, not a per-repo one.
        subprocess.run([claude, "mcp", "remove", "--scope", "user", name],
                       capture_output=True, text=True, timeout=30)
        r = subprocess.run(
            [claude, "mcp", "add", "--scope", "user", "--transport", "http", name, url,
             "--header", f"Authorization: Bearer {token}"],
            capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return "error", (r.stderr or r.stdout or "claude mcp add failed").strip()[:200]
        return "ok", "via `claude mcp add --scope user`"
    except Exception as exc:  # noqa: BLE001
        return "error", str(exc)


def install_mcp(*, base_url: str, token: str, server_name: str = "treg",
                only: list[str] | None = None) -> dict:
    """Register the treg MCP server into every supported, installed agent on this machine.

    Returns {results: [(agent_display, status, detail)], manual: [(display, how)], mcp_url}.
    `status` ∈ ok | skipped | error. `only` restricts to named agents; None means all detected.

    `only=[]` means NONE, not all. The original `if only and …` guard read an empty list as "no
    filter", so a test that passed `only=[]` expecting a no-op wrote its dummy token into the
    developer's REAL Claude/Cursor/opencode configs on every suite run — which is exactly how a
    literal `Bearer K` ended up breaking every MCP call on a dev machine, days later, looking like
    a provider outage."""
    url = base_url.rstrip("/") + "/mcp/"
    results: list[tuple[str, str, str]] = []
    for key, meta in MCP_AGENTS.items():
        if only is not None and key not in only:
            continue
        if not _installed(meta):
            continue
        if meta["kind"] == "claude_cli":
            status, detail = _write_claude(server_name, url, token)
        elif meta["kind"] == "toml":
            status, detail = _write_toml_agent(meta, server_name, url, token)
        else:
            status, detail = _write_json_agent(meta, server_name, url, token)
        results.append((meta["display"], status, detail))
    manual = [(m["display"], m["how"]) for k, m in MANUAL_AGENTS.items()
              if (only is None or k in only) and _installed(m)]
    return {"results": results, "manual": manual, "mcp_url": url}
