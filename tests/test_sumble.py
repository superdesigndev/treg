"""Sumble pricing evidence, platform boundary, and faithful BYOK behavior."""
import json

import httpx
import pytest

from treg.application.call import resolve as resolution, service, settle
from treg.config import get_settings
from treg.domain.catalog import store
from treg.domain.capacity import collectors
from treg.domain.capacity.policy import default_policy
from test_marketplace_call import _balance, _entries, _mk, platform_on  # noqa: F401


@pytest.fixture
def sumble_on(monkeypatch, platform_on):  # noqa: F811
    monkeypatch.setenv('TREG_PLATFORM_KEY_SUMBLE', 'PLATFORM-SUMBLE')
    monkeypatch.setenv('TREG_PLATFORM_PROVIDERS', 'sumble')
    get_settings.cache_clear()


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
    cat=store.load()
    ep=cat.by_id['sumble.'+endpoint]
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


@pytest.mark.parametrize('remaining,expected',[(9900,9900),(0,0),(None,None),(-1,None),(True,None),('10',None)])
async def test_capacity_probe_and_subscription_policy(remaining,expected):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'credits_used':0,'credits_remaining':remaining}))) as c:
        row=await collectors._sumble(c,'test')
    assert row['value']==expected
    policy=default_policy('sumble',has_key=True)
    assert policy.capacity_type=='monthly_quota'
