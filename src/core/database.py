"""SQLite persistence layer."""
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
        CREATE TABLE IF NOT EXISTS bets (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at    TEXT    NOT NULL,
            platform      TEXT    NOT NULL,
            sport         TEXT    NOT NULL,
            event_id      TEXT    NOT NULL,
            event_name    TEXT    NOT NULL,
            market        TEXT    NOT NULL,
            selection     TEXT    NOT NULL,
            odds          REAL    NOT NULL,
            stake         REAL    NOT NULL,
            kelly_fraction REAL   NOT NULL,
            ai_confidence REAL    NOT NULL,
            edge          REAL    NOT NULL,
            status        TEXT    NOT NULL DEFAULT 'pending',
            result        TEXT,
            pnl           REAL,
            settled_at    TEXT,
            raw_signal    TEXT
        );

        CREATE TABLE IF NOT EXISTS market_snapshots (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at TEXT   NOT NULL,
            platform   TEXT    NOT NULL,
            sport      TEXT    NOT NULL,
            event_id   TEXT    NOT NULL,
            market     TEXT    NOT NULL,
            data       TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bankroll_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at TEXT   NOT NULL,
            balance    REAL    NOT NULL,
            note       TEXT
        );

        CREATE TABLE IF NOT EXISTS injuries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at  TEXT   NOT NULL,
            sport       TEXT   NOT NULL,
            player_name TEXT   NOT NULL,
            team        TEXT   NOT NULL,
            status      TEXT   NOT NULL,
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


# ── Bet helpers ───────────────────────────────────────────────────────────────

def insert_bet(bet: dict) -> int:
    sql = """
        INSERT INTO bets
          (created_at, platform, sport, event_id, event_name, market,
           selection, odds, stake, kelly_fraction, ai_confidence, edge, raw_signal)
        VALUES
          (:created_at,:platform,:sport,:event_id,:event_name,:market,
           :selection,:odds,:stake,:kelly_fraction,:ai_confidence,:edge,:raw_signal)
    """
    bet.setdefault("created_at", datetime.utcnow().isoformat())
    if isinstance(bet.get("raw_signal"), dict):
        bet["raw_signal"] = json.dumps(bet["raw_signal"])
    with get_conn() as conn:
        cur = conn.execute(sql, bet)
        return cur.lastrowid


def settle_bet(bet_id: int, result: str, pnl: float) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE bets SET status='settled', result=?, pnl=?, settled_at=? WHERE id=?",
            (result, pnl, datetime.utcnow().isoformat(), bet_id),
        )


def get_open_bets() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM bets WHERE status='pending' ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_bet_stats() -> dict:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*)                            AS total,
                SUM(CASE WHEN result='win' THEN 1 ELSE 0 END)  AS wins,
                SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END) AS losses,
                SUM(COALESCE(pnl, 0))               AS total_pnl,
                AVG(ai_confidence)                  AS avg_confidence,
                AVG(edge)                           AS avg_edge
            FROM bets WHERE status='settled'
        """).fetchone()
        return dict(row) if row else {}


# ── Bankroll helpers ──────────────────────────────────────────────────────────

def record_bankroll(balance: float, note: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO bankroll_history (recorded_at, balance, note) VALUES (?, ?, ?)",
            (datetime.utcnow().isoformat(), balance, note),
        )


def latest_bankroll() -> float | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT balance FROM bankroll_history ORDER BY recorded_at DESC LIMIT 1"
        ).fetchone()
        return row["balance"] if row else None


# ── Market snapshot helpers ───────────────────────────────────────────────────

def save_snapshot(platform: str, sport: str, event_id: str, market: str, data: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO market_snapshots
               (captured_at, platform, sport, event_id, market, data)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (datetime.utcnow().isoformat(), platform, sport, event_id, market, json.dumps(data)),
        )


# ── Injury helpers ────────────────────────────────────────────────────────────

def upsert_injuries(injuries: list[dict]) -> None:
    with get_conn() as conn:
        for inj in injuries:
            conn.execute(
                """INSERT INTO injuries (fetched_at, sport, player_name, team, status, detail)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    datetime.utcnow().isoformat(),
                    inj.get("sport", ""),
                    inj.get("player_name", ""),
                    inj.get("team", ""),
                    inj.get("status", ""),
                    inj.get("detail", ""),
                ),
            )


def get_recent_injuries(sport: str, hours: int = 24) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM injuries
               WHERE sport = ?
                 AND fetched_at >= datetime('now', ? || ' hours')
               ORDER BY fetched_at DESC""",
            (sport, f"-{hours}"),
        ).fetchall()
        return [dict(r) for r in rows]
