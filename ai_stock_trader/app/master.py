"""국내 종목 마스터.

KIS OpenAPI에는 "종목명으로 검색"하는 엔드포인트가 없다.
대신 한국투자증권이 공개하는 종목 마스터 파일(kospi_code.mst / kosdaq_code.mst)을
받아 로컬 DB에 넣어두고 검색한다. 한 번 받아두면 오프라인에서도 검색된다.

파일 포맷: cp949 고정폭. 각 줄의 뒤쪽 N바이트가 고정 필드이고,
앞부분이 [단축코드 9 | 표준코드 12 | 한글종목명 나머지] 이다.
"""
from __future__ import annotations

import io
import logging
import zipfile

import requests

from .storage import Store

log = logging.getLogger(__name__)

SOURCES = [
    ("KOSPI", "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip", 228),
    ("KOSDAQ", "https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip", 222),
]


class StockMaster:
    def __init__(self, store: Store):
        self.store = store

    @property
    def count(self) -> int:
        return self.store.stock_count()

    def ensure(self, log_fn=None) -> int:
        """비어 있으면 한 번 받아온다."""
        if self.count > 0:
            return self.count
        return self.refresh(log_fn)

    def refresh(self, log_fn=None) -> int:
        total = 0
        for market, url, tail in SOURCES:
            try:
                _say(log_fn, f"{market} 종목 마스터 내려받는 중...")
                r = requests.get(url, timeout=60)
                r.raise_for_status()
                z = zipfile.ZipFile(io.BytesIO(r.content))
                raw = z.read(z.namelist()[0]).decode("cp949", errors="replace")
                rows = []
                for line in raw.splitlines():
                    if len(line) <= tail:
                        continue
                    head = line[:len(line) - tail]
                    symbol = head[0:9].rstrip()
                    # 6자리 숫자 코드만 (펀드/ELW 등 비상장 코드 제외)
                    if len(symbol) != 6 or not symbol.isdigit():
                        continue
                    name = head[21:].strip()
                    if not name:
                        continue
                    rows.append({"symbol": symbol, "name": name, "market": market,
                                 "std_code": head[9:21].rstrip()})
                n = self.store.upsert_stocks(rows)
                total += n
                _say(log_fn, f"  {market} {n:,}종목")
            except Exception as e:
                _say(log_fn, f"  {market} 실패: {e}")
        _say(log_fn, f"종목 마스터 갱신 완료: 총 {self.store.stock_count():,}종목")
        return total

    def search(self, q: str, limit: int = 60) -> list[dict]:
        return self.store.search_stocks(q, limit)

    def name(self, symbol: str) -> str:
        return self.store.stock_name(symbol)

    def names(self, symbols: list[str]) -> dict[str, str]:
        return {s: self.store.stock_name(s) for s in symbols}


def _say(fn, msg: str) -> None:
    log.info(msg)
    if fn:
        try:
            fn(msg)
        except Exception:
            pass
