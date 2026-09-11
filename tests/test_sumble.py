"""Sumble pricing evidence, platform boundary, and faithful BYOK behavior."""
import json

import httpx
import pytest

from treg.application.call import resolve as resolution, service, settle
from treg.application.call.types import ResolutionFailed
from treg.config import get_settings
from treg.domain.catalog import store
from treg.domain.capacity import collectors
from treg.domain.capacity.policy import default_policy
from treg import oauth_providers as P
from test_marketplace_call import _balance, _entries, _fake_relay, _mk, platform_on


@pytest.fixture
def sumble_on(monkeypatch, platform_on):
    monkeypatch.setenv('TREG_PLATFORM_KEY_SUMBLE', 'PLATFORM-SUMBLE')
    monkeypatch.setenv('TREG_PLATFORM_PROVIDERS', 'sumble')
    get_settings.cache_clear()


def test_full_surface_and_platform_gate():
    cat = store.load()
    rows = [e for e in cat.endpoints if e['provider'] == 'sumble']
    assert len(rows) == 30
    assert len({(e['method'], e['path']) for e in rows}) == 30
    enabled = {e['id'].removeprefix('sumble.') for e in rows if cat.platform_eligible(e)}
    assert enabled == {'organizations', 'organizations.techs', 'jobs', 'teams', 'technologies.find',
                       'technologies.lookup', 'technologies.categories.lookup', 'projects.lookup',
                       'job_functions.lookup', 'documentation.search'}
    assert all(e.get('verified') for e in rows if cat.platform_eligible(e))
    assert cat.credit_rates['sumble'] == .01
    assert P.platform_bindings(P.get('sumble'))[0]['format'] == 'Bearer {secret}'
    assert 'sumble.organizations' in cat.by_id['treg.companies.enrich']['routed_children']


@pytest.mark.parametrize('endpoint,body,credits', [
    ('organizations', {'organizations': [{'url':'stripe.com'}]*2, 'select': {'attributes':['id','name','industry','employee_count']}}, 6),
    ('organizations', {'organizations': [{'id':330446}], 'select': {'entities':[{'type':'technology','term':'python','metrics':['job_post_count','people_count']}]}}, 3),
    ('jobs', {'filter':{'organization_ids':[330446]}, 'limit':2, 'select':{'attributes':['title','location','posted_date']}}, 6),
    ('teams', {'teams':[1,2], 'select':{'attributes':['jobs_count','breadcrumbs']}}, 6),
    ('technologies.lookup', {'technologies':['kubernetes']*101}, 2),
    ('technologies.lookup', {'technologies':['kubernetes']*100}, 1),
    ('technologies.lookup', {'technologies':['kubernetes']}, 1),
    ('organizations.techs', {}, 100),
])
def test_estimates_follow_composable_and_rounded_credit_rules(endpoint, body, credits):
    cat=store.load(); ep=cat.by_id['sumble.'+endpoint]
    cost=cat.cost_view(ep['cost'], 'sumble')
    assert resolution._marketplace_pricing('sumble',ep['id'],cost,resolution.QueryValues(()),json.dumps(body).encode()) == (credits*10000,10000)


@pytest.mark.parametrize('credits,expected', [(3,30000),(0,0),(101,1010000),(-1,None),(True,None),('3',None),(1.5,None),(None,None)])
def test_reported_usage_uses_frozen_rate(credits, expected):
    mk=_mk('sumble',endpoint_id='sumble.organizations',unit_micro=10000)
    assert settle._observed_cost_micro(mk,json.dumps({'credits_used':credits}).encode()) == expected


