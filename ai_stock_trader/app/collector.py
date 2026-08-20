"""시세 수집기.

모의투자로 돌리는 기간 동안 조용히 데이터를 쌓는 게 이 모듈의 일이다.
쌓인 일봉/분봉이 곧 백테스트의 재료가 되고, 그게 전략 개선의 유일한 근거다.

KIS 제약:
  * 일봉 기간조회 - 1회 최대 100건  -> 뒤로 걸어가며 여러 번 호출
  * 1분봉 조회    - 1회 최대 30건   -> 기준시각을 당겨가며 하루치를 긁는다
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from .kis_client import KISClient, KISError
from .storage import Store

log = logging.getLogger(__name__)


class Collector:
    def __init__(self, store: Store, quote_client: KISClient):
        self.store = store
        self.qc = quote_client

    # -- 일봉 ---------------------------------------------------------------
    def sync_daily(self, symbol: str, days: int = 400,
                   log_fn=None) -> int:
        """과거 days일치 일봉을 채운다. 이미 있는 구간은 갱신만."""
        total = 0
        end = date.today()
        oldest_needed = end - timedelta(days=int(days * 1.5))  # 휴장일 감안
        # 1회 호출이 달력 140일치를 덮으므로 필요한 만큼 반복 횟수를 잡는다.
        # (고정 12회로 묶어두면 장기 전략에 필요한 수년치를 못 받는다)
        max_chunks = int(days * 1.5 / 135) + 3
        guard = 0
        while end > oldest_needed and guard < max_chunks:
            guard += 1
            start = max(end - timedelta(days=140), oldest_needed)
            try:
                rows = self.qc.daily_candles(symbol,
                                             start.strftime("%Y%m%d"),
                                             end.strftime("%Y%m%d"))
            except KISError as e:
                _say(log_fn, f"  {symbol} 일봉 실패: {e}")
                break
            if not rows:
                break
            total += self.store.upsert_candles(symbol, "D", rows)
            first = datetime.strptime(rows[0]["ts"], "%Y-%m-%d").date()
            if first <= oldest_needed:
                break
            end = first - timedelta(days=1)
        _say(log_fn, f"  {symbol} 일봉 {total}건")
        return total

    def sync_daily_all(self, symbols: list[str], days: int = 400, log_fn=None) -> int:
        n = 0
        _say(log_fn, f"일봉 수집 시작 ({len(symbols)}종목, {days}일)")
        for s in symbols:
            n += self.sync_daily(s, days, log_fn)
        _say(log_fn, f"일봉 수집 완료: 총 {n}건")
        return n

    # -- 분봉 ---------------------------------------------------------------
    def sync_minute_day(self, symbol: str, until: str = "153000",
                        max_calls: int = 16, log_fn=None) -> int:
        """오늘(또는 마지막 거래일) 1분봉을 뒤로 걸어가며 수집."""
        total, cursor = 0, until
        seen: set[str] = set()
        for _ in range(max_calls):
            try:
                rows = self.qc.minute_candles(symbol, cursor)
            except KISError as e:
                _say(log_fn, f"  {symbol} 분봉 실패: {e}")
                break
            rows = [r for r in rows if r["ts"] not in seen]
            if not rows:
                break
            for r in rows:
                seen.add(r["ts"])
            total += self.store.upsert_candles(symbol, "1m", rows)
            earliest = rows[0]["ts"]                       # 'YYYY-MM-DD HH:MM'
            hh, mm = earliest[11:13], earliest[14:16]
            t = datetime.strptime(f"{hh}{mm}", "%H%M") - timedelta(minutes=1)
            if t.strftime("%H%M") < "0900":
                break
            cursor = t.strftime("%H%M") + "00"
        return total

    def sync_minute_all(self, symbols: list[str], log_fn=None) -> int:
        n = 0
        _say(log_fn, f"분봉 수집 시작 ({len(symbols)}종목)")
        now = datetime.now().strftime("%H%M%S")
        until = now if "090000" < now < "153000" else "153000"
        for s in symbols:
            c = self.sync_minute_day(s, until, log_fn=log_fn)
            _say(log_fn, f"  {s} 분봉 {c}건")
            n += c
        _say(log_fn, f"분봉 수집 완료: 총 {n}건")
        return n

    # -- 실시간 스냅샷 -------------------------------------------------------
    def snapshot(self, symbols: list[str]) -> dict[str, dict]:
        out = {}
        for s in symbols:
            try:
                q = self.qc.current_price(s)
                out[s] = q
                self.store.add_quote(s, q["price"], q["change_pct"], q["volume"])
            except KISError as e:
                log.debug("스냅샷 실패 %s: %s", s, e)
        return out

    def coverage(self, symbols: list[str]) -> list[dict]:
        rows = []
        for s in symbols:
            a, b = self.store.candle_range(s, "D")
            am, bm = self.store.candle_range(s, "1m")
            n_d = len(self.store.get_candles(s, "D", limit=100000))
            n_m = len(self.store.get_candles(s, "1m", limit=200000))
            rows.append({"symbol": s, "daily": n_d, "daily_from": a or "-",
                         "daily_to": b or "-", "minute": n_m,
                         "minute_from": am or "-", "minute_to": bm or "-"})
        return rows


def _say(fn, msg: str) -> None:
    log.info(msg)
    if fn:
        try:
            fn(msg)
        except Exception:
            pass
