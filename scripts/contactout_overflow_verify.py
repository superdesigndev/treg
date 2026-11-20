#!/usr/bin/env python3
"""Renew ContactOut overflow evidence with a discovered profile, without storing contact data.

Run with the worker environment and --apply to persist stamps and sync routes. This makes paid
calls; --budget-usd bounds estimated direct plus aggregator cost (default $10).
People routes have no catalog test requests under the PII rule;
this explicit paid command builds their requests from an ephemeral discovered profile.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

import httpx
from treg.config import get_settings
from treg.domain.catalog import store
from treg.domain.capacity import routes as R, verify as V
from treg.application.call.contactout import estimate


async def main(args):
    settings = get_settings()
    catalog = store.load()
    candidates = R.load_seed()
    # Only previously proven compatible routes are renewed. Failed wrapper experiments stay off.
    rows = [r for r in candidates if r['provider'] == 'contactout' and r.get('verified_at')]
    rows = [r for r in rows if not (r['aggregator'] == 'orthogonal' and r['agg_unit'] == 'call'
                                  and catalog.by_id[r['endpoint_id']]['cost']['type'] == 'per_result')]
    if not settings.platform_key_contactout:
        raise SystemExit('ContactOut platform key is required for direct comparison')
    if args.budget_usd < .02:
        raise SystemExit('Budget must cover the $0.02 discovery request')
    budget = round(args.budget_usd * 1_000_000)
    reserved = 20_000
    headers = {'token': settings.platform_key_contactout, 'Content-Type': 'application/json'}
    passed, failed = [], []
    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post('https://api.contactout.com/v1/people/search', headers=headers,
                                     json={'company': ['ContactOut'], 'page_size': 1, 'reveal_info': False})
        if response.status_code != 200:
            raise SystemExit(f'Discovery failed (HTTP {response.status_code})')
        profiles = response.json().get('profiles') or {}
        profile = next(iter(profiles.values()), {})
        vanity = profile.get('li_vanity')
        if not isinstance(vanity, str) or not vanity:
            raise SystemExit('Discovery returned no usable profile')
        url = 'https://www.linkedin.com/in/' + vanity.strip('/')
        for row in rows:
            key = settings.overflow_key_for(row['aggregator'])
            if not key:
                continue
            ep = catalog.by_id[row['endpoint_id']]
            request = json.loads(json.dumps(ep.get('test_request') or {}))
            inputs = ep.get('input') or {}
            for location in ('queryParams', 'body'):
                specs = inputs.get(location) or {}
                if not specs:
                    continue
                params = request.setdefault(location, {})
                for name, spec in specs.items():
                    if name in ('profile', 'linkedin_url'):
                        params[name] = url
                    elif spec.get('required') and len(spec.get('enum', [])) == 1:
                        params[name] = spec['enum'][0]
            query = request.get('queryParams', {})
            body = request.get('body')
            if 'profile' in query:
                query['profile'] = url
            if body and 'linkedin_url' in body:
                body['linkedin_url'] = url
            if ep['id'] == 'contactout.people.linkedin.enrich':
                query['profile_only'] = False
            if ep['id'] == 'contactout.people.contact.work':
                query['include_phone'] = True
            query = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in query.items()}
            request['queryParams'] = query
            cost = catalog.cost_view(ep['cost'], 'contactout')
            direct = estimate(cost, body or query)
            # Verified result-priced company searches return at most 25; domain calls use input count.
            n = (len(body.get('domains', [])) if body and 'domains' in body else 25) if row['agg_unit'] == 'result' else 1
            ceiling = direct + round(row['agg_price_usd'] * 1_000_000) * n
            if reserved + ceiling > budget:
                print(f"SKIP budget: {row['endpoint_id']} via {row['aggregator']}")
                continue
            reserved += ceiling
            result = await V.verify_route(client, SimpleNamespace(**row), key=key,
                direct=('https://api.contactout.com' + ep['path'], query,
                        json.dumps(body).encode() if body is not None else None),
                test_request=request, direct_headers=headers)
            verdict = V.verdict(result)
            print(f"{row['endpoint_id']} via {row['aggregator']}: {verdict}; "
                  f"HTTP {result.direct_status}/{result.relay_status}; cost_micro={result.cost_micro}")
            if verdict == 'passed':
                row['verified_at'] = result.verified_at.isoformat()
                passed.append(row)
            elif verdict == 'failed':
                failed.append(row)
    if args.apply:
        from treg.infra.db import session_maker, verify_db
        await verify_db()
        async with session_maker() as db:
            # Retain standard sync behavior; failures are withheld so they cannot keep serving.
            rejected = {(r['endpoint_id'],r['aggregator']) for r in failed}
            current = [r for r in candidates if (r['endpoint_id'],r['aggregator']) not in rejected]
            await R.apply_sync(db, current, catalog=catalog)
            await db.commit()
    print(f'{len(passed)} passed; {len(failed)} failed; budget reserved ${reserved / 1e6:g}')
    return 0 if passed and not failed else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--budget-usd', type=float, default=10)
    parser.add_argument('--apply', action='store_true')
    raise SystemExit(asyncio.run(main(parser.parse_args())))
