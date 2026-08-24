"""조정 레이더 - "지금 이 종목이 어디쯤 와 있나"를 보여준다.

이 파일은 주문을 내지 않는다. 판단 재료만 만든다.

왜 매매를 안 하는가:
    5년(2021-07~2026-08, 18종목) 데이터로 확인한 결과,
    "많이 떨어졌으니 산다"는 규칙은 아무 날에나 사는 것보다 **나빴다**.

      규칙                아무 날에나 산 것 대비 1년 후 초과수익
      52주 고점 -30%       -1.1%p
      200일선 -15%        -19.2%p
      RSI < 30            -21.5%p
      52주 고점 -20% +200일선 위   **+55.6%p**

    싼 게 좋은 게 아니라, 추세가 살아 있는데 눌린 게 좋은 것이다.
    다만 이걸 자동매매로 돌리면 손절·트레일링이 장기 보유를 끊어서
    이점이 사라진다(보유 20봉). 그래서 매매가 아니라 감지만 한다.
"""
from __future__ import annotations

import logging
import statistics as st
from dataclasses import dataclass, field

from . import indicators as ind
from .storage import Store

log = logging.getLogger(__name__)

# 조정 단계 - 이름과 색을 한 곳에서 관리한다
STAGES = [
    ("고점권", 0.0, -5.0),
    ("얕은 조정", -5.0, -12.0),
    ("조정", -12.0, -25.0),
    ("깊은 조정", -25.0, -40.0),
    ("급락", -40.0, -100.0),
]


def stage_of(dip_pct: float) -> str:
    for name, hi, lo in STAGES:
        if lo < dip_pct <= hi:
            return name
    return "고점권" if dip_pct > 0 else "급락"


