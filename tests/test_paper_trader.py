"""Tests for the Executor (paper/live bet placement)."""
import pytest
from unittest.mock import patch
from src.core.signal_engine import Signal


@pytest.fixture(autouse=True)
def temp_db(tmp_path):
    db = str(tmp_path / "test.db")
    with patch("config.settings.DB_PATH", db), patch("src.core.database.DB_PATH", db):
        from src.core.database import init_db
        init_db()
        yield db


def _make_signal(event_id: str = "e1") -> Signal:
    return Signal(
        sport="basketball_nba", event_id=event_id,
        event_name="Lakers vs Celtics", market="h2h",
        selection="Lakers", book="draftkings", american_odds=-110,
        win_probability=0.65, ai_confidence=0.68,
        composite_score=0.67, edge=0.05,
    )


def test_place_straight_bet_returns_id():
    from src.core.paper_trader import Executor
    executor = Executor()
    sizing = {"stake": 15.0, "kelly_fraction": 0.03, "edge": 0.05}
    with patch("src.core.paper_trader.insert_position", return_value=99) as mock_insert:
        pos_id = executor.place_straight_bet(_make_signal(), sizing)
    assert pos_id == 99
    assert mock_insert.called


def test_place_parlay_returns_ids():
    from src.core.paper_trader import Executor
    from src.core.parlay_builder import ParlayCandidate
    from unittest.mock import patch as _patch

    executor = Executor()
    legs = [_make_signal(f"e{i}") for i in range(2)]
    candidate = ParlayCandidate(
        legs=legs,
        combined_decimal=4.0,
        win_probability=0.42,
        ev=0.05,
        sizing={"stake": 10.0, "kelly_fraction": 0.02, "edge": 0.05},
    )

    call_count = {"n": 0}

    def fake_insert_pos(pos):
        call_count["n"] += 1
        return call_count["n"]

    def fake_insert_parlay(p):
        return 1

    with _patch("src.core.paper_trader.insert_position", side_effect=fake_insert_pos), \
         _patch("src.core.paper_trader.insert_parlay", return_value=1), \
         _patch("src.core.database.get_conn") as mock_conn:
        mock_conn.return_value.__enter__ = lambda s: mock_conn.return_value
        mock_conn.return_value.__exit__  = lambda s, *a: False
        mock_conn.return_value.execute   = lambda *a, **kw: None

        parlay_id, leg_ids = executor.place_parlay(candidate)

    assert parlay_id == 1
    assert len(leg_ids) == 2
