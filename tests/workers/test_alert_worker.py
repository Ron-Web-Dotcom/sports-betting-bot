"""Tests for alert_worker."""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from src.workers.alert_worker import _run_async, send_pick_alerts, send_parlay_alerts, send_result_alert


def test_run_async_executes_coroutine():
    import asyncio

    async def _coro():
        return 42

    result = _run_async(_coro())
    assert result == 42


def test_run_async_does_not_reuse_loops():
    """asyncio.run() must be used, not get_event_loop()."""
    import inspect
    import src.workers.alert_worker as aw
    src_lines = inspect.getsource(aw._run_async)
    assert "asyncio.run(" in src_lines
    assert "get_event_loop" not in src_lines


def test_send_pick_alerts_calls_post_pick():
    async def mock_post(pick):
        return None

    with patch("src.discord_bot.bot.post_pick", side_effect=mock_post):
        send_pick_alerts([{"bet": "Lakers ML", "sport": "nba"}])


def test_send_pick_alerts_continues_on_error():
    """One failing alert should not stop the rest."""
    call_count = []

    async def mock_post(pick):
        call_count.append(1)
        if len(call_count) == 1:
            raise Exception("Discord down")

    with patch("src.discord_bot.bot.post_pick", side_effect=mock_post):
        # Should not raise even if first pick fails
        send_pick_alerts([{"bet": "A"}, {"bet": "B"}])

    assert len(call_count) == 2


def test_send_parlay_alerts_calls_post_parlay():
    async def mock_post(parlay):
        return None

    with patch("src.discord_bot.bot.post_parlay", side_effect=mock_post):
        send_parlay_alerts([{"legs": [], "combined_odds": 300}])


def test_send_result_alert_calls_post_result():
    async def mock_post(pick, result):
        return None

    with patch("src.discord_bot.bot.post_result", side_effect=mock_post):
        send_result_alert({"bet": "Lakers"}, "won")


def test_send_result_alert_handles_discord_failure():
    async def mock_post(pick, result):
        raise Exception("timeout")

    with patch("src.discord_bot.bot.post_result", side_effect=mock_post):
        # Must not raise — error is logged
        send_result_alert({"bet": "Lakers"}, "won")
