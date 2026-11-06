from __future__ import annotations

import pytest

from .cli_go_harness import FakeRegistry, build_go


@pytest.fixture(scope="session")
def go_bin(tmp_path_factory):
    path = build_go(tmp_path_factory.mktemp("go"))
    if path is None:
        pytest.skip("set TREG_GO_BIN or put `go` on PATH to run the Go CLI parity tests")
    return path


@pytest.fixture
def fake():
    registry = FakeRegistry()
    yield registry
    registry.close()


@pytest.fixture(scope="session")
def live(tmp_path_factory):
    from .cli_go_live import LiveRegistry
    registry = LiveRegistry(tmp_path_factory.mktemp("live"))
    yield registry
    registry.close()


@pytest.fixture(scope="session")
def echo():
    from .cli_go_live import EchoUpstream
    upstream = EchoUpstream()
    yield upstream
    upstream.close()


@pytest.fixture(scope="session")
def team(live, echo):
    t = live.signup("dev@example.com", "Acme Parity")
    live.own_tool(t, "echo", echo.url)
    return t
