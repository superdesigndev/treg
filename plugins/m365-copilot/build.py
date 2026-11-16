"""Build the Microsoft 365 Copilot app package (a zip an admin uploads).

    python3 plugins/m365-copilot/build.py --auth-reference-id <id> [--mcp-url URL] [--out FILE]

The auth reference id is the OAuth registration id Microsoft issues for this agent (README.md).
"""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent / "appPackage"


def build(auth_reference_id: str, mcp_url: str, out: Path) -> Path:
    agent = json.loads((HERE / "declarativeAgent.json").read_text())
    # `$[file(...)]` is an Agents Toolkit packaging directive; an uploaded zip needs the text inline.
    agent["instructions"] = (HERE / "instructions.txt").read_text().strip()
    plugin = (HERE / "ai-plugin.json").read_text()
    plugin = plugin.replace("${MCP_URL}", mcp_url).replace("${AUTH_REFERENCE_ID}", auth_reference_id)
    json.loads(plugin)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(HERE / "manifest.json", "manifest.json")
        z.write(HERE / "color.png", "color.png")
        z.write(HERE / "outline.png", "outline.png")
        z.writestr("declarativeAgent.json", json.dumps(agent, indent=2, ensure_ascii=False) + "\n")
        z.writestr("ai-plugin.json", plugin)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--auth-reference-id", required=True)
    ap.add_argument("--mcp-url", default="https://treg.to/mcp/")
    ap.add_argument("--out", type=Path, default=Path("dist/treg-m365-copilot.zip"))
    a = ap.parse_args()
    print(build(a.auth_reference_id, a.mcp_url, a.out))


if __name__ == "__main__":
    main()
