"""SQLite 저장소.

모의투자로 돌리면서 쌓이는 모든 것 - 캔들, 신호, 주문, 체결, 완결거래, 자산곡선 -
을 한 파일에 누적한다. 나중에 백테스트/파라미터 튜닝의 원재료가 된다.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path
from typing import Iterable, Any

from .settings import DB_PATH

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    symbol   TEXT NOT NULL,
    tf       TEXT NOT NULL,
    ts       TEXT NOT NULL,
    open     REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, tf, ts)
);
CREATE INDEX IF NOT EXISTS ix_candles_sym_tf_ts ON candles(symbol, tf, ts);

CREATE TABLE IF NOT EXISTS quotes (
    symbol TEXT NOT NULL, ts TEXT NOT NULL,
    price REAL, change_pct REAL, volume REAL,
    ask REAL, bid REAL,
    PRIMARY KEY (symbol, ts)
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, symbol TEXT NOT NULL, strategy TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL, strength REAL, reason TEXT, acted INTEGER DEFAULT 0,
    blocked_by TEXT, mode TEXT
);
CREATE INDEX IF NOT EXISTS ix_signals_ts ON signals(ts);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, mode TEXT NOT NULL, symbol TEXT NOT NULL,
    side TEXT NOT NULL, qty INTEGER, price REAL, ord_dvsn TEXT,
    odno TEXT, org_no TEXT, status TEXT, message TEXT,
    strategy TEXT, signal_id INTEGER
);
CREATE INDEX IF NOT EXISTS ix_orders_ts ON orders(ts);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, order_id INTEGER, symbol TEXT, side TEXT,
    qty INTEGER, price REAL, fee REAL, tax REAL
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT, symbol TEXT, strategy TEXT,
    entry_ts TEXT, entry_price REAL, qty INTEGER,
    exit_ts TEXT, exit_price REAL, exit_reason TEXT,
    pnl REAL, pnl_pct REAL, fee REAL, tax REAL,
    stop_price REAL, target_price REAL, peak_price REAL,
    open INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_trades_open ON trades(open, symbol);

CREATE TABLE IF NOT EXISTS equity (
    ts TEXT PRIMARY KEY, mode TEXT,
    total_eval REAL, cash REAL, stock_eval REAL, day_pnl REAL
);

CREATE TABLE IF NOT EXISTS backtests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, strategy TEXT, params TEXT, symbols TEXT,
    start TEXT, end TEXT, metrics TEXT
);

CREATE TABLE IF NOT EXISTS ai_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, kind TEXT, content TEXT
);

CREATE TABLE IF NOT EXISTS holidays (
    d TEXT PRIMARY KEY, is_open INTEGER
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY, v TEXT, updated TEXT
);

CREATE TABLE IF NOT EXISTS stocks (
    symbol TEXT PRIMARY KEY,
    name TEXT, market TEXT, std_code TEXT, updated TEXT
);
CREATE INDEX IF NOT EXISTS ix_stocks_name ON stocks(name);

CREATE TABLE IF NOT EXISTS recent_views (
    symbol TEXT PRIMARY KEY, ts TEXT
);
"""


