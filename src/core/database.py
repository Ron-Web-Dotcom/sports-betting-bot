"""SQLite persistence — positions, parlays, signals, bankroll, arbs."""
import sqlite3
import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from config.settings import DB_PATH


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript("""
        -- Individual legs / straight bets
        CREATE TABLE IF NOT EXISTS positions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at      TEXT    NOT NULL,
            sport           TEXT    NOT NULL,
            event_id        TEXT    NOT NULL,
            event_name      TEXT    NOT NULL,
            market          TEXT    NOT NULL,
            selection       TEXT    NOT NULL,
            book            TEXT    NOT NULL,
            american_odds   INTEGER NOT NULL,
            decimal_odds    REAL    NOT NULL,
            stake           REAL    NOT NULL,
            kelly_fraction  REAL    NOT NULL,
            edge            REAL    NOT NULL,
            ai_confidence   REAL    NOT NULL,
            status          TEXT    NOT NULL DEFAULT 'open',
            result          TEXT,
            pnl             REAL,
            settled_at      TEXT,
            parlay_id       INTEGER,
            signal_data     TEXT
        );

        -- Parlay tickets (groups of legs)
        CREATE TABLE IF NOT EXISTS parlays (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at      TEXT    NOT NULL,
            legs            TEXT    NOT NULL,   -- JSON list of position IDs
            combined_odds   REAL    NOT NULL,
            stake           REAL    NOT NULL,
            potential_payout REAL   NOT NULL,
            leg_count       INTEGER NOT NULL,
            status          TEXT    NOT NULL DEFAULT 'open',
            legs_won        INTEGER NOT NULL DEFAULT 0,
            pnl             REAL,
            settled_at      TEXT,
            parlay_type     TEXT    NOT NULL DEFAULT 'standard'  -- standard|sgp|teaser|round_robin
        );

        -- Arbitrage opportunities found
        CREATE TABLE IF NOT EXISTS arb_opportunities (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            found_at        TEXT    NOT NULL,
            sport           TEXT    NOT NULL,
            event_id        TEXT    NOT NULL,
            event_name      TEXT    NOT NULL,
            market          TEXT    NOT NULL,
            book_a          TEXT    NOT NULL,
            book_b          TEXT    NOT NULL,
            odds_a          INTEGER NOT NULL,
            odds_b          INTEGER NOT NULL,
            selection_a     TEXT    NOT NULL,
            selection_b     TEXT    NOT NULL,
            guaranteed_profit_pct REAL NOT NULL,
            stake_a         REAL,
            stake_b         REAL,
            status          TEXT    NOT NULL DEFAULT 'found'  -- found|placed|expired|missed
        );

        -- Hedge recommendations
        CREATE TABLE IF NOT EXISTS hedges (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at      TEXT    NOT NULL,
            parlay_id       INTEGER NOT NULL,
            trigger_leg     TEXT    NOT NULL,   -- which leg triggered the hedge
            hedge_selection TEXT    NOT NULL,
            hedge_book      TEXT    NOT NULL,
            hedge_odds      INTEGER NOT NULL,
            hedge_stake     REAL    NOT NULL,
            guaranteed_profit REAL  NOT NULL,
            status          TEXT    NOT NULL DEFAULT 'recommended'
        );

        -- Multi-factor signals per event
        CREATE TABLE IF NOT EXISTS signals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            generated_at    TEXT    NOT NULL,
            sport           TEXT    NOT NULL,
            event_id        TEXT    NOT NULL,
            event_name      TEXT    NOT NULL,
            market          TEXT    NOT NULL,
            selection       TEXT    NOT NULL,
            line_value      REAL,
            public_bet_pct  REAL,
            line_movement   REAL,
            injury_impact   REAL,
            ai_confidence   REAL    NOT NULL,
            composite_score REAL    NOT NULL,
            signal_type     TEXT    NOT NULL,   -- value|fade|steam|sharp|parlay_leg
            raw_data        TEXT
        );

        -- Bankroll history
        CREATE TABLE IF NOT EXISTS bankroll_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at TEXT    NOT NULL,
            balance     REAL    NOT NULL,
            note        TEXT
        );

        -- Market snapshots (line history for movement tracking)
        CREATE TABLE IF NOT EXISTS market_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at TEXT    NOT NULL,
            book        TEXT    NOT NULL,
            sport       TEXT    NOT NULL,
            event_id    TEXT    NOT NULL,
            market      TEXT    NOT NULL,
            selection   TEXT    NOT NULL,
            american_odds INTEGER NOT NULL
        );

        -- Injury cache
        CREATE TABLE IF NOT EXISTS injuries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at  TEXT    NOT NULL,
            sport       TEXT    NOT NULL,
            player_name TEXT    NOT NULL,
            team        TEXT    NOT NULL,
            status      TEXT    NOT NULL,
            detail      TEXT
        );
        """)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── Position (straight bet) helpers ───────────────────────────────────────────

def insert_position(pos: dict) -> int:
    pos.setdefault("created_at", datetime.utcnow().isoformat())
    if isinstance(pos.get("signal_data"), dict):
        pos["signal_data"] = json.dumps(pos["signal_data"])
    sql = """INSERT INTO positions
        (created_at,sport,event_id,event_name,market,selection,book,
         american_odds,decimal_odds,stake,kelly_fraction,edge,ai_confidence,
         parlay_id,signal_data)
        VALUES
        (:created_at,:sport,:event_id,:event_name,:market,:selection,:book,
         :american_odds,:decimal_odds,:stake,:kelly_fraction,:edge,:ai_confidence,
         :parlay_id,:signal_data)"""
    with get_conn() as conn:
        return conn.execute(sql, pos).lastrowid


