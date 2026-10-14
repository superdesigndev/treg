#!/usr/bin/env python3
"""Live-verify catalog endpoints and capture example responses.

Usage (credential comes ONLY from the environment — never from a file or argv):
    TREG_CATALOG_CRED='<secret>' uv run python scripts/catalog_verify.py <service> [--id <endpoint-id>] [--write]
    TREG_CATALOG_CRED='<secret>' uv run python scripts/catalog_verify.py <service> --extended [--id …]

For each endpoint in src/treg/catalog/<service>.yaml it sends `test_request` to base_url+path with
the provider's auth shape and required protocol headers (taken from treg.oauth_providers —
token_header/format/location/encode/required_headers),
checks `expect` (default: HTTP 2xx), and with --write saves a truncated example response to
src/treg/catalog/examples/<endpoint-id>.json. Prints one PASS/FAIL line per endpoint.

`--extended` reads <service>.extended.yaml instead, to REPLAY what a bulk verification run already
stamped there — it is a re-verifier, not the bulk runner (that is catalog_verify_extended.py). Three
differences the extended tier forces, all no-ops for core files:
  * an entry without a `test_request` is skipped rather than called with no parameters;
  * a leading duplicate of base_url's own path is stripped from `path`, because the extended tier
    stores routes exactly as the provider's spec spells them and DataForSEO's include the /v3 that
    base_url already ends with;
  * `test_request.bodyType: form` sends the body as a form AND moves a query-param credential into
    it — Just One API's POST routes read the token from the body and reject it in the query.

It does NOT stamp `verified:` in the YAML — the curator does that, only for PASSes, so a stamp is
always a human-reviewed claim. Truncation: arrays -> first 2 items, strings -> 500 chars, and a
whole-document cap; the curator must still read every example for PII before committing.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "src" / "treg" / "catalog"
MAX_STR = 500
MAX_LIST = 2
MAX_BYTES = 10_000


def truncate(node, depth=0):
    if isinstance(node, dict):
        return {k: truncate(v, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        out = [truncate(v, depth + 1) for v in node[:MAX_LIST]]
        if len(node) > MAX_LIST:
            out.append(f"… {len(node) - MAX_LIST} more item(s) truncated")
        return out
    if isinstance(node, str) and len(node) > MAX_STR:
        return node[:MAX_STR] + f"… [{len(node)} chars total]"
    return node


def _shrink_strings(node, limit: int):
    if isinstance(node, dict):
        return {k: _shrink_strings(v, limit) for k, v in node.items()}
    if isinstance(node, list):
        return [_shrink_strings(v, limit) for v in node]
    if isinstance(node, str) and len(node) > limit:
        return node[:limit] + "…"
    return node


def dig(doc, dotted: str):
    cur = doc
    for part in dotted.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("service")
    ap.add_argument("--id", action="append", help="only these endpoint ids")
    ap.add_argument("--write", action="store_true", help="write examples/<id>.json for passes")
    ap.add_argument("--extended", action="store_true",
                    help="verify <service>.extended.yaml instead of the curated <service>.yaml "
                         "(only its entries that carry a test_request are callable)")
    ap.add_argument("--via-treg", metavar="TOOL",
                    help="route through a treg registry's /call proxy (tool name) using ~/.treg/config.json "
                         "instead of a raw credential — for OAuth providers whose token treg holds")
    args = ap.parse_args()

    cred = os.environ.get("TREG_CATALOG_CRED")
    if not cred and not args.via_treg:
        print("TREG_CATALOG_CRED not set (or use --via-treg <tool>)", file=sys.stderr)
        return 2

    sys.path.insert(0, str(ROOT / "src"))
    from treg.oauth_providers import REGISTRY

    prov = REGISTRY.get(args.service)
    if prov is None:
        print(f"unknown provider '{args.service}'", file=sys.stderr)
        return 2
    suffix = ".extended.yaml" if args.extended else ".yaml"
    data = yaml.safe_load((CATALOG / f"{args.service}{suffix}").read_text())

    headers, base_query, call_base = {}, {}, None
    if args.via_treg:
        cfg = json.loads((Path.home() / ".treg" / "config.json").read_text())
        call_base = cfg["base_url"].rstrip("/") + f"/call/{args.via_treg}"
        headers["X-Treg-Token"] = cfg["token"]  # treg's own auth header, not Bearer
        if cfg.get("active_org"):
            headers["X-Treg-Org"] = str(cfg["active_org"])
    else:
        secret = cred
        if prov.token_encode == "base64" and ":" in secret:
            # curator may paste the raw login:password; the registry stores it base64ed
            secret = base64.b64encode(secret.encode()).decode()
        if prov.token_location == "query":
            base_query[prov.token_param] = prov.token_format.format(secret=secret)
        else:
            headers[prov.token_header or "Authorization"] = (prov.token_format or "Bearer {secret}").format(secret=secret)
        headers.update(dict(prov.required_headers))

    failures = 0
    today = date.today().isoformat()
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        for ep in data.get("endpoints", []):
            eid = ep["id"]
            if args.id and eid not in args.id:
                continue
            treq = ep.get("test_request")
            if not treq:
                continue  # extended entries without one were never callable; nothing to replay
            path = ep["path"]
            for k, v in (treq.get("pathParams") or {}).items():
                path = path.replace("{%s}" % k, str(v))
            base = (call_base or prov.base_url.rstrip("/"))
            # the extended tier stores each route exactly as the provider's spec spells it, which
            # for DataForSEO includes the /v3 that base_url already ends with — joining blindly
            # would request /v3/v3/…
            prefix = urlsplit(base).path.rstrip("/")
            if prefix and path.startswith(prefix + "/"):
                path = path[len(prefix):]
            url = base + path
            params = {**base_query, **(treq.get("queryParams") or {})}
            body = treq.get("body")
            form = body if treq.get("bodyType") == "form" else None
            if form is not None and base_query:
                # form-bodied routes read the credential from the body; leaving it in the query
                # as well makes the provider reject the call (verified live on Just One API)
                form = {**base_query, **form}
                params = {k: v for k, v in params.items() if k not in base_query}
            try:
                resp = client.request(ep["method"], url, params=params, headers=headers,
                                      data=form,
                                      json=body if body is not None and form is None else None)
            except httpx.HTTPError as exc:
                print(f"FAIL {eid} — transport error: {exc}")
                failures += 1
                continue

            ok = 200 <= resp.status_code < 300
            detail = f"http {resp.status_code}"
            doc = None
            try:
                doc = resp.json()
            except ValueError:
                pass
            expect = ep.get("expect")
            if ok and expect and doc is not None:
                got = dig(doc, expect["json_path"])
                ok = got == expect.get("equals")
                detail += f", {expect['json_path']}={got!r}"
            if ok:
                note = ""
                if args.write and doc is not None:
                    ex_rel = ep.get("example_response") or f"examples/{eid}.json"
                    out = CATALOG / ex_rel
                    out.parent.mkdir(parents=True, exist_ok=True)
                    # shrink recursively until under the cap — never byte-slice serialized JSON
                    # (a mid-token slice writes an unparseable example file)
                    shrunk, max_str = truncate(doc), MAX_STR
                    dump = json.dumps(shrunk, indent=2, ensure_ascii=False)
                    while len(dump.encode()) > MAX_BYTES and max_str > 15:
                        max_str //= 4
                        shrunk = _shrink_strings(shrunk, max_str)
                        dump = json.dumps(shrunk, indent=2, ensure_ascii=False)
                    out.write_text(dump + "\n")
                    note = f" -> {ex_rel}"
                print(f"PASS {eid} ({detail}){note}  [stamp verified: {today}]")
            else:
                snippet = (resp.text or "")[:160].replace("\n", " ")
                print(f"FAIL {eid} — {detail}: {snippet}")
                failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
