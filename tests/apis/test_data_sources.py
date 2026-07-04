"""
Tests for all data source adapters.

All tests mock the HTTP layer — no real network calls.
We test that each adapter correctly parses responses and handles failures.
"""
import pytest
from unittest.mock import patch, MagicMock


# ── ESPN ───────────────────────────────────────────────────────────────────────

class TestESPN:
    def test_fetch_injuries_parses_response(self):
        mock_response = {
            "injuries": [
                {
                    "team": {"displayName": "Los Angeles Lakers"},
                    "injuries": [
                        {
                            "athlete": {
                                "displayName": "LeBron James",
                                "position": {"abbreviation": "SF"},
                            },
                            "status": "Questionable",
                            "shortComment": "Left ankle soreness",
                        }
                    ],
                }
            ]
        }
        with patch("src.apis.espn.get_json", return_value=mock_response):
            from src.apis.espn import fetch_injuries
            result = fetch_injuries("basketball_nba")

        assert len(result) == 1
        assert result[0]["player"] == "LeBron James"
        assert result[0]["status"] == "questionable"
        assert result[0]["source"] == "espn"

    def test_fetch_injuries_unknown_sport_returns_empty(self):
        from src.apis.espn import fetch_injuries
        result = fetch_injuries("unknown_sport_xyz")
        assert result == []

    def test_fetch_injuries_api_failure_returns_empty(self):
        with patch("src.apis.espn.get_json", return_value=None):
            from src.apis.espn import fetch_injuries
            result = fetch_injuries("basketball_nba")
        assert result == []

    def test_fetch_news_parses_articles(self):
        mock_response = {
            "articles": [
                {"headline": "Lakers injury update", "description": "LeBron out tonight", "published": "2024-01-15T18:00:00Z"},
                {"headline": "Warriors trade news", "description": "Curry returns", "published": "2024-01-15T17:00:00Z"},
            ]
        }
        with patch("src.apis.espn.get_json", return_value=mock_response):
            from src.apis.espn import fetch_news
            result = fetch_news("basketball_nba", limit=2)

        assert len(result) == 2
        assert result[0]["headline"] == "Lakers injury update"
        assert result[0]["source"] == "espn"

    def test_fetch_scoreboard_parses_games(self):
        mock_response = {
            "events": [
                {
                    "id": "401234",
                    "name": "Lakers vs Celtics",
                    "date": "2024-01-15T20:00Z",
                    "competitions": [{
                        "competitors": [
                            {"homeAway": "home", "team": {"displayName": "Lakers"}, "score": ""},
                            {"homeAway": "away", "team": {"displayName": "Celtics"}, "score": ""},
                        ]
                    }],
                    "status": {"type": {"description": "Scheduled", "completed": False}},
                }
            ]
        }
        with patch("src.apis.espn.get_json", return_value=mock_response):
            from src.apis.espn import fetch_scoreboard
            result = fetch_scoreboard("basketball_nba")

        assert len(result) == 1
        assert result[0]["home_team"] == "Lakers"
        assert result[0]["away_team"] == "Celtics"
        assert result[0]["source"] == "espn"


# ── StatMuse ───────────────────────────────────────────────────────────────────

class TestStatMuse:
    _NEXT_DATA = """<script id="__NEXT_DATA__" type="application/json">
    {"props":{"pageProps":{"data":{
        "players":[{"pts":27.3,"reb":7.1,"ast":7.4,"gp":55}],
        "summary":{"text":"LeBron James averaged 27.3 points per game this season."}
    }}}}
    </script>"""

    def test_player_season_stats_parses_next_data(self):
        with patch("src.apis.statmuse.get_html", return_value=self._NEXT_DATA):
            from src.apis.statmuse import player_season_stats
            result = player_season_stats("LeBron James", "basketball_nba")

        assert result["player"] == "LeBron James"
        assert "27.3" in result["summary"] or result["stats"].get("pts") == 27.3
        assert result["source"] == "statmuse"

    def test_player_season_stats_http_failure_returns_empty(self):
        with patch("src.apis.statmuse.get_html", return_value=None):
            from src.apis.statmuse import player_season_stats
            result = player_season_stats("LeBron James", "basketball_nba")

        assert result["stats"] == {}
        assert result["source"] == "statmuse"

    def test_head_to_head_structure(self):
        with patch("src.apis.statmuse.get_html", return_value=self._NEXT_DATA):
            from src.apis.statmuse import head_to_head
            result = head_to_head("Lakers", "Celtics", "basketball_nba")

        assert result["home"] == "Lakers"
        assert result["away"] == "Celtics"
        assert result["source"] == "statmuse"

    def test_query_returns_summary(self):
        with patch("src.apis.statmuse.get_html", return_value=self._NEXT_DATA):
            from src.apis.statmuse import query
            result = query("LeBron James points last 10 games", "basketball_nba")

        assert result["source"] == "statmuse"
        assert "query" in result


# ── Ball Don't Lie ─────────────────────────────────────────────────────────────

