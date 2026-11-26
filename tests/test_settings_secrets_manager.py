from __future__ import annotations

import json

import boto3
import pytest

from treg.config import Settings

ARN = "arn:aws:secretsmanager:us-west-2:123456789012:secret:treg/prod"


class _FakeSecretsManager:
    def __init__(self, secret: dict[str, str]):
        self.secret = secret
        self.requests: list[dict] = []
        self.clients: list[tuple[str, str | None]] = []

    def get_secret_value(self, **request):
        self.requests.append(request)
        return {"SecretString": json.dumps(self.secret)}


@pytest.fixture
def secrets_manager(monkeypatch):
    fake = _FakeSecretsManager({"TREG_PLATFORM_KEY_TIKHUB": "from-secret", "TREG_RESEND_API_KEY": "re_secret"})

    def client(service, region_name=None, endpoint_url=None):
        fake.clients.append((service, region_name))
        return fake

    monkeypatch.setattr(boto3, "client", client)
    return fake


def test_secret_values_win_over_the_environment(monkeypatch, secrets_manager):
    monkeypatch.setenv("TREG_AWS_SECRET_ID", ARN)
    monkeypatch.setenv("TREG_PLATFORM_KEY_TIKHUB", "from-env")
    monkeypatch.setenv("TREG_PLATFORM_KEY_SERPAPI", "env-only")

    settings = Settings(_env_file=None)

    assert settings.platform_key_tikhub == "from-secret"
    assert settings.resend_api_key == "re_secret"
    assert settings.platform_key_serpapi == "env-only"
    assert secrets_manager.clients == [("secretsmanager", "us-west-2")]
    assert secrets_manager.requests == [{"SecretId": ARN}]


def test_without_a_secret_id_aws_is_never_called(monkeypatch, secrets_manager):
    monkeypatch.delenv("TREG_AWS_SECRET_ID", raising=False)
    monkeypatch.setenv("TREG_PLATFORM_KEY_TIKHUB", "from-env")

    assert Settings(_env_file=None).platform_key_tikhub == "from-env"
    assert secrets_manager.clients == []


def test_an_unreadable_secret_stops_startup(monkeypatch):
    def client(service, region_name=None, endpoint_url=None):
        raise RuntimeError("access denied")

    monkeypatch.setattr(boto3, "client", client)
    monkeypatch.setenv("TREG_AWS_SECRET_ID", ARN)

    with pytest.raises(RuntimeError, match="access denied"):
        Settings(_env_file=None)
