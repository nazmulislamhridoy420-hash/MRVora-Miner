from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil
from pathlib import Path
from typing import Iterator, Literal


MINE_REWARD = 10
MINE_COOLDOWN_SECONDS = 60
DAILY_REWARD = 100
REFERRAL_REWARD = 100
LEADERBOARD_SIZE = 10
DailyClaimStatus = Literal["claimed", "already_claimed", "repaired"]
VIPPurchaseStatus = Literal[
    "purchased",
    "already_owned",
    "out_of_order",
    "max_level",
    "insufficient_coins",
    "invalid_level",
]


@dataclass(frozen=True)
class VipTier:
    level: int
    name: str
    upgrade_cost: int | None
    mine_reward: int
    daily_reward: int
    mining_bonus_percent: int


VIP_TIERS = (
    VipTier(0, "No VIP", None, MINE_REWARD, DAILY_REWARD, 0),
    VipTier(1, "VIP 1", 500, 11, 110, 10),
    VipTier(2, "VIP 2", 1_500, 12, 125, 20),
    VipTier(3, "VIP 3", 4_000, 13, 150, 30),
    VipTier(4, "VIP 4", 10_000, 14, 175, 40),
    VipTier(5, "VIP 5", 25_000, 15, 200, 50),
)

DB_PATH = Path(os.getenv("MRVORA_DB_PATH", "mrvora_mining.sqlite3"))


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _transaction() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT NOT NULL,
                created_at INTEGER,
                balance INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
                referred_by INTEGER REFERENCES users(telegram_id),
                last_mined_at REAL NOT NULL DEFAULT 0,
                last_daily_claim TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                amount INTEGER NOT NULL,
                kind TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS transactions_user_idx "
            "ON transactions(telegram_id, created_at)"
        )
        user_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(users)")
        }
        if "vip_level" not in user_columns:
            connection.execute(
                "ALTER TABLE users ADD COLUMN vip_level INTEGER NOT NULL "
                "DEFAULT 0 CHECK (vip_level BETWEEN 0 AND 5)"
            )
        user_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(users)")
        }
        user_migrations = {
            "xp": "INTEGER NOT NULL DEFAULT 0 CHECK (xp >= 0)",
            "total_mined": "INTEGER NOT NULL DEFAULT 0 CHECK (total_mined >= 0)",
            "mining_power": "INTEGER NOT NULL DEFAULT 1 CHECK (mining_power >= 1)",
            "mining_streak": "INTEGER NOT NULL DEFAULT 0 CHECK (mining_streak >= 0)",
            "energy": "INTEGER NOT NULL DEFAULT 100 CHECK (energy >= 0)",
            "energy_updated_at": "REAL NOT NULL DEFAULT 0",
            "created_at": "INTEGER",
            "referral_earnings": (
                "INTEGER NOT NULL DEFAULT 0 CHECK (referral_earnings >= 0)"
            ),
        }
        for column, definition in user_migrations.items():
            if column not in user_columns:
                connection.execute(
                    f"ALTER TABLE users ADD COLUMN {column} {definition}"
                )

        connection.execute(
            """
            UPDATE users
            SET created_at = (
                SELECT MIN(created_at) FROM transactions
                WHERE transactions.telegram_id = users.telegram_id
            )
            WHERE created_at IS NULL
              AND EXISTS (
                SELECT 1 FROM transactions
                WHERE transactions.telegram_id = users.telegram_id
              )
            """
        )
        connection.execute(
            """
            UPDATE users
            SET total_mined = COALESCE((
                SELECT SUM(amount) FROM transactions
                WHERE transactions.telegram_id = users.telegram_id
                  AND transactions.kind = 'mine'
            ), 0)
            WHERE total_mined = 0
            """
        )
        connection.execute(
            """
            UPDATE users
            SET referral_earnings = COALESCE((
                SELECT SUM(amount) FROM transactions
                WHERE transactions.telegram_id = users.telegram_id
                  AND transactions.kind = 'referral'
            ), 0)
            WHERE referral_earnings = 0
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS vip_purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                previous_level INTEGER NOT NULL
                    CHECK (previous_level BETWEEN 0 AND 4),
                new_level INTEGER NOT NULL
                    CHECK (new_level BETWEEN 1 AND 5),
                cost INTEGER NOT NULL CHECK (cost > 0),
                purchased_at INTEGER NOT NULL,
                UNIQUE (telegram_id, new_level),
                CHECK (new_level = previous_level + 1)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS vip_purchases_user_idx "
            "ON vip_purchases(telegram_id, purchased_at)"
        )


def _ensure_user_row(
    connection: sqlite3.Connection,
    telegram_id: int,
    username: str | None,
    first_name: str,
) -> None:
    connection.execute(
        """
            INSERT INTO users (telegram_id, username, first_name, created_at)
            VALUES (?, ?, ?, ?)
        ON CONFLICT (telegram_id) DO NOTHING
        """,
        (
            telegram_id,
            username,
            first_name or "Miner",
            int(datetime.now(timezone.utc).timestamp()),
        ),
    )


def ensure_user(
    telegram_id: int,
    username: str | None = None,
    first_name: str = "Miner",
) -> int:
    """Ensure an account exists without changing its saved balance or claim state."""
    with _transaction() as connection:
        _ensure_user_row(connection, telegram_id, username, first_name)
        row = connection.execute(
            "SELECT balance FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        return int(row["balance"])


def register_user(
    telegram_id: int,
    username: str | None,
    first_name: str,
    referrer_id: int | None = None,
) -> tuple[bool, int, bool]:
    """Create a user once; a valid new referral gives the inviter a one-time bonus."""
    with _transaction() as connection:
        existing = connection.execute(
            "SELECT balance FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        if existing:
            connection.execute(
                "UPDATE users SET username = ?, first_name = ? WHERE telegram_id = ?",
                (username, first_name, telegram_id),
            )
            return False, int(existing["balance"]), False

        valid_referrer: int | None = None
        if referrer_id is not None and referrer_id != telegram_id:
            referrer = connection.execute(
                "SELECT telegram_id FROM users WHERE telegram_id = ?", (referrer_id,)
            ).fetchone()
            if referrer:
                valid_referrer = referrer_id

        connection.execute(
            """
            INSERT INTO users
                (telegram_id, username, first_name, referred_by, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                telegram_id,
                username,
                first_name,
                valid_referrer,
                int(datetime.now(timezone.utc).timestamp()),
            ),
        )

        if valid_referrer is not None:
            connection.execute(
                """
                UPDATE users
                SET balance = balance + ?,
                    referral_earnings = referral_earnings + ?
                WHERE telegram_id = ?
                """,
                (REFERRAL_REWARD, REFERRAL_REWARD, valid_referrer),
            )
            connection.execute(
                """
                INSERT INTO transactions (telegram_id, amount, kind, created_at)
                VALUES (?, ?, 'referral', ?)
                """,
                (valid_referrer, REFERRAL_REWARD, int(datetime.now(timezone.utc).timestamp())),
            )

        return True, 0, valid_referrer is not None


