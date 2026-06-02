"""
SQLAlchemy ORM models — full PostgreSQL schema.
Falls back to SQLite for development (USE_SQLITE=true).
"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, Float, String, Boolean, Text, DateTime,
    ForeignKey, Enum, Index, UniqueConstraint, JSON,
    create_engine,
)
from sqlalchemy.orm import relationship, declarative_base
import enum

Base = declarative_base()


# ── Enums ──────────────────────────────────────────────────────────────────────

class BetResult(str, enum.Enum):
    PENDING  = "pending"
    LIVE     = "live"
    WINNING  = "winning"
    LOSING   = "losing"
    WON      = "won"
    LOST     = "lost"
    VOID     = "void"
    CANCELED = "canceled"
    PUSH     = "push"

class AlertPriority(str, enum.Enum):
    CRITICAL = "critical"
    HIGH     = "high"
    MEDIUM   = "medium"
    LOW      = "low"

class RecommendationType(str, enum.Enum):
    BET  = "bet"
    PASS = "pass"

class SignalType(str, enum.Enum):
    VALUE  = "value"
    STEAM  = "steam"
    SHARP  = "sharp"
    FADE   = "fade"
    INJURY = "injury"
    WEATHER= "weather"

class MarketType(str, enum.Enum):
    MONEYLINE    = "moneyline"
    SPREAD       = "spread"
    TOTAL        = "total"
    PLAYER_PROP  = "player_prop"
    SGP          = "sgp"
    PARLAY       = "parlay"


# ── Core tables ────────────────────────────────────────────────────────────────

class Sport(Base):
    __tablename__ = "sports"
    id          = Column(Integer, primary_key=True)
    key         = Column(String(50), unique=True, nullable=False)
    name        = Column(String(100), nullable=False)
    active      = Column(Boolean, default=True)
    games       = relationship("Game", back_populates="sport")


class Team(Base):
    __tablename__ = "teams"
    id          = Column(Integer, primary_key=True)
    sport_id    = Column(Integer, ForeignKey("sports.id"))
    name        = Column(String(200), nullable=False)
    abbrev      = Column(String(10))
    city        = Column(String(100))
    created_at  = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (Index("ix_teams_sport", "sport_id"),)


class Player(Base):
    __tablename__ = "players"
    id              = Column(Integer, primary_key=True)
    sport_id        = Column(Integer, ForeignKey("sports.id"))
    team_id         = Column(Integer, ForeignKey("teams.id"), nullable=True)
    name            = Column(String(200), nullable=False)
    position        = Column(String(20))
    status          = Column(String(50), default="active")  # active|injured|suspended|out
    injury_detail   = Column(Text)
    injury_updated  = Column(DateTime)
    external_id     = Column(String(100))  # ESPN / platform player ID
    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (
        Index("ix_players_sport",  "sport_id"),
        Index("ix_players_team",   "team_id"),
        Index("ix_players_status", "status"),
    )


class Game(Base):
    __tablename__ = "games"
    id            = Column(Integer, primary_key=True)
    external_id   = Column(String(100), unique=True, nullable=False)
    sport_id      = Column(Integer, ForeignKey("sports.id"))
    home_team_id  = Column(Integer, ForeignKey("teams.id"), nullable=True)
    away_team_id  = Column(Integer, ForeignKey("teams.id"), nullable=True)
    home_team     = Column(String(200))
    away_team     = Column(String(200))
    commence_time = Column(DateTime, nullable=False)
    status        = Column(String(50), default="scheduled")  # scheduled|live|final|canceled
    home_score    = Column(Float)
    away_score    = Column(Float)
    venue         = Column(String(200))
    weather_temp  = Column(Float)
    weather_wind  = Column(Float)
    weather_precip= Column(Float)
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    sport   = relationship("Sport", back_populates="games")
    odds    = relationship("OddsSnapshot", back_populates="game")
    picks   = relationship("Pick", back_populates="game")

    __table_args__ = (Index("ix_games_commence", "commence_time"),)


class OddsSnapshot(Base):
    """Every odds reading from every book, timestamped for line movement tracking."""
    __tablename__ = "odds_snapshots"
    id            = Column(Integer, primary_key=True)
    game_id       = Column(Integer, ForeignKey("games.id"), nullable=False)
    captured_at   = Column(DateTime, default=datetime.utcnow, nullable=False)
    book          = Column(String(50), nullable=False)
    market        = Column(String(50), nullable=False)
    selection     = Column(String(200), nullable=False)
    american_odds = Column(Integer, nullable=False)
    decimal_odds  = Column(Float,   nullable=False)
    implied_prob  = Column(Float,   nullable=False)
    line_value    = Column(Float)   # spread / total value

    game = relationship("Game", back_populates="odds")

    __table_args__ = (
        Index("ix_odds_game_market",    "game_id", "market"),
        Index("ix_odds_captured_at",    "captured_at"),
        Index("ix_odds_book_market",    "book", "market"),
    )


class Pick(Base):
    """A generated recommendation — immutable audit record."""
    __tablename__ = "picks"
    id                  = Column(Integer, primary_key=True)
    generated_at        = Column(DateTime, default=datetime.utcnow, nullable=False)
    game_id             = Column(Integer, ForeignKey("games.id"), nullable=False)
    sport               = Column(String(50), nullable=False)
    market              = Column(String(50), nullable=False)
    selection           = Column(String(200), nullable=False)
    best_book           = Column(String(50), nullable=False)
    american_odds_at_gen= Column(Integer, nullable=False)
    decimal_odds_at_gen = Column(Float,   nullable=False)

    recommendation      = Column(String(10), nullable=False)   # BET | PASS
    units               = Column(Integer, nullable=False)       # 0-5
    ev_pct              = Column(Float,   nullable=False)
    confidence_pct      = Column(Float,   nullable=False)
    risk_score          = Column(Float,   nullable=False)       # 0-100
    opportunity_score   = Column(Float,   nullable=False)       # 0-100

    # Model sub-scores
    statistical_score   = Column(Float)
    ml_score            = Column(Float)
    market_score        = Column(Float)
    trend_score         = Column(Float)
    news_impact_score   = Column(Float)
    injury_impact_score = Column(Float)

    # Explanation
    reasoning           = Column(Text, nullable=False)
    key_factors         = Column(JSON)   # list of strings
    risk_flags          = Column(JSON)   # list of strings

    # CLV tracking
    american_odds_close = Column(Integer)   # filled on game close
    clv_pct             = Column(Float)     # CLV = (close - gen) in prob terms

    # Result
    result              = Column(String(20), default=BetResult.PENDING)
    settled_at          = Column(DateTime)
    actual_pnl_units    = Column(Float)

    # Parlay linkage
    parlay_id           = Column(Integer, ForeignKey("parlays.id"), nullable=True)
    is_parlay_leg       = Column(Boolean, default=False)

    game   = relationship("Game", back_populates="picks")
    parlay = relationship("Parlay", back_populates="legs", foreign_keys=[parlay_id])

    __table_args__ = (
        Index("ix_picks_generated_at", "generated_at"),
        Index("ix_picks_result",       "result"),
        Index("ix_picks_sport",        "sport"),
        Index("ix_picks_units",        "units"),
    )


class Parlay(Base):
    __tablename__ = "parlays"
    id              = Column(Integer, primary_key=True)
    created_at      = Column(DateTime, default=datetime.utcnow)
    parlay_type     = Column(String(30), default="standard")  # standard|sgp|round_robin
    combined_odds   = Column(Float, nullable=False)
    units           = Column(Float, nullable=False)
    potential_payout= Column(Float, nullable=False)
    ev_pct          = Column(Float)
    win_probability = Column(Float)
    result          = Column(String(20), default=BetResult.PENDING)
    legs_won        = Column(Integer, default=0)
    pnl_units       = Column(Float)
    settled_at      = Column(DateTime)
    legs            = relationship("Pick", back_populates="parlay", foreign_keys=[Pick.parlay_id])


class CLVRecord(Base):
    """Closing Line Value tracking per pick."""
    __tablename__ = "clv_records"
    id              = Column(Integer, primary_key=True)
    pick_id         = Column(Integer, ForeignKey("picks.id"), unique=True)
    sport           = Column(String(50))
    market          = Column(String(50))
    book            = Column(String(50))
    odds_at_pick    = Column(Integer, nullable=False)
    odds_at_close   = Column(Integer, nullable=False)
    implied_at_pick = Column(Float,   nullable=False)
    implied_at_close= Column(Float,   nullable=False)
    clv_pct         = Column(Float,   nullable=False)  # positive = beat closing line
    recorded_at     = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_clv_sport",  "sport"),
        Index("ix_clv_market", "market"),
    )


class BankrollSnapshot(Base):
    __tablename__ = "bankroll_snapshots"
    id           = Column(Integer, primary_key=True)
    recorded_at  = Column(DateTime, default=datetime.utcnow)
    balance      = Column(Float, nullable=False)
    units_total  = Column(Float, default=0.0)
    note         = Column(String(500))
    __table_args__ = (Index("ix_bankroll_recorded_at", "recorded_at"),)


class DailyPortfolio(Base):
    """Daily portfolio snapshot — picks grouped by risk tier."""
    __tablename__ = "daily_portfolios"
    id              = Column(Integer, primary_key=True)
    date            = Column(String(10), nullable=False, unique=True)  # YYYY-MM-DD
    generated_at    = Column(DateTime, default=datetime.utcnow)
    safe_pick_ids   = Column(JSON)   # list of pick IDs
    medium_pick_ids = Column(JSON)
    high_pick_ids   = Column(JSON)
    parlay_pick_ids = Column(JSON)
    total_units     = Column(Float, default=0.0)
    expected_roi    = Column(Float, default=0.0)
    actual_units    = Column(Float)
    actual_roi      = Column(Float)
    settled         = Column(Boolean, default=False)


class NewsEvent(Base):
    __tablename__ = "news_events"
    id           = Column(Integer, primary_key=True)
    fetched_at   = Column(DateTime, default=datetime.utcnow)
    sport        = Column(String(50))
    category     = Column(String(50))  # injury|lineup|trade|weather|suspension
    headline     = Column(String(500), nullable=False)
    detail       = Column(Text)
    impact_score = Column(Float, default=0.0)   # 0-1 how much this matters
    affected_players = Column(JSON)  # list of player names
    affected_teams   = Column(JSON)  # list of team names
    processed    = Column(Boolean, default=False)
    __table_args__ = (Index("ix_news_sport", "sport"), Index("ix_news_fetched", "fetched_at"))


class LineMovement(Base):
    """Detected significant line movements — steam moves, sharp action."""
    __tablename__ = "line_movements"
    id              = Column(Integer, primary_key=True)
    detected_at     = Column(DateTime, default=datetime.utcnow)
    game_id         = Column(Integer, ForeignKey("games.id"))
    market          = Column(String(50))
    selection       = Column(String(200))
    book            = Column(String(50))
    odds_before     = Column(Integer)
    odds_after      = Column(Integer)
    implied_before  = Column(Float)
    implied_after   = Column(Float)
    delta_pct       = Column(Float)  # change in implied prob
    movement_type   = Column(String(30))  # steam|reverse_line|sharp|public
    alert_sent      = Column(Boolean, default=False)
    __table_args__ = (Index("ix_lm_detected", "detected_at"),)


class ModelConsensus(Base):
    """Record of multi-model agreement for a pick."""
    __tablename__ = "model_consensus"
    id              = Column(Integer, primary_key=True)
    pick_id         = Column(Integer, ForeignKey("picks.id"))
    statistical_rec = Column(String(10))   # BET|PASS
    ml_rec          = Column(String(10))
    market_rec      = Column(String(10))
    trend_rec       = Column(String(10))
    consensus_pct   = Column(Float)        # fraction of models agreeing
    generated_at    = Column(DateTime, default=datetime.utcnow)


class ConfidenceCalibration(Base):
    """Tracks predicted vs actual win rate per confidence bucket."""
    __tablename__ = "confidence_calibration"
    id              = Column(Integer, primary_key=True)
    sport           = Column(String(50))
    market          = Column(String(50))
    confidence_bucket = Column(String(20))  # e.g. "60-65", "65-70"
    predicted_win_rate= Column(Float, nullable=False)
    actual_win_rate   = Column(Float)
    sample_size       = Column(Integer, default=0)
    calibration_error = Column(Float)   # abs(predicted - actual)
    updated_at        = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (UniqueConstraint("sport", "market", "confidence_bucket"),)


class PerformanceMetrics(Base):
    """Aggregated ROI/CLV metrics by dimension — updated by self-improvement engine."""
    __tablename__ = "performance_metrics"
    id              = Column(Integer, primary_key=True)
    dimension       = Column(String(50))   # sport|market|book|unit_level|signal_type
    dimension_value = Column(String(100))  # nba | moneyline | draftkings | 3 | steam
    period          = Column(String(20))   # lifetime|monthly|weekly|daily
    period_start    = Column(DateTime)
    total_picks     = Column(Integer, default=0)
    wins            = Column(Integer, default=0)
    losses          = Column(Integer, default=0)
    units_won       = Column(Float, default=0.0)
    units_lost      = Column(Float, default=0.0)
    roi_pct         = Column(Float)
    avg_clv_pct     = Column(Float)
    avg_ev_pct      = Column(Float)
    hit_rate        = Column(Float)
    profit_factor   = Column(Float)  # gross_win / gross_loss
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint("dimension", "dimension_value", "period", "period_start"),
        Index("ix_perf_dimension", "dimension", "dimension_value"),
    )


class AuditTrail(Base):
    """Immutable record of every system action — never modified."""
    __tablename__ = "audit_trail"
    id          = Column(Integer, primary_key=True)
    occurred_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    event_type  = Column(String(100), nullable=False)   # pick_generated|bet_placed|odds_updated|alert_sent
    entity_type = Column(String(50))   # pick|parlay|odds|news
    entity_id   = Column(Integer)
    payload     = Column(JSON)
    __table_args__ = (Index("ix_audit_occurred", "occurred_at"), Index("ix_audit_type", "event_type"))


class UserProfile(Base):
    __tablename__ = "user_profiles"
    id           = Column(Integer, primary_key=True)
    user_id      = Column(String(100), unique=True, nullable=False)
    bankroll     = Column(Float, default=1000.0)
    risk_profile = Column(String(20), default="balanced")
    sports       = Column(JSON, default=lambda: [])
    max_units    = Column(Integer, default=3)
    min_ev       = Column(Float, default=0.02)
    created_at   = Column(DateTime, default=datetime.utcnow)
    updated_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class UserFeedback(Base):
    __tablename__ = "user_feedback"
    id                 = Column(Integer, primary_key=True)
    pick_id            = Column(Integer, ForeignKey("picks.id"), nullable=False)
    user_id            = Column(String(100), nullable=False)
    rating             = Column(String(20), nullable=False)  # helpful|not_helpful
    explanation_rating = Column(Integer, default=3)          # 1-5
    comment            = Column(Text, default="")
    submitted_at       = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (Index("ix_feedback_pick_id", "pick_id"),)


class AlertRecord(Base):
    __tablename__ = "alert_records"
    id              = Column(Integer, primary_key=True)
    sent_at         = Column(DateTime, default=datetime.utcnow)
    priority        = Column(String(20))   # critical|high|medium|low
    channel         = Column(String(100))
    alert_type      = Column(String(100))  # new_pick|line_move|pregame|result|value_expiring
    pick_id         = Column(Integer, ForeignKey("picks.id"), nullable=True)
    message_id      = Column(String(100))  # Discord message ID
    delivered       = Column(Boolean, default=True)
    __table_args__ = (Index("ix_alerts_sent_at", "sent_at"),)
