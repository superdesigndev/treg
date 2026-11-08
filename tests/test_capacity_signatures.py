"""Capacity signature classification: the table that decides when a provider response
means treg's own account is exhausted."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from treg.domain.capacity.signatures import (
    _blitzapi_body_reset,
    _parse_iso_reset,
    classify,
    is_exhausting,
)
from treg.timeutil import utcnow_naive


class TestBlitzAPIFairUseSignature:
    """BlitzAPI's monthly fair-use exhaustion (402) should be recognized as a quota signal
    with the reset time parsed from the body's next_reset_at field."""

    def test_fair_use_limit_reached_is_quota_signal(self):
        body = json.dumps({
            "error": "Fair Use limit reached",
            "records_remaining": 0,
            "next_reset_at": "2026-11-29T23:02:59Z",
        })
        signal = classify("blitzapi", 402, body=body)
        assert signal is not None
        assert signal.kind == "quota"
        assert is_exhausting(signal)

    def test_records_remaining_zero_is_quota_signal(self):
        body = json.dumps({
            "message": "Account limit",
            "records_remaining": 0,
            "fair_usage": {"next_reset_at": "2026-11-29T23:02:59Z"},
        })
        signal = classify("blitzapi", 402, body=body)
        assert signal is not None
        assert signal.kind == "quota"
        assert is_exhausting(signal)

    def test_reset_time_parsed_from_body_next_reset_at(self):
        reset_str = "2026-11-29T23:02:59Z"
        body = json.dumps({
            "error": "Fair Use limit reached",
            "records_remaining": 0,
            "next_reset_at": reset_str,
        })
        signal = classify("blitzapi", 402, body=body)
        assert signal is not None
        assert signal.resets_at == datetime(2026, 11, 29, 23, 2, 59)

    def test_reset_time_parsed_from_fair_usage_next_reset_at(self):
        reset_str = "2026-11-29T23:02:59Z"
        body = json.dumps({
            "error": "Fair Use limit reached",
            "records_remaining": 0,
            "fair_usage": {"next_reset_at": reset_str},
        })
        signal = classify("blitzapi", 402, body=body)
        assert signal is not None
        assert signal.resets_at == datetime(2026, 11, 29, 23, 2, 59)

    def test_header_reset_takes_precedence_over_body(self):
        now = utcnow_naive()
        body = json.dumps({
            "error": "Fair Use limit reached",
            "records_remaining": 0,
            "next_reset_at": "2099-01-01T00:00:00Z",
        })
        signal = classify("blitzapi", 402, headers={"retry-after": "3600"}, body=body, now=now)
        assert signal is not None
        assert signal.resets_at is not None
        expected_reset = now + timedelta(hours=1)
        assert abs((signal.resets_at - expected_reset).total_seconds()) < 2

    def test_blitzapi_402_without_fair_use_phrase_falls_to_generic(self):
        body = json.dumps({"error": "Something else", "code": 402})
        signal = classify("blitzapi", 402, body=body)
        assert signal is not None
        assert signal.kind == "balance"

    def test_non_blitzapi_402_uses_generic_balance(self):
        body = json.dumps({
            "error": "Fair Use limit reached",
            "records_remaining": 0,
            "next_reset_at": "2026-11-29T23:02:59Z",
        })
        signal = classify("other_provider", 402, body=body)
        assert signal is not None
        assert signal.kind == "balance"
        assert signal.resets_at is None


class TestBodyResetParsing:
    """Tests for the body-based reset time parsing functions."""

    def test_parse_iso_reset_with_z_suffix(self):
        result = _parse_iso_reset("2026-11-29T23:02:59Z")
        assert result == datetime(2026, 11, 29, 23, 2, 59)

    def test_parse_iso_reset_with_offset(self):
        result = _parse_iso_reset("2026-11-29T18:02:59-05:00")
        assert result == datetime(2026, 11, 29, 23, 2, 59)

    def test_parse_iso_reset_naive(self):
        result = _parse_iso_reset("2026-11-29T23:02:59")
        assert result == datetime(2026, 11, 29, 23, 2, 59)

    def test_parse_iso_reset_invalid_returns_none(self):
        assert _parse_iso_reset("not-a-date") is None
        assert _parse_iso_reset("") is None
        assert _parse_iso_reset(None) is None

    def test_blitzapi_body_reset_from_top_level(self):
        body = json.dumps({"next_reset_at": "2026-11-29T23:02:59Z"})
        result = _blitzapi_body_reset(body)
        assert result == datetime(2026, 11, 29, 23, 2, 59)

    def test_blitzapi_body_reset_from_fair_usage(self):
        body = json.dumps({"fair_usage": {"next_reset_at": "2026-11-29T23:02:59Z"}})
        result = _blitzapi_body_reset(body)
        assert result == datetime(2026, 11, 29, 23, 2, 59)

    def test_blitzapi_body_reset_prefers_fair_usage(self):
        body = json.dumps({
            "next_reset_at": "2099-01-01T00:00:00Z",
            "fair_usage": {"next_reset_at": "2026-11-29T23:02:59Z"},
        })
        result = _blitzapi_body_reset(body)
        assert result == datetime(2026, 11, 29, 23, 2, 59)

    def test_blitzapi_body_reset_invalid_json_returns_none(self):
        assert _blitzapi_body_reset("not json") is None
        assert _blitzapi_body_reset("") is None

    def test_blitzapi_body_reset_missing_field_returns_none(self):
        assert _blitzapi_body_reset(json.dumps({"other": "data"})) is None


class TestExistingSignatures:
    """Verify existing signatures still work correctly after the changes."""

    def test_generic_402_still_matches_unknown_provider(self):
        signal = classify("unknown_provider", 402, body=b"any error")
        assert signal is not None
        assert signal.kind == "balance"

    def test_lusha_quota_still_matches(self):
        signal = classify("lusha", 429, body=b"daily limit exceeded")
        assert signal is not None
        assert signal.kind == "quota"

    def test_hunter_quota_still_matches(self):
        signal = classify("hunter", 429, body=b"per billing period")
        assert signal is not None
        assert signal.kind == "quota"

    def test_enrow_balance_still_matches(self):
        signal = classify("enrow", 402, body=b"insufficient credits")
        assert signal is not None
        assert signal.kind == "balance"