@pytest.mark.parametrize('endpoint,body', [
    ('organizations',{'organizations':[{'id':1}], 'select':{'attributes':['account_status']}}),
    ('organizations',{'organizations':[{'id':1}], 'select':{'attributes':['sumble_score']}}),
    ('organizations',{'filter':{'query':"account_score GT 1"}, 'select':{'attributes':['name']}}),
    ('organizations',{'organizations':[{'id':1}], 'select':{'entities':[{'type':'technology_category','term':'crm','metrics':['people_count'],'granularity':'exploded'}]}}),
    ('organizations',{'organizations':[{'id':1}], 'select':{'entities':[{'type':'technology','term':'python','metrics':'all'}]}}),
    ('jobs',{'filter':{'organization_list_id':1}, 'select':{'attributes':['title']}}),
    ('teams',{'teams':[1], 'select':{'attributes':'all'}}),
    ('teams',{'teams':[1], 'select':{'attributes':['score']}}),
    ('teams',{'teams':[1], 'select':{'attributes':[]}, 'order_by_column':'score'}),
    ('jobs',{'jobs':[{'job_id':'1'}], 'select':{'attributes':[], 'related_people':{}}}),
    ('job_functions.lookup',{'titles':['Engineer'], 'file_name':'private.csv'}),
    ('technologies.lookup',{'technologies':['python'], 'request_id':'another-team'}),
    ('technologies.lookup',{'technologies':[]}),
    ('jobs',{'filter':{'organization_ids':[1]}, 'select':{'attributes':[]}, 'limit':True}),
])
async def test_platform_rejects_unsafe_variants_before_spend(clients,sumble_on,monkeypatch,endpoint,body):
    async def forbidden(*args,**kwargs):raise AssertionError('guard must precede relay')
    monkeypatch.setattr(service,'relay',forbidden)
    before=await _balance(clients)
    r=await clients.post('/call/sumble.'+endpoint,json=body)
    assert r.status_code==400,r.text
    assert await _balance(clients)==before
    assert not [e for e in await _entries(clients) if e['kind'] in ('reserve','settle','release')]


async def test_duplicate_keys_rejected(clients,sumble_on):
    r=await clients.post('/call/sumble.technologies.find',content=b'{"query":"python","query":"other"}',headers={'content-type':'application/json'})
    assert r.status_code==400


async def test_platform_usage_and_error_release(clients,sumble_on,monkeypatch):
    before=await _balance(clients)
    for status,credits in [(200,3),(200,0),(402,0),(429,0),(500,0)]:
        raw=json.dumps({'credits_used':credits,'organizations':[]}).encode()
        monkeypatch.setattr(service,'relay',_fake_relay(status,raw))
        r=await clients.post('/call/sumble.organizations',json={'organizations':[{'url':'stripe.com'}],'select':{'attributes':['industry','employee_count']}})
        assert r.status_code==status and r.content==raw
        before-=credits*10000
        assert await _balance(clients)==before


@pytest.mark.parametrize('endpoint,method,body', [('people','POST',{'request_id':'other-team'}),('organization_lists.list','GET',None),('signals.search','POST',{}),('organizations.signals','GET',None)])
async def test_workspace_and_async_are_byok_only(clients,sumble_on,endpoint,method,body):
    r=await clients.request(method,'/call/sumble.'+endpoint,params={'organization_id':1726684} if endpoint=='organizations.signals' else {},json=body)
    assert r.status_code == 404,r.text
    assert not [e for e in await _entries(clients) if e['kind'] in ('reserve','settle','release')]


async def test_byok_full_surface_keeps_original_request_and_no_meter(clients,sumble_on):
    await clients.post('/secrets',json={'name':'sumble','value':'OWN-SUMBLE'})
    before=await _balance(clients)
    for endpoint,body in [('people',{'request_id':'my-job'}),('organizations',{'filter':{'query':'account_score GT 1'},'select':{'attributes':['account_status','sumble_score']}}),('signals.search',{})]:
        r=await clients.post('/call/sumble.'+endpoint,json=body)
        assert r.status_code==200,r.text
        assert r.json()['auth']=='Bearer OWN-SUMBLE'
        assert json.loads(r.json()['body']) == body
        assert await _balance(clients)==before
    assert not [e for e in await _entries(clients) if e['kind'] in ('reserve','settle','release')]