class TestBallDontLie:
    def test_search_player_returns_list(self):
        mock_response = {
            "data": [
                {"id": 237, "first_name": "LeBron", "last_name": "James",
                 "position": "F", "team": {"full_name": "Los Angeles Lakers"}}
            ]
        }
        with patch("src.apis.balldontlie.get_json", return_value=mock_response):
            from src.apis.balldontlie import search_player
            result = search_player("LeBron")

        assert len(result) == 1
        assert result[0]["name"] == "LeBron James"
        assert result[0]["source"] == "balldontlie"

    def test_player_season_averages(self):
        mock_response = {
            "data": [{"pts": 27.3, "reb": 7.1, "ast": 7.4, "fg_pct": 0.54, "games_played": 55}]
        }
        with patch("src.apis.balldontlie.get_json", return_value=mock_response):
            from src.apis.balldontlie import player_season_averages
            result = player_season_averages(237, 2023)

        assert result["pts"] == 27.3
        assert result["source"] == "balldontlie"

    def test_empty_response_returns_empty(self):
        with patch("src.apis.balldontlie.get_json", return_value={"data": []}):
            from src.apis.balldontlie import player_season_averages
            result = player_season_averages(999, 2023)
        assert result == {}


# ── Weather ────────────────────────────────────────────────────────────────────

class TestWeather:
    def test_impact_high_wind_bets_under(self):
        from src.apis.weather import _assess_impact
        impact = _assess_impact(temp_f=45, wind_mph=25, precip_mm=0)
        assert impact["bet_under"] is True
        assert impact["total_adjustment"] < -0.10

    def test_impact_mild_conditions_no_signal(self):
        from src.apis.weather import _assess_impact
        impact = _assess_impact(temp_f=72, wind_mph=5, precip_mm=0)
        assert impact["bet_under"] is False
        assert impact["total_adjustment"] == 0.0

    def test_impact_freezing_temp_reduces_scoring(self):
        from src.apis.weather import _assess_impact
        impact = _assess_impact(temp_f=15, wind_mph=8, precip_mm=0)
        assert any("freezing" in f.lower() or "cold" in f.lower() for f in impact["factors"])
        assert impact["total_adjustment"] < -0.10

    def test_weather_not_relevant_for_indoor_sports(self):
        from src.apis.weather import WEATHER_RELEVANT
        assert "basketball_nba" not in WEATHER_RELEVANT
        assert "americanfootball_nfl" in WEATHER_RELEVANT
        assert "baseball_mlb" in WEATHER_RELEVANT


# ── Action Network ─────────────────────────────────────────────────────────────

class TestActionNetwork:
    def test_detect_sharp_action_signals_reverse_line(self):
        from src.apis.action_network import detect_sharp_action
        consensus = {
            "home": "Lakers",
            "away": "Celtics",
            "lines": [{
                "market":        "moneyline",
                "home_bets_pct": 75,
                "away_bets_pct": 25,
                "home_money_pct": 35,
                "away_money_pct": 65,
            }]
        }
        signals = detect_sharp_action(consensus)
        assert len(signals) > 0
        assert "Sharp money" in signals[0]
        assert "Celtics" in signals[0]

    def test_no_sharp_signal_when_balanced(self):
        from src.apis.action_network import detect_sharp_action
        consensus = {
            "home": "Lakers", "away": "Celtics",
            "lines": [{
                "market": "moneyline",
                "home_bets_pct": 52, "away_bets_pct": 48,
                "home_money_pct": 54, "away_money_pct": 46,
            }]
        }
        signals = detect_sharp_action(consensus)
        assert signals == []


# ── Data Hub ───────────────────────────────────────────────────────────────────

class TestDataHub:
    def test_build_game_context_structure(self):
        _ALL = [
            "src.apis.data_hub._fetch_scoreboard_espn",
            "src.apis.data_hub._fetch_sharp_action",
            "src.apis.data_hub._fetch_pinnacle_signal",
            "src.apis.data_hub._fetch_weather",
            "src.apis.data_hub._fetch_trending",
            "src.apis.data_hub._fetch_sleeper_injuries",
            "src.apis.data_hub._fetch_sportsdataio",
            "src.apis.data_hub._fetch_kalshi_markets",
            "src.apis.data_hub._fetch_sportradar_game",
            "src.apis.data_hub._fetch_thesportsdb",
        ]
        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(patch("src.apis.data_hub._fetch_injuries_espn", return_value=[{"player": "X"}]))
            stack.enter_context(patch("src.apis.data_hub._fetch_news_espn", return_value=[{"headline": "Y"}]))
            for p in _ALL:
                stack.enter_context(patch(p, return_value=[]))
            from src.apis.data_hub import build_game_context
            ctx = build_game_context("basketball_nba", "Lakers", "Celtics", "2024-01-15T20:00:00Z")

        assert ctx["sport"] == "basketball_nba"
        assert ctx["home_team"] == "Lakers"
        assert "sources_used" in ctx
        assert "data_completeness" in ctx
        assert 0.0 <= ctx["data_completeness"] <= 1.0

    def test_completeness_score_with_all_data(self):
        from src.apis.data_hub import _score_completeness
        context = {
            "thesportsdb":        {"available": True},
            "sportradar":         {"available": True},
            "injuries_espn_home": [{"player": "X"}],
            "news_espn":          [{"headline": "Z"}],
            "kalshi_markets":     [{"market": "A"}],
            "sportsdataio":       {"standings": []},
            "mlb_stats":          {"pitcher": "X"},
        }
        score = _score_completeness(context)
        assert score == 1.0

    def test_completeness_score_with_no_data(self):
        from src.apis.data_hub import _score_completeness
        score = _score_completeness({})
        # No thesportsdb → core_score=0.0, no bonuses → completeness is 0.0
        assert score == pytest.approx(0.0)


