"""The light review command and stderr invitation."""
import json

import httpx
import pytest

from treg import cli


def client(monkeypatch, handler):
    monkeypatch.setattr(cli, '_client', lambda cfg: httpx.Client(
        transport=httpx.MockTransport(handler), base_url='https://registry.example.test'))


def run(*args):
    args = cli.build_parser().parse_args(['review', *args])
    args.fn(args, {})


@pytest.mark.parametrize('status,receipt_status', [(201, 'received'), (200, 'already_reviewed')])
def test_cli_review(monkeypatch, capsys, status, receipt_status):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(status, json={'review_id': 9, 'status': receipt_status})
    client(monkeypatch, handle)
    run('call-id', 'partly', '--reason', '  Some results helped.  ')
    assert json.loads(requests[0].content) == {
        'call_id': 'call-id', 'usefulness': 'partly', 'reason': 'Some results helped.'}
    assert requests[0].url.path == '/reviews'
    assert json.loads(capsys.readouterr().out) == {'review_id': 9, 'status': receipt_status}


@pytest.mark.parametrize('status,code', [(400, 'not_catalog_call'), (401, 'authentication_required'),
    (403, 'access_denied'), (404, 'not_found'), (422, 'invalid_review'), (503, 'submission_unconfirmed')])
def test_cli_review_errors_are_structured_and_private(monkeypatch, capsys, status, code):
    client(monkeypatch, lambda request: httpx.Response(status, json={'detail': 'private response'}))
    with pytest.raises(SystemExit):
        run('call-id', 'useful')
    output = capsys.readouterr()
    assert json.loads(output.out)['error'] == code
    assert 'private response' not in output.out + output.err


def test_cli_review_network_outcome_unconfirmed(monkeypatch, capsys):
    def handle(request):
        raise httpx.ReadTimeout('private transport data', request=request)
    client(monkeypatch, handle)
    with pytest.raises(SystemExit):
        run('call-id', 'not_sure')
    output = capsys.readouterr().out
    assert json.loads(output)['error'] == 'submission_unconfirmed'
    assert 'Could not confirm' in output
    assert 'private transport data' not in output


@pytest.mark.parametrize('args', [('bad@ref', 'useful'), ('id', 'useful', '--reason', '  '),
                                  ('id', 'useful', '--reason', 'x' * 201)])
def test_cli_review_rejects_invalid_fields_locally(monkeypatch, capsys, args):
    client(monkeypatch, lambda request: pytest.fail('invalid review must not send'))
    with pytest.raises(SystemExit):
        run(*args)
    assert json.loads(capsys.readouterr().out)['error'].startswith('invalid_')


@pytest.mark.parametrize('headers,expect', [
    ({'X-Treg-Hint': 'review'}, 'treg review call-id'),
    ({'X-Treg-Review': 'requested'}, 'treg review call-id'),  # a pre-0.19 registry
    ({'X-Treg-Hint': 'feedback'}, 'treg feedback submit <quality|pricing|friction|other> "what you saw" --call-id call-id'),
    ({'X-Treg-Hint': 'unknown-kind'}, None),
    ({}, None),
])
def test_invitation_only_on_stderr(capsys, headers, expect):
    response = httpx.Response(200, content=b'raw upstream text', headers={'X-Treg-Call-Id': 'call-id', **headers})
    cli._show_call_response(response)
    output = capsys.readouterr()
    assert output.out == 'raw upstream text\n'
    assert (expect in output.err) if expect else (output.err == '')


def test_invitation_needs_a_call_id(capsys):
    cli._show_call_response(httpx.Response(200, content=b'x', headers={'X-Treg-Hint': 'feedback'}))
    assert capsys.readouterr().err == ''


def test_cli_review_help_shares_description():
    from treg.feedback_contract import REVIEW_DESCRIPTION
    parser = cli.build_parser()
    sub = next(action for action in parser._actions if hasattr(action, 'choices') and isinstance(action.choices, dict))
    assert REVIEW_DESCRIPTION in sub.choices['review'].format_help().replace('\n', ' ') or sub.choices['review'].description == REVIEW_DESCRIPTION


def test_invitation_preserves_pretty_printed_json(capsys):
    body = '{"items": [1,  2]}\n'
    cli._show_call_response(httpx.Response(200, text=body, headers={
        'content-type': 'application/json', 'X-Treg-Call-Id': 'call-id', 'X-Treg-Review': 'requested',
    }))
    output = capsys.readouterr()
    assert output.out == json.dumps(json.loads(body), indent=2) + "\n"
    assert 'treg review call-id' in output.err