def settle_position(pos_id: int, result: str, pnl: float) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE positions SET status='settled',result=?,pnl=?,settled_at=? WHERE id=?",
            (result, pnl, datetime.utcnow().isoformat(), pos_id),
        )


def get_open_positions() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM positions WHERE status='open' ORDER BY created_at DESC"
        ).fetchall()]


def get_position_stats() -> dict:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT COUNT(*) total,
                   SUM(CASE WHEN result='win'  THEN 1 ELSE 0 END) wins,
                   SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END) losses,
                   SUM(COALESCE(pnl,0)) total_pnl,
                   AVG(ai_confidence) avg_confidence,
                   AVG(edge) avg_edge
            FROM positions WHERE status='settled'
        """).fetchone()
        return dict(row) if row else {}


# ── Parlay helpers ────────────────────────────────────────────────────────────

def insert_parlay(parlay: dict) -> int:
    parlay.setdefault("created_at", datetime.utcnow().isoformat())
    if isinstance(parlay.get("legs"), list):
        parlay["legs"] = json.dumps(parlay["legs"])
    sql = """INSERT INTO parlays
        (created_at,legs,combined_odds,stake,potential_payout,leg_count,parlay_type)
        VALUES
        (:created_at,:legs,:combined_odds,:stake,:potential_payout,:leg_count,:parlay_type)"""
    with get_conn() as conn:
        return conn.execute(sql, parlay).lastrowid


def update_parlay_leg_won(parlay_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE parlays SET legs_won = legs_won + 1 WHERE id=?", (parlay_id,)
        )


def settle_parlay(parlay_id: int, result: str, pnl: float) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE parlays SET status='settled',pnl=?,settled_at=? WHERE id=?",
            (pnl, datetime.utcnow().isoformat(), parlay_id),
        )


def get_open_parlays() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM parlays WHERE status='open' ORDER BY created_at DESC"
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["legs"] = json.loads(d["legs"])
            result.append(d)
        return result


# ── Arbitrage helpers ─────────────────────────────────────────────────────────

def insert_arb(arb: dict) -> int:
    arb.setdefault("found_at", datetime.utcnow().isoformat())
    sql = """INSERT INTO arb_opportunities
        (found_at,sport,event_id,event_name,market,book_a,book_b,
         odds_a,odds_b,selection_a,selection_b,guaranteed_profit_pct,stake_a,stake_b)
        VALUES
        (:found_at,:sport,:event_id,:event_name,:market,:book_a,:book_b,
         :odds_a,:odds_b,:selection_a,:selection_b,:guaranteed_profit_pct,:stake_a,:stake_b)"""
    with get_conn() as conn:
        return conn.execute(sql, arb).lastrowid


def get_recent_arbs(hours: int = 1) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM arb_opportunities WHERE found_at >= datetime('now',?) ORDER BY guaranteed_profit_pct DESC",
            (f"-{hours} hours",),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Signal helpers ────────────────────────────────────────────────────────────

def insert_signal(sig: dict) -> int:
    sig.setdefault("generated_at", datetime.utcnow().isoformat())
    if isinstance(sig.get("raw_data"), dict):
        sig["raw_data"] = json.dumps(sig["raw_data"])
    sql = """INSERT INTO signals
        (generated_at,sport,event_id,event_name,market,selection,line_value,
         public_bet_pct,line_movement,injury_impact,ai_confidence,composite_score,signal_type,raw_data)
        VALUES
        (:generated_at,:sport,:event_id,:event_name,:market,:selection,:line_value,
         :public_bet_pct,:line_movement,:injury_impact,:ai_confidence,:composite_score,:signal_type,:raw_data)"""
    with get_conn() as conn:
        return conn.execute(sql, sig).lastrowid


def get_top_signals(limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE generated_at >= datetime('now','-1 hours') ORDER BY composite_score DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Bankroll helpers ──────────────────────────────────────────────────────────

def record_bankroll(balance: float, note: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO bankroll_history (recorded_at,balance,note) VALUES (?,?,?)",
            (datetime.utcnow().isoformat(), balance, note),
        )


def latest_bankroll() -> float | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT balance FROM bankroll_history ORDER BY recorded_at DESC LIMIT 1"
        ).fetchone()
        return row["balance"] if row else None


# ── Market snapshot helpers ───────────────────────────────────────────────────

def save_snapshot(book: str, sport: str, event_id: str, market: str, selection: str, american_odds: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO market_snapshots (captured_at,book,sport,event_id,market,selection,american_odds) VALUES (?,?,?,?,?,?,?)",
            (datetime.utcnow().isoformat(), book, sport, event_id, market, selection, american_odds),
        )


def get_line_movement(event_id: str, market: str, selection: str, hours: int = 6) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT book,american_odds,captured_at FROM market_snapshots
               WHERE event_id=? AND market=? AND selection=?
               AND captured_at >= datetime('now',?)
               ORDER BY captured_at ASC""",
            (event_id, market, selection, f"-{hours} hours"),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Injury helpers ────────────────────────────────────────────────────────────

def upsert_injuries(injuries: list[dict]) -> None:
    with get_conn() as conn:
        for inj in injuries:
            conn.execute(
                "INSERT INTO injuries (fetched_at,sport,player_name,team,status,detail) VALUES (?,?,?,?,?,?)",
                (datetime.utcnow().isoformat(), inj.get("sport",""), inj.get("player_name",""),
                 inj.get("team",""), inj.get("status",""), inj.get("detail","")),
            )


def get_recent_injuries(sport: str, hours: int = 24) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM injuries WHERE sport=? AND fetched_at >= datetime('now',?) ORDER BY fetched_at DESC",
            (sport, f"-{hours} hours"),
        ).fetchall()
        return [dict(r) for r in rows]
