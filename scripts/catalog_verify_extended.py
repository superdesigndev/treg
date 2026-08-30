#!/usr/bin/env python3
"""Live-verify the EXTENDED tier in bulk, under a hard spend cap.

    TREG_CATALOG_CRED='<secret>' uv run python scripts/catalog_verify_extended.py tikhub \
        --budget 1.80 [--platform tiktok] [--limit 50] [--dry-run] [--refresh]

scripts/catalog_verify.py is the CORE-tier tool: a handful of hand-written endpoints, one line of
output each, a human reading every result. That does not survive contact with 1385 machine-
generated entries, where the questions are different — what does this cost, what can I afford to
skip, and how big does the repo get. This script is the extended-tier answer to those:

  * **Spend cap, enforced before the call.** Entries run CHEAPEST FIRST and the running total is
    the sum of the per-endpoint `cost` of the calls that SUCCEEDED (TikHub bills per_success, so a
    failure is free). The next call is only made if its price still fits in `--budget`. Cheapest-
    first is not just safety: it buys the most verified endpoints per dollar, and it means a run
    that stops early stops on the expensive tail, not somewhere arbitrary.
  * **The account's own balance is the ground truth.** The provider's balance is read before and
    after and printed as the real spend, because a rate card can be stale in a way our arithmetic
    cannot detect. Where the provider states a per-call charge in its response (DataForSEO's
    `tasks.0.cost`), that number is also written onto the endpoint as `observed_cost` — what it
    ACTUALLY cost, next to the `cost` rate card that says what it should. Balance arithmetic cannot
    separate this run's spend from anything else touching the same key, so a summed observed_cost
    is the defensible figure for a report.
  * **Examples trimmed far harder than core.** 1385 files at core's 10 KB cap would add ~14 MB of
    JSON to the repo. Here: arrays keep ONE item, strings clip at 200 chars, 2 KB per document.
    An extended example exists to show the response SHAPE; the core tier is where a full-fidelity
    example belongs.
  * **Deterministic and re-runnable.** Results are written back into the yaml — `verified` + a
    `example_response` path on a pass, a one-line `unverified: http <code> …` on a failure. A
    re-run skips whatever already carries `verified` (use `--refresh` to re-test those too), so
    interrupting a run and restarting it resumes rather than re-billing.

Two retry rules, both free because only a successful call is billed:

  * a failure that reads like a flaky upstream ("please retry", a timeout, any 5xx, a 429, a
    transport error) is retried up to --retry-attempts times. This is not politeness, it is the
    single highest-value lever the script has: TikHub's LinkedIn family went from 8% to 90%
    verified across four passes with no change other than being asked again. Conversion per pass
    decays fast (672, +30, +16, +14, +2 on tikhub) — a pass that converts ~2 is convergence, and
    what is left is genuinely broken rather than flaky.
  * a 4xx on an endpoint whose page-size knob we clamped is retried once with the documented
    values, which separates "our test request was wrong" from "this endpoint is broken". If that
    retry passes, the working `test_request` is what gets written back.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "src" / "treg" / "catalog"

# far tighter than catalog_verify.py's core-tier caps — see the module docstring
MAX_STR = 200
MAX_LIST = 1
MAX_BYTES = 2_000

RATE_LIMIT_PER_SEC = 8.0  # provider allows 10/s; leave headroom so we are never the cause of a 429

# Per-call ceiling. Was 60s, which sat right on top of the slowest real endpoint we have measured:
# DataForSEO's /v3/merchant/amazon/products/live/advanced answered in 55.3s on a live probe. A
# timeout gets recorded as the endpoint's verdict, so a margin that thin turns a slow-but-working
# route into an intermittent "unverified" — the failure mode this file exists to avoid.
CALL_TIMEOUT = 180


class Throttle:
    """Global request pacer shared by the worker threads."""

    def __init__(self, per_sec: float) -> None:
        self._gap = 1.0 / per_sec
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            at = max(now, self._next)
            self._next = at + self._gap
        time.sleep(max(0.0, at - now))


MIN_STR = 60  # a string clipped below this stops showing what the field CONTAINS


def trim(node, str_max: int = MAX_STR, depth_max: int = 99, depth: int = 0, keys_max: int = 99):
    if isinstance(node, dict):
        if depth >= depth_max:
            return f"… {{{len(node)} field(s) truncated}}"
        out = {k: trim(v, str_max, depth_max, depth + 1, keys_max)
               for k, v in list(node.items())[:keys_max]}
        if len(node) > keys_max:
            out["…"] = f"{len(node) - keys_max} more field(s) truncated"
        return out
    if isinstance(node, list):
        if depth >= depth_max:
            return f"… [{len(node)} item(s) truncated]"
        out = [trim(v, str_max, depth_max, depth + 1, keys_max) for v in node[:MAX_LIST]]
        if len(node) > MAX_LIST:
            out.append(f"… {len(node) - MAX_LIST} more item(s) truncated")
        return out
    if isinstance(node, str) and len(node) > str_max:
        return node[:str_max] + f"… [{len(node)} chars]"
    return node


def render_example(doc) -> str:
    """Trim `doc` until it serialises under MAX_BYTES. Never byte-slices the JSON.

    Depth is given up before string length. What an extended example is FOR is showing the shape
    of the response — which fields exist and what a value looks like — so clipping every string to
    a dozen characters to preserve six levels of nesting gets the trade backwards: it keeps the
    skeleton and throws away the only part that tells you what the endpoint returns.
    """
    for str_max in (MAX_STR, 120, MIN_STR):
        for depth_max, keys_max in ((99, 99), (6, 40), (5, 25), (4, 15), (3, 10)):
            dump = json.dumps(trim(doc, str_max, depth_max, 0, keys_max), indent=2, ensure_ascii=False)
            if len(dump.encode()) <= MAX_BYTES:
                return dump + "\n"
    return dump + "\n"


def cost_of(ep: dict) -> float:
    """What the next call to this endpoint should be budgeted at.

    The rate card (`cost`) is preferred, but several providers publish prices per API family rather
    than per route and their entries carry none at all — every DataForSEO entry, for one. A missing
    price silently reads as FREE, which makes `--budget` inert: a run can queue the whole file at an
    estimated $0.000 and still spend real money, with only the balance readback noticing afterwards.
    So fall back to `observed_cost`, what the provider charged last time. It is the better number
    anyway — measured rather than transcribed — and it makes the cap bind from the second run on.
    """
    c = ep.get("cost") or {}
    try:
        listed = float(c.get("value") or 0.0)
    except (TypeError, ValueError):
        listed = 0.0
    if listed:
        return listed
    observed = ep.get("observed_cost")
    return float(observed) if isinstance(observed, (int, float)) else 0.0


def auth_headers(service: str, cred: str) -> tuple[dict, dict, str]:
    sys.path.insert(0, str(ROOT / "src"))
    from treg.oauth_providers import REGISTRY

    prov = REGISTRY.get(service)
    if prov is None:
        raise SystemExit(f"unknown provider '{service}'")
    headers, query = {}, {}
    secret = cred
    if prov.token_encode == "base64" and ":" in secret:
        secret = base64.b64encode(secret.encode()).decode()
    if prov.token_location == "query":
        query[prov.token_param] = prov.token_format.format(secret=secret)
    else:
        headers[prov.token_header or "Authorization"] = (
            prov.token_format or "Bearer {secret}").format(secret=secret)
    return headers, query, prov.base_url.rstrip("/")


def join(base: str, path: str) -> str:
    """base_url + a catalog path, without doubling a shared prefix.

    The extended tier stores each route exactly as the provider's spec spells it, and DataForSEO's
    include the `/v3` that its `base_url` already ends with — so a blind join requests
    `/v3/v3/serp/...` and every call 404s. Same fix as catalog_verify.py; a no-op for providers
    whose base_url is a bare host.
    """
    prefix = urlsplit(base).path.rstrip("/")
    if prefix and path.startswith(prefix + "/"):
        path = path[len(prefix):]
    return base + path


# service -> (free route that reports the account balance, dotted path to the number)
BALANCE_ROUTE = {
    "tikhub": ("/api/v1/tikhub/user/get_user_info", "user_data.balance"),
    "dataforseo": ("/v3/appendix/user_data", "tasks.0.result.0.money.balance"),
}


def dig(doc, dotted: str):
    """Walk a dotted path, where a numeric segment indexes a list (`tasks.0.result.0.money`)."""
    cur = doc
    for part in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.lstrip("-").isdigit():
            try:
                cur = cur[int(part)]
            except IndexError:
                return None
        else:
            return None
    return cur


BALANCE_ATTEMPTS = 3


def read_balance(client: httpx.Client, service: str, base: str, headers: dict, query: dict):
    """The account's own balance, or None if the provider won't say.

    Retried, because a balance endpoint under repeated polling is exactly where providers get
    flaky: DataForSEO answers with a null `result` when polled in quick succession. A None here is
    not fatal — the run's own arithmetic still bounds the spend — but it silently removes the only
    independent check on that arithmetic, so it is worth asking more than once.
    """
    route = BALANCE_ROUTE.get(service)
    if not route:
        return None
    for attempt in range(BALANCE_ATTEMPTS):
        try:
            r = client.get(join(base, route[0]), headers=headers, params=query, timeout=30)
            value = dig(r.json(), route[1])
            if isinstance(value, (int, float)):
                return value
        except Exception:
            pass
        time.sleep(1.0 * (attempt + 1))
    return None


def unclamped(ep: dict) -> dict | None:
    """`test_request` with page-size knobs restored to their documented values, or None.

    Used for the single free retry after a 4xx: it tells apart an endpoint that rejects our small
    page size from an endpoint that is actually broken.
    """
    test = json.loads(json.dumps(ep.get("test_request") or {}))
    schema = ((ep.get("input") or {}).get("queryParams") or {})
    changed = False
    for name, value in list((test.get("queryParams") or {}).items()):
        doc_example = (schema.get(name) or {}).get("example")
        if doc_example not in (None, "") and doc_example != value:
            test["queryParams"][name] = doc_example
            changed = True
    return test if changed else None


# TikHub answers a failed SCRAPE with 400 and this text — the request was well-formed, their
# upstream fetch just did not come back. LinkedIn measured ~1-in-3 on first attempt and passed on
# a retry, so a single attempt would have written off 44 working endpoints as broken. Retrying is
# free (nothing is billed unless the call succeeds), which makes "try again" strictly better than
# recording a verdict we know is unreliable.
RETRYABLE = re.compile(r"please retry|timed? ?out|temporarily|try again|too fast", re.I)
RETRY_ATTEMPTS = 3
# TikHub rate-limits PER ROUTE at 1 request/second, independently of the account-wide 10/s that
# RATE_LIMIT_PER_SEC paces. The global throttle cannot help here — it spaces consecutive requests
# across DIFFERENT routes — so any retry, which by definition hits the same route again, must wait
# out that second itself. A sub-second backoff turns a retry into a guaranteed 429, and the 429
# then gets recorded as if the endpoint had failed. That mistake cost 66 endpoints a real verdict
# on the first full run.
PER_ROUTE_GAP = 1.3


def retryable(detail: str, status: int | None) -> bool:
    if status is not None and (status >= 500 or status == 429):
        return True
    if detail.startswith("transport"):
        return True
    return bool(RETRYABLE.search(detail))


def call_with_retries(client: httpx.Client, base: str, ep: dict, test: dict, headers: dict,
                      query: dict, throttle: Throttle, max_attempts: int = RETRY_ATTEMPTS
                      ) -> tuple[bool, str, object, int]:
    """`call`, repeated while the failure looks like a flaky upstream rather than a verdict."""
    attempts = 0
    for attempt in range(max_attempts):
        attempts += 1
        ok, detail, doc, status = call(client, base, ep, test, headers, query, throttle)
        if ok or not retryable(detail, status):
            return ok, detail, doc, attempts
        time.sleep(PER_ROUTE_GAP * (attempt + 1))
    return ok, detail, doc, attempts


def call(client: httpx.Client, base: str, ep: dict, test: dict, headers: dict, query: dict,
         throttle: Throttle) -> tuple[bool, str, object, int | None]:
    path = ep["path"]
    for k, v in (test.get("pathParams") or {}).items():
        path = path.replace("{%s}" % k, str(v))
    params = {**query, **(test.get("queryParams") or {})}
    body = test.get("body")
    form = body if test.get("bodyType") == "form" else None
    if form is not None and query:
        # these routes take the token in the form body; leaving a copy in the query string makes
        # the provider reject the call with a misleading auth error (verified live on Just One API)
        form = {**query, **form}
        params = {k: v for k, v in params.items() if k not in query}
    throttle.wait()
    started = time.monotonic()
    try:
        resp = client.request(ep["method"], join(base, path), params=params, headers=headers,
                              data=form, json=body if body is not None and form is None else None,
                              timeout=CALL_TIMEOUT)
    except httpx.HTTPError as exc:
        return False, f"transport {type(exc).__name__}", None, None
    finally:
        LAST_ELAPSED.seconds = round(time.monotonic() - started, 2)
    doc = None
    try:
        doc = resp.json()
    except ValueError:
        pass
    ok = 200 <= resp.status_code < 300
    # TikHub answers 200 for the HTTP layer and puts the real verdict in the body's `code`
    if ok and isinstance(doc, dict) and isinstance(doc.get("code"), int) and doc["code"] not in (0, 200):
        ok, detail = False, f"http {resp.status_code} body-code {doc['code']}"
    else:
        detail = f"http {resp.status_code}"
    expect = ep.get("expect")
    if ok and expect:
        path = expect["json_path"]
        got = dig(doc, path) if doc is not None else None
        ok = got == expect.get("equals")
        detail += f", {path}={got!r}"
    if not ok:
        if doc is None:
            msg = resp.text or ""
        else:
            msg = (doc.get("detail") if isinstance(doc, dict) else None) or doc
            if isinstance(msg, dict):
                msg = msg.get("message") or msg.get("detail") or ""
        snippet = re.sub(r"\s+", " ", str(msg)).strip()[:80]
        if snippet:
            detail = f"{detail}: {snippet}"
    return ok, detail, doc, resp.status_code


def save(path: Path, data: dict, header: str) -> None:
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=4096,
                          default_flow_style=False)
    path.write_text(header + body)


RESULT_FIELDS = ("verified", "unverified", "skipped", "example_response", "test_request",
                 "observed_cost", "observed_time")


class _Elapsed(threading.local):
    """Per-thread stopwatch for the last request, so `call` keeps its return signature."""
    seconds = None


LAST_ELAPSED = _Elapsed()
# `observed_time` is OUR wall-clock around the request, not a number read out of the response.
# Only DataForSEO reports its own duration, and TikHub's `time` field is a TIMESTAMP
# ("2026-07-27 23:27:48") — an extractor that trusted the field name would write a date string
# into a numeric column. Client-side is also the number that matters here: CALL_TIMEOUT applies to
# our client, so what tells us a route is near the ceiling is how long WE waited. Recorded because
# a timeout is written down as the endpoint's verdict, and DataForSEO's
# /v3/merchant/amazon/products/live/advanced answered in 55.3s and 26.2s on two identical calls —
# a 2x spread that would have made a healthy route intermittently "unverified" under the old 60s.

# service -> how to read what the provider says THIS call cost, from its own response.
# `cost` in the catalog is a rate card: what the price list claims, entered by hand and liable to
# age. `observed_cost` is what the provider actually charged, straight from the response that
# produced the example — so the two can be compared, a run's spend can be summed without balance
# arithmetic (which cannot separate our calls from anyone else's on the same key), and the router
# later has a real per-endpoint price to rank on.
# TikHub is deliberately absent: it publishes no per-call charge in the response body, so its
# entries carry no observed_cost and its rate card stays the only figure available.
OBSERVED_COST = {
    "dataforseo": lambda doc: dig(doc, "tasks.0.cost"),
}


def retrim(service: str) -> int:
    """Re-apply the current size caps to already-captured examples, without calling anything.

    The captured JSON is on disk, so shrinking it further is a local transformation — re-running
    the endpoint to get a smaller file would pay the provider a second time for a response we
    already have. Only genuinely unreadable files are worth a re-capture, and this reports those
    rather than deleting them.

    Trimming is one-way: this cannot restore detail an earlier, coarser pass already dropped. It
    exists to bring old captures under a cap that got stricter, which is the only direction that
    matters for repo size.
    """
    file = CATALOG / f"{service}.extended.yaml"
    eps = (yaml.safe_load(file.read_text()) or {}).get("endpoints") or []
    shrunk = saved = unreadable = 0
    broken: list[str] = []
    for ep in eps:
        rel = ep.get("example_response")
        if not rel:
            continue
        path = CATALOG / rel
        if not path.is_file():
            continue
        before = path.stat().st_size
        if before <= MAX_BYTES:
            continue
        try:
            doc = json.loads(path.read_text())
        except ValueError:
            unreadable += 1
            broken.append(ep["id"])
            continue
        path.write_text(render_example(doc))
        after = path.stat().st_size
        if after < before:
            shrunk += 1
            saved += before - after
    print(f"re-trimmed {shrunk} example(s), reclaimed {saved / 1024:.0f} KB, $0 spent")
    if unreadable:
        print(f"{unreadable} unreadable file(s) need a re-capture: {broken[:5]}", file=sys.stderr)
    over = [ep["id"] for ep in eps if ep.get("example_response")
            and (CATALOG / ep["example_response"]).is_file()
            and (CATALOG / ep["example_response"]).stat().st_size > MAX_BYTES]
    print(f"{len(over)} example(s) still over {MAX_BYTES} B" + (f", e.g. {over[:3]}" if over else ""))
    return 0


def merge_results(service: str, paths: list[Path]) -> int:
    """Fold batch result files into `<service>.extended.yaml`. The ONLY writer of that file.

    Splitting a provider across concurrent agents means splitting the endpoints, not the file: two
    processes rewriting one yaml lose each other's work, and the loss is silent because both
    produce a valid document. So a batch run takes `--out` and writes only its own results, and one
    merge folds them in. Example RESPONSES need no such care — each is its own file named after the
    endpoint that produced it, so disjoint batches never touch the same one.

    A batch may only report on endpoints it was given; an id that is not already in the yaml is
    refused rather than added, because the endpoint list is catalog_ingest.py's to own.
    """
    file = CATALOG / f"{service}.extended.yaml"
    text = file.read_text()
    header = text[: text.index("\nprovider:") + 1] if "\nprovider:" in text else ""
    data = yaml.safe_load(text)
    by_id = {e["id"]: e for e in data["endpoints"]}

    applied, unknown, conflicts = 0, [], []
    seen: dict[str, Path] = {}
    for path in paths:
        payload = json.loads(path.read_text())
        if payload.get("service") != service:
            raise SystemExit(f"{path}: results are for '{payload.get('service')}', not '{service}'")
        for res in payload.get("results") or []:
            eid = res.get("id")
            ep = by_id.get(eid)
            if ep is None:
                unknown.append(eid)
                continue
            if eid in seen:
                conflicts.append(f"{eid} (in {seen[eid].name} and {path.name})")
                continue
            seen[eid] = path
            for field in RESULT_FIELDS:
                if field in res:
                    # an EMPTY value counts as absent, never as a claim. A state key present but
                    # blank passes a "has a state" check while saying nothing, which is worse than
                    # no key at all — the entry looks handled and isn't. (Same bug the dataforseo
                    # run hit when a re-run dropped a curated reason string.)
                    if res[field] is None or res[field] == "":
                        ep.pop(field, None)
                    else:
                        ep[field] = res[field]
            if res.get("verified"):
                ep.pop("untestable", None)
            # exactly one outcome state survives a merge: a batch that reports an endpoint as
            # verified must not leave last run's `unverified` sitting beside it
            states = {"verified", "unverified", "skipped"}
            if states & set(res):
                for stale in states - {s for s in res if res.get(s)}:
                    ep.pop(stale, None)
            applied += 1

    if conflicts:
        # two batches claiming one endpoint means the split was wrong; merging either result would
        # hide that, so refuse and let the caller fix the partition
        raise SystemExit("overlapping batches: " + "; ".join(conflicts[:10]))
    if unknown:
        print(f"warning: {len(unknown)} unknown id(s) ignored, e.g. {unknown[:3]}", file=sys.stderr)
    save(file, data, header)
    print(f"merged {applied} result(s) from {len(paths)} batch file(s) into {file.relative_to(ROOT)}")
    print(f"{sum(1 for e in data['endpoints'] if e.get('verified'))} of {len(data['endpoints'])} verified")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("service")
    ap.add_argument("--budget", type=float, default=1.0, help="max USD to spend this run")
    ap.add_argument("--platform", action="append", help="only these platforms")
    ap.add_argument("--limit", type=int, help="stop after N calls")
    ap.add_argument("--max-cost", type=float, help="skip endpoints priced above this")
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--refresh", action="store_true", help="re-test endpoints already verified")
    ap.add_argument("--dry-run", action="store_true", help="plan and cost only; make no calls")
    ap.add_argument("--out", type=Path, metavar="FILE",
                    help="write results as JSON here instead of touching the yaml (batch mode)")
    ap.add_argument("--merge", type=Path, nargs="+", metavar="FILE",
                    help="fold --out result files into the yaml; makes no calls")
    ap.add_argument("--retry-attempts", type=int, default=RETRY_ATTEMPTS,
                    help="attempts per endpoint when the failure looks transient. Raising this is "
                         "free (only successes bill) and pays off on providers whose scraper is "
                         "flaky rather than broken — TikHub's Douyin/LinkedIn families convert "
                         "steadily across repeated attempts")
    ap.add_argument("--retrim", action="store_true",
                    help="re-apply the size caps to captured examples locally; makes no calls")
    args = ap.parse_args(argv)

    if args.merge:
        return merge_results(args.service, args.merge)
    if args.retrim:
        return retrim(args.service)

    cred = os.environ.get("TREG_CATALOG_CRED")
    if not cred and not args.dry_run:
        print("TREG_CATALOG_CRED not set", file=sys.stderr)
        return 2

    file = CATALOG / f"{args.service}.extended.yaml"
    text = file.read_text()
    header = text[: text.index("\nprovider:") + 1] if "\nprovider:" in text else ""
    data = yaml.safe_load(text)
    eps = data["endpoints"]

    todo = [e for e in eps if e.get("test_request") is not None]
    if args.platform:
        todo = [e for e in todo if e["platform"] in set(args.platform)]
    if not args.refresh:
        todo = [e for e in todo if not e.get("verified")]
    if args.max_cost is not None:
        todo = [e for e in todo if cost_of(e) <= args.max_cost]
    todo.sort(key=lambda e: (cost_of(e), e["platform"], e["path"]))

    print(f"{len(eps)} endpoint(s); {sum(1 for e in eps if e.get('test_request') is not None)} testable; "
          f"{len(todo)} queued this run")
    print(f"queued at list price: ${sum(cost_of(e) for e in todo):.3f}  budget ${args.budget:.2f}")
    if args.dry_run:
        return 0

    headers, query, base = auth_headers(args.service, cred)
    spent = 0.0
    stats = {"pass": 0, "fail": 0, "skipped_budget": 0, "retried": 0}
    touched: set[str] = set()
    lock = threading.Lock()
    throttle = Throttle(RATE_LIMIT_PER_SEC)
    stop = threading.Event()
    calls = 0

    with httpx.Client(timeout=60, follow_redirects=True) as client:
        before = read_balance(client, args.service, base, headers, query)
        print(f"balance before: {before}")

        def work(ep: dict) -> None:
            nonlocal spent, calls
            price = cost_of(ep)
            with lock:
                if stop.is_set():
                    return
                if spent + price > args.budget:
                    # the FOURTH state (convention shared with the dataforseo/justoneapi run): a
                    # usable test request exists and the call was simply never made. Calling this
                    # `unverified` would invent a failure that never happened, and `untestable` would
                    # be a false claim about the endpoint. A `skipped` entry clears with money and a
                    # re-run; the other two need a human.
                    stats["skipped_budget"] += 1
                    if ep.get("verified"):
                        # already proven by an earlier run and merely not RE-checked by this one.
                        # Downgrading it to `skipped` would throw away a real verification, and
                        # emitting both states puts a contradiction in the --out file that a later
                        # --merge would fold into the yaml. Leave it alone and report nothing.
                        return
                    ep["skipped"] = (f"not called — ${price:.4f} would exceed this run's "
                                     f"${args.budget:.2f} budget")
                    ep.pop("unverified", None)
                    touched.add(ep["id"])
                    return
                if args.limit and calls >= args.limit:
                    stop.set()
                    return
                calls += 1
                # reserve the price up front so N concurrent workers cannot jointly overshoot;
                # it is released below if the call fails (failures are not billed)
                spent += price
            ok, detail, doc, tries = call_with_retries(client, base, ep, ep["test_request"],
                                                        headers, query, throttle,
                                                        args.retry_attempts)
            if not ok and detail.startswith("http 4"):
                alt = unclamped(ep)
                if alt:
                    time.sleep(PER_ROUTE_GAP)  # same route again — clear its 1/s window first
                    stats["retried"] += 1
                    ok2, detail2, doc2, _ = call_with_retries(client, base, ep, alt, headers,
                                                             query, throttle, args.retry_attempts)
                    if ok2:
                        ep["test_request"] = alt
                        ok, detail, doc = ok2, detail2, doc2
            with lock:
                if not ok:
                    spent -= price
            if ok and doc is not None:
                rel = f"examples/{ep['id']}.json"
                out = CATALOG / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(render_example(doc))
                ep["verified"] = date.today().isoformat()
                ep["example_response"] = rel
                observed = (OBSERVED_COST.get(args.service) or (lambda _d: None))(doc)
                if isinstance(observed, (int, float)):
                    ep["observed_cost"] = observed
                if isinstance(LAST_ELAPSED.seconds, (int, float)):
                    ep["observed_time"] = LAST_ELAPSED.seconds
                ep.pop("unverified", None)
                ep.pop("skipped", None)
                with lock:
                    touched.add(ep["id"])
                    stats["pass"] += 1
            else:
                # never write a blank state: an empty reason satisfies "has a state" while telling
                # the next reader nothing, so fall back to the bare status rather than to silence
                ep["unverified"] = detail.strip() or "failed with no status or message"
                ep.pop("verified", None)
                ep.pop("example_response", None)
                ep.pop("skipped", None)
                with lock:
                    touched.add(ep["id"])
                    stats["fail"] += 1
            done = stats["pass"] + stats["fail"]
            if done % 50 == 0:
                print(f"  … {done} called, {stats['pass']} pass, ${spent:.3f} spent", flush=True)

        try:
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                list(pool.map(work, todo))
        except KeyboardInterrupt:
            stop.set()
            print("interrupted — writing partial results", file=sys.stderr)

        after = read_balance(client, args.service, base, headers, query)

    if args.out:
        # batch mode: report only what THIS run called, so the merge cannot resurrect a stale
        # verdict for an endpoint another batch owns
        results = [{"id": e["id"], **{f: e.get(f) for f in RESULT_FIELDS}}
                   for e in eps if e["id"] in touched]
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(
            {"service": args.service, "generated": date.today().isoformat(),
             "platforms": sorted({e["platform"] for e in todo}), "results": results},
            indent=2, ensure_ascii=False) + "\n")
    else:
        save(file, data, header)
    print(f"\nPASS {stats['pass']}  FAIL {stats['fail']}  skipped-for-budget {stats['skipped_budget']}"
          f"  retried {stats['retried']}")
    print(f"estimated spend ${spent:.3f}; balance {before} -> {after}"
          + (f" (actual ${before - after:.3f})" if isinstance(before, (int, float))
             and isinstance(after, (int, float)) else ""))
    if args.out:
        print(f"wrote {len(results)} result(s) to {args.out}"
              f" — {file.name} NOT touched; apply with --merge {args.out}")
    else:
        print(f"{sum(1 for e in eps if e.get('verified'))} of {len(eps)} endpoint(s) now verified;"
              f" wrote {file.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