# ── Prop change detection ──────────────────────────────────────────────────────

class TestPropChangeDetection:
    _PROP = {
        "subject": "LeBron James", "stat": "Points", "sport_key": "basketball_nba",
        "line": 25.5, "source": "prizepicks",
    }

    def test_moved_line_detected(self):
        from src.workers.odds_worker import _detect_prop_changes
        prev = [{**self._PROP, "line": 25.5}]
        curr = [{**self._PROP, "line": 27.5}]
        changes = _detect_prop_changes(prev, curr, "prizepicks")
        assert len(changes) == 1
        assert changes[0]["change_type"] == "moved"
        assert changes[0]["old_line"] == 25.5
        assert changes[0]["new_line"] == 27.5

    def test_added_prop_detected(self):
        from src.workers.odds_worker import _detect_prop_changes
        changes = _detect_prop_changes([], [self._PROP], "prizepicks")
        assert len(changes) == 1
        assert changes[0]["change_type"] == "added"
        assert changes[0]["new_line"] == 25.5

    def test_removed_prop_detected(self):
        from src.workers.odds_worker import _detect_prop_changes
        changes = _detect_prop_changes([self._PROP], [], "prizepicks")
        assert len(changes) == 1
        assert changes[0]["change_type"] == "removed"
        assert changes[0]["old_line"] == 25.5

    def test_unchanged_prop_not_reported(self):
        from src.workers.odds_worker import _detect_prop_changes
        changes = _detect_prop_changes([self._PROP], [self._PROP], "prizepicks")
        assert changes == []

    def test_multiple_sources_tracked_independently(self):
        from src.workers.odds_worker import _detect_prop_changes
        prev = [{**self._PROP, "line": 25.5}]
        curr = [{**self._PROP, "line": 26.5}]
        pp = _detect_prop_changes(prev, curr, "prizepicks")
        ud = _detect_prop_changes(prev, curr, "underdog")
        assert pp[0]["source"] == "prizepicks"
        assert ud[0]["source"] == "underdog"


# ── Prop engine ────────────────────────────────────────────────────────────────

class TestPropEngine:
    _PROP = {
        "subject": "LeBron James", "stat": "Points", "sport_key": "basketball_nba",
        "line": 25.5, "source": "prizepicks", "is_team_prop": False,
        "opponent": "Celtics", "team": "Lakers", "game_time": "2026-06-10T00:00:00Z",
    }

    def test_record_prop_result_over_win(self):
        from unittest.mock import patch, MagicMock
        with patch("src.db.session.get_db") as mock_db:
            ctx = MagicMock()
            mock_db.return_value.__enter__ = lambda s: ctx
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            from src.engines.prop_engine import record_prop_result
            result = record_prop_result("LeBron James", "Points", "basketball_nba", "over", 25.5, 28.0)
        assert result == "won"

    def test_record_prop_result_over_loss(self):
        from unittest.mock import patch, MagicMock
        with patch("src.db.session.get_db") as mock_db:
            ctx = MagicMock()
            mock_db.return_value.__enter__ = lambda s: ctx
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            from src.engines.prop_engine import record_prop_result
            result = record_prop_result("LeBron James", "Points", "basketball_nba", "over", 25.5, 22.0)
        assert result == "lost"

    def test_record_prop_result_push(self):
        from unittest.mock import patch, MagicMock
        with patch("src.db.session.get_db") as mock_db:
            ctx = MagicMock()
            mock_db.return_value.__enter__ = lambda s: ctx
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            from src.engines.prop_engine import record_prop_result
            result = record_prop_result("LeBron James", "Points", "basketball_nba", "under", 25.5, 25.5)
        # Exact line = lost (no push — CASHED/DEAD only)
        assert result == "lost"

    def test_under_win(self):
        from unittest.mock import patch, MagicMock
        with patch("src.db.session.get_db") as mock_db:
            ctx = MagicMock()
            mock_db.return_value.__enter__ = lambda s: ctx
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            from src.engines.prop_engine import record_prop_result
            result = record_prop_result("Patrick Mahomes", "Pass Yards", "americanfootball_nfl", "under", 287.5, 241.0)
        assert result == "won"
