"""
APEX/SPORTS BOT — Database
SQLite storage for slips, rollovers, balance, and portfolio history.
"""
import sqlite3
import json
import time
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class SportsDatabase:
    def __init__(self, db_path: str):
        self._path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()
        logger.info(f"[DB] Sports database ready: {db_path}")

    def _create_tables(self):
        self._conn.executescript("""
        -- Individual slip legs
        CREATE TABLE IF NOT EXISTS slip_legs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            slip_id         INTEGER NOT NULL,
            sport           TEXT NOT NULL,          -- NBA / MLB / SOCCER
            game            TEXT NOT NULL,          -- "OKC vs LAL"
            market_type     TEXT NOT NULL,          -- GAME_WINNER / TOTAL / PLAYER_PROP
            pick            TEXT NOT NULL,          -- "OKC -3.5" or "OVER 218.5"
            original_line   TEXT,                   -- Line before calibration
            calibrated_line TEXT,                   -- Line after calibration
            kalshi_ticker   TEXT,                   -- Kalshi market ticker
            individual_odds REAL NOT NULL,          -- Odds for this leg
            confidence      REAL NOT NULL,          -- AI confidence 0-10
            ai_reasoning    TEXT,                   -- AI explanation
            game_start      TEXT,                   -- ISO datetime
            game_end        TEXT,                   -- Projected end
            status          TEXT DEFAULT 'PENDING', -- PENDING/WON/LOST/VOID
            created_at      TEXT DEFAULT (datetime('now'))
        );

        -- Slips (collections of legs)
        CREATE TABLE IF NOT EXISTS slips (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            slip_type       TEXT NOT NULL,          -- DAILY / ROLLOVER / LOTTO
            sport_mix       TEXT NOT NULL,          -- "NBA,MLB" or "NBA"
            combined_odds   REAL NOT NULL,
            stake           REAL NOT NULL,
            potential_payout REAL NOT NULL,
            confidence      REAL NOT NULL,          -- Overall slip confidence
            status          TEXT DEFAULT 'PENDING', -- PENDING/WON/LOST/VOID
            net_pnl         REAL DEFAULT 0,
            rollover_id     INTEGER,                -- FK to rollover_sessions
            rollover_day    INTEGER,                -- Which day of rollover
            projected_finish TEXT,                  -- When last game ends
            actual_finish   TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
            resolved_at     TEXT
        );

        -- Rollover sessions
        CREATE TABLE IF NOT EXISTS rollover_sessions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source          TEXT DEFAULT 'AUTO',    -- AUTO or MANUAL
            target_odds     REAL NOT NULL,          -- Target odds per day
            target_days     INTEGER NOT NULL,       -- Total days planned
            starting_stake  REAL NOT NULL,
            current_day     INTEGER DEFAULT 1,
            current_stake   REAL NOT NULL,          -- Compounded stake
            total_compounded REAL DEFAULT 0,        -- Running total won
            status          TEXT DEFAULT 'ACTIVE',  -- ACTIVE/COMPLETED/FAILED
            failure_reason  TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
            completed_at    TEXT,
            days_json       TEXT DEFAULT '[]'       -- JSON array of daily results
        );

        -- Balance snapshots for portfolio chart
        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT NOT NULL,
            balance         REAL NOT NULL,
            daily_pnl       REAL DEFAULT 0,
            cumulative_pnl_daily    REAL DEFAULT 0,
            cumulative_pnl_rollover REAL DEFAULT 0,
            cumulative_pnl_lotto    REAL DEFAULT 0
        );

        -- Bot state persistence
        CREATE TABLE IF NOT EXISTS bot_state (
            key             TEXT PRIMARY KEY,
            value           TEXT NOT NULL,
            updated_at      TEXT DEFAULT (datetime('now'))
        );

        -- Circuit breaker log
        CREATE TABLE IF NOT EXISTS circuit_breaker_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            triggered_at    TEXT NOT NULL,
            reason          TEXT NOT NULL,
            win_rate        REAL,
            total_slips     INTEGER,
            resume_at       TEXT,
            manually_reset  INTEGER DEFAULT 0,
            reset_at        TEXT
        );
        """)
        self._conn.commit()

    # ── BALANCE ───────────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        row = self._conn.execute(
            "SELECT value FROM bot_state WHERE key='balance'"
        ).fetchone()
        return float(row["value"]) if row else 100.0

    def set_balance(self, balance: float):
        self._conn.execute(
            "INSERT OR REPLACE INTO bot_state (key, value, updated_at) "
            "VALUES ('balance', ?, datetime('now'))",
            (str(balance),)
        )
        self._conn.commit()

    # ── SLIPS ─────────────────────────────────────────────────────────────────

    def save_slip(self, slip: dict, legs: list) -> int:
        """Save a slip and its legs. Returns slip_id."""
        cursor = self._conn.execute("""
            INSERT INTO slips 
            (slip_type, sport_mix, combined_odds, stake, potential_payout,
             confidence, status, rollover_id, rollover_day, projected_finish)
            VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
        """, (
            slip["slip_type"],
            slip["sport_mix"],
            slip["combined_odds"],
            slip["stake"],
            slip["potential_payout"],
            slip["confidence"],
            slip.get("rollover_id"),
            slip.get("rollover_day"),
            slip.get("projected_finish"),
        ))
        slip_id = cursor.lastrowid

        for leg in legs:
            self._conn.execute("""
                INSERT INTO slip_legs
                (slip_id, sport, game, market_type, pick, original_line,
                 calibrated_line, kalshi_ticker, individual_odds, confidence,
                 ai_reasoning, game_start, game_end)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                slip_id,
                leg["sport"],
                leg["game"],
                leg["market_type"],
                leg["pick"],
                leg.get("original_line"),
                leg.get("calibrated_line"),
                leg.get("kalshi_ticker"),
                leg["individual_odds"],
                leg["confidence"],
                leg.get("ai_reasoning"),
                leg.get("game_start"),
                leg.get("game_end"),
            ))

        self._conn.commit()
        logger.info(f"[DB] Saved slip #{slip_id} ({slip['slip_type']}) "
                   f"with {len(legs)} legs")
        return slip_id

    def resolve_slip(self, slip_id: int, status: str, net_pnl: float):
        """Mark a slip as WON or LOST."""
        self._conn.execute("""
            UPDATE slips SET status=?, net_pnl=?, 
            actual_finish=datetime('now'), resolved_at=datetime('now')
            WHERE id=?
        """, (status, net_pnl, slip_id))
        self._conn.commit()

    def resolve_leg(self, leg_id: int, status: str):
        self._conn.execute(
            "UPDATE slip_legs SET status=? WHERE id=?", (status, leg_id)
        )
        self._conn.commit()

    def get_recent_slips(self, limit: int = 25) -> list:
        rows = self._conn.execute("""
            SELECT s.*, 
                   COUNT(l.id) as leg_count
            FROM slips s
            LEFT JOIN slip_legs l ON l.slip_id = s.id
            GROUP BY s.id
            ORDER BY s.created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_slip_legs(self, slip_id: int) -> list:
        rows = self._conn.execute(
            "SELECT * FROM slip_legs WHERE slip_id=? ORDER BY id",
            (slip_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_open_slips(self) -> list:
        rows = self._conn.execute(
            "SELECT * FROM slips WHERE status='PENDING' ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        row = self._conn.execute("""
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN status='WON' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN status='LOST' THEN 1 ELSE 0 END) as losses,
                SUM(net_pnl) as total_pnl,
                SUM(CASE WHEN slip_type='DAILY' AND status='WON' THEN 1 ELSE 0 END) as daily_wins,
                SUM(CASE WHEN slip_type='DAILY' THEN 1 ELSE 0 END) as daily_total,
                SUM(CASE WHEN slip_type='ROLLOVER' AND status='WON' THEN 1 ELSE 0 END) as rollover_wins,
                SUM(CASE WHEN slip_type='ROLLOVER' THEN 1 ELSE 0 END) as rollover_total,
                SUM(CASE WHEN slip_type='LOTTO' AND status='WON' THEN 1 ELSE 0 END) as lotto_wins,
                SUM(CASE WHEN slip_type='LOTTO' THEN 1 ELSE 0 END) as lotto_total
            FROM slips WHERE status != 'PENDING'
        """).fetchone()
        return dict(row) if row else {}

    def get_today_stats(self) -> dict:
        row = self._conn.execute("""
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN status='WON' THEN 1 ELSE 0 END) as wins,
                SUM(net_pnl) as pnl
            FROM slips 
            WHERE DATE(created_at) = DATE('now')
            AND status != 'PENDING'
        """).fetchone()
        return dict(row) if row else {"total": 0, "wins": 0, "pnl": 0}

    # ── ROLLOVERS ─────────────────────────────────────────────────────────────

    def create_rollover(self, target_odds: float, target_days: int,
                        starting_stake: float, source: str = "AUTO") -> int:
        cursor = self._conn.execute("""
            INSERT INTO rollover_sessions
            (source, target_odds, target_days, starting_stake, current_stake,
             current_day, status, days_json)
            VALUES (?, ?, ?, ?, ?, 1, 'ACTIVE', '[]')
        """, (source, target_odds, target_days, starting_stake, starting_stake))
        self._conn.commit()
        return cursor.lastrowid

    def get_active_rollover(self) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM rollover_sessions WHERE status='ACTIVE' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def advance_rollover(self, rollover_id: int, won: bool,
                         payout: float, next_stake: float):
        """Record a day's result and advance (or end) the rollover."""
        rollover = self._conn.execute(
            "SELECT * FROM rollover_sessions WHERE id=?", (rollover_id,)
        ).fetchone()
        if not rollover:
            return

        days = json.loads(rollover["days_json"])
        days.append({"day": rollover["current_day"], "won": won,
                     "payout": payout})

        if not won:
            # Rollover failed
            self._conn.execute("""
                UPDATE rollover_sessions 
                SET status='FAILED', days_json=?, failure_reason='Day lost',
                    completed_at=datetime('now')
                WHERE id=?
            """, (json.dumps(days), rollover_id))
        elif rollover["current_day"] >= rollover["target_days"]:
            # Rollover completed!
            self._conn.execute("""
                UPDATE rollover_sessions
                SET status='COMPLETED', days_json=?, current_day=?,
                    current_stake=?, total_compounded=?,
                    completed_at=datetime('now')
                WHERE id=?
            """, (json.dumps(days), rollover["current_day"] + 1,
                  next_stake, payout, rollover_id))
        else:
            # Continue to next day
            self._conn.execute("""
                UPDATE rollover_sessions
                SET current_day=?, current_stake=?, days_json=?,
                    total_compounded=?
                WHERE id=?
            """, (rollover["current_day"] + 1, next_stake,
                  json.dumps(days), payout, rollover_id))

        self._conn.commit()

    def get_rollover_history(self, limit: int = 10) -> list:
        rows = self._conn.execute("""
            SELECT * FROM rollover_sessions
            ORDER BY created_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ── PORTFOLIO ─────────────────────────────────────────────────────────────

    def save_snapshot(self, balance: float, daily_pnl: float,
                      cum_daily: float, cum_rollover: float,
                      cum_lotto: float):
        self._conn.execute("""
            INSERT INTO portfolio_snapshots
            (timestamp, balance, daily_pnl, cumulative_pnl_daily,
             cumulative_pnl_rollover, cumulative_pnl_lotto)
            VALUES (datetime('now'), ?, ?, ?, ?, ?)
        """, (balance, daily_pnl, cum_daily, cum_rollover, cum_lotto))
        self._conn.commit()

    def get_portfolio_history(self, days: int = 30) -> list:
        rows = self._conn.execute("""
            SELECT * FROM portfolio_snapshots
            WHERE timestamp >= datetime('now', ?)
            ORDER BY timestamp ASC
        """, (f"-{days} days",)).fetchall()
        return [dict(r) for r in rows]

    # ── CIRCUIT BREAKER ───────────────────────────────────────────────────────

    def log_circuit_breaker(self, reason: str, win_rate: float,
                             total_slips: int, resume_at: str):
        self._conn.execute("""
            INSERT INTO circuit_breaker_log
            (triggered_at, reason, win_rate, total_slips, resume_at)
            VALUES (datetime('now'), ?, ?, ?, ?)
        """, (reason, win_rate, total_slips, resume_at))
        self._conn.commit()

    def mark_cb_reset(self, manually: bool = True):
        self._conn.execute("""
            UPDATE circuit_breaker_log
            SET manually_reset=?, reset_at=datetime('now')
            WHERE reset_at IS NULL
        """, (1 if manually else 0,))
        self._conn.commit()

    def get_cb_history(self, limit: int = 10) -> list:
        rows = self._conn.execute("""
            SELECT * FROM circuit_breaker_log
            ORDER BY triggered_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ── BOT STATE ─────────────────────────────────────────────────────────────

    def get_state(self, key: str, default=None):
        row = self._conn.execute(
            "SELECT value FROM bot_state WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str):
        self._conn.execute("""
            INSERT OR REPLACE INTO bot_state (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
        """, (key, value))
        self._conn.commit()

    def close(self):
        self._conn.close()
