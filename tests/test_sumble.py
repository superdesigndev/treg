"""Sumble pricing evidence, platform boundary, and faithful BYOK behavior."""
import json

import httpx
import pytest

from conftest import auth_digest
from treg.application.call import service
from treg.config import get_settings
from treg.domain import money
from treg.domain.capacity import collectors
from treg.infra.db import session_maker
from test_marketplace_call import _balance, _entries, _fake_relay, platform_on  # noqa: F401


@pytest.fixture
def sumble_on(monkeypatch, platform_on):
    monkeypatch.setenv('TREG_PLATFORM_KEY_SUMBLE', 'PLATFORM-SUMBLE')
    monkeypatch.setenv('TREG_PLATFORM_PROVIDERS', 'sumble')
    get_settings.cache_clear()


async def test_platform_settles_the_reported_credits_and_holds_a_request_at_its_max(clients, sumble_on, monkeypatch):
    # 400 organizations x (1 base + 2 paid attributes) is the 1,200-credit row max: held, then settled
    # at the credits Sumble reports. One more organization is refused before any hold.
    body = {'organizations': [{'url': 'stripe.com'}] * 400, 'select': {'attributes': ['id', 'industry', 'employee_count']}}
    monkeypatch.setattr(service, 'relay', _fake_relay(200, json.dumps({'credits_used': 7, 'organizations': []}).encode()))
    org_id = (await clients.get('/orgs')).json()[0]['org_id']
    async with session_maker() as db:
        await money.grant(db, org_id, amount_micro=20_000_000, kind='promotional', once=False)
        await db.commit()
    before = await _balance(clients)
    r = await clients.post('/call/sumble.organizations', json=body)
    assert r.status_code == 200, r.text
    reserves = [e for e in await _entries(clients) if e['kind'] == 'reserve']
    assert [-e['amount_micro'] for e in reserves] == [money.with_margin(1200 * 10_000)]
    assert before - await _balance(clients) == money.with_margin(7 * 10_000)
    r = await clients.post('/call/sumble.organizations', json={**body, 'organizations': [{'url': 'stripe.com'}] * 401})
    assert r.status_code == 400, r.text


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
    ('jobs',{'filter':{'organization_ids':[1]}, 'select':{'attributes':[]}, 'order_by_column':'jobs_count'}),
    ('organizations',{'organizations':[{'id':1}], 'select':{'attributes':[]}, 'include_closed':True}),
    ('organizations',{'organizations':[{'id':1}], 'select':{'attributes':[]}, 'order_by_column':'industry'}),
    ('technologies.lookup',{'technologies':['python']*1001}),
    ('jobs',{'jobs':[{'job_id':1}]*200, 'select':{'attributes':['description','location','posted_date','organization','technologies','teams']}}),
    ('teams',{'filter':{'organization_ids':[1]}, 'limit':200, 'select':{'attributes':['organization','breadcrumbs']}}),
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
        assert r.json()['auth_sha256']==auth_digest('Bearer OWN-SUMBLE')
        assert json.loads(r.json()['body']) == body
        assert await _balance(clients)==before
    assert not [e for e in await _entries(clients) if e['kind'] in ('reserve','settle','release')]


@pytest.mark.parametrize('remaining,expected',[(9900,9900),(0,0),(None,None),(-1,None),(True,None),('10',None)])
async def test_capacity_probe(remaining,expected):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'credits_used':0,'credits_remaining':remaining}))) as c:
        row=await collectors._sumble(c,'test')
    assert row['value']==expected
