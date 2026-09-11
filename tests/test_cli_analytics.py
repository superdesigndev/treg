"""Exercise the installed SDK and real CLI process against local ingestion."""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from treg import cli, cli_analytics


@pytest.fixture
def ingestion():
    events = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            events.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}', events
    server.shutdown()
    server.server_close()
    thread.join()


def test_real_cli_sdk_delivery_and_opt_out(tmp_path, ingestion):
    host, events = ingestion
    env = {**os.environ, 'TREG_CONFIG': str(tmp_path / 'config.json'),
           'TREG_TELEMETRY': '1', 'DO_NOT_TRACK': '0',
           'PYTHONPYCACHEPREFIX': str(tmp_path / 'bytecode'),
           'TREG_CLI_POSTHOG_KEY': 'phc_test', 'TREG_CLI_POSTHOG_HOST': host}

    def run(*args):
        # This test asserts delivery, which the production one-second best-effort budget
        # cannot guarantee during cold SDK imports under xdist load. Exercise the real
        # CLI/SDK with a longer test-only join; the slow-ingestion test below separately
        # verifies the unchanged production exit budget.
        entry = ('from treg import cli, cli_analytics; '
                 'cli_analytics.EXIT_WAIT_SECONDS = 10; cli.main()')
        return subprocess.run([sys.executable, '-c', entry, *args], env=env,
                              capture_output=True, text=True, timeout=30)

    result = run('version')
    assert result.returncode == 0
    assert result.stdout.strip() and not result.stderr
    assert len(events) == 1
    event = events[0]['batch'][0]
    assert event['event'] == 'cli_command_completed'
    props = event['properties']
    assert props['command'] == 'version'
    assert props['success'] is True
    assert props['exit_code'] == 0
    assert props['duration_ms'] >= 0
    assert props['cli_version'] and props['os']
    identifier = (tmp_path / 'analytics-id').read_text()
    assert event.get('distinct_id', props.get('distinct_id')) == identifier

    # Raw argument contents must never enter an event.
    result = run('secret', 'add', 'private-name', '--value', 'secret-token')
    assert result.returncode != 0
    assert len(events) == 2
    failed = events[-1]['batch'][0]['properties']
    assert failed['command'] == 'secret_add'
    assert failed['success'] is False
    assert failed['exit_code'] == result.returncode
    assert events[-1]['batch'][0].get('distinct_id', failed.get('distinct_id')) == identifier
    assert 'private-name' not in json.dumps(events)
    assert run('--help').returncode == 0
    assert len(events) == 2
    assert 'secret-token' not in json.dumps(events)
    env['TREG_TELEMETRY'] = '0'
    count = len(events)
    assert run('version').returncode == 0
    assert len(events) == count
    assert run('--help').returncode == 0


@pytest.mark.parametrize('error, expected', [(None, 0), (SystemExit(3), 3),
    (SystemExit('private error'), 1), (KeyboardInterrupt(), 130), (RuntimeError('private error'), 1)])
def test_dispatch_result_preserved(monkeypatch, tmp_path, error, expected):
    def cmd_version(args, cfg):
        if error is not None:
            raise error
    monkeypatch.setattr(cli, 'cmd_version', cmd_version)
    monkeypatch.setattr(cli, 'CONFIG_PATH', tmp_path / 'config.json')
    captured = []
    monkeypatch.setattr(cli_analytics, 'track_command', lambda **kwargs: captured.append(kwargs))
    if error is None:
        cli.main(['version'])
    else:
        with pytest.raises(type(error)) as raised:
            cli.main(['version'])
        assert raised.value is error
    assert captured[0]['exit_code'] == expected
    assert captured[0]['command'] == 'version'


@pytest.mark.parametrize('env, base', [({'TREG_TELEMETRY': '0'}, 'https://treg.to'),
    ({'DO_NOT_TRACK': '1'}, 'https://treg.to'), ({}, 'https://self.example')])
def test_disabled_has_no_side_effects(monkeypatch, tmp_path, env, base):
    for key in ('TREG_TELEMETRY', 'DO_NOT_TRACK', 'TREG_CLI_POSTHOG_KEY'):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(cli_analytics, '_send', lambda *args: pytest.fail('unexpected send'))
    cli_analytics.track_command(command='version', exit_code=0, duration_ms=1,
                                base_url=base, config_path=tmp_path / 'config.json')
    assert not list(tmp_path.iterdir())


def test_slow_ingestion_does_not_block_exit(monkeypatch, tmp_path):
    monkeypatch.setenv('TREG_TELEMETRY', '1')
    monkeypatch.delenv('DO_NOT_TRACK', raising=False)
    finished = threading.Event()
    monkeypatch.setattr(cli_analytics, '_send', lambda *args: finished.wait(5))
    started = time.monotonic()
    try:
        cli_analytics.track_command(command='version', exit_code=0, duration_ms=1,
                                    base_url='https://treg.to', config_path=tmp_path / 'config.json')
        assert time.monotonic() - started < 1.5
    finally:
        finished.set()


def test_sdk_failure_is_silent(monkeypatch, tmp_path, capsys):
    import posthog

    def fail(*args, **kwargs):
        raise RuntimeError("ingestion unavailable")

    monkeypatch.setattr(posthog, "Posthog", fail)
    cli_analytics._send("phc_test", "http://127.0.0.1:1", tmp_path / "config.json", {})
    assert capsys.readouterr() == ("", "")