class Store:
    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        with self.conn() as c:
            c.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """예전 DB에 새로 생긴 컬럼을 채워 넣는다 (CREATE TABLE IF NOT EXISTS로는 안 됨)."""
        wanted = {
            "signals": [("mode", "TEXT")],
            "stocks": [("sector", "TEXT")],
            "trades": [("stop_price", "REAL"), ("target_price", "REAL"),
                       ("peak_price", "REAL")],
        }
        with self.conn() as c:
            for table, cols in wanted.items():
                have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
                for name, typ in cols:
                    if name not in have:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
                        log.info("DB 마이그레이션: %s.%s 추가", table, name)

    # -- connection ---------------------------------------------------------
    def _get(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    @contextmanager
    def conn(self):
        c = self._get()
        with self._write_lock:
            try:
                yield c
                c.commit()
            except Exception:
                c.rollback()
                raise

    def query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self._get().execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()):
        return self._get().execute(sql, args).fetchone()

    # -- candles ------------------------------------------------------------
    def upsert_candles(self, symbol: str, tf: str, rows: Iterable[dict]) -> int:
        data = [
            (symbol, tf, r["ts"], r["open"], r["high"], r["low"], r["close"], r.get("volume", 0))
            for r in rows
        ]
        if not data:
            return 0
        with self.conn() as c:
            c.executemany(
                "INSERT INTO candles(symbol,tf,ts,open,high,low,close,volume) "
                "VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(symbol,tf,ts) DO UPDATE SET "
                "open=excluded.open,high=excluded.high,low=excluded.low,"
                "close=excluded.close,volume=excluded.volume",
                data,
            )
        return len(data)

    def get_candles(self, symbol: str, tf: str = "D", limit: int = 300,
                    start: str | None = None, end: str | None = None) -> list[dict]:
        sql = "SELECT ts,open,high,low,close,volume FROM candles WHERE symbol=? AND tf=?"
        args: list[Any] = [symbol, tf]
        if start:
            sql += " AND ts>=?"
            args.append(start)
        if end:
            sql += " AND ts<=?"
            args.append(end)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        rows = self.query(sql, tuple(args))
        return [dict(r) for r in reversed(rows)]      # 오래된 -> 최신 순

    def candle_count(self, tf: str = "D") -> int:
        r = self.one("SELECT COUNT(*) n FROM candles WHERE tf=?", (tf,))
        return r["n"] if r else 0

    def symbols_with_data(self, tf: str = "D") -> list[str]:
        return [r["symbol"] for r in
                self.query("SELECT DISTINCT symbol FROM candles WHERE tf=? ORDER BY symbol", (tf,))]

    def candle_range(self, symbol: str, tf: str = "D") -> tuple[str | None, str | None]:
        r = self.one("SELECT MIN(ts) a, MAX(ts) b FROM candles WHERE symbol=? AND tf=?",
                     (symbol, tf))
        return (r["a"], r["b"]) if r else (None, None)

    # -- quotes -------------------------------------------------------------
    def add_quote(self, symbol: str, price: float, change_pct: float = 0,
                  volume: float = 0, ask: float = 0, bid: float = 0) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO quotes(symbol,ts,price,change_pct,volume,ask,bid) "
                "VALUES(?,?,?,?,?,?,?)",
                (symbol, _now(), price, change_pct, volume, ask, bid),
            )

    # -- signals / orders / fills ------------------------------------------
    def add_signal(self, symbol: str, strategy: str, side: str, price: float,
                   strength: float, reason: str, mode: str = "") -> int:
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO signals(ts,symbol,strategy,side,price,strength,reason,mode) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (_now(), symbol, strategy, side, price, strength, reason, mode),
            )
            return cur.lastrowid

    def mark_signal(self, signal_id: int, acted: bool, blocked_by: str = "") -> None:
        with self.conn() as c:
            c.execute("UPDATE signals SET acted=?, blocked_by=? WHERE id=?",
                      (1 if acted else 0, blocked_by, signal_id))

    def add_order(self, mode: str, symbol: str, side: str, qty: int, price: float,
                  ord_dvsn: str, odno: str, org_no: str, status: str,
                  message: str = "", strategy: str = "",
                  signal_id: int | None = None) -> int:
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO orders(ts,mode,symbol,side,qty,price,ord_dvsn,odno,org_no,"
                "status,message,strategy,signal_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_now(), mode, symbol, side, qty, price, ord_dvsn, odno, org_no,
                 status, message, strategy, signal_id),
            )
            return cur.lastrowid

    def add_fill(self, order_id: int, symbol: str, side: str, qty: int,
                 price: float, fee: float, tax: float) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO fills(ts,order_id,symbol,side,qty,price,fee,tax) "
                      "VALUES(?,?,?,?,?,?,?,?)",
                      (_now(), order_id, symbol, side, qty, price, fee, tax))

    def orders_today(self, mode: str) -> int:
        r = self.one("SELECT COUNT(*) n FROM orders WHERE mode=? AND substr(ts,1,10)=?",
                     (mode, _today()))
        return r["n"] if r else 0

    def recent_orders(self, mode: str | None = None, limit: int = 100) -> list[dict]:
        if mode:
            rows = self.query("SELECT * FROM orders WHERE mode=? ORDER BY id DESC LIMIT ?",
                              (mode, limit))
        else:
            rows = self.query("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def recent_signals(self, limit: int = 100, mode: str | None = None) -> list[dict]:
        if mode:
            return [dict(r) for r in self.query(
                "SELECT * FROM signals WHERE mode=? ORDER BY id DESC LIMIT ?", (mode, limit))]
        return [dict(r) for r in
                self.query("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,))]

    # -- trades (완결 거래) --------------------------------------------------
    def open_trade(self, mode: str, symbol: str, strategy: str, price: float,
                   qty: int, fee: float, stop: float = 0, target: float = 0) -> int:
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO trades(mode,symbol,strategy,entry_ts,entry_price,qty,fee,"
                "stop_price,target_price,peak_price,open) VALUES(?,?,?,?,?,?,?,?,?,?,1)",
                (mode, symbol, strategy, _now(), price, qty, fee, stop, target, price),
            )
            return cur.lastrowid

    def get_open_trade(self, symbol: str, mode: str) -> dict | None:
        r = self.one("SELECT * FROM trades WHERE open=1 AND symbol=? AND mode=? "
                     "ORDER BY id DESC LIMIT 1", (symbol, mode))
        return dict(r) if r else None

    def open_trades(self, mode: str) -> list[dict]:
        return [dict(r) for r in
                self.query("SELECT * FROM trades WHERE open=1 AND mode=? ORDER BY id", (mode,))]

    def update_trade_levels(self, trade_id: int, stop: float | None = None,
                            peak: float | None = None) -> None:
        with self.conn() as c:
            if stop is not None:
                c.execute("UPDATE trades SET stop_price=? WHERE id=?", (stop, trade_id))
            if peak is not None:
                c.execute("UPDATE trades SET peak_price=? WHERE id=?", (peak, trade_id))

    def close_trade(self, trade_id: int, exit_price: float, reason: str,
                    fee: float, tax: float) -> dict | None:
        t = self.one("SELECT * FROM trades WHERE id=?", (trade_id,))
        if not t:
            return None
        gross_in = (t["entry_price"] or 0) * (t["qty"] or 0)
        gross_out = exit_price * (t["qty"] or 0)
        total_fee = (t["fee"] or 0) + fee
        pnl = gross_out - gross_in - total_fee - tax
        pnl_pct = (pnl / gross_in * 100) if gross_in else 0.0
        with self.conn() as c:
            c.execute("UPDATE trades SET exit_ts=?,exit_price=?,exit_reason=?,pnl=?,"
                      "pnl_pct=?,fee=?,tax=?,open=0 WHERE id=?",
                      (_now(), exit_price, reason, pnl, pnl_pct, total_fee, tax, trade_id))
        d = dict(t)
        d.update(pnl=pnl, pnl_pct=pnl_pct, exit_price=exit_price, exit_reason=reason)
        return d

    def closed_trades(self, mode: str | None = None, limit: int = 500) -> list[dict]:
        if mode:
            rows = self.query("SELECT * FROM trades WHERE open=0 AND mode=? "
                              "ORDER BY id DESC LIMIT ?", (mode, limit))
        else:
            rows = self.query("SELECT * FROM trades WHERE open=0 ORDER BY id DESC LIMIT ?",
                              (limit,))
        return [dict(r) for r in rows]

    def consecutive_losses(self, mode: str) -> int:
        rows = self.query("SELECT pnl FROM trades WHERE open=0 AND mode=? "
                          "ORDER BY id DESC LIMIT 20", (mode,))
        n = 0
        for r in rows:
            if (r["pnl"] or 0) < 0:
                n += 1
            else:
                break
        return n

    def last_exit_time(self, symbol: str, mode: str) -> str | None:
        r = self.one("SELECT exit_ts FROM trades WHERE open=0 AND symbol=? AND mode=? "
                     "ORDER BY id DESC LIMIT 1", (symbol, mode))
        return r["exit_ts"] if r else None

    def realized_pnl_today(self, mode: str) -> float:
        r = self.one("SELECT COALESCE(SUM(pnl),0) s FROM trades "
                     "WHERE open=0 AND mode=? AND substr(exit_ts,1,10)=?", (mode, _today()))
        return float(r["s"]) if r else 0.0

    # -- equity -------------------------------------------------------------
    def add_equity(self, mode: str, total: float, cash: float,
                   stock: float, day_pnl: float) -> None:
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO equity"
                      "(ts,mode,total_eval,cash,stock_eval,day_pnl) VALUES(?,?,?,?,?,?)",
                      (_now(), mode, total, cash, stock, day_pnl))

    def equity_series(self, mode: str, limit: int = 2000) -> list[dict]:
        rows = self.query("SELECT ts,total_eval FROM equity WHERE mode=? "
                          "ORDER BY ts DESC LIMIT ?", (mode, limit))
        return [dict(r) for r in reversed(rows)]

    def peak_equity(self, mode: str) -> float:
        r = self.one("SELECT MAX(total_eval) m FROM equity WHERE mode=?", (mode,))
        return float(r["m"]) if r and r["m"] else 0.0

    def day_start_equity(self, mode: str) -> float | None:
        r = self.one("SELECT total_eval FROM equity WHERE mode=? AND substr(ts,1,10)=? "
                     "ORDER BY ts LIMIT 1", (mode, _today()))
        return float(r["total_eval"]) if r else None

    # -- backtests / ai -----------------------------------------------------
    def save_backtest(self, strategy: str, params: dict, symbols: list[str],
                      start: str, end: str, metrics: dict) -> int:
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO backtests(ts,strategy,params,symbols,start,end,metrics) "
                "VALUES(?,?,?,?,?,?,?)",
                (_now(), strategy, json.dumps(params, ensure_ascii=False),
                 ",".join(symbols), start, end, json.dumps(metrics, ensure_ascii=False)),
            )
            return cur.lastrowid

    def recent_backtests(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in
                self.query("SELECT * FROM backtests ORDER BY id DESC LIMIT ?", (limit,))]

    def add_ai_note(self, kind: str, content: str) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO ai_notes(ts,kind,content) VALUES(?,?,?)",
                      (_now(), kind, content))

    def recent_ai_notes(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in
                self.query("SELECT * FROM ai_notes ORDER BY id DESC LIMIT ?", (limit,))]

    # -- holidays / kv ------------------------------------------------------
    def set_holiday(self, d: str, is_open: bool) -> None:
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO holidays(d,is_open) VALUES(?,?)",
                      (d, 1 if is_open else 0))

    def get_holiday(self, d: str) -> bool | None:
        r = self.one("SELECT is_open FROM holidays WHERE d=?", (d,))
        return bool(r["is_open"]) if r else None

    def kv_set(self, k: str, v: str) -> None:
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO kv(k,v,updated) VALUES(?,?,?)", (k, v, _now()))

    def kv_get(self, k: str) -> str | None:
        r = self.one("SELECT v FROM kv WHERE k=?", (k,))
        return r["v"] if r else None

    # -- 종목 마스터 / 최근조회 --------------------------------------------
    def upsert_stocks(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        data = [(r["symbol"], r["name"], r["market"], r.get("std_code", ""), _now())
                for r in rows]
        with self.conn() as c:
            c.executemany(
                "INSERT INTO stocks(symbol,name,market,std_code,updated) VALUES(?,?,?,?,?) "
                "ON CONFLICT(symbol) DO UPDATE SET name=excluded.name,"
                "market=excluded.market,std_code=excluded.std_code,updated=excluded.updated",
                data)
        return len(data)

    def stock_count(self) -> int:
        r = self.one("SELECT COUNT(*) n FROM stocks")
        return r["n"] if r else 0

    def stock_name(self, symbol: str) -> str:
        r = self.one("SELECT name FROM stocks WHERE symbol=?", (symbol,))
        return r["name"] if r else symbol

    def search_stocks(self, q: str, limit: int = 60) -> list[dict]:
        """코드 정확일치 -> 이름 시작일치 -> 이름 포함 순으로 정렬."""
        q = (q or "").strip()
        if not q:
            return []
        like = f"%{q}%"
        rows = self.query(
            "SELECT symbol,name,market FROM stocks "
            "WHERE symbol LIKE ? OR name LIKE ? "
            "ORDER BY CASE WHEN symbol=? THEN 0 "
            "              WHEN symbol LIKE ? THEN 1 "
            "              WHEN name=? THEN 2 "
            "              WHEN name LIKE ? THEN 3 ELSE 4 END, length(name), name "
            "LIMIT ?",
            (f"{q}%", like, q, f"{q}%", q, f"{q}%", limit))
        return [dict(r) for r in rows]

    def set_sector(self, symbol: str, sector: str) -> None:
        if not sector:
            return
        with self.conn() as c:
            c.execute("UPDATE stocks SET sector=? WHERE symbol=?", (sector, symbol))

    def get_sector(self, symbol: str) -> str:
        r = self.one("SELECT sector FROM stocks WHERE symbol=?", (symbol,))
        return (r["sector"] or "") if r else ""

    def touch_recent(self, symbol: str) -> None:
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO recent_views(symbol,ts) VALUES(?,?)",
                      (symbol, _now()))
            c.execute("DELETE FROM recent_views WHERE symbol NOT IN "
                      "(SELECT symbol FROM recent_views ORDER BY ts DESC LIMIT 50)")

    def recent_views(self, limit: int = 30) -> list[str]:
        return [r["symbol"] for r in
                self.query("SELECT symbol FROM recent_views ORDER BY ts DESC LIMIT ?",
                           (limit,))]

    def stats(self) -> dict:
        def n(sql):
            r = self.one(sql)
            return r["n"] if r else 0
        return {
            "daily_candles": self.candle_count("D"),
            "minute_candles": self.candle_count("1m"),
            "symbols": len(self.symbols_with_data("D")),
            "signals": n("SELECT COUNT(*) n FROM signals"),
            "orders": n("SELECT COUNT(*) n FROM orders"),
            "closed_trades": n("SELECT COUNT(*) n FROM trades WHERE open=0"),
            "db_mb": round(self.path.stat().st_size / 1024 / 1024, 2) if self.path.exists() else 0,
        }


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return date.today().isoformat()


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store