def get_balance(telegram_id: int) -> int:
    with _transaction() as connection:
        row = connection.execute(
            "SELECT balance FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        return int(row["balance"]) if row else 0


def mine(
    telegram_id: int,
    now: float,
    username: str | None = None,
    first_name: str = "Miner",
) -> tuple[bool, int, int]:
    """Return (success, balance, seconds_until_available)."""
    with _transaction() as connection:
        _ensure_user_row(connection, telegram_id, username, first_name)
        row = connection.execute(
            """
            SELECT balance, last_mined_at, vip_level
            FROM users WHERE telegram_id = ?
            """,
            (telegram_id,),
        ).fetchone()
        if not row:
            return False, 0, 0

        remaining = float(row["last_mined_at"]) + MINE_COOLDOWN_SECONDS - now
        if remaining > 0:
            return False, int(row["balance"]), ceil(remaining)

        reward = VIP_TIERS[int(row["vip_level"])].mine_reward
        balance = int(row["balance"]) + reward
        connection.execute(
            """
            UPDATE users
            SET balance = ?, last_mined_at = ?, total_mined = total_mined + ?
            WHERE telegram_id = ?
            """,
            (balance, now, reward, telegram_id),
        )
        connection.execute(
            """
            INSERT INTO transactions (telegram_id, amount, kind, created_at)
            VALUES (?, ?, 'mine', ?)
            """,
            (telegram_id, reward, now),
        )
        return True, balance, 0


def claim_daily(
    telegram_id: int,
    today: str,
    username: str | None = None,
    first_name: str = "Miner",
) -> tuple[DailyClaimStatus, int]:
    """Claim today's reward, or repair a recorded claim with a missing balance."""
    with _transaction() as connection:
        _ensure_user_row(connection, telegram_id, username, first_name)
        row = connection.execute(
            """
            SELECT balance, last_daily_claim, vip_level, mining_streak
            FROM users WHERE telegram_id = ?
            """,
            (telegram_id,),
        ).fetchone()
        if row["last_daily_claim"] == today:
            current_balance = int(row["balance"])
            day_start = int(
                datetime.strptime(today, "%Y-%m-%d")
                .replace(tzinfo=timezone.utc)
                .timestamp()
            )
            day_end = day_start + 86_400
            daily_ledger = connection.execute(
                """
                SELECT COUNT(*) AS count, COALESCE(SUM(amount), 0) AS amount
                FROM transactions
                WHERE telegram_id = ?
                  AND kind IN ('daily', 'daily_recovery')
                  AND created_at >= ? AND created_at < ?
                """,
                (telegram_id, day_start, day_end),
            ).fetchone()
            ledger_total = int(
                connection.execute(
                    "SELECT COALESCE(SUM(amount), 0) AS amount "
                    "FROM transactions WHERE telegram_id = ?",
                    (telegram_id,),
                ).fetchone()["amount"]
            )

            if int(daily_ledger["count"]) == 0 and current_balance == 0:
                repair_amount = VIP_TIERS[int(row["vip_level"])].daily_reward
                repaired_balance = max(0, ledger_total) + repair_amount
                connection.execute(
                    """
                    INSERT INTO transactions
                        (telegram_id, amount, kind, created_at)
                    VALUES (?, ?, 'daily_recovery', ?)
                    """,
                    (
                        telegram_id,
                        repair_amount,
                        int(datetime.now(timezone.utc).timestamp()),
                    ),
                )
                connection.execute(
                    "UPDATE users SET balance = ? WHERE telegram_id = ?",
                    (repaired_balance, telegram_id),
                )
                return "repaired", repaired_balance

            if (
                int(daily_ledger["count"]) > 0
                and current_balance == 0
                and ledger_total > 0
            ):
                connection.execute(
                    "UPDATE users SET balance = ? WHERE telegram_id = ?",
                    (ledger_total, telegram_id),
                )
                return "repaired", ledger_total

            return "already_claimed", current_balance

        reward = VIP_TIERS[int(row["vip_level"])].daily_reward
        balance = int(row["balance"]) + reward
        yesterday = (
            datetime.strptime(today, "%Y-%m-%d").date() - timedelta(days=1)
        ).isoformat()
        streak = (
            int(row["mining_streak"]) + 1
            if row["last_daily_claim"] == yesterday
            else 1
        )
        connection.execute(
            """
            UPDATE users
            SET balance = ?, last_daily_claim = ?, mining_streak = ?
            WHERE telegram_id = ?
            """,
            (balance, today, streak, telegram_id),
        )
        connection.execute(
            """
            INSERT INTO transactions (telegram_id, amount, kind, created_at)
            VALUES (?, ?, 'daily', ?)
            """,
            (telegram_id, reward, int(datetime.now(timezone.utc).timestamp())),
        )
        return "claimed", balance


def get_vip_membership(
    telegram_id: int,
    username: str | None = None,
    first_name: str = "Miner",
) -> tuple[int, int, list[sqlite3.Row]]:
    with _transaction() as connection:
        _ensure_user_row(connection, telegram_id, username, first_name)
        row = connection.execute(
            "SELECT vip_level, balance FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        history = connection.execute(
            """
            SELECT previous_level, new_level, cost, purchased_at
            FROM vip_purchases
            WHERE telegram_id = ?
            ORDER BY purchased_at DESC, id DESC
            """,
            (telegram_id,),
        ).fetchall()
        return int(row["vip_level"]), int(row["balance"]), list(history)


def purchase_vip(
    telegram_id: int,
    target_level: int,
    username: str | None = None,
    first_name: str = "Miner",
    now: int | None = None,
) -> tuple[VIPPurchaseStatus, int, int]:
    """Purchase only the next VIP level; return status, current level, balance."""
    if not 1 <= target_level < len(VIP_TIERS):
        return "invalid_level", 0, 0

    with _transaction() as connection:
        _ensure_user_row(connection, telegram_id, username, first_name)
        row = connection.execute(
            "SELECT vip_level, balance FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        current_level = int(row["vip_level"])
        balance = int(row["balance"])

        if current_level >= len(VIP_TIERS) - 1:
            return "max_level", current_level, balance
        if target_level <= current_level:
            return "already_owned", current_level, balance
        if target_level != current_level + 1:
            return "out_of_order", current_level, balance

        tier = VIP_TIERS[target_level]
        cost = tier.upgrade_cost
        if cost is None:
            raise RuntimeError(f"VIP tier {target_level} has no upgrade cost")
        if balance < cost:
            return "insufficient_coins", current_level, balance

        updated = connection.execute(
            """
            UPDATE users
            SET balance = balance - ?, vip_level = ?
            WHERE telegram_id = ? AND balance >= ? AND vip_level = ?
            """,
            (cost, target_level, telegram_id, cost, current_level),
        )
        if updated.rowcount != 1:
            return "insufficient_coins", current_level, balance

        purchased_at = (
            now if now is not None else int(datetime.now(timezone.utc).timestamp())
        )
        connection.execute(
            """
            INSERT INTO vip_purchases
                (telegram_id, previous_level, new_level, cost, purchased_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (telegram_id, current_level, target_level, cost, purchased_at),
        )
        connection.execute(
            """
            INSERT INTO transactions (telegram_id, amount, kind, created_at)
            VALUES (?, ?, 'vip_purchase', ?)
            """,
            (telegram_id, -cost, purchased_at),
        )
        return "purchased", target_level, balance - cost


def get_referral_stats(telegram_id: int) -> tuple[int, int]:
    with _transaction() as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS referral_count, COALESCE(SUM(balance), 0) AS balance
            FROM users WHERE referred_by = ?
            """,
            (telegram_id,),
        ).fetchone()
        return int(row["referral_count"]), int(row["balance"])


def get_leaderboard(limit: int = LEADERBOARD_SIZE) -> list[sqlite3.Row]:
    with _transaction() as connection:
        return list(
            connection.execute(
                """
                SELECT telegram_id, username, first_name, balance
                FROM users
                ORDER BY balance DESC, telegram_id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        )