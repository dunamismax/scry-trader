"""Trade journal — SQLite-backed trade logging and analysis history."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from augur.models import (
    AnalysisLogEntry,
    Direction,
    PortfolioSnapshot,
    TradeJournalEntry,
    TradeOutcome,
)

SCHEMA = """\
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_price REAL,
    exit_price REAL,
    shares REAL,
    open_shares REAL DEFAULT 0.0,
    entry_date TEXT,
    exit_date TEXT,
    parent_trade_id INTEGER,
    thesis TEXT,
    claude_analysis TEXT,
    outcome TEXT DEFAULT 'open',
    pnl REAL DEFAULT 0.0,
    notes TEXT,
    tags TEXT
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    total_value REAL,
    cash REAL,
    positions_json TEXT,
    daily_pnl REAL
);

CREATE TABLE IF NOT EXISTS analysis_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    query TEXT,
    context_json TEXT,
    response TEXT,
    tokens_used INTEGER DEFAULT 0
);
"""

_TRADE_COLUMNS = [
    "id",
    "ticker",
    "direction",
    "entry_price",
    "exit_price",
    "shares",
    "open_shares",
    "entry_date",
    "exit_date",
    "parent_trade_id",
    "thesis",
    "claude_analysis",
    "outcome",
    "pnl",
    "notes",
    "tags",
]


class Journal:
    """SQLite-backed trade journal."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate_trades_schema(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # --- Trade Operations ---

    def add_trade(self, entry: TradeJournalEntry) -> int:
        """Record a new trade. Returns the trade ID."""
        with self._connect() as conn:
            return self._insert_trade(conn, entry)

    def close_trade(
        self,
        trade_id: int,
        exit_price: float,
        notes: str = "",
        shares: float | None = None,
    ) -> TradeJournalEntry | None:
        """Close all or part of an open trade lot and calculate realized P&L."""
        trade = self.get_trade(trade_id)
        if trade is None:
            return None
        if trade.outcome != TradeOutcome.OPEN or trade.open_shares <= 0:
            return trade

        remaining_shares = trade.open_shares
        close_shares = remaining_shares if shares is None else min(shares, remaining_shares)
        if close_shares <= 0:
            raise ValueError("shares to close must be positive")

        entry_price = trade.entry_price or 0.0
        if trade.direction == Direction.LONG:
            pnl = (exit_price - entry_price) * close_shares
        else:
            pnl = (entry_price - exit_price) * close_shares

        if pnl > 0:
            outcome = TradeOutcome.WIN
        elif pnl < 0:
            outcome = TradeOutcome.LOSS
        else:
            outcome = TradeOutcome.BREAKEVEN

        with self._connect() as conn:
            if close_shares < remaining_shares:
                remaining_after = remaining_shares - close_shares
                conn.execute(
                    """UPDATE trades
                    SET shares = ?, open_shares = ?
                    WHERE id = ?""",
                    (
                        remaining_after,
                        remaining_after,
                        trade_id,
                    ),
                )
                closed_trade = trade.model_copy(
                    update={
                        "id": None,
                        "shares": close_shares,
                        "open_shares": 0.0,
                        "exit_price": exit_price,
                        "exit_date": datetime.now(),
                        "parent_trade_id": trade.id,
                        "outcome": outcome,
                        "pnl": pnl,
                        "notes": _merge_notes(trade.notes, notes),
                    }
                )
                closed_trade_id = self._insert_trade(conn, closed_trade)
                return self._get_trade(conn, closed_trade_id)

            if notes:
                conn.execute(
                    """UPDATE trades
                    SET exit_price = ?, exit_date = ?, outcome = ?, pnl = ?, open_shares = 0.0,
                        notes = CASE WHEN notes IS NOT NULL AND notes != ''
                                     THEN notes || '\n' || ?
                                     ELSE ? END
                    WHERE id = ?""",
                    (
                        exit_price,
                        datetime.now().isoformat(),
                        outcome.value,
                        pnl,
                        notes,
                        notes,
                        trade_id,
                    ),
                )
            else:
                conn.execute(
                    """UPDATE trades
                    SET exit_price = ?, exit_date = ?, outcome = ?, pnl = ?, open_shares = 0.0
                    WHERE id = ?""",
                    (exit_price, datetime.now().isoformat(), outcome.value, pnl, trade_id),
                )

        return self.get_trade(trade_id)

    def close_position(
        self,
        ticker: str,
        direction: Direction,
        shares: float,
        exit_price: float,
        notes: str = "",
    ) -> list[TradeJournalEntry]:
        """Close a position across open lots using FIFO matching."""
        if shares <= 0:
            raise ValueError("shares to close must be positive")

        remaining = shares
        closed: list[TradeJournalEntry] = []
        for lot in self.get_open_lots(ticker=ticker, direction=direction):
            if remaining <= 0:
                break
            lot_close = min(lot.open_shares, remaining)
            realized = self.close_trade(
                lot.id or 0,
                exit_price=exit_price,
                notes=notes,
                shares=lot_close,
            )
            if realized is not None:
                closed.append(realized)
            remaining -= lot_close
        return closed

    def get_trade(self, trade_id: int) -> TradeJournalEntry | None:
        """Get a single trade by ID."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
            if row is None:
                return None
            return _row_to_trade(row)

    def get_open_trades(self) -> list[TradeJournalEntry]:
        """Get all open trades."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM trades
                WHERE outcome = 'open' AND open_shares > 0
                ORDER BY entry_date DESC, id DESC"""
            ).fetchall()
            return [_row_to_trade(r) for r in rows]

    def get_open_lots(
        self,
        ticker: str | None = None,
        direction: Direction | None = None,
    ) -> list[TradeJournalEntry]:
        """Get currently open lots, optionally filtered by ticker and direction."""
        query = [
            "SELECT * FROM trades WHERE outcome = 'open' AND open_shares > 0"
        ]
        params: list[str] = []
        if ticker is not None:
            query.append("AND ticker = ?")
            params.append(ticker.upper())
        if direction is not None:
            query.append("AND direction = ?")
            params.append(direction.value)
        query.append("ORDER BY entry_date ASC, id ASC")
        with self._connect() as conn:
            rows = conn.execute(" ".join(query), params).fetchall()
        return [_row_to_trade(r) for r in rows]

    def get_recent_trades(self, limit: int = 20) -> list[TradeJournalEntry]:
        """Get recent trades, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [_row_to_trade(r) for r in rows]

    def get_trades_by_ticker(self, ticker: str) -> list[TradeJournalEntry]:
        """Get all trades for a specific ticker."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE ticker = ? ORDER BY entry_date DESC", (ticker,)
            ).fetchall()
            return [_row_to_trade(r) for r in rows]

    def get_trade_stats(self) -> dict[str, float | int]:
        """Get aggregate trade statistics."""
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            wins = conn.execute("SELECT COUNT(*) FROM trades WHERE outcome = 'win'").fetchone()[0]
            losses = conn.execute("SELECT COUNT(*) FROM trades WHERE outcome = 'loss'").fetchone()[
                0
            ]
            open_count = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE outcome = 'open' AND open_shares > 0"
            ).fetchone()[0]
            total_pnl = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE outcome != 'open'"
            ).fetchone()[0]
            avg_win = conn.execute(
                "SELECT COALESCE(AVG(pnl), 0) FROM trades WHERE outcome = 'win'"
            ).fetchone()[0]
            avg_loss = conn.execute(
                "SELECT COALESCE(AVG(pnl), 0) FROM trades WHERE outcome = 'loss'"
            ).fetchone()[0]

        win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0.0
        return {
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "open": open_count,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
        }

    # --- Portfolio Snapshots ---

    def save_snapshot(self, snapshot: PortfolioSnapshot) -> int:
        """Save a portfolio snapshot."""
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO portfolio_snapshots
                (timestamp, total_value, cash, positions_json, daily_pnl)
                VALUES (?, ?, ?, ?, ?)""",
                (
                    snapshot.timestamp.isoformat(),
                    snapshot.total_value,
                    snapshot.cash,
                    snapshot.positions_json,
                    snapshot.daily_pnl,
                ),
            )
            return cursor.lastrowid or 0

    def get_recent_snapshots(self, limit: int = 30) -> list[PortfolioSnapshot]:
        """Get recent portfolio snapshots."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM portfolio_snapshots ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            results: list[PortfolioSnapshot] = []
            for r in rows:
                results.append(
                    PortfolioSnapshot(
                        id=r["id"],
                        timestamp=datetime.fromisoformat(r["timestamp"]),
                        total_value=r["total_value"] or 0.0,
                        cash=r["cash"] or 0.0,
                        positions_json=r["positions_json"] or "",
                        daily_pnl=r["daily_pnl"] or 0.0,
                    )
                )
            return results

    # --- Analysis Log ---

    def log_analysis(self, entry: AnalysisLogEntry) -> int:
        """Log a Claude analysis query and response."""
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO analysis_log
                (timestamp, query, context_json, response, tokens_used)
                VALUES (?, ?, ?, ?, ?)""",
                (
                    entry.timestamp.isoformat(),
                    entry.query,
                    entry.context_json,
                    entry.response,
                    entry.tokens_used,
                ),
            )
            return cursor.lastrowid or 0

    def _insert_trade(self, conn: sqlite3.Connection, entry: TradeJournalEntry) -> int:
        cursor = conn.execute(
            """INSERT INTO trades
            (ticker, direction, entry_price, exit_price, shares, open_shares,
             entry_date, exit_date, parent_trade_id, thesis, claude_analysis,
             outcome, pnl, notes, tags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry.ticker,
                entry.direction.value,
                entry.entry_price,
                entry.exit_price,
                entry.shares,
                entry.open_shares,
                entry.entry_date.isoformat() if entry.entry_date else None,
                entry.exit_date.isoformat() if entry.exit_date else None,
                entry.parent_trade_id,
                entry.thesis,
                entry.claude_analysis,
                entry.outcome.value,
                entry.pnl,
                entry.notes,
                ",".join(entry.tags) if entry.tags else "",
            ),
        )
        return cursor.lastrowid or 0

    def _migrate_trades_schema(self, conn: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(trades)").fetchall()
        }
        if "open_shares" not in columns:
            conn.execute("ALTER TABLE trades ADD COLUMN open_shares REAL DEFAULT 0.0")
        if "parent_trade_id" not in columns:
            conn.execute("ALTER TABLE trades ADD COLUMN parent_trade_id INTEGER")
        conn.execute(
            """UPDATE trades
            SET open_shares = CASE
                WHEN outcome = 'open' THEN COALESCE(NULLIF(shares, 0), 0)
                ELSE 0.0
            END
            WHERE open_shares IS NULL OR open_shares = 0.0"""
        )

    def _get_trade(self, conn: sqlite3.Connection, trade_id: int) -> TradeJournalEntry | None:
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        if row is None:
            return None
        return _row_to_trade(row)


def _row_to_trade(row: sqlite3.Row) -> TradeJournalEntry:
    """Convert a database row to a TradeJournalEntry."""
    return TradeJournalEntry(
        id=row["id"],
        ticker=row["ticker"],
        direction=Direction(row["direction"]),
        entry_price=row["entry_price"],
        exit_price=row["exit_price"],
        shares=row["shares"] or 0.0,
        open_shares=row["open_shares"] or 0.0,
        entry_date=datetime.fromisoformat(row["entry_date"]) if row["entry_date"] else None,
        exit_date=datetime.fromisoformat(row["exit_date"]) if row["exit_date"] else None,
        parent_trade_id=row["parent_trade_id"],
        thesis=row["thesis"] or "",
        claude_analysis=row["claude_analysis"] or "",
        outcome=TradeOutcome(row["outcome"]) if row["outcome"] else TradeOutcome.OPEN,
        pnl=row["pnl"] or 0.0,
        notes=row["notes"] or "",
        tags=row["tags"].split(",") if row["tags"] else [],
    )


def _merge_notes(existing: str, update: str) -> str:
    if existing and update:
        return f"{existing}\n{update}"
    return existing or update