async def test_sumble_connection_probe(clients,monkeypatch):
    from treg.api import app
    def probe(request):
        assert request.method=='POST' and request.url.path=='/v9/technologies/find'
        assert json.loads(request.content)==P.get('sumble').probe_json
        return httpx.Response(401,json={'detail':'Invalid API key'}) if request.headers['authorization']=='Bearer bogus' else httpx.Response(200,json={'credits_used':0,'credits_remaining':0,'technologies':[]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(app.state,'http',upstream)
        assert (await clients.post('/connections/token',json={'provider':'sumble','token':'bogus'})).status_code==422
        r=await clients.post('/connections/token',json={'provider':'sumble','token':'good'})
        assert r.status_code==200,r.text


@pytest.mark.parametrize('remaining,expected',[(9900,9900),(0,0),(None,None),(-1,None),(True,None),('10',None)])
async def test_capacity_probe_and_subscription_policy(remaining,expected):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'credits_used':0,'credits_remaining':remaining}))) as c:
        row=await collectors._sumble(c,'test')
    assert row['value']==expected
    policy=default_policy('sumble',has_key=True)
    assert policy.capacity_type=='monthly_quota'


async def test_routed_company_enrichment_preserves_raw_and_charges_usage(clients, sumble_on, monkeypatch):
    from test_routing import _relay_by_provider
    raw = {'credits_used': 3, 'matched_count': 1, 'organizations': [
        {'attributes': {'id': 330446, 'name': 'Stripe', 'url': 'stripe.com',
                        'employee_count': 13421, 'industry': 'Scientific and Technical Services'}}]}
    seen = []
    monkeypatch.setattr(service, 'relay', _relay_by_provider({'sumble': [(200, raw)]}, seen))
    before = await _balance(clients)
    r = await clients.post('/call/treg.companies.enrich', json={'domain': 'stripe.com'})
    assert r.status_code == 200, r.text
    assert r.json()['_treg']['served_by'] == 'sumble.organizations'
    assert r.json()['raw'] == raw and r.json()['output']['name'] == 'Stripe'
    assert seen[0][3]['organizations'] == [{'url': 'stripe.com'}]
    assert before - await _balance(clients) == 30000


def test_catalog_placement_and_price_display():
    from treg.routers.web import _price_label
    cat = store.load()
    rows = cat.for_provider('sumble')
    assert sum(e['platform'] == 'companies' for e in rows) == 25
    assert {e['id'] for e in rows if e['platform'] == 'people'} == {
        'sumble.people', 'sumble.contact_lists.list', 'sumble.contact_lists.get',
        'sumble.contact_lists.create', 'sumble.contact_lists.add'}
    assert all(e['kind'] == 'account' for e in rows
               if '.contact_lists.' in e['id'] or '.organization_lists.' in e['id']
               or e['id'].startswith('sumble.support'))
    expected = {'organizations': '$0.01+/result',
                'technologies.lookup': '$0.01/started 100 matches',
                'organizations.techs': '$0.01/technology',
                'technologies.find': '$0.01/success', 'documentation.search': 'free'}
    for suffix, label in expected.items():
        cost = cat.cost_view(cat.by_id['sumble.' + suffix]['cost'], 'sumble')
        assert _price_label(cost) == label


async def test_sumble_page_full_inventory_and_access(clients):
    response = await clients.get('/tools/sumble')
    assert response.status_code == 200
    html = response.text
    assert '30 tools · 10 platform + BYOK · 20 BYOK only' in html
    assert '10 of 30 tools on this page are live-verified' in html
    assert 'no Sumble signup: 30 tools' not in html
    assert '$0.01/started 100 matches' in html
    assert '$0.01/technology' in html
    assert 'No treg charge; upstream' in html
    assert '$0.01+/result' in html
    for ep in store.load().for_provider('sumble'):
        assert '<code>' + ep['id'] + '</code>' in html
