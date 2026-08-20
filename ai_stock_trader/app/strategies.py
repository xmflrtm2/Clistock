"""매매 전략.

원칙 - 전략은 "언제 사고 언제 파는가"만 결정한다.
       "얼마나 사는가"와 "오늘 더 사도 되는가"는 risk.py 담당이다.
       이 둘을 섞으면 백테스트와 실거래가 달라진다.

모든 전략은 백테스트와 실거래에서 완전히 같은 코드 경로를 탄다.
그래서 백테스트 결과를 그대로 믿을 수 있다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import indicators as ind


@dataclass
class Signal:
    side: str = "HOLD"          # BUY | SELL | HOLD
    strength: float = 0.0       # 0.0 ~ 1.0 (포지션 크기 가중치)
    reason: str = ""
    stop: float = 0.0
    target: float = 0.0


@dataclass
class Position:
    symbol: str
    entry_price: float
    qty: int
    stop: float = 0.0
    target: float = 0.0
    peak: float = 0.0
    bars_held: int = 0
    strategy: str = ""
    meta: dict = field(default_factory=dict)


HOLD = Signal()


class Strategy:
    name = "base"
    label = "기본"
    description = ""
    timeframe = "D"          # 'D' | '1m'
    warmup = 60              # 판단에 필요한 최소 봉 개수
    intrabar = False         # True = 당일 장중 가격 돌파로 진입하는 유형
    exit_on_close = False    # True = 당일 종가 청산 (오버나이트 없음)
    default_params: dict = {}

    def __init__(self, params: dict | None = None):
        self.p = dict(self.default_params)
        self.p.update(params or {})

    # 장중 돌파형 전략만 구현. 오늘 시가를 알면 돌파 목표가를 계산할 수 있다.
    def trigger_price(self, hist: list[dict], today_open: float) -> float | None:
        return None

    def entry(self, hist: list[dict], price: float, ctx: dict) -> Signal:
        return HOLD

    def exit(self, hist: list[dict], price: float, pos: Position, ctx: dict) -> Signal:
        return HOLD

    # 트레일링 스톱 - 모든 전략 공통. 이익을 뱉어내지 않게 잠근다.
    def update_stop(self, pos: Position, price: float, atr_val: float | None) -> float:
        trail = float(self.p.get("trail_atr", 0) or 0)
        if trail <= 0 or not atr_val:
            return pos.stop
        pos.peak = max(pos.peak or pos.entry_price, price)
        new_stop = pos.peak - trail * atr_val
        # 본전 위로 올라왔으면 최소한 본전은 지킨다
        if pos.peak >= pos.entry_price * (1 + float(self.p.get("breakeven_pct", 1.5)) / 100):
            new_stop = max(new_stop, pos.entry_price * 1.001)
        return max(pos.stop, new_stop)

    def _clamp_stop(self, price: float, stop: float) -> float:
        """손절폭을 진입가 대비 일정 % 이내로 제한한다.

        ATR만으로 손절을 잡으면 변동성 큰 종목에서 손절폭이 진입가의 15~20%까지
        벌어진다. 당일 청산 전략에서 그건 사실상 손절이 없는 것이고,
        그 폭으로 수량을 역산하면 1주도 못 사게 된다.
        """
        m = float(self.p.get("max_stop_pct", 0) or 0)
        if m > 0:
            stop = max(stop, price * (1 - m / 100))
        return stop

    def _ok(self, hist: list[dict]) -> bool:
        return len(hist) >= self.warmup


# --------------------------------------------------------------------------
# 1. 변동성 돌파 (래리 윌리엄스) - 국내 단타의 고전. 일봉 기준, 당일 청산.
# --------------------------------------------------------------------------
class VolatilityBreakout(Strategy):
    name = "volatility_breakout"
    label = "변동성 돌파"
    description = ("전일 변동폭의 k배만큼 오늘 시가 위로 뚫으면 진입, 종가에 청산. "
                   "추세필터(이평)와 거래량 필터로 가짜 돌파를 걸러낸다.")
    timeframe = "D"
    warmup = 60
    intrabar = True
    exit_on_close = True
    default_params = {
        "k": 0.5,                 # 돌파 계수
        "ma_filter": 20,          # 종가가 이 이평 위일 때만 진입 (0=사용안함)
        "vol_filter": 1.0,        # 전일 거래량 / 20일 평균 >= 이 값
        "atr_stop": 2.0,          # 손절폭 = ATR * 이 값
        "max_stop_pct": 2.5,      # 단, 손절폭은 진입가의 이 % 이내 (당일청산이라 좁게)
        "take_profit_pct": 4.0,   # 익절 (%)
        "trail_atr": 0.0,
        "breakeven_pct": 1.5,
        "min_range_pct": 0.8,     # 전일 변동폭이 너무 작으면 스킵 (%)
    }

    def trigger_price(self, hist: list[dict], today_open: float) -> float | None:
        if not self._ok(hist) or not today_open:
            return None
        prev = hist[-1]
        rng = float(prev["high"]) - float(prev["low"])
        if rng <= 0:
            return None
        if rng / float(prev["close"]) * 100 < float(self.p["min_range_pct"]):
            return None

        # 추세 필터: 전일 종가가 이평 위
        ma = int(self.p.get("ma_filter") or 0)
        if ma > 0:
            m = ind.last(ind.sma(ind.closes(hist), ma))
            if m is None or float(prev["close"]) < m:
                return None

        # 거래량 필터
        vf = float(self.p.get("vol_filter") or 0)
        if vf > 0:
            vavg = ind.last(ind.sma(ind.volumes(hist), 20))
            if vavg and float(prev.get("volume") or 0) < vavg * vf:
                return None

        return today_open + rng * float(self.p["k"])

    def entry(self, hist: list[dict], price: float, ctx: dict) -> Signal:
        t = self.trigger_price(hist, float(ctx.get("today_open") or 0))
        if t is None or price < t:
            return HOLD
        # 시가 대비 이미 너무 많이 뛴 뒤에 따라붙지 않는다 (슬리피지 방어)
        chase = float(self.p.get("max_chase_pct", 1.5))
        if price > t * (1 + chase / 100):
            return Signal("HOLD", 0, f"돌파는 했으나 목표가({t:,.0f}) 대비 과열 - 추격 금지")

        a = ind.last(ind.atr(hist, 14)) or (price * 0.02)
        stop = self._clamp_stop(price, price - float(self.p["atr_stop"]) * a)
        target = price * (1 + float(self.p["take_profit_pct"]) / 100)
        rng = float(hist[-1]["high"]) - float(hist[-1]["low"])
        return Signal("BUY", 1.0,
                      f"변동성돌파 진입: 목표가 {t:,.0f} 돌파 (전일변동폭 {rng:,.0f} x k={self.p['k']})",
                      stop=stop, target=target)

    def exit(self, hist: list[dict], price: float, pos: Position, ctx: dict) -> Signal:
        if price <= pos.stop:
            return Signal("SELL", 1.0, f"손절 (기준 {pos.stop:,.0f})")
        if pos.target and price >= pos.target:
            return Signal("SELL", 1.0, f"익절 (기준 {pos.target:,.0f})")
        return HOLD


# --------------------------------------------------------------------------
# 2. 추세 눌림목 - 상승추세 중 조정 구간을 산다. 며칠 보유형.
# --------------------------------------------------------------------------
class TrendPullback(Strategy):
    name = "trend_pullback"
    label = "추세 눌림목"
    description = ("정배열(단기이평>장기이평)이 살아 있는 동안, 가격이 단기이평까지 눌렸다가 "
                   "되돌아 올라오는 순간 진입. 추세가 꺾이거나 과열되면 청산.")
    timeframe = "D"
    warmup = 90
    intrabar = False
    exit_on_close = False
    default_params = {
        "fast": 20, "slow": 60,
        "pullback_lookback": 5,   # 최근 N봉 안에 단기이평을 건드렸는가
        "rsi_period": 14,
        "rsi_max": 65,            # 이미 과열이면 진입 금지
        "rsi_exit": 72,
        "atr_stop": 2.0, "max_stop_pct": 8.0, "take_profit_pct": 6.0,
        "trail_atr": 2.5, "breakeven_pct": 2.0,
        "max_hold_bars": 15,
    }

    def entry(self, hist: list[dict], price: float, ctx: dict) -> Signal:
        if not self._ok(hist):
            return HOLD
        cs = ind.closes(hist)
        ls = ind.lows(hist)
        fast, slow = int(self.p["fast"]), int(self.p["slow"])
        f = ind.sma(cs, fast)
        s = ind.sma(cs, slow)
        r = ind.rsi(cs, int(self.p["rsi_period"]))
        if f[-1] is None or s[-1] is None or r[-1] is None:
            return HOLD

        # 1) 추세가 살아 있는가
        if not (f[-1] > s[-1] and cs[-1] > s[-1]):
            return HOLD
        if s[-6] is None or s[-1] <= s[-6]:
            return HOLD

        # 2) 실제로 눌렸는가 - 최근 N봉 중 저가가 단기이평을 건드린 적이 있어야 한다.
        #    (RSI 과매도로 잡으면 추세가 이미 깨진 뒤라 정배열 조건과 거의 동시에 성립하지 않는다)
        n = int(self.p["pullback_lookback"])
        touched = any(ls[i] <= f[i]
                      for i in range(max(len(hist) - n, 0), len(hist))
                      if f[i] is not None)
        if not touched:
            return HOLD

        # 3) 되돌아 올라왔는가
        if not (cs[-1] > f[-1] and cs[-1] > cs[-2]):
            return HOLD

        # 4) 이미 과열이면 진입하지 않는다
        if r[-1] >= float(self.p["rsi_max"]):
            return HOLD

        a = ind.last(ind.atr(hist, 14)) or (price * 0.02)
        stop = self._clamp_stop(price, price - float(self.p["atr_stop"]) * a)
        target = price * (1 + float(self.p["take_profit_pct"]) / 100)
        return Signal("BUY", 1.0,
                      f"정배열 눌림목: MA{fast}>{slow} 유지, 최근 {n}봉 내 MA{fast} 터치 후 "
                      f"반등 (RSI {r[-1]:.1f})",
                      stop=stop, target=target)

    def exit(self, hist: list[dict], price: float, pos: Position, ctx: dict) -> Signal:
        if price <= pos.stop:
            return Signal("SELL", 1.0, f"손절 (기준 {pos.stop:,.0f})")
        if pos.target and price >= pos.target:
            return Signal("SELL", 1.0, f"익절 (기준 {pos.target:,.0f})")
        if pos.bars_held >= int(self.p["max_hold_bars"]):
            return Signal("SELL", 1.0, f"보유기간 초과 ({pos.bars_held}봉)")
        if not self._ok(hist):
            return HOLD
        cs = ind.closes(hist)
        f = ind.sma(cs, int(self.p["fast"]))
        s = ind.sma(cs, int(self.p["slow"]))
        r = ind.rsi(cs, int(self.p["rsi_period"]))
        if f[-1] is not None and s[-1] is not None and f[-1] < s[-1]:
            return Signal("SELL", 1.0, "추세 이탈 (데드크로스)")
        if r[-1] is not None and r[-1] >= float(self.p["rsi_exit"]):
            return Signal("SELL", 1.0, f"과열 청산 (RSI {r[-1]:.1f})")
        return HOLD


# --------------------------------------------------------------------------
# 3. 시가 레인지 돌파 (분봉) - 장 초반 레인지를 뚫는 방향으로 따라붙는다.
# --------------------------------------------------------------------------
class OpeningRangeBreakout(Strategy):
    name = "opening_range_breakout"
    label = "시가 레인지 돌파"
    description = ("장 시작 후 N분간의 고가/저가 레인지를 만들고, 고가를 상향 돌파하면 "
                   "진입. 손절은 레인지 저가. 당일 청산.")
    timeframe = "1m"
    warmup = 35
    intrabar = False
    exit_on_close = True
    default_params = {
        "range_min": 30,          # 레인지 구성 시간(분)
        "atr_stop": 1.5,
        "max_stop_pct": 1.5,
        "take_profit_pct": 2.0,
        "trail_atr": 2.0,
        "breakeven_pct": 1.0,
        "max_chase_pct": 0.4,
        "entry_deadline": "13:30",  # 이 시각 이후에는 신규 진입 안 함
    }

    def _range(self, hist: list[dict]) -> tuple[float, float] | None:
        """오늘 09:00부터 range_min 분간의 고가/저가."""
        if not hist:
            return None
        day = hist[-1]["ts"][:10]
        today = [c for c in hist if c["ts"][:10] == day]
        if not today:
            return None
        n = int(self.p["range_min"])
        window = today[:n]
        if len(window) < max(5, n // 2):
            return None
        return max(float(c["high"]) for c in window), min(float(c["low"]) for c in window)

    def entry(self, hist: list[dict], price: float, ctx: dict) -> Signal:
        if len(hist) < self.warmup:
            return HOLD
        now = str(ctx.get("now_hhmm") or hist[-1]["ts"][11:16])
        if now >= str(self.p["entry_deadline"]):
            return HOLD
        rg = self._range(hist)
        if not rg:
            return HOLD
        hi, lo = rg
        if price < hi:
            return HOLD
        chase = float(self.p["max_chase_pct"])
        if price > hi * (1 + chase / 100):
            return Signal("HOLD", 0, f"레인지 고가({hi:,.0f}) 돌파 후 과열 - 추격 금지")

        a = ind.last(ind.atr(hist, 14)) or (price * 0.005)
        stop = self._clamp_stop(price, max(lo, price - float(self.p["atr_stop"]) * a))
        target = price * (1 + float(self.p["take_profit_pct"]) / 100)
        return Signal("BUY", 1.0,
                      f"시가레인지({self.p['range_min']}분) 고가 {hi:,.0f} 상향돌파",
                      stop=stop, target=target)

    def exit(self, hist: list[dict], price: float, pos: Position, ctx: dict) -> Signal:
        if price <= pos.stop:
            return Signal("SELL", 1.0, f"손절 (기준 {pos.stop:,.0f})")
        if pos.target and price >= pos.target:
            return Signal("SELL", 1.0, f"익절 (기준 {pos.target:,.0f})")
        return HOLD


# --------------------------------------------------------------------------
# 4. 장기 추세추종 - 오래 들고 간다. 자주 사고팔지 않는 게 핵심.
# --------------------------------------------------------------------------
class LongTermTrend(Strategy):
    name = "long_term_trend"
    label = "장기 추세추종"
    description = ("장기이평(기본 200일) 위에서 중기 모멘텀이 살아 있을 때 진입하고, "
                   "추세가 꺾일 때까지 계속 보유한다. 익절 목표를 두지 않고 "
                   "트레일링 스톱으로만 따라간다 - 크게 먹는 구간을 놓치지 않기 위해서다.")
    timeframe = "D"
    warmup = 220
    intrabar = False
    exit_on_close = False
    default_params = {
        "trend_ma": 200,          # 이 이평 위에서만 산다
        "entry_ma": 60,           # 진입 확인용 중기 이평
        "momentum_days": 120,     # 이 기간 수익률이 양수여야 한다
        "min_momentum_pct": 5.0,  # 최소 모멘텀
        "atr_stop": 3.5,          # 장투는 손절을 넉넉히 (흔들림에 털리지 않게)
        "max_stop_pct": 20.0,
        # 트레일링을 넉넉히(4.0) 두면 MDD가 34%까지 벌어진다. 2.0~2.5 구간에서
        # 낙폭이 일관되게 25%대로 내려가므로 그 구간 중간값을 쓴다.
        # (수익률 차이는 표본이 작아 노이즈이므로 최고값을 고르지 않았다)
        "trail_atr": 2.5,
        "breakeven_pct": 15.0,    # 15% 이상 오르면 본전 방어
        "take_profit_pct": 0.0,   # 0 = 익절 안 함 (추세가 끝날 때까지 보유)
        "exit_ma": 200,           # 이 이평 이탈 시 청산
        "max_extension_pct": 25.0,  # 장기이평 대비 이만큼 위면 과열 - 신규진입 안 함
    }

    def entry(self, hist: list[dict], price: float, ctx: dict) -> Signal:
        if not self._ok(hist):
            return HOLD
        cs = ind.closes(hist)
        tma = ind.sma(cs, int(self.p["trend_ma"]))
        ema_ = ind.sma(cs, int(self.p["entry_ma"]))
        if tma[-1] is None or ema_[-1] is None:
            return HOLD

        # 1) 장기 추세: 종가가 장기이평 위 + 장기이평 자체가 우상향
        if cs[-1] <= tma[-1]:
            return HOLD
        look = min(20, len(tma) - 1)
        if tma[-1 - look] is None or tma[-1] <= tma[-1 - look]:
            return HOLD

        # 2) 중기 이평도 장기 위 (정배열)
        if ema_[-1] <= tma[-1]:
            return HOLD

        # 3) 모멘텀: 최근 N일 수익률이 기준 이상
        n = int(self.p["momentum_days"])
        if len(cs) <= n:
            return HOLD
        mom = (cs[-1] / cs[-1 - n] - 1) * 100
        if mom < float(self.p["min_momentum_pct"]):
            return HOLD

        # 4) 과열 구간에서 추격 매수 금지
        ext = (cs[-1] / tma[-1] - 1) * 100
        if ext > float(self.p["max_extension_pct"]):
            return Signal("HOLD", 0,
                          f"장기이평 대비 {ext:.1f}% 과열 - 눌림 기다림")

        a = ind.last(ind.atr(hist, 14)) or (price * 0.02)
        stop = self._clamp_stop(price, price - float(self.p["atr_stop"]) * a)
        # 장기이평 아래로는 어차피 청산하므로 손절을 그보다 낮게 둘 이유가 없다
        stop = max(stop, tma[-1] * 0.97)
        tp = float(self.p["take_profit_pct"])
        target = price * (1 + tp / 100) if tp > 0 else 0.0
        return Signal("BUY", 1.0,
                      f"장기추세 진입: {int(self.p['trend_ma'])}일선 위 "
                      f"(+{ext:.1f}%), {n}일 모멘텀 {mom:+.1f}%",
                      stop=stop, target=target)

    def exit(self, hist: list[dict], price: float, pos: Position, ctx: dict) -> Signal:
        if pos.stop > 0 and price <= pos.stop:
            return Signal("SELL", 1.0, f"손절/트레일링 (기준 {pos.stop:,.0f})")
        if pos.target and price >= pos.target:
            return Signal("SELL", 1.0, f"익절 (기준 {pos.target:,.0f})")
        if not self._ok(hist):
            return HOLD
        cs = ind.closes(hist)
        xma = ind.sma(cs, int(self.p["exit_ma"]))
        if xma[-1] is not None and cs[-1] < xma[-1]:
            return Signal("SELL", 1.0, f"장기추세 이탈 ({int(self.p['exit_ma'])}일선 하향)")
        return HOLD


REGISTRY: dict[str, type[Strategy]] = {
    VolatilityBreakout.name: VolatilityBreakout,
    TrendPullback.name: TrendPullback,
    OpeningRangeBreakout.name: OpeningRangeBreakout,
    LongTermTrend.name: LongTermTrend,
}


def build(name: str, params: dict | None = None) -> Strategy:
    cls = REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"알 수 없는 전략: {name}")
    return cls(params)


def default_params(name: str) -> dict:
    cls = REGISTRY.get(name)
    return dict(cls.default_params) if cls else {}
