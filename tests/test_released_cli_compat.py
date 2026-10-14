"""Opt-in checks of the unmodified PyPI CLI against this checkout's ASGI server.

Set TREG_RELEASED_CLI_DIR to a directory with verified 0.16.0 and 0.19.0 wheel
contents. No CLI config, browser, paid upstream call, or production service is used.
"""
from __future__ import annotations

import asyncio
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from treg.api import app
from treg.domain.identity import session as sess


@pytest.mark.parametrize('version', ['0.16.0', '0.19.0', 'current'])
async def test_released_cli_flows(clients, tmp_path, monkeypatch, capsys, version):
    if version == 'current':
        from treg import cli
    else:
        root = os.environ.get('TREG_RELEASED_CLI_DIR')
        if not root:
            pytest.skip('set TREG_RELEASED_CLI_DIR to verified released wheels')
        package = Path(root) / version / 'treg'
        hashes = {
            '0.16.0': 'db4e241b01a81d57ea1d4d17947736f2589aff61ac6a4b1e28bc8524c03f4df1',
            '0.19.0': '825aa562e75582667c2d555d836db5a3d7a8ce3371af62b0eed6f96aa41edb26',
        }
        assert hashlib.sha256((package / 'cli.py').read_bytes()).hexdigest() == hashes[version]
        name = 'released_treg_' + version.replace('.', '_')
        spec = importlib.util.spec_from_file_location(name, package / '__init__.py',
                                                    submodule_search_locations=[str(package)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        cli = __import__(name + '.cli', fromlist=['cli'])
    monkeypatch.setattr(cli, 'CONFIG_PATH', tmp_path / 'config.json')
    monkeypatch.setattr(cli, '_maybe_offer_onboarding', lambda cfg: None)
    # The sync CLI runs on a thread; each HTTP request reaches the real async server on
    # the fixture loop. The normal httpx cookie and header handling remains in place.
    loop = asyncio.get_running_loop()
    trace = []
    otp = {}

    async def send(request):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as c:
            response = await c.send(request)
            trace.append((request.method, request.url.path, response.status_code))
            if request.url.path == '/auth/email/start' and response.status_code == 200:
                otp['code'] = response.json()['dev_code']
            return httpx.Response(response.status_code, headers=response.headers,
                                  content=response.content, request=request)

    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            return asyncio.run_coroutine_threadsafe(send(request), loop).result(timeout=20)

    class Client(httpx.Client):
        def __init__(self, **kw):
            kw['transport'] = Transport()
            super().__init__(**kw)

    def request(method, url, **kw):
        with Client() as c:
            return c.request(method, url, **kw)

    httpx_api = {k: v for k, v in vars(httpx).items() if k not in {"Client", "post", "get"}}
    monkeypatch.setattr(cli, "httpx", SimpleNamespace(
        **httpx_api, Client=Client,
        post=lambda url, **kw: request('POST', url, **kw),
        get=lambda url, **kw: request('GET', url, **kw),
    ))
    # _RegistryClient was defined before the test transport was installed.
    monkeypatch.setattr(cli, '_RegistryClient', Client)
    monkeypatch.setattr('builtins.input', lambda prompt='': otp['code'])
    # Fixture's owner already has a team and a valid browser session can be derived in test.
    owner_token = clients.headers['X-Treg-Token']
    claims = sess.read_identity_claims(owner_token)
    cookie = sess.make_session(claims['uid'])
    team = (await clients.get('/orgs')).json()[0]['slug']
    second = (await clients.post('/orgs', json={'name': 'Second'})).json()['org']

    def browser_open(url):
        parsed = urlsplit(url)
        lid = parse_qs(parsed.query)['cli'][0]
        code = parse_qs(parsed.fragment)['code'][0]
        response = request('POST', 'http://test/auth/cli/approve',
                           json={'login_id': lid, 'code': code, 'org': team},
                           headers={'Cookie': f'treg_session={cookie}'})
        assert response.status_code == 200, response.text
        return True

    monkeypatch.setattr(cli, "webbrowser", SimpleNamespace(open=browser_open))
    monkeypatch.setattr(cli, "time", SimpleNamespace(sleep=lambda seconds: None))
    cfg = {'base_url': 'http://test', 'token': None}
    parser = cli.build_parser()

    async def command(*argv):
        args = parser.parse_args(argv)
        return await asyncio.to_thread(args.fn, args, cfg)

    async def resources():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
            r = await c.get('/tools', headers={'X-Treg-Token': cfg['token'], 'X-Treg-Org': cfg['active_org']})
            return r.status_code

    outcomes = {}
    await command('login')
    outcomes['browser_login'] = await resources()
    await command('org', 'use', second)
    outcomes['team_switch'] = await resources()
    before = cli.CONFIG_PATH.read_bytes()
    before_orgs = len((await clients.get('/orgs')).json())
    if version == 'current':
        await command('org', 'create', 'Browser-created')
    else:
        capsys.readouterr()
        with pytest.raises(SystemExit):
            await command('org', 'create', 'Browser-created')
        error = capsys.readouterr().err
        assert 'HTTP 426 — treg refused the call' in error
        assert 'the provider answered' not in error
        assert cli.CONFIG_PATH.read_bytes() == before
        assert len((await clients.get('/orgs')).json()) == before_orgs
        assert trace[-1] == ('POST', '/orgs', 426)
    outcomes['browser_team_create'] = await resources()
    # An existing saved unscoped identity token from the old server.
    cfg.update(token=sess.make_identity(claims['uid']), identity=True, active_org=team)
    outcomes['saved_token'] = await resources()
    cli._save_config(cfg)
    before = cli.CONFIG_PATH.read_bytes()
    email = (await clients.get('/auth/me')).json()['email']
    if version == 'current':
        await command('login', '--email', email)
    else:
        capsys.readouterr()
        with pytest.raises(SystemExit):
            await command('login', '--email', email)
        error = capsys.readouterr().err
        assert 'HTTP 426 — treg refused the call' in error
        assert 'the provider answered' not in error
        assert cli.CONFIG_PATH.read_bytes() == before
        assert trace[-1] == ('POST', '/auth/email/start', 426)
    outcomes['email_login'] = await resources() if cfg.get('active_org') else 'no team'
    await command('org', 'create', 'Email-created')
    outcomes['email_team_create'] = await resources()
    (tmp_path / 'outcomes.json').write_text(json.dumps(outcomes))
    print('COMPAT', version, json.dumps(outcomes), 'TRACE', trace)
    assert set(outcomes.values()) == {200}
