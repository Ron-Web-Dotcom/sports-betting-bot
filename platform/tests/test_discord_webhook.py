"""Tests for Discord webhook integration (webhook-only, no discord.py bot)."""
import pytest
import json
from unittest.mock import patch, AsyncMock, MagicMock


# ── Sanity: no discord.py bot code remains ────────────────────────────────────

def test_no_discord_bot_library():
    import inspect
    from src.discord_bot import bot
    src = inspect.getsource(bot)
    assert "commands.Bot" not in src
    assert "discord.Intents" not in src
    assert "bot.run(" not in src


def test_no_bot_token_in_config():
    import inspect
    from src.core import config
    src = inspect.getsource(config)
    assert "DISCORD_BOT_TOKEN" not in src
    assert "DISCORD_GUILD_ID" not in src


def test_webhook_url_used():
    import inspect
    from src.discord_bot import bot
    src = inspect.getsource(bot)
    assert "DISCORD_WEBHOOK_URL" in src
    assert "httpx" in src


# ── _post helper ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_post_sends_to_webhook():
    import src.discord_bot.bot as wb
    original_url = wb.DISCORD_WEBHOOK_URL

    mock_resp = MagicMock()
    mock_resp.status_code = 204

    with patch.object(wb, "DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/fake"):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            result = await wb._post({"content": "hello"})

    assert result is True
    mock_client.post.assert_called_once()
    call_kwargs = mock_client.post.call_args
    assert call_kwargs[0][0] == "https://discord.com/api/webhooks/fake"


@pytest.mark.asyncio
async def test_post_returns_false_when_no_url():
    import src.discord_bot.bot as wb
    with patch.object(wb, "DISCORD_WEBHOOK_URL", ""):
        result = await wb._post({"content": "hello"})
    assert result is False


@pytest.mark.asyncio
async def test_post_returns_false_on_non_204():
    import src.discord_bot.bot as wb
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = "bad request"

    with patch.object(wb, "DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/fake"):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client
            result = await wb._post({"content": "hello"})

    assert result is False


@pytest.mark.asyncio
async def test_post_returns_false_on_http_error():
    import httpx
    import src.discord_bot.bot as wb

    with patch.object(wb, "DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/fake"):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
            mock_client_cls.return_value = mock_client
            result = await wb._post({"content": "hello"})

    assert result is False


# ── _embed helper ─────────────────────────────────────────────────────────────

def test_embed_basic_structure():
    from src.discord_bot.bot import _embed
    e = _embed("Title", "Description", 0x2E7D32)
    assert e["title"] == "Title"
    assert e["description"] == "Description"
    assert e["color"] == 0x2E7D32
    assert "fields" not in e


def test_embed_with_fields():
    from src.discord_bot.bot import _embed
    fields = [{"name": "Odds", "value": "-110", "inline": True}]
    e = _embed("T", "D", 0, fields=fields)
    assert len(e["fields"]) == 1
    assert e["fields"][0]["name"] == "Odds"


def test_embed_title_truncated():
    from src.discord_bot.bot import _embed
    long_title = "A" * 300
    e = _embed(long_title, "D", 0)
    assert len(e["title"]) <= 256


def test_embed_description_truncated():
    from src.discord_bot.bot import _embed
    long_desc = "B" * 5000
    e = _embed("T", long_desc, 0)
    assert len(e["description"]) <= 4096


# ── Alert functions send embeds ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_post_pick_sends_embed():
    import src.discord_bot.bot as wb
    sent = []

    async def mock_post(payload):
        sent.append(payload)
        return True

    with patch.object(wb, "_post", side_effect=mock_post):
        await wb.post_pick({
            "recommendation": "BET", "bet": "Lakers ML", "game": "Lakers vs Celtics",
            "sport": "nba", "odds": -110, "units": 3, "ev_pct": 0.06,
            "confidence_pct": 70, "best_book": "draftkings", "reasoning": "Good value",
        })

    assert len(sent) == 1
    assert "embeds" in sent[0]
    assert sent[0]["embeds"][0]["title"].startswith("[BET]")


@pytest.mark.asyncio
async def test_post_result_won():
    import src.discord_bot.bot as wb
    sent = []

    async def mock_post(payload):
        sent.append(payload)
        return True

    with patch.object(wb, "_post", side_effect=mock_post):
        await wb.post_result({"bet": "Lakers ML", "actual_pnl_units": 0.91,
                               "game": "Lakers vs Celtics", "sport": "nba", "odds": -110},
                              "won")

    assert "[WIN]" in sent[0]["embeds"][0]["title"]


@pytest.mark.asyncio
async def test_post_result_lost():
    import src.discord_bot.bot as wb
    sent = []

    async def mock_post(p):
        sent.append(p)
        return True

    with patch.object(wb, "_post", side_effect=mock_post):
        await wb.post_result({"bet": "Celtics ML", "actual_pnl_units": -1.0,
                               "game": "Lakers vs Celtics", "sport": "nba", "odds": 110},
                              "lost")

    assert "[LOSS]" in sent[0]["embeds"][0]["title"]


@pytest.mark.asyncio
async def test_post_daily_summary():
    import src.discord_bot.bot as wb
    sent = []

    async def mock_post(p):
        sent.append(p)
        return True

    with patch.object(wb, "_post", side_effect=mock_post):
        await wb.post_daily_summary("3W-2L | +1.7u | ROI 17%")

    assert sent[0]["embeds"][0]["title"] == "Daily Summary"
    assert "1.7u" in sent[0]["embeds"][0]["description"]


@pytest.mark.asyncio
async def test_post_parlay_sends_embed():
    import src.discord_bot.bot as wb
    sent = []

    async def mock_post(p):
        sent.append(p)
        return True

    with patch.object(wb, "_post", side_effect=mock_post):
        await wb.post_parlay({
            "legs": [{"bet": "Lakers -3"}, {"bet": "Over 220"}],
            "combined_odds": 260, "combined_ev": 0.08, "units": 1,
        })

    assert "Parlay" in sent[0]["embeds"][0]["title"]
    assert "2 legs" in sent[0]["embeds"][0]["title"]


@pytest.mark.asyncio
async def test_send_to_channel_with_content():
    import src.discord_bot.bot as wb
    sent = []

    async def mock_post(p):
        sent.append(p)
        return True

    with patch.object(wb, "_post", side_effect=mock_post):
        await wb.send_to_channel("daily-summary", content="Hello")

    assert sent[0]["content"] == "Hello"


@pytest.mark.asyncio
async def test_send_to_channel_empty_sends_nothing():
    import src.discord_bot.bot as wb
    called = []

    async def mock_post(p):
        called.append(p)
        return True

    with patch.object(wb, "_post", side_effect=mock_post):
        result = await wb.send_to_channel("anything")

    assert called == []
    assert result is False
