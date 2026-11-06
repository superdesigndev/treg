"""A real registry on this machine for end-to-end CLI runs: `python -m treg` on a fresh sqlite
database in dev mode, one signed-up team, and an echo upstream registered as that team's own tool.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl

import httpx

from .cli_go_harness import ROOT

ADMIN_TOKEN = "admin-" + secrets.token_hex(8)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EchoUpstream:
    """An upstream that answers with what it received; `/status/<n>` answers that status, `/text`
    plain text and `/bytes` binary."""

    def __init__(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                parts = urlsplit(self.path)
                if parts.path == "/text":
                    self._send(200, "text/plain; charset=utf-8", "plain answer\n".encode())
                    return
                if parts.path == "/bytes":
                    self._send(200, "application/octet-stream", bytes(range(256)))
                    return
                status = 200
                if parts.path.startswith("/status/"):
                    status = int(parts.path.rsplit("/", 1)[1])
                echo = {"method": self.command, "path": parts.path, "query": parse_qsl(parts.query, keep_blank_values=True),
                        "authorization": self.headers.get("Authorization"),
                        "content_type": self.headers.get("Content-Type"),
                        "body": body.decode("utf-8", "replace")}
                self._send(status, "application/json", json.dumps(echo).encode())

            def _send(self, status, ctype, data):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # Registration refuses loopback hosts; the trailing-dot spelling of localhost is a DNS name
        # to that check and still resolves here, and the test registry runs without the call-time check.
        self.url = f"http://localhost.:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


@dataclass
class Team:
    email: str
    identity_token: str
    token: str
    slug: str
    org_id: int


class LiveRegistry:
    def __init__(self, tmp: Path, extra_env: dict[str, str] | None = None,
                 catalog_patch: dict | None = None) -> None:
        self.port = _free_port()
        self.url = f"http://localhost:{self.port}"
        key = subprocess.run([sys.executable, "-m", "treg", "keygen"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout.strip()
        env = {k: v for k, v in os.environ.items() if not k.startswith("TREG_")}
        env |= {
            "PORT": str(self.port),
            "TREG_DATABASE_URL": f"sqlite+aiosqlite:///{tmp / 'registry.db'}",
            "TREG_PUBLIC_URL": self.url,
            "TREG_EMAIL_DEV_MODE": "true",
            "TREG_SECRET_KEY": key,
            "TREG_SESSION_SECRET": secrets.token_hex(24),
            "TREG_PROXY_SSRF_CHECK": "false",
            "TREG_ADMIN_TOKEN": ADMIN_TOKEN,
        } | (extra_env or {})
        command = [sys.executable, "-m", "treg"]
        if catalog_patch is not None:
            env["TREG_TEST_CATALOG_PATCH"] = json.dumps(catalog_patch)
            command = [sys.executable, str(Path(__file__).with_name("cli_go_boot.py"))]
        self.log = (tmp / "registry.log").open("wb")
        self.proc = subprocess.Popen(command, cwd=ROOT, env=env,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"the registry exited: {(tmp / 'registry.log').read_text()[-2000:]}")
            try:
                if httpx.get(f"{self.url}/health", timeout=2).status_code < 500:
                    break
            except httpx.HTTPError:
                time.sleep(0.3)
        else:
            raise RuntimeError("the registry did not come up")
        self.http = httpx.Client(base_url=self.url, headers={"X-Treg-Key-Protocol": "1"}, timeout=30)

    def signup(self, email: str, team: str) -> Team:
        started = self.http.post("/auth/email/start", json={"email": email})
        started.raise_for_status()
        proof = self.http.post("/auth/email/verify", json={"email": email, "code": started.json()["dev_code"]})
        proof.raise_for_status()
        identity = proof.json()["token"]
        created = self.http.post("/orgs", json={"name": team}, headers={"X-Treg-Token": identity})
        created.raise_for_status()
        org = created.json()
        slug = org.get("slug") or org["org"]
        # A bootstrap token cannot mint a Default key; the verified browser session can.
        key = self.http.get("/auth/cli-token", headers={"X-Treg-Org": slug})
        key.raise_for_status()
        self.http.cookies.clear()
        orgs = self.http.get("/orgs", headers={"X-Treg-Token": identity}).json()
        org_id = next(o["org_id"] for o in orgs if o["slug"] == slug)
        return Team(email, identity, key.json()["token"], slug, org_id)

    def credit(self, team: Team, usd: float) -> None:
        r = self.http.post(f"/admin/orgs/{team.org_id}/credit", headers={"X-Treg-Token": ADMIN_TOKEN},
                           json={"amount_usd": usd, "ref": secrets.token_hex(6), "reason": "cli parity tests"})
        r.raise_for_status()

    def own_tool(self, team: Team, name: str, base_url: str) -> None:
        h = {"X-Treg-Token": team.token}
        secret = self.http.post("/secrets", headers=h, json={"name": f"{name}-key", "value": "sekret", "kind": "env"})
        secret.raise_for_status()
        tool = self.http.post("/tools", headers=h, json={
            "name": name, "base_url": base_url, "secret_id": secret.json()["id"], "injector": "env",
            "auth_in": "header", "auth_name": "Authorization", "auth_format": "Bearer {secret}",
            "secret_field": "access_token"})
        tool.raise_for_status()

    def close(self) -> None:
        self.http.close()
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()
