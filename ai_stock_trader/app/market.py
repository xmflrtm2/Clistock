"""국내 증시 개장 여부 / 세션 판정.

휴장일은 KIS 휴장일 API(실전 도메인 전용)로 조회해 DB에 캐시한다.
API를 못 쓰면(모의 키만 있거나 서버 장애) 내장 휴장일 표로 폴백하고,
표에도 없으면 "확실치 않음"을 알린다 - 조용히 틀리는 것보다 낫다.
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

# API를 못 쓸 때의 폴백용 내장 휴장일 (주말 제외한 KRX 휴장일).
# 2025~2026은 실제 거래일 데이터로 검증했고, 2026-10 이후는 KIS API 및
# 공휴일 규정(설/추석/어린이날/3·1절/광복절/개천절/한글날/성탄절 대체공휴일)
# 기준이다. 2027은 음력 환산 추정이 섞여 있으므로 연말에 KRX 공고로 갱신할 것.
# 임시공휴일/임시휴장은 미리 알 수 없다 - 그건 API만 안다.
KNOWN_HOLIDAYS = frozenset({
    # 2025
    "2025-01-01", "2025-01-27", "2025-01-28", "2025-01-29", "2025-01-30",
    "2025-03-03", "2025-05-01", "2025-05-05", "2025-05-06", "2025-06-03",
    "2025-06-06", "2025-08-15", "2025-10-03", "2025-10-06", "2025-10-07",
    "2025-10-08", "2025-10-09", "2025-12-25", "2025-12-31",
    # 2026
    "2026-01-01", "2026-02-16", "2026-02-17", "2026-02-18", "2026-03-02",
    "2026-05-01", "2026-05-05", "2026-05-25", "2026-06-03", "2026-08-17",
    "2026-09-24", "2026-09-25", "2026-10-05", "2026-10-09", "2026-12-25",
    "2026-12-31",
    # 2027 (설/부처님/추석은 음력 환산 추정 - KRX 공고 확인 후 갱신)
    "2027-01-01", "2027-02-08", "2027-02-09", "2027-03-01", "2027-05-05",
    "2027-05-13", "2027-08-16", "2027-09-14", "2027-09-15", "2027-09-16",
    "2027-10-04", "2027-10-11", "2027-12-27", "2027-12-31",
})

# API가 죽었을 때 30초 틱마다 재호출하지 않기 위한 백오프 (초)
_API_RETRY_SEC = 600


class MarketCalendar:
    def __init__(self, store: Store, quote_client=None):
        self.store = store
        self.client = quote_client        # 실전 도메인 KISClient (없으면 내장 표 판정)
        self._api_fail_until: datetime | None = None   # 이 시각까지 API 재시도 안 함

    def is_trading_day(self, d: date | None = None) -> tuple[bool, str]:
        d = d or date.today()
        iso = d.isoformat()

        cached = self.store.get_holiday(iso)
        if cached is not None:
            return cached, "캐시" if cached else "휴장일(캐시)"

        if d.weekday() >= 5:
            self.store.set_holiday(iso, False)
            return False, "주말"

        if self.client is not None and (
                self._api_fail_until is None
                or datetime.now() >= self._api_fail_until):
            got = self.client.is_market_open_day(d.strftime("%Y%m%d"))
            if got is not None:
                self._api_fail_until = None
                self.store.set_holiday(iso, got)
                return got, "KIS 휴장일 API" if got else "휴장일(KIS 확인)"
            # 실패를 기억해 뒀다가 잠시 후에만 다시 묻는다 (틱마다 호출 방지)
            self._api_fail_until = datetime.now() + timedelta(seconds=_API_RETRY_SEC)
            log.info("휴장일 API 실패 - %d초 동안 내장 휴장일 표로 판정", _API_RETRY_SEC)

        # 내장 표 폴백. API가 나중에 정정할 수 있도록 DB에는 캐시하지 않는다.
        if iso in KNOWN_HOLIDAYS:
            return False, "휴장일(내장 달력)"
        return True, "평일(휴장일 미확인)"

    def prefetch_holidays(self, days: int = 60) -> int:
        """앞으로 days일 휴장일을 미리 받아 캐시.

        휴장일 API는 1회 호출에 한 달 남짓만 돌려주므로, 마지막으로 받은
        날짜의 다음 날을 기준일로 다시 호출하며 days만큼 앞으로 민다.
        """
        if self.client is None:
            return 0
        n = 0
        cur = date.today()
        limit = cur + timedelta(days=days)
        for _ in range(6):                      # 호출당 약 한 달치 - 6번이면 넉넉하다
            try:
                got = self.client._request(
                    "GET", "/uapi/domestic-stock/v1/quotations/chk-holiday", "CTCA0903R",
                    params={"BASS_DT": cur.strftime("%Y%m%d"),
                            "CTX_AREA_NK": "", "CTX_AREA_FK": ""},
                )
            except Exception as e:
                log.debug("휴장일 사전조회 실패: %s", e)
                break
            latest = cur
            for row in got.get("output") or []:
                bd = row.get("bass_dt")
                if not bd or len(bd) != 8:
                    continue
                iso = f"{bd[:4]}-{bd[4:6]}-{bd[6:8]}"
                self.store.set_holiday(iso, row.get("opnd_yn") == "Y")
                n += 1
                try:
                    d = date(int(bd[:4]), int(bd[4:6]), int(bd[6:8]))
                    if d > latest:
                        latest = d
                except ValueError:
                    pass
            if latest <= cur:                   # 더 못 나아가면 그만
                break
            cur = latest + timedelta(days=1)
            if cur > limit:
                break
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
