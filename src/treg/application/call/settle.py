"""Settlement, response buffering, and completion helpers for proxied calls."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

from sqlalchemy import update
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from ... import adsconv, catalog_store, ledger
from ...domain.capacity import marks as capacity_marks
from ...domain.capacity import overflow_spend as overflow_spend_ledger
from ...domain.capacity import signatures as capacity_signatures
from ...db import session_maker
from ...models import Org
from ...timeutil import utcnow_naive as _utcnow_naive
from .idempotency import _release_idempotent_claim
from .resolve import MarketplaceCall, _usd_to_micro
from .types import UpstreamResponse


# 4xx statuses that mean "the provider did not serve this, and it is NOT the caller's input" — our
# credential was rejected, exhausted, throttled, or the request timed out. The provider bills nothing
# for these, so neither may we: charging here would pass OUR expired or over-quota platform key on to
# a team as real spend, and for a builder reselling treg it would land on their end customers' bills.
# 403 is deliberately included even though some providers use it for a genuinely caller-driven
# "resource not accessible": when it is unclear whether the provider charged us, the safe direction
# is not to charge. Absorbing a rare few micro-USD is recoverable; over-billing out of an append-only
# ledger is not.
_NOT_THE_CALLERS_FAULT = frozenset({401, 402, 403, 405, 407, 408, 429})


def _platform_billable(status_code: int, cost_type: str) -> bool:
    """Does a response with this status cost us money? (plan §2.2)
      2xx                        → yes, the provider served it.
      4xx                        → only under `per_call`, and only when the rejection is about the
                                   CALLER'S INPUT (400/404/422 …): the provider charges for accepting
                                   such a request, so it is on the caller. A credential/quota refusal
                                   (`_NOT_THE_CALLERS_FAULT`) is on us and is never billed — a 405
                                   rejects the method OUR catalog selected, while a 429 on a
                                   SHARED-plan key is treg's own saturation. Billing either would
                                   charge teams for our metadata or congestion. Under
                                   `per_result`/`per_success` a rejected request produced nothing.
      5xx / 3xx / network error  → no. An upstream failure is never billed to the caller.
    """
    if 200 <= status_code < 300:
        return True
    if 400 <= status_code < 500:
        return cost_type == "per_call" and status_code not in _NOT_THE_CALLERS_FAULT
    return False


_PLATFORM_BODY_MAX = 8 * 1024 * 1024  # buffer ceiling for a metered response (API JSON, not downloads)
def _brightdata_record_count(body: bytes) -> int | None:
    """How many RECORDS a Bright Data Web Scraper response delivered, or None for "settle at the
    estimate". Bright Data bills $1.50/1000 records *delivered* and reports no charge field, so the
    response body is the only bill we will ever see. Counting it is what closed the 39x gap found
    2026-08-24: $13.61 consumed upstream in three weeks vs $0.35 billed, because a per_result call
    always settled as ONE record — a Google Play reviews job that delivered ~6,000 records billed
    $0.0015.

    Shapes, per docs + live traffic:
      - sync /scrape and /snapshot downloads, format=json → a JSON ARRAY, one element per record;
      - the >60s sync fallback and /trigger → a JSON OBJECT carrying `snapshot_id` — zero records
        HERE; the job's records bill when the snapshot is downloaded (its catalog entry is priced
        per_result for exactly that reason);
      - format=ndjson → one JSON object per line; format=csv → header line + one line per record.
    A body that STARTS like JSON but does not parse is treated as truncated (the metered buffer
    caps at _PLATFORM_BODY_MAX and drops the tail) → None, settle at the estimate, never a
    line-count guess over a partial payload. Any other unrecognised shape → None for the same
    reason: when we cannot count, the estimate is the honest number."""
    if body[:2] == b"\x1f\x8b":  # compress=true gzips the download — we can't count, estimate wins
        return None
    text = body.decode("utf-8", "replace").strip()
    if not text:
        return None
    try:
        doc = json.loads(text)
    except ValueError:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        try:  # ndjson: every line is its own record — EVERY line must parse, or it isn't ndjson
            for ln in lines:
                json.loads(ln)
            return len(lines)
        except ValueError:
            pass
        if text[0] in "[{":  # JSON that broke mid-stream: the 8MB buffer truncated it
            return None
        return len(lines) - 1 if len(lines) > 1 else None  # csv: header + rows
    if isinstance(doc, list):
        return len(doc)
    if isinstance(doc, dict):
        # Zero records delivered, whatever the object says: the async handoff (`snapshot_id` — the
        # records bill at the snapshot download), an early download's {"status": "running"}, or any
        # other envelope. Pay-per-success means an answer with no records costs nothing.
        return 0
    return None

def _observed_cost_micro(mk: MarketplaceCall, body: bytes, headers=None) -> int | None:
    """The provider's OWN reported charge for this call, in micro-USD, or None when it doesn't say.

    For an oauth-billed `per_result` call (X reads), the response body IS the bill: X charges per
    resource returned, so counting `data` beats trusting the estimate — a timeline asked for 100
    posts that returned 7 settles at 7, and an empty page settles at zero. The count is capped at
    the reserved estimate's row assumption only implicitly (a bigger-than-asked response charges
    more, which `ledger.settle` handles as an overrun).

    Several providers volunteer the number, in different denominations:
      - dataforseo: a top-level `cost` in USD — including 0 when it decided not to charge (a free
        route, or a request it rejected before metering). That zero is real information and settles the
        call at zero, which is why the test is `>= 0` and not truthiness.
      - scrapecreators (`credits_charged`), akta and leadmagic (`credits_consumed`): provider
        credits, converted through the provider's credit rate (fx.yaml) — the same conversion
        `cost_view` uses, so a settle can't disagree with the catalog's price. Akta is the one that
        NEEDS this: its enrich route is priced per SECTION requested and its news route adds a
        per-article rider, so the catalog's single estimate can only be an upper bound — the actual
        charge lives here. LeadMagic answers a miss with 2xx and `credits_consumed: 0` (observed at
        verify time), so honouring the field is what keeps a free miss from billing the estimate;
        it also reports fractions (email verify is 0.25).
      - lusha: `billing.creditsCharged`, one level down — the same reported-credits contract,
        including 0 on a 2xx miss (the captured people.enrich example IS one) and the 2-credit
        company enrich. Converted through the lusha rate like the others.
      - apollo: DERIVED, not reported. Apollo answers a miss with 2xx (`organization: null` on
        enrich, an empty `organizations` page on search) and charges nothing for it, so status-based
        billing alone would bill the caller for a response Apollo gave away. The body says whether
        the charged thing came back; when it didn't, the call settles at 0.
      - hunter (domain search): DERIVED too, and for the opposite reason — its price is not
        per row but one whole SEARCH credit per 10 emails returned, rounded up, with an empty
        domain free. `data.emails` is the only place that number exists.
      - hunter (email finder): DERIVED, the flat case — one whole SEARCH credit when an email is
        found, nothing on a miss ("a miss is free", per Hunter's own pricing), yet a miss still
        answers HTTP 200, so the estimate billed the full credit for a name Hunter had nothing on.
      - tikhub: REPORTED in prose rather than a number. Every envelope says whether the call is
        billed; only the explicit no-charge phrasing settles at zero, because TikHub really does
        charge for a 2xx whose payload is an embedded error (verified live 2026-07-30 — see
        docs/context/architecture/catalog.md, "the provider decides what counts as success").

      - exa: REPORTED in dollars, `costDollars.total` on every 2xx body (same contract as
        dataforseo's `cost`) — the only place the per-result and per-content riders exist.
      - signaliz Company Signals: REPORTED in provider credits as `credits_used` (with
        `credits_charged` on dry-run responses). A fresh PAYG lookup can use up to three credits,
        while a cache hit or an included-plan call reports zero; settling the response is what
        releases the conservative three-credit hold correctly.

    Everyone else settles at the estimate. This is the same signal the catalog's `observed_cost`
    harvests, which is what lets phase 5's drift detector compare the two numbers directly."""
    provider = mk.provider
    catalog = catalog_store.load()
    ep = catalog.by_id.get(mk.endpoint_id)
    cost = catalog.cost_view(ep.get("cost"), provider) if ep else None
    if cost and cost.get("settle") == "base" and cost.get("usd") is not None:
        # The reserve can include documented request riders while the observed settlement remains
        # the catalog base. Aviato simple search earned this rule from two multi-row live probes:
        # enrich=true returned only id rows and charged the same 0.25-credit base both times.
        return _usd_to_micro(float(cost["usd"]))
    if provider == "crustdata" and headers is not None:
        raw = headers.get("x-credits-used")
        rate = catalog_store.load().credit_rates.get("crustdata")
        try:
            credits = float(raw)
        except (TypeError, ValueError):
            credits = -1
        if credits >= 0 and rate:
            return _usd_to_micro(credits * rate)
    if not body:
        return None
    if provider == "brightdata" and mk.cost_type == "per_result" and mk.unit_micro > 0:
        # DERIVED by counting records — Bright Data's bill is per record delivered and the body is
        # the only place that number exists (see _brightdata_record_count for the shapes).
        n = _brightdata_record_count(body)
        return None if n is None else n * mk.unit_micro
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if provider == "aviato" and mk.endpoint_id == "aviato.people.enrich.bulk":
        if isinstance(doc, list) and mk.unit_micro > 0:
            return sum(item is not None for item in doc) * mk.unit_micro
        return None
    if not isinstance(doc, dict):
        return None
    if provider == "aviato" and mk.endpoint_id == "aviato.companies.enrich.bulk":
        rows = doc.get("companies")
        if isinstance(rows, list) and mk.unit_micro > 0:
            return sum(item is not None for item in rows) * mk.unit_micro
        return None
    if provider == "aviato" and cost and cost.get("settle") == "modifiers" and mk.unit_micro > 0:
        # The request-time unit excludes catalog modifiers marked reserve_only. Bulk routes above
        # multiply that unit by successful rows; a single route settles one such unit.
        return mk.unit_micro
    if mk.billed_oauth and mk.cost_type == "per_result" and mk.unit_micro > 0:
        data = doc.get("data")
        n = len(data) if isinstance(data, list) else (1 if data else 0)
        return n * mk.unit_micro
    if provider == "dataforseo":
        cost = doc.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
            return int(cost * 1_000_000 + 0.5)
        return None
    if provider == "exa":
        # REPORTED in dollars: every Exa response carries `costDollars.total` — the search base,
        # the per-result rider beyond 10, deep-mode uplifts and each contents type summed (verified
        # live 2026-08-27: 20 results → 0.016, highlights+summary → 0.002). The catalog holds the
        # base price; this is what makes a 20-result search settle at what Exa actually charged.
        cost = doc.get("costDollars")
        total = cost.get("total") if isinstance(cost, dict) else None
        if isinstance(total, (int, float)) and not isinstance(total, bool) and total >= 0:
            return int(total * 1_000_000 + 0.5)
        return None
    if provider in ("scrapecreators", "akta", "leadmagic"):
        credits = doc.get("credits_charged" if provider == "scrapecreators" else "credits_consumed")
        rate = catalog_store.load().credit_rates.get(provider)
        if isinstance(credits, (int, float)) and not isinstance(credits, bool) and credits >= 0 and rate:
            return int(credits * rate * 1_000_000 + 0.5)
        return None
    if provider == "signaliz" and mk.endpoint_id == "signaliz.companies.news":
        credits = doc.get("credits_used")
        if not isinstance(credits, (int, float)) or isinstance(credits, bool):
            credits = doc.get("credits_charged")
        rate = catalog_store.load().credit_rates.get("signaliz")
        if isinstance(credits, (int, float)) and not isinstance(credits, bool) and credits >= 0 and rate:
            return int(credits * rate * 1_000_000 + 0.5)
        return None
    if provider == "lusha":
        billing = doc.get("billing")
        credits = billing.get("creditsCharged") if isinstance(billing, dict) else None
        rate = catalog_store.load().credit_rates.get("lusha")
        if isinstance(credits, (int, float)) and not isinstance(credits, bool) and credits >= 0 and rate:
            return int(credits * rate * 1_000_000 + 0.5)
        return None
    if provider == "hunter" and mk.endpoint_id == "hunter.companies.emails":
        # DERIVED, like apollo. Hunter's domain search does not bill per row at all: it takes ONE
        # whole search credit per 10 emails RETURNED, rounded up, and a domain it knows nobody at is
        # free. Neither half of that rule survives being flattened into the catalog's per-row price
        # (1 credit ÷ 10 = $0.00245/result), so settling at the estimate is wrong in BOTH
        # directions — a search with no `limit` reserved the 20-row default page and settled a
        # ZERO-email answer at $0.0490, 20x the published per-result price for results nobody got,
        # while `limit=1` on a domain that did answer settled at $0.00245, a tenth of the credit
        # Hunter actually took. The returned list is the bill.
        data = doc.get("data")
        emails = data.get("emails") if isinstance(data, dict) else None
        rate = catalog_store.load().credit_rates.get("hunter")
        if isinstance(emails, list) and rate:
            credits = -(-len(emails) // 10)  # whole credits, rounded up; no emails = no charge
            return int(credits * rate * 1_000_000 + 0.5)
        return None
    if provider == "hunter" and mk.endpoint_id == "hunter.people.email.find":
        # DERIVED, the flat case of the same family: the finder takes ONE whole search credit when
        # it finds an email and nothing when it doesn't — the catalog note says "a miss is free" in
        # as many words, yet a miss still answers HTTP 200 with `email: null`, so settling at the
        # estimate billed the full credit ($0.0245) for a name Hunter had nothing on. A body
        # without the `email` key (an error shape) still falls back to the estimate.
        data = doc.get("data")
        rate = catalog_store.load().credit_rates.get("hunter")
        if isinstance(data, dict) and "email" in data and rate:
            return int(rate * 1_000_000 + 0.5) if data["email"] else 0
        return None
    if provider == "tikhub":
        # REPORTED in prose rather than a number: every TikHub envelope states whether the call is
        # billed. A 2xx whose payload is an embedded error still says "This request will incur a
        # charge." and TikHub really does charge us for it (verified live 2026-07-30 — see
        # docs/context/architecture/catalog.md, "the provider decides what counts as success"), so
        # a dead page settling at the estimate is faithful, not an over-charge. Only the explicit
        # no-charge phrasing settles at zero; anything else stays at the estimate.
        msg = doc.get("message")
        if isinstance(msg, str):
            low = msg.lower()
            if "won't be charged" in low or "will not be charged" in low or "not incur" in low:
                return 0
        return None
    if provider == "apollo":
        # Only the shapes whose billing rule is documented and body-decidable: company enrichment
        # (1 credit per organization returned, null on a miss) and company search (1 credit per
        # non-empty PAGE). A body carrying neither key — people enrichment's 1-9 credit range
        # included — falls through to the estimate rather than guessing.
        rate = catalog_store.load().credit_rates.get("apollo")
        if rate:
            for key in ("organization", "organizations"):
                if key in doc:
                    return int(rate * 1_000_000 + 0.5) if doc[key] else 0
        return None
    # GENERIC miss rule: a `per_success` endpoint bills only when it found something, and the
    # routing adapter (catalog/adapters.yaml, fixture-verified at load) already knows what "nothing"
    # looks like in this provider's body. Before this, tomba / findymail / leadsforge misses settled
    # at the estimate — a whole credit for a miss the catalog and the provider both call free
    # (found live by the first routed waterfall, 2026-08-28: three of five misses were charged).
    if mk.cost_type == "per_success" and isinstance(doc, dict):
        adapter = catalog_store.load().adapters.get(mk.endpoint_id)
        if adapter is not None and adapter.verified:
            try:
                if adapter.is_miss(doc):
                    return 0
            except Exception:  # noqa: BLE001 — a predicate that cannot decide settles at the estimate
                pass
    return None


async def _buffer_response(response: UpstreamResponse) -> tuple[UpstreamResponse, bytes]:
    """Drain a relayed streaming response into memory and return an equivalent plain Response.

    Metered calls give up streaming on purpose: settling needs the provider's own reported cost (which
    lives in the body) and the telemetry row wants the response size, and neither can be known while
    the bytes are still in flight. These are JSON API answers — the same payloads the catalog stores as
    examples — so the memory cost is a few KB, and buffering happens BEFORE anything is sent to the
    caller, which is what lets a mid-stream upstream failure still become a clean 502 + release."""
    chunks, size = [], 0
    async for chunk in response.body_stream:
        raw = chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8", "replace")
        size += len(raw)
        if size <= _PLATFORM_BODY_MAX:
            chunks.append(raw)
    body = b"".join(chunks)
    await response.close()

    async def buffered_body():
        yield body

    async def closed() -> None:
        return None

    # Carry the upstream's headers verbatim (the relay already dropped hop-by-hop + our own), with a
    # content-length that matches what we are actually about to send.
    raw_headers = tuple(
        [(k, v) for k, v in response.raw_headers if k.lower() != b"content-length"]
        + [(b"content-length", str(len(body)).encode())]
    )
    out = UpstreamResponse(response.status, raw_headers, buffered_body(), closed)
    return out, body


async def _peek_stream_head(response: UpstreamResponse, limit: int) -> tuple[UpstreamResponse, bytes]:
    """Read at most ``limit`` response bytes for evidence, then replay every byte to the caller.

    Unmetered calls retain their streaming contract. The consumed chunks are yielded first by the
    replacement response, followed by the untouched iterator; the relay's upstream-close background
    task moves with it and therefore still runs after the caller finishes reading.
    """
    iterator = response.body_stream.__aiter__()
    consumed: list[bytes] = []
    head = bytearray()
    while len(head) < limit:
        try:
            chunk = await iterator.__anext__()
        except StopAsyncIteration:
            break
        raw = chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8", "replace")
        consumed.append(raw)
        head.extend(raw[:limit - len(head)])

    async def replay():
        for chunk in consumed:
            yield chunk
        async for chunk in iterator:
            yield chunk

    out = UpstreamResponse(response.status, response.raw_headers, replay(), response.close)
    return out, bytes(head)
async def _platform_settle(
    mk: MarketplaceCall, status_code: int | None, body: bytes = b"", *, headers=None,
    reason: str = "", finalized: Callable[[], None] | None = None,
    observed_override: int | None = None, overflow_spend: tuple[str, int, int] | None = None,
) -> tuple[int, int | None]:
    """Close the hold for a metered call → (charged_micro, observed_micro). `charged_micro` is what
    actually hit the org's balance (0 on a release) — the number the Activity feed must show, because
    the estimate alone over-reports a released call as spend.

    `status_code=None` means the provider never answered us (our own 4xx, an injection error, a network
    failure) — always a release, never a charge, whatever the endpoint's billing type says.

    Never raises: the caller already has their answer (or their error), and a ledger hiccup must not
    turn a served call into a 500. A hold that fails to close is not lost money either — the reaper
    releases it, which errs in the org's favour. Runs on its OWN session because the request's session
    may be mid-rollback from the very error we are releasing for."""
    if not mk.metered or not mk.call_id:
        return 0, None
    billable = status_code is not None and _platform_billable(status_code, mk.cost_type)
    # `observed_override`: the overflow child cycle knows its cost from the aggregator's envelope, not
    # from the vendor body — the caller pays exactly that (plan §4.3 step 5), whatever the vendor's
    # own billing shape. `overflow_spend` = (aggregator, adjustment from the budget reservation,
    # delta vs treg's direct price): folded into the SAME settle transaction, the one allowlisted
    # overflow write (`overflow_spend_in_settle`). It is recorded even when the vendor response is
    # not billable to the caller because the aggregator's prepaid account still incurred the cost.
    observed = ((observed_override if observed_override is not None
                 else _observed_cost_micro(mk, body, headers)) if billable else None)
    call_id, mk.call_id = mk.call_id, None  # closing is once-only, even if two paths try
    charged = 0

    async def _close() -> int:
        async with session_maker() as db:
            if billable:
                charged = await ledger.settle_in_transaction(db, call_id, observed, meta={
                    "provider": mk.provider, "status_code": status_code, "cost_type": mk.cost_type,
                    "cost_source": ("aggregator" if overflow_spend is not None
                                    else "provider" if observed is not None else "estimate"),
                    **({"served_via": f"overflow:{overflow_spend[0]}"} if overflow_spend else {})})
            else:
                await ledger.release_in_transaction(
                    db, call_id, reason=reason or f"not_billable_{status_code}",
                    meta={"provider": mk.provider, "cost_type": mk.cost_type,
                          "status_code": status_code})
                charged = 0
            if overflow_spend is not None:
                await overflow_spend_ledger.add_in_transaction(
                    db, overflow_spend[0], overflow_spend[1], overflow_spend[2])
            await db.commit()
            if finalized is not None:
                finalized()
            return charged

    try:
        try:
            charged = await _close()
        except PoolTimeoutError:
            # No pool slot within `pool_timeout`: a transient wait, not a broken ledger. A settle that
            # gives up here forfeits the charge (the hold is reaped in the org's favour) — real revenue,
            # so one short retry is worth it. Anything else falls straight through to the log.
            await asyncio.sleep(0.5)
            charged = await _close()
    except Exception as exc:  # noqa: BLE001 — loudly, but never into the caller's response
        logging.getLogger("treg.ledger").error(
            "settle/release failed for call %s (%s, status %s): %s",
            call_id, mk.endpoint_id, status_code, exc, exc_info=True)
    return charged, observed


async def _finish_cancelled_call(
    claim: tuple[int, str] | None,
    mk: MarketplaceCall | None,
    call_ref: str,
    response: UpstreamResponse | None = None,
) -> None:
    """Finish compensation before propagating cancellation from a call that may have reserved."""
    # A cancelled request cannot own this cleanup: another cancellation while it is returning the
    # first one would strand the upstream response, hold, or idempotency label halfway through.
    async def _cleanup() -> None:
        # Every branch contains its own failure: raising here would replace the original cancellation
        # when shield joins this task, instead of letting the remaining compensation finish.
        if response is not None:
            try:
                await response.close()
            except (Exception, asyncio.CancelledError):  # noqa: BLE001
                logging.getLogger("treg.proxy").error(
                    "upstream close failed for cancelled call %s", call_ref, exc_info=True)
        if mk is not None and mk.metered:
            # `ledger.reserve` may have committed without returning, so `mk.call_id` is not an
            # authority here. The pre-reserve call_ref is the hold id in either outcome, and release
            # conditionally claims it: committed means refund, rolled back means a safe no-op.
            mk.call_id = None
            try:
                async with session_maker() as cleanup_db:
                    # The parent hold AND the overflow child's (`{call_ref}:overflow`, plan §4.3
                    # step 2): each release is a conditional claim, so a hold that never existed or
                    # was already closed is a safe no-op, and both are released exactly once.
                    for hold_id in (call_ref, f"{call_ref}:overflow"):
                        await ledger.release_in_transaction(
                            cleanup_db,
                            hold_id,
                            reason="call_cancelled",
                            meta={"provider": mk.provider, "cost_type": mk.cost_type,
                                  "status_code": None},
                        )
                    await cleanup_db.commit()
            except (Exception, asyncio.CancelledError):  # noqa: BLE001
                logging.getLogger("treg.ledger").error(
                    "cancellation release failed for call %s", call_ref, exc_info=True)
        try:
            await _release_idempotent_claim(claim)
        except (Exception, asyncio.CancelledError):  # noqa: BLE001
            logging.getLogger("treg.idempotency").error(
                "cancellation claim release failed for call %s", call_ref, exc_info=True)

    cleanup = asyncio.create_task(_cleanup())
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # A repeated cancel may interrupt the shield await, but not its child. Keep joining the
            # same cleanup task so compensation completes before the original cancellation escapes.
            continue
    await cleanup
async def _note_capacity_signal(mk: MarketplaceCall, status_code: int, headers, body: bytes) -> None:
    """After a tier-4 answer: did the provider just tell us OUR account is out? A confirmed balance/
    quota signature (domain.capacity.signatures) marks the provider exhausted in ratestore so the next
    call is refused before a hold exists. Burst/unknown 429s only log (D′ smooths them). Runs after
    the settle, on its own short session, and never raises. Platform tier only: an org's own key
    running dry is the org's business, and an oauth-billed connect has no shared account to mark."""
    if mk.tier != "platform" or status_code < 400:
        return
    try:
        signal = capacity_signatures.classify(mk.provider, status_code, headers, body[:4096])
        if signal is None:
            return
        if capacity_signatures.is_exhausting(signal):
            await capacity_marks.mark_exhausted(
                mk.provider, until=signal.resets_at,
                note=f"{signal.kind} signature on {mk.endpoint_id}: {signal.detail[:80]}")
            logging.getLogger("treg.capacity").warning(
                "platform account exhausted: %s (%s on %s)", mk.provider, signal.kind, mk.endpoint_id)
        else:
            logging.getLogger("treg.capacity").info(
                "rate signal on %s: %s retry_after=%s", mk.provider, signal.kind, signal.retry_after_s)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — the caller already has the provider's answer; a mark is a hint
        logging.getLogger("treg.capacity").warning("capacity signal handling failed", exc_info=True)


async def _record_first_call(org_id: int) -> None:
    """Set Org.first_call_at once — the metric that decides whether a marketing channel is real (see
    marketing/landing/_measurement.md). A CONDITIONAL UPDATE, not read-then-write: concurrent first
    calls would both see NULL and both fire. Set for EVERY org (it is a product metric in its own
    right); adsconv.queue() itself no-ops for orgs with no ad_gclid, so the conversion side stays
    ad-attributed-only.

    Runs on its OWN session, same reason as _platform_settle: this fires after the response is built,
    while the request's `db` may still be mid-settlement (or mid-rollback from one), and a commit or
    rollback issued here would land on THAT transaction instead of this one. Never raises — a metric
    write must not turn a working proxied call into a 500."""
    try:
        async with session_maker() as db:
            result = await db.execute(
                update(Org)
                .where(Org.id == org_id, Org.first_call_at.is_(None))
                .values(first_call_at=_utcnow_naive())  # naive UTC — asyncpg rejects tz-aware here
            )
            if result.rowcount:
                org_row = await db.get(Org, org_id)
                if org_row is not None:
                    await adsconv.queue(db, org_row, adsconv.ACTION_FIRST_CALL)
                await db.commit()
    except Exception:  # noqa: BLE001 — loudly, but never into the caller's response
        logging.getLogger("treg.adsconv").error(
            "first_call_at update/queue failed for org %s", org_id, exc_info=True)
