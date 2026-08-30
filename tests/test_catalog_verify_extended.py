"""Regression tests for the extended-catalog live verifier."""

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "catalog_verify_extended.py"
SPEC = importlib.util.spec_from_file_location("catalog_verify_extended", SCRIPT)
assert SPEC and SPEC.loader
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


class _Response:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class _Client:
    def __init__(self, payload):
        self.payload = payload

    def request(self, *_args, **_kwargs):
        return _Response(self.payload)


class _NoThrottle:
    def wait(self):
        pass


def _call(payload):
    endpoint = {
        "method": "POST",
        "path": "/search",
        "expect": {"json_path": "tasks.0.status_code", "equals": 20000},
    }
    return verify.call(
        _Client(payload), "https://example.com", endpoint, {}, {}, {}, _NoThrottle()
    )


def test_business_error_inside_http_200_fails_verification():
    ok, detail, *_ = _call({"tasks": [{"status_code": 40501, "cost": 0}]})

    assert ok is False
    assert "tasks.0.status_code=40501" in detail


def test_expected_business_status_passes_verification():
    ok, detail, *_ = _call({"tasks": [{"status_code": 20000, "cost": 0.102}]})

    assert ok is True
    assert "tasks.0.status_code=20000" in detail
