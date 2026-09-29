"""Settings and duration parsing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tapdrop.config import Settings, parse_duration


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("30s", timedelta(seconds=30)),
        ("15m", timedelta(minutes=15)),
        ("2h", timedelta(hours=2)),
        ("24h", timedelta(hours=24)),
        ("7d", timedelta(days=7)),
        ("1w", timedelta(weeks=1)),
        ("2", timedelta(seconds=2)),
        (" 2h ", timedelta(hours=2)),
        ("2H", timedelta(hours=2)),
    ],
)
def test_parse_duration_accepts_contract_forms(text: str, expected: timedelta) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "h", "2 years", "-3h", "0h", "2hh"])
def test_parse_duration_rejects_garbage(text: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(text)


def test_share_without_ttl_still_expires() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    settings = Settings(sources=["./data"], share=True)
    assert settings.resolve_down_at(now) == now + timedelta(hours=24)


def test_local_service_without_ttl_never_expires() -> None:
    settings = Settings(sources=["./data"])
    assert settings.resolve_down_at() is None


def test_explicit_ttl_wins_over_the_share_default() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    settings = Settings(sources=["./data"], share=True, ttl="2h")
    assert settings.resolve_down_at(now) == now + timedelta(hours=2)


def test_malformed_ttl_fails_at_startup() -> None:
    with pytest.raises(ValueError):
        Settings(sources=["./data"], ttl="banana")


def test_sources_come_from_a_comma_separated_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAPDROP_SOURCES", "./a, s3://b/c ,https://d/e.fits")
    settings = Settings()
    assert settings.sources == ["./a", "s3://b/c", "https://d/e.fits"]


def test_maxrec_is_clamped_to_the_hard_ceiling() -> None:
    settings = Settings(sources=["./data"], max_rows=10, hard_max_rows=100)
    assert settings.effective_maxrec(None) == 10
    assert settings.effective_maxrec(50) == 50
    assert settings.effective_maxrec(1_000) == 100
    # MAXREC=0 is "metadata only" in TAP, not "unset".
    assert settings.effective_maxrec(0) == 0


def test_negative_maxrec_is_rejected() -> None:
    settings = Settings(sources=["./data"])
    with pytest.raises(ValueError):
        settings.effective_maxrec(-1)


def test_token_moves_the_service_root() -> None:
    assert Settings(sources=["./d"]).root_path == ""
    assert Settings(sources=["./d"], token="secret").root_path == "/t/secret"


def test_s3_result_store_has_no_local_path() -> None:
    assert Settings(sources=["./d"], result_store="s3://bucket/results").result_store_path is None
    assert Settings(sources=["./d"], result_store="/tmp/r").result_store_path is not None
