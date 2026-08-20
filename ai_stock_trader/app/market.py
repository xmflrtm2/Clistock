"""국내 증시 개장 여부 / 세션 판정.

휴장일은 KIS 휴장일 API(실전 도메인 전용)로 조회해 DB에 캐시한다.
API를 못 쓰면 주말만 걸러내고 "확실치 않음"을 알린다 - 조용히 틀리는 것보다 낫다.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from .storage import Store

log = logging.getLogger(__name__)

OPEN = time(9, 0)
CLOSE = time(15, 30)
PRE_AUCTION = time(8, 30)
CLOSE_AUCTION = time(15, 20)


class MarketCalendar:
    def __init__(self, store: Store, quote_client=None):
        self.store = store
        self.client = quote_client        # 실전 도메인 KISClient (없으면 주말만 판정)

    def is_trading_day(self, d: date | None = None) -> tuple[bool, str]:
        d = d or date.today()
        iso = d.isoformat()

        cached = self.store.get_holiday(iso)
        if cached is not None:
            return cached, "캐시" if cached else "휴장일(캐시)"

        if d.weekday() >= 5:
            self.store.set_holiday(iso, False)
            return False, "주말"

        if self.client is not None:
            got = self.client.is_market_open_day(d.strftime("%Y%m%d"))
            if got is not None:
                self.store.set_holiday(iso, got)
                return got, "KIS 휴장일 API" if got else "휴장일(KIS 확인)"

        return True, "평일(휴장일 미확인)"

    def prefetch_holidays(self, days: int = 60) -> int:
        """앞으로 N일 휴장일을 미리 받아 캐시."""
        if self.client is None:
            return 0
        n = 0
        d = date.today()
        try:
            got = self.client._request(
                "GET", "/uapi/domestic-stock/v1/quotations/chk-holiday", "CTCA0903R",
                params={"BASS_DT": d.strftime("%Y%m%d"), "CTX_AREA_NK": "", "CTX_AREA_FK": ""},
            )
            for row in got.get("output") or []:
                bd = row.get("bass_dt")
                if not bd:
                    continue
                iso = f"{bd[:4]}-{bd[4:6]}-{bd[6:8]}"
                self.store.set_holiday(iso, row.get("opnd_yn") == "Y")
                n += 1
        except Exception as e:
            log.debug("휴장일 사전조회 실패: %s", e)
        return n

    def session(self, now: datetime | None = None) -> str:
        """closed | pre_auction | regular | close_auction | after"""
        now = now or datetime.now()
        ok, _ = self.is_trading_day(now.date())
        if not ok:
            return "closed"
        t = now.time()
        if t < PRE_AUCTION:
            return "closed"
        if t < OPEN:
            return "pre_auction"
        if t < CLOSE_AUCTION:
            return "regular"
        if t < CLOSE:
            return "close_auction"
        return "after"

    def is_open(self, now: datetime | None = None) -> bool:
        return self.session(now) in ("regular", "close_auction")

    def describe(self, now: datetime | None = None) -> str:
        now = now or datetime.now()
        s = self.session(now)
        return {
            "closed": "장 마감 / 휴장",
            "pre_auction": "장전 동시호가 (08:30~09:00)",
            "regular": "정규장 (09:00~15:20)",
            "close_auction": "장마감 동시호가 (15:20~15:30)",
            "after": "장 종료 (15:30 이후)",
        }.get(s, s)

    def last_trading_day(self, before: date | None = None) -> date:
        d = (before or date.today())
        for _ in range(15):
            d -= timedelta(days=1)
            ok, _r = self.is_trading_day(d)
            if ok:
                return d
        return d


def hhmm(t: str) -> time:
    h, m = t.split(":")
    return time(int(h), int(m))
