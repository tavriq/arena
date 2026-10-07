"""SQLite: per-client rate limit, the daily budget in rubles, attempts kept for 30 days.

Budget flow, as in demo-rag's cost guard: ``reserve`` checks the client limit and
that today's spend + the attempt's worst case fit the daily budget, and records
the worst case; ``settle`` replaces it with the real cost after the attempt.
Reserving the worst case keeps concurrent attempts from overshooting together.

Client keys (IPv4 or IPv6 /64) are stored only as HMAC hashes with a salt that
lives in memory and changes on every start. Letters are stored with contacts
masked (app.pii) and deleted after ``retention_days``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from app.pii import mask_pii

HOUR = 3600
MSK = timezone(timedelta(hours=3))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (ip_hash TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS requests_ip_ts ON requests (ip_hash, ts);
CREATE TABLE IF NOT EXISTS spend (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    ts REAL NOT NULL,
    rub REAL NOT NULL,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS spend_day ON spend (day);
CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    level INTEGER NOT NULL,
    model TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    verdict TEXT NOT NULL,
    held_by TEXT,
    talked_into INTEGER NOT NULL,
    hacks TEXT NOT NULL,
    incident_rub INTEGER NOT NULL,
    cost_rub REAL NOT NULL,
    ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS attempts_ts ON attempts (ts);
"""


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str | None = None
    message: str | None = None
    reservation_id: int | None = None
    retry_after_s: int | None = None


def _rub(value: float) -> str:
    return f"{value:.0f} ₽" if value == int(value) else f"{value:.2f} ₽".replace(".", ",")


class Store:
    def __init__(self, db_path: str | Path, daily_budget_rub: float, rate_limit_per_hour: int,
                 retention_days: int = 30, clock: Callable[[], float] = time.time):
        self.db_path = Path(db_path)
        self.daily_budget_rub = daily_budget_rub
        self.rate_limit_per_hour = rate_limit_per_hour
        self.retention_s = retention_days * 86400
        self.clock = clock
        self._salt = secrets.token_bytes(16)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)
            conn.execute("DELETE FROM requests")  # hashes with an old salt only keep personal data around
        self.purge()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def day(self, ts: float | None = None) -> str:
        """Budget day in Moscow time: the page says "до 00:00 МСК"."""
        return datetime.fromtimestamp(self.clock() if ts is None else ts, tz=MSK).date().isoformat()

    def _hash(self, key: str) -> str:
        return hmac.new(self._salt, key.encode(), hashlib.sha256).hexdigest()

    # ---------- limits ----------

    def reserve(self, client_key: str, worst_rub: float) -> Decision:
        now = self.clock()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM requests WHERE ts <= ?", (now - HOUR,))
            key = self._hash(client_key)
            count, oldest = conn.execute("SELECT COUNT(*), MIN(ts) FROM requests WHERE ip_hash = ?",
                                         (key,)).fetchone()
            if count >= self.rate_limit_per_hour:
                conn.execute("COMMIT")
                retry = max(1, math.ceil(oldest + HOUR - now))
                return Decision(False, "rate_limited",
                                f"Не больше {self.rate_limit_per_hour} писем в час с одного адреса. "
                                f"Попробуйте через {max(1, (retry + 59) // 60)} мин. Записанные атаки работают и "
                                "сейчас.", retry_after_s=retry)
            spent = conn.execute("SELECT COALESCE(SUM(rub), 0) FROM spend WHERE day = ?",
                                 (self.day(now),)).fetchone()[0]
            if spent + worst_rub > self.daily_budget_rub:
                conn.execute("COMMIT")
                return Decision(False, "budget_exhausted",
                                f"Дневной бюджет демо ({_rub(self.daily_budget_rub)}) на сегодня закончился, "
                                "новые письма — после 00:00 по Москве. Записанные атаки работают и сейчас.")
            conn.execute("INSERT INTO requests (ip_hash, ts) VALUES (?, ?)", (key, now))
            cur = conn.execute("INSERT INTO spend (day, ts, rub, status) VALUES (?, ?, ?, 'reserved')",
                               (self.day(now), now, float(worst_rub)))
            conn.execute("COMMIT")
            return Decision(True, reservation_id=cur.lastrowid)
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def settle(self, reservation_id: int, rub: float) -> None:
        with closing(self._connect()) as conn:
            conn.execute("UPDATE spend SET rub = ?, status = 'settled' WHERE id = ?", (float(rub), reservation_id))

    def spent_today(self) -> float:
        with closing(self._connect()) as conn:
            return float(conn.execute("SELECT COALESCE(SUM(rub), 0) FROM spend WHERE day = ?",
                                      (self.day(),)).fetchone()[0])

    # ---------- attempts ----------

    def save_attempt(self, result: dict, subject: str, body: str) -> None:
        """Letter texts are masked before they touch the disk."""
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (result["id"], self.clock(), result["level"], result["model"], mask_pii(subject)[0],
                 mask_pii(body)[0], result["verdict"], result["held_by"], int(result["talked_into"]),
                 json.dumps([h["kind"] for h in result["hacks"]]), int(result["incident_rub"]),
                 float(result["cost_rub"]), int(result["ms"])))
        self.purge()

    def counters(self) -> list[dict]:
        since = self.clock() - self.retention_s
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT level, SUM(verdict = 'hacked'), COUNT(*) FROM attempts WHERE ts > ? GROUP BY level",
                (since,)).fetchall()
        by_level = {lvl: (int(h), int(t)) for lvl, h, t in rows}
        return [{"level": lvl, "hacked": by_level.get(lvl, (0, 0))[0], "total": by_level.get(lvl, (0, 0))[1]}
                for lvl in range(1, 6)]

    def purge(self) -> None:
        now = self.clock()
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM attempts WHERE ts <= ?", (now - self.retention_s,))
            conn.execute("DELETE FROM requests WHERE ts <= ?", (now - HOUR,))
            conn.execute("DELETE FROM spend WHERE ts <= ?", (now - 3 * 86400,))