@dataclass
class RadarRow:
    symbol: str
    name: str = ""
    price: float = 0.0
    hi52: float = 0.0
    lo52: float = 0.0
    dip_pct: float = 0.0          # 52주 고점 대비 (음수)
    off_low_pct: float = 0.0      # 52주 저점 대비 (양수)
    ma200: float = 0.0
    ma200_dev: float = 0.0        # 200일선 대비 이격 (%)
    ma200_rising: bool = False
    ma60_dev: float = 0.0
    rsi: float = 0.0
    stage: str = ""
    trend: str = ""               # 추세 생존 / 추세 이탈
    rebounding: bool = False
    verdict: str = ""             # 관심 / 관망 / 위험 / 자료부족
    reason: str = ""
    hist_n: int = 0               # 이 종목의 과거 유사 상황 건수
    hist_median: float | None = None   # 그때 1년 후 수익률 중앙값
    hist_win: float | None = None
    bars: int = 0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class DipRadar:
    """관심종목이 각각 조정 어디쯤에 있는지 계산한다.

    무거운 건 과거 통계뿐이라 종목별로 캐시한다 (하루 한 번이면 충분).
    """

    def __init__(self, store: Store, dip_min: float = 15.0, dip_max: float = 40.0,
                 trend_ma: int = 200, rsi_max: float = 60.0):
        self.store = store
        self.dip_min = dip_min
        self.dip_max = dip_max
        self.trend_ma = trend_ma
        self.rsi_max = rsi_max
        self._hist_cache: dict[str, tuple[int, float | None, float | None]] = {}

    # ------------------------------------------------------------------
    def scan(self, symbols: list[str], with_history: bool = True,
             live_price: dict | None = None) -> list[RadarRow]:
        rows = []
        for s in symbols:
            try:
                rows.append(self.row(s, with_history=with_history,
                                     price=(live_price or {}).get(s)))
            except Exception as e:
                log.debug("레이더 계산 실패 %s: %s", s, e)
                rows.append(RadarRow(symbol=s, name=self._name(s),
                                     verdict="자료부족", reason=str(e)[:60]))
        # 관심 -> 조정 깊은 순
        order = {"관심": 0, "관망": 1, "위험": 2, "자료부족": 3}
        rows.sort(key=lambda r: (order.get(r.verdict, 9), r.dip_pct))
        return rows

    def _name(self, sym: str) -> str:
        try:
            return self.store.stock_name(sym) or sym
        except Exception:
            return sym

    def row(self, symbol: str, with_history: bool = True,
            price: float | None = None) -> RadarRow:
        bars = self.store.get_candles(symbol, "D", limit=100000)
        r = RadarRow(symbol=symbol, name=self._name(symbol), bars=len(bars))
        if len(bars) < 260:
            r.verdict = "자료부족"
            r.reason = f"일봉 {len(bars)}/260봉 - [데이터] 탭에서 더 받아오세요"
            return r

        cs = ind.closes(bars)
        ls = ind.lows(bars)
        cur = float(price) if price else cs[-1]
        r.price = cur

        look = min(250, len(cs) - 1)
        r.hi52 = max(cs[-1 - look:])
        r.lo52 = min(cs[-1 - look:])
        r.dip_pct = (cur / r.hi52 - 1) * 100 if r.hi52 else 0.0
        r.off_low_pct = (cur / r.lo52 - 1) * 100 if r.lo52 else 0.0

        tma = ind.sma(cs, self.trend_ma)
        m60 = ind.sma(cs, 60)
        rsi = ind.rsi(cs, 14)
        r.ma200 = tma[-1] or 0.0
        r.ma200_dev = (cur / r.ma200 - 1) * 100 if r.ma200 else 0.0
        r.ma60_dev = (cur / m60[-1] - 1) * 100 if m60[-1] else 0.0
        r.rsi = rsi[-1] or 0.0
        rise = min(20, len(tma) - 1)
        r.ma200_rising = bool(tma[-1 - rise] is not None and r.ma200 > tma[-1 - rise])

        r.stage = stage_of(r.dip_pct)
        r.trend = "추세 생존" if (r.ma200 and cur > r.ma200) else "추세 이탈"
        recent_low = min(ls[-4:]) if len(ls) > 4 else ls[-1]
        r.rebounding = cur > cs[-2] and cur > recent_low

        r.verdict, r.reason = self._judge(r)

        if with_history:
            n, med, win = self._history(symbol)
            r.hist_n, r.hist_median, r.hist_win = n, med, win
        return r

    def _judge(self, r: RadarRow) -> tuple[str, str]:
        """감지 규칙. 매수 지시가 아니라 '볼 만하다'는 표시다."""
        if r.trend == "추세 이탈":
            return "위험", (f"200일선 아래 ({r.ma200_dev:+.1f}%) - "
                            f"싸 보여도 추세가 꺾인 구간입니다")
        if r.dip_pct > -self.dip_min:
            return "관망", (f"고점 대비 {r.dip_pct:.1f}% - "
                            f"아직 -{self.dip_min:.0f}%까지 조정되지 않았습니다")
        if r.dip_pct < -self.dip_max:
            return "위험", (f"고점 대비 {r.dip_pct:.1f}% - "
                            f"-{self.dip_max:.0f}%를 넘는 하락은 추세 붕괴로 봅니다")
        if not r.ma200_rising:
            return "관망", "조정 폭은 맞지만 200일선이 우상향이 아닙니다"
        if r.rsi >= self.rsi_max:
            return "관망", f"RSI {r.rsi:.0f} - 반등이 이미 진행됐습니다"
        if not r.rebounding:
            return "관망", (f"조건은 맞지만 아직 반등 신호가 없습니다 "
                            f"(떨어지는 중에는 사지 않습니다)")
        return "관심", (f"고점 대비 {r.dip_pct:.1f}%, 200일선 {r.ma200_dev:+.1f}% "
                        f"위에서 반등 시작 - RSI {r.rsi:.0f}")

    # ------------------------------------------------------------------
    def _history(self, symbol: str, hold: int = 250,
                 cooldown: int = 60) -> tuple[int, float | None, float | None]:
        """이 종목에서 과거 같은 상황이 몇 번 있었고 1년 뒤 어땠나.

        표본이 몇 건뿐인 경우가 대부분이다. 그래서 평균이 아니라 중앙값을 쓴다
        (한 번의 대박이 평균을 통째로 끌어올린다).
        """
        if symbol in self._hist_cache:
            return self._hist_cache[symbol]

        bars = self.store.get_candles(symbol, "D", limit=100000)
        if len(bars) < 260 + hold:
            out = (0, None, None)
            self._hist_cache[symbol] = out
            return out

        cs = ind.closes(bars)
        tma = ind.sma(cs, self.trend_ma)
        rets, last = [], -10 ** 9
        for i in range(260, len(cs) - hold):
            if i - last < cooldown or tma[i] is None:
                continue
            hi = max(cs[i - 250:i + 1])
            dip = (cs[i] / hi - 1) * 100 if hi else 0
            if -self.dip_max <= dip <= -self.dip_min and cs[i] > tma[i]:
                rets.append((cs[i + hold] / cs[i] - 1) * 100)
                last = i

        if not rets:
            out = (0, None, None)
        else:
            out = (len(rets), st.median(rets),
                   sum(1 for x in rets if x > 0) / len(rets) * 100)
        self._hist_cache[symbol] = out
        return out

    def clear_cache(self) -> None:
        self._hist_cache.clear()

    # ------------------------------------------------------------------
    @staticmethod
    def summary(rows: list[RadarRow]) -> str:
        if not rows:
            return "관심종목이 없습니다."
        n = {"관심": 0, "관망": 0, "위험": 0, "자료부족": 0}
        for r in rows:
            n[r.verdict] = n.get(r.verdict, 0) + 1
        hot = [r.name for r in rows if r.verdict == "관심"]
        s = (f"관심 {n['관심']} · 관망 {n['관망']} · 위험 {n['위험']}"
             + (f" · 자료부족 {n['자료부족']}" if n["자료부족"] else ""))
        if hot:
            s += f"   |   지금 볼 만한 종목: {', '.join(hot[:5])}"
        return s
