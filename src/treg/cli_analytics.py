"""Best-effort anonymous CLI usage, isolated from the server analytics stack."""
from __future__ import annotations

import logging
import os
import platform
import threading
import uuid
from importlib.metadata import version
from pathlib import Path

# Public ingestion token, also published by https://treg.to/meta (not a personal API key).
POSTHOG_KEY = "phc_sAgf5A7TPRRA6faCzuqGVUo8pyb3x5Nq7TJYfn6ZVif9"
POSTHOG_HOST = "https://eu.i.posthog.com"
EXIT_WAIT_SECONDS = 0.3


def _installation_id(config_path: Path) -> str:
    path = config_path.with_name("analytics-id")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x") as file:
            identifier = str(uuid.uuid4())
            file.write(identifier)
            return identifier
    except FileExistsError:
        return str(uuid.UUID(path.read_text().strip()))


def _send(key: str, host: str, config_path: Path, properties: dict) -> None:
    try:
        from posthog import Posthog

        identifier = _installation_id(config_path)
        properties = {**properties, "cli_version": version("tools-registry"),
                      "os": platform.system(), "$process_person_profile": False}
        # Synchronous SDK mode avoids its unbounded atexit flush. Our daemon gets a
        # short exit budget; slow/offline ingestion may drop the event.
        client = Posthog(key, host=host, sync_mode=True, timeout=0.2, max_retries=0,
                         disable_geoip=True, is_server=False, enable_local_evaluation=False)
        logger = logging.Logger("treg.cli.analytics")
        logger.addHandler(logging.NullHandler())
        client.log = logger
        try:
            client.capture("cli_command_completed", distinct_id=identifier, properties=properties)
        finally:
            client.shutdown()
    except Exception:
        pass  # Analytics must never change command output or its exit status.


def track_command(*, command: str, exit_code: int, duration_ms: int,
                  base_url: str, config_path: Path) -> None:
    """Accept only fixed command names and coarse execution metadata, never argv/config."""
    try:
        if os.environ.get("TREG_TELEMETRY", "").lower() in {"0", "false", "off"}:
            return
        if os.environ.get("DO_NOT_TRACK") == "1":
            return
        hosted = base_url.rstrip("/") in {"https://treg.to", "https://treg.superdesign.dev"}
        key = os.environ.get("TREG_CLI_POSTHOG_KEY", POSTHOG_KEY if hosted else "")
        if not key:
            return
        host = os.environ.get("TREG_CLI_POSTHOG_HOST", POSTHOG_HOST)
        worker = threading.Thread(target=_send, args=(key, host, config_path, {
            "command": command, "exit_code": exit_code, "success": exit_code == 0,
            "duration_ms": duration_ms,
        }), daemon=True)
        worker.start()
        worker.join(EXIT_WAIT_SECONDS)
    except Exception:
        pass
