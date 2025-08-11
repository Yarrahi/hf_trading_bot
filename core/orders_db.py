import os
import sqlite3
import threading
import time
from pathlib import Path
from contextlib import contextmanager

class OrdersDB:
    @staticmethod
    def _now_ms() -> int:
        return int(time.time_ns() // 1_000_000)

    def __init__(self, path: str):
        self.path = Path(path)
        # ensure parent directory exists so sqlite can create the file
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            # fallback to project root if the configured folder is not creatable
            self.path = Path("orders.db")
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._ensure()

    def _ensure(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        with self._conn() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS orders(
                    client_oid TEXT PRIMARY KEY,
                    exch_order_id TEXT,
                    symbol TEXT,
                    side TEXT,
                    price TEXT,
                    qty TEXT,
                    state TEXT,
                    last_error TEXT,
                    ts_created INTEGER,
                    last_update INTEGER
                )
                """
            )
            # Add missing column if upgrading from older schema
            try:
                con.execute("ALTER TABLE orders ADD COLUMN last_update INTEGER")
            except sqlite3.OperationalError:
                pass
            # Helpful index for cleanup/lookups
            try:
                con.execute("CREATE INDEX IF NOT EXISTS idx_orders_state_update ON orders(state, last_update)")
            except sqlite3.OperationalError:
                pass

    @contextmanager
    def _conn(self):
        abs_path = self.path.resolve()
        con = sqlite3.connect(abs_path.as_posix(), timeout=5.0, isolation_level=None, check_same_thread=False)
        try:
            yield con
        finally:
            con.close()

    def upsert_sent(self, client_oid, symbol, side, price, qty):
        now_ms = self._now_ms()
        with self._lock, self._conn() as con:
            con.execute(
                """
                INSERT INTO orders(client_oid, symbol, side, price, qty, state, ts_created, last_update)
                VALUES(?,?,?,?,?, 'sent', ?, ?)
                ON CONFLICT(client_oid) DO UPDATE SET
                  symbol=excluded.symbol,
                  side=excluded.side,
                  price=excluded.price,
                  qty=excluded.qty,
                  state='sent',
                  last_update=excluded.last_update
                """,
                (client_oid, symbol, side, str(price), str(qty), now_ms, now_ms)
            )
            row = con.execute("SELECT state FROM orders WHERE client_oid=?", (client_oid,)).fetchone()
            return row and row[0]

    def set_state(self, client_oid, state, exch_order_id=None, last_error=None):
        now_ms = self._now_ms()
        with self._lock, self._conn() as con:
            con.execute(
                """
                UPDATE orders
                   SET state=?,
                       exch_order_id=COALESCE(?, exch_order_id),
                       last_error=?,
                       last_update=?
                 WHERE client_oid=?
                """,
                (state, exch_order_id, last_error, now_ms, client_oid)
            )

    def exists_active(self, client_oid, ttl_sec: int = 5):
        now_ms = self._now_ms()
        cutoff = now_ms - ttl_sec * 1000
        with self._lock, self._conn() as con:
            row = con.execute(
                """
                SELECT state, COALESCE(last_update, ts_created)
                  FROM orders
                 WHERE client_oid=?
                """,
                (client_oid,)
            ).fetchone()
            if not row:
                return False
            state, ts = row[0], (row[1] or 0)
            if state in ('open','partial','filled','ack'):
                return True
            if state in ('sent','pending') and ts >= cutoff:
                return True
            return False

    def get(self, client_oid: str):
        with self._lock, self._conn() as con:
            cur = con.execute(
                "SELECT client_oid, state, exch_order_id, symbol, side, price, qty, ts_created, COALESCE(last_update, ts_created) FROM orders WHERE client_oid=?",
                (client_oid,)
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                'client_oid': row[0],
                'state': row[1],
                'exch_order_id': row[2],
                'symbol': row[3],
                'side': row[4],
                'price': row[5],
                'qty': row[6],
                'ts_created': int(row[7]) if row[7] is not None else None,
                'last_update': int(row[8]) if row[8] is not None else None,
            }

    def purge_stale(self, ttl_sec: int = 5):
        cutoff = self._now_ms() - ttl_sec * 1000
        with self._lock, self._conn() as con:
            con.execute(
                "DELETE FROM orders WHERE state IN ('pending','sent') AND COALESCE(last_update, ts_created) < ?",
                (cutoff,)
            )

db_singleton: OrdersDB | None = None

def get_db(path="data/db/orders.db") -> OrdersDB:
    global db_singleton
    if db_singleton is None:
        db_singleton = OrdersDB(path)
    return db_singleton
