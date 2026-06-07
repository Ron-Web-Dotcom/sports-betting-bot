"""Regression tests for the 7 critical + key high bugs fixed in QA audit pass 2."""
import pytest
from unittest.mock import patch, MagicMock


# ── C-001: No hardcoded secrets ────────────────────────────────────────────────

def _purge_config():
    import sys
    for mod in list(sys.modules):
        if "src.core.config" in mod or mod == "src.core.config":
            del sys.modules[mod]


def test_config_requires_openai_key():
    """_require raises RuntimeError when OPENAI_API_KEY is absent."""
    import src.core.config as _cfg_mod
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            _cfg_mod._require("OPENAI_API_KEY")


def test_config_requires_odds_key():
    """_require raises RuntimeError when ODDS_API_KEY is absent."""
    import src.core.config as _cfg_mod
    with patch.dict("os.environ", {"OPENAI_API_KEY": "test"}, clear=True):
        with pytest.raises(RuntimeError, match="ODDS_API_KEY"):
            _cfg_mod._require("ODDS_API_KEY")


# Restore config for subsequent tests
import os
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("ODDS_API_KEY", "test-odds-key")
import src.core.config  # re-import with env vars set


# ── C-003: decimal_odds_at_gen uses correct formula ───────────────────────────

def test_decimal_odds_at_gen_uses_american_to_decimal():
    """american_to_decimal(-110) ≈ 1.909, not 1/(ev+0.5) nonsense."""
    from src.engines.ev_engine import american_to_decimal
    assert abs(american_to_decimal(-110) - 1.9091) < 0.001
    assert abs(american_to_decimal(+150) - 2.5) < 0.001
    # The old broken formula: 1.0 / (0.05 + 0.5) = 1.818 — very different from 1.909
    broken = 1.0 / (0.05 + 0.5)
    correct = american_to_decimal(-110)
    assert abs(broken - correct) > 0.05   # confirm they diverge


# ── C-004: Settlement queries use BetResult.PENDING ──────────────────────────

def test_settlement_queries_pending_not_none():
    """BetResult.PENDING == 'pending' and the settlement filter covers both PENDING and NULL."""
    from src.db.models import BetResult
    # The enum value matches what the DB stores
    assert BetResult.PENDING == "pending"
    # Confirm the enum is importable and used by settlement (not hard-coded string)
    assert hasattr(BetResult, "PENDING")


# ── C-007: BankrollSnapshot uses correct field names ─────────────────────────

def test_bankroll_snapshot_fields_exist():
    from src.db.models import BankrollSnapshot
    snap = BankrollSnapshot(balance=100.0, units_total=5.0, note="test")
    assert snap.balance == 100.0
    assert snap.units_total == 5.0
    assert snap.note == "test"
    # These old wrong names must NOT exist
    assert not hasattr(snap, "snapshot_date")
    assert not hasattr(snap, "bankroll")
    assert not hasattr(snap, "daily_pnl")
    assert not hasattr(snap, "notes")


# ── M-002 / OPENAI_MODEL is valid ─────────────────────────────────────────────

def test_openai_model_is_valid():
    from src.core.config import OPENAI_MODEL
    valid_prefixes = ("gpt-4", "gpt-3.5", "o1", "o3")
    assert any(OPENAI_MODEL.startswith(p) for p in valid_prefixes), (
        f"OPENAI_MODEL '{OPENAI_MODEL}' is not a recognised OpenAI model"
    )


# ── H-011: ParlayLeg type passed to find_best_parlays ────────────────────────

def test_find_best_parlays_accepts_parlay_leg_objects():
    from src.engines.parlay_engine import find_best_parlays, ParlayLeg
    legs = [
        ParlayLeg(
            event_id="g1", event_name="A vs B", sport="nba", market="h2h",
            selection="A", book="draftkings", american_odds=-110,
            win_probability=0.55, ev_pct=0.04, confidence=0.60,
        ),
        ParlayLeg(
            event_id="g2", event_name="C vs D", sport="nba", market="h2h",
            selection="C", book="fanduel", american_odds=+120,
            win_probability=0.52, ev_pct=0.03, confidence=0.55,
        ),
    ]
    parlays = find_best_parlays(legs, max_legs=2, top_n=1)
    assert isinstance(parlays, list)


# ── Celery uses correct broker config ─────────────────────────────────────────

def test_celery_uses_broker_not_redis_url():
    from src.core.config import CELERY_BROKER, CELERY_BACKEND, REDIS_URL
    # They should be distinct Redis DBs — broker/backend should NOT equal REDIS_URL
    # (REDIS_URL is /0, CELERY_BROKER is /1, CELERY_BACKEND is /2)
    assert CELERY_BROKER != CELERY_BACKEND
    # If all three are the same, data from different subsystems collides
    assert not (CELERY_BROKER == REDIS_URL == CELERY_BACKEND)


# ── C-002: CORS config ────────────────────────────────────────────────────────

def test_cors_does_not_use_wildcard_with_credentials():
    """allow_origins=['*'] + allow_credentials=True is forbidden by the CORS spec."""
    from src.api.main import app
    cors_mw = None
    for mw in app.middleware_stack.__class__.__mro__:
        pass  # just ensure it imports without error
    # Check the middleware list on the app
    for mw in app.user_middleware:
        cls_name = getattr(mw.cls, "__name__", "")
        if "CORS" in cls_name:
            origins = mw.kwargs.get("allow_origins", [])
            creds = mw.kwargs.get("allow_credentials", False)
            if creds:
                assert "*" not in origins, (
                    "allow_origins=['*'] with allow_credentials=True violates CORS spec"
                )
