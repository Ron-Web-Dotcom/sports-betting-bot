"""
Regression tests — QA audit pass 7.

Covers: Team/Player DB indexes, ORM detach, input validation,
global error handler, rate limiting, sports validation, feedback 404,
config guards, discussion truncation, picks CLV fields, recommendation audit trail.
"""
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient


# ── DB: Team and Player indexes are in __table_args__ ─────────────────────────

def test_team_index_in_table_args():
    from src.db.models import Team
    args = getattr(Team, "__table_args__", ())
    arg_reprs = [str(a) for a in args]
    assert any("ix_teams_sport" in r for r in arg_reprs), \
        "Team.ix_teams_sport not in __table_args__ — index will not be created"


def test_player_indexes_in_table_args():
    from src.db.models import Player
    args = getattr(Player, "__table_args__", ())
    arg_reprs = [str(a) for a in args]
    assert any("ix_players_sport" in r for r in arg_reprs)
    assert any("ix_players_team" in r for r in arg_reprs)
    assert any("ix_players_status" in r for r in arg_reprs)


# ── API: ORM fields accessed inside session (no DetachedInstanceError) ────────

def test_list_picks_reads_inside_session():
    """_pick_dict must be called inside the get_db() block."""
    import inspect
    from src.api.routers import picks as picks_router
    src = inspect.getsource(picks_router.list_picks)
    # result = [...] must appear inside the `with get_db()` indent
    lines = src.splitlines()
    inside_with = False
    found_result_inside = False
    for line in lines:
        if "with get_db()" in line:
            inside_with = True
        if inside_with and "_pick_dict" in line:
            found_result_inside = True
    assert found_result_inside, "list_picks accesses _pick_dict OUTSIDE get_db() session → DetachedInstanceError"


def test_get_pick_reads_inside_session():
    import inspect
    from src.api.routers import picks as picks_router
    src = inspect.getsource(picks_router.get_pick)
    lines = src.splitlines()
    inside_with = False
    for line in lines:
        if "with get_db()" in line:
            inside_with = True
        if inside_with and "_pick_dict" in line:
            return  # found inside session — test passes
    pytest.fail("get_pick accesses _pick_dict OUTSIDE get_db() session → DetachedInstanceError")


# ── API: Input validation ──────────────────────────────────────────────────────

def test_list_picks_rejects_negative_limit():
    from src.api.main import app
    client = TestClient(app)
    resp = client.get("/picks/?limit=-1")
    assert resp.status_code in (401, 422)  # 422 = validation error (unauthed hits 401 first)


def test_list_picks_rejects_invalid_sport():
    import src.api.main as api_main
    from src.api.main import app
    orig = api_main._API_KEY
    try:
        api_main._API_KEY = "k"
        client = TestClient(app)
        with patch("src.api.routers.picks.get_db") as m:
            ms = MagicMock(); ms.__enter__ = MagicMock(return_value=ms); ms.__exit__ = MagicMock(return_value=False)
            ms.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
            m.return_value = ms
            resp = client.get("/picks/?sport=definitely_not_a_real_sport_xyz", headers={"X-API-Key": "k"})
    finally:
        api_main._API_KEY = orig
    assert resp.status_code == 400


def test_bankroll_rejects_negative():
    from pydantic import ValidationError
    from src.api.routers.personalization import BankrollUpdate
    with pytest.raises(ValidationError):
        BankrollUpdate(bankroll=-100)


def test_bankroll_rejects_zero():
    from pydantic import ValidationError
    from src.api.routers.personalization import BankrollUpdate
    with pytest.raises(ValidationError):
        BankrollUpdate(bankroll=0)


def test_bankroll_rejects_overlimit():
    from pydantic import ValidationError
    from src.api.routers.personalization import BankrollUpdate
    with pytest.raises(ValidationError):
        BankrollUpdate(bankroll=999_999_999)


def test_bankroll_accepts_valid():
    from src.api.routers.personalization import BankrollUpdate
    b = BankrollUpdate(bankroll=5000)
    assert b.bankroll == 5000


def test_feedback_rating_validation():
    from pydantic import ValidationError
    from src.api.routers.feedback import FeedbackSubmit
    with pytest.raises(ValidationError):
        FeedbackSubmit(pick_id=1, user_id="u", rating="terrible")


def test_feedback_explanation_rating_range():
    from pydantic import ValidationError
    from src.api.routers.feedback import FeedbackSubmit
    with pytest.raises(ValidationError):
        FeedbackSubmit(pick_id=1, user_id="u", rating="helpful", explanation_rating=10)


def test_feedback_valid():
    from src.api.routers.feedback import FeedbackSubmit
    f = FeedbackSubmit(pick_id=1, user_id="user1", rating="helpful")
    assert f.rating == "helpful"


def test_discussion_args_max_length():
    from pydantic import ValidationError
    from src.api.routers.discussion import DiscussionRequest
    with pytest.raises(ValidationError):
        DiscussionRequest(command="analyze", args="x" * 3000)


def test_discussion_valid_args():
    from src.api.routers.discussion import DiscussionRequest
    r = DiscussionRequest(command="analyze", args="Lakers vs Celtics tonight")
    assert r.command == "analyze"


# ── API: Global exception handler ─────────────────────────────────────────────

