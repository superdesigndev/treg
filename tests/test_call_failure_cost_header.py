"""Pre-reserve metered refusals state X-Treg-Cost-Micro: 0; own-key pauses stay silent."""

from treg.application.call.types import (
    CallFailure,
    GatewayFailed,
    ReservationFailed,
    ResolutionFailed,
    cost_micro_for_failure,
)
from treg.routers.call import _translate_call_failure


def test_pre_reserve_metered_refusals_state_zero() -> None:
    cases = [
        ReservationFailed("money_admission_timeout", status_code=429, detail={
            "error": "money_admission_timeout",
            "message": "the team's money queue is busy; retry shortly. Nothing was reserved or charged.",
        }, retry_after_s=2),
        ResolutionFailed("provider_capacity", status_code=503, detail={
            "error": "provider_capacity_unavailable", "provider": "tikhub",
        }),
        ResolutionFailed("route_max_cost", status_code=402, detail={
            "error": "route_max_cost", "max_cost_micro": 1,
        }),
        ReservationFailed("insufficient_balance", status_code=402, detail={
            "error": "insufficient_balance", "balance_micro": 0,
        }),
    ]
    for exc in cases:
        assert cost_micro_for_failure(exc) == 0
        http = _translate_call_failure(exc)
        assert http.headers["X-Treg-Cost-Micro"] == "0"
        if exc.retry_after_s is not None:
            assert http.headers["Retry-After"] == str(exc.retry_after_s)


def test_detail_charged_micro_wins_over_the_kind_default() -> None:
    exc = ResolutionFailed("route_failed", status_code=502, detail={
        "error": "route_failed", "charged_micro": 8900,
    })
    assert cost_micro_for_failure(exc) == 8900
    assert _translate_call_failure(exc).headers["X-Treg-Cost-Micro"] == "8900"


def test_paused_and_gateway_failures_omit_the_header_without_a_named_charge() -> None:
    paused = ResolutionFailed("provider_paused", status_code=503, detail={
        "error": "provider_paused", "provider": "google-business-profile",
    })
    gateway = GatewayFailed("connect_failed", status_code=502, detail="upstream closed")
    unknown = CallFailure("target_not_found", status_code=404, detail="no such tool")
    for exc in (paused, gateway, unknown):
        assert cost_micro_for_failure(exc) is None
        assert not (_translate_call_failure(exc).headers or {})