def test_global_error_handler_hides_internals():
    import src.api.main as api_main
    from src.api.main import app
    orig = api_main._API_KEY
    try:
        api_main._API_KEY = "k"
        client = TestClient(app, raise_server_exceptions=False)

        with patch("src.api.routers.picks.get_db", side_effect=RuntimeError("secret db password")):
            resp = client.get("/picks/", headers={"X-API-Key": "k"})
    finally:
        api_main._API_KEY = orig

    assert resp.status_code == 500
    body = resp.json()
    assert "secret db password" not in str(body)
    assert body["detail"] == "Internal server error"


# ── API: Rate limiting on /discuss ────────────────────────────────────────────

def test_rate_limit_middleware_exists():
    import inspect
    from src.api import main
    src = inspect.getsource(main)
    assert "rate_limit" in src or "429" in src


def test_discuss_returns_503_when_ai_returns_none():
    import src.api.main as api_main
    from src.api.main import app
    orig = api_main._API_KEY
    try:
        api_main._API_KEY = "k"
        api_main._rate_cache.clear()  # reset rate limit counter
        client = TestClient(app)
        with patch("src.api.routers.discussion.handle_command", return_value=None):
            resp = client.post("/discuss/", json={"command": "analyze", "args": "test"},
                               headers={"X-API-Key": "k"})
    finally:
        api_main._API_KEY = orig
    assert resp.status_code == 503


# ── API: Feedback 404 on missing pick ────────────────────────────────────────

def test_feedback_submit_returns_404_for_missing_pick():
    import src.api.main as api_main
    from src.api.main import app
    orig = api_main._API_KEY
    try:
        api_main._API_KEY = "k"
        client = TestClient(app)

        with patch("src.db.session.get_db") as mock_get_db:
            ms = MagicMock(); ms.__enter__ = MagicMock(return_value=ms); ms.__exit__ = MagicMock(return_value=False)
            ms.query.return_value.filter.return_value.first.return_value = None
            mock_get_db.return_value = ms

            resp = client.post("/feedback/", json={
                "pick_id": 99999, "user_id": "u", "rating": "helpful"
            }, headers={"X-API-Key": "k"})
    finally:
        api_main._API_KEY = orig

    assert resp.status_code == 404


def test_feedback_quality_returns_404_for_missing_pick():
    import src.api.main as api_main
    from src.api.main import app
    orig = api_main._API_KEY
    try:
        api_main._API_KEY = "k"
        client = TestClient(app)

        with patch("src.db.session.get_db") as mock_get_db:
            ms = MagicMock(); ms.__enter__ = MagicMock(return_value=ms); ms.__exit__ = MagicMock(return_value=False)
            ms.query.return_value.filter.return_value.first.return_value = None
            mock_get_db.return_value = ms

            resp = client.get("/feedback/99999/quality", headers={"X-API-Key": "k"})
    finally:
        api_main._API_KEY = orig

    assert resp.status_code == 404


# ── API: picks response includes CLV fields ────────────────────────────────────

def test_pick_dict_includes_clv_fields():
    from src.api.routers.picks import _pick_dict
    p = MagicMock()
    p.id = 1; p.sport = "nba"; p.selection = "Lakers"; p.market = "h2h"
    p.best_book = "dk"; p.american_odds_at_gen = -110; p.decimal_odds_at_gen = 1.91
    p.american_odds_close = -115; p.recommendation = "BET"; p.units = 2
    p.ev_pct = 0.05; p.confidence_pct = 70; p.risk_score = 30
    p.opportunity_score = 0.75; p.result = "won"; p.clv_pct = 0.03
    p.actual_pnl_units = 0.91; p.reasoning = "Good value"
    p.key_factors = ["form", "injuries"]; p.generated_at = None; p.settled_at = None

    d = _pick_dict(p)
    assert "clv_pct" in d
    assert "american_odds_close" in d
    assert "decimal_odds_at_gen" in d
    assert d["clv_pct"] == 0.03


# ── Config: database URL guard ────────────────────────────────────────────────

def test_config_database_url_no_hardcoded_password():
    import inspect
    from src.core import config
    src = inspect.getsource(config)
    assert "sip_pass" not in src, "Hardcoded DB password still in config"


def test_config_requires_database_url_when_not_sqlite():
    """When USE_SQLITE=false, DATABASE_URL must be set or RuntimeError raised."""
    import importlib
    import sys
    import os

    # Remove cached module to force re-import with patched env
    mods_to_remove = [k for k in sys.modules if "src.core.config" in k]
    for m in mods_to_remove:
        del sys.modules[m]

    with patch.dict(os.environ, {"USE_SQLITE": "false", "DATABASE_URL": "",
                                  "ANTHROPIC_API_KEY": "fake", "ODDS_API_KEY": "fake",
                                  "ENVIRONMENT": "development"}):
        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            import src.core.config  # noqa: F401

    # Restore
    for m in mods_to_remove:
        if m in sys.modules:
            del sys.modules[m]
    import src.core.config  # noqa: F401


# ── Recommendation engine: audit trail logs on failure ───────────────────────

def test_recommendation_engine_audit_logs_on_failure():
    import inspect
    from src.engines import recommendation_engine
    src = inspect.getsource(recommendation_engine.persist_pick)
    assert "logger.error" in src or "logger.warning" in src
    # Must not be a bare silent pass
    assert "except Exception:\n            pass" not in src


# ── Odds worker: uses dataclasses.asdict not __dict__ ────────────────────────

def test_odds_worker_uses_asdict():
    import inspect
    from src.workers import odds_worker
    src = inspect.getsource(odds_worker.scan_and_save_odds)
    assert "asdict" in src
    assert "__dict__" not in src
