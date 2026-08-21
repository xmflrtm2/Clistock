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
class Check:
    """진입 조건 하나에 대한 판정.

    엔진은 매 루프마다 전 종목에 대해 이걸 만들어 저장한다.
    "왜 안 샀는가"가 쌓여야 전략을 고칠 근거가 생긴다.
    """
    label: str
    ok: bool
    detail: str = ""

    def to_dict(self) -> dict:
        return {"label": self.label, "ok": bool(self.ok), "detail": self.detail}


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

    # -- 진단 --------------------------------------------------------------
    def checklist(self, hist: list[dict], price: float, ctx: dict) -> list[Check]:
        """진입 조건을 항목별로 풀어서 보여준다. 매매 판단에는 쓰지 않는다.

        판단의 근거는 어디까지나 entry() 하나다 (백테스트와 같은 경로).
        여기는 사람이 읽기 위한 계기판이다.
        """
        return []

    def entry_gap_pct(self, hist: list[dict], price: float, ctx: dict) -> float | None:
        """진입 트리거까지 남은 거리 (%). 음수면 이미 넘어섰다는 뜻.

        돌파형 전략만 의미가 있다. 조건형 전략은 None.
        """
        t = self.trigger_price(hist, float(ctx.get("today_open") or 0))
        if t is None or price <= 0:
            return None
        return (t - price) / price * 100

    # -- 종목별 비율 스케일 --------------------------------------------------
    def vol_pct(self, hist: list[dict], price: float) -> float:
        """이 종목의 변동성 척도 (가격 대비 %).

        같은 '손절 2.5%'라도 하루 1% 움직이는 종목과 5% 움직이는 종목에서
        의미가 전혀 다르다. 종목마다 다른 기준을 만들려면 먼저 이 값이 필요하다.
        """
        if price <= 0 or not hist:
            return 0.0
        a = ind.last(ind.atr(hist, 14))
        return (a / price * 100) if a else 0.0

    def levels(self, price: float, hist: list[dict], base_stop: float) -> tuple[float, float]:
        """손절가와 목표가를 종목의 변동성에 맞춰 정한다.

        고정 %로 정하면 종목마다 의미가 달라진다. 하루 8% 움직이는 장에서
        '손절 2.5%'는 손절이 아니라 진입 직후 강제퇴장에 가깝다.
        그래서 기준을 ATR 배수로 두고, 절대 상한은 안전벨트로만 남긴다.

        - stop_cap_atr : 손절폭 상한 = ATR 의 몇 배 (0 이면 아래 고정 % 사용)
        - hard_stop_pct: 변동성이 아무리 커도 넘지 않을 절대 상한 (%)
        - max_stop_pct : stop_cap_atr 이 0 일 때만 쓰이는 예전 방식 고정 상한
        - take_profit_r: 목표 = 손절폭의 몇 배(R). 종목이 달라도 손익비는 같다.
        """
        stop = base_stop
        v = self.vol_pct(hist, price)

        # 너무 좁은 손절은 손절이 아니라 노이즈에 털리는 장치다.
        # 왕복 비용(0.2%)조차 못 덮는 폭이면 이길 수가 없다.
        floor = float(self.p.get("min_stop_atr", 0) or 0)
        if floor > 0 and v > 0:
            stop = min(stop, price * (1 - floor * v / 100))

        cap = float(self.p.get("stop_cap_atr", 0) or 0)
        if cap > 0 and v > 0:
            stop = max(stop, price * (1 - cap * v / 100))
            hard = float(self.p.get("hard_stop_pct", 0) or 0)
            if hard > 0:
                stop = max(stop, price * (1 - hard / 100))
        else:
            stop = self._clamp_stop(price, stop)

        r = float(self.p.get("take_profit_r", 0) or 0)
        if r > 0 and 0 < stop < price:
            return stop, price + r * (price - stop)
        tp = float(self.p.get("take_profit_pct", 0) or 0)
        return stop, (price * (1 + tp / 100) if tp > 0 else 0.0)

    def effective_pcts(self, hist: list[dict], price: float) -> dict:
        """이 종목에 지금 적용되는 실제 손절/목표 폭(%)."""
        v = self.vol_pct(hist, price)
        a = ind.last(ind.atr(hist, 14)) or (price * v / 100)
        stop, target = self.levels(price, hist, price - float(self.p.get("atr_stop", 2) or 2) * (a or 0))
        return {
            "atr_pct": v,
            "stop_pct": ((price - stop) / price * 100) if price > 0 and stop > 0 else 0.0,
            "target_pct": ((target - price) / price * 100) if price > 0 and target > 0 else 0.0,
        }

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
        "take_profit_pct": 4.0,   # 익절 (%) - take_profit_r 이 0일 때만 쓰임
        "take_profit_r": 1.6,     # 목표 = 손절폭의 몇 배 (종목이 달라도 손익비는 동일)
        "stop_cap_atr": 1.2,      # 손절폭 상한을 ATR 의 몇 배로 (종목별 자동 환산)
        "hard_stop_pct": 12.0,    # 변동성이 아무리 커도 넘지 않을 절대 상한 (%)
                                  # 평소엔 안 걸린다. 이상치 방어용이다.
        "trail_atr": 0.0,
        "breakeven_pct": 1.5,
        "min_range_pct": 0.8,     # 전일 변동폭이 너무 작으면 스킵 (%)
        "max_chase_pct": 1.5,     # 트리거 대비 이만큼 넘게 뛴 뒤에는 추격 금지 (%)
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
        stop, target = self.levels(price, hist, price - float(self.p["atr_stop"]) * a)
        rng = float(hist[-1]["high"]) - float(hist[-1]["low"])
        return Signal("BUY", 1.0,
                      f"변동성돌파 진입: 목표가 {t:,.0f} 돌파 (전일변동폭 {rng:,.0f} x k={self.p['k']})",
                      stop=stop, target=target)

    def checklist(self, hist: list[dict], price: float, ctx: dict) -> list[Check]:
        if not self._ok(hist):
            return [Check("일봉 데이터", False, f"{len(hist)}/{self.warmup}봉")]
        prev = hist[-1]
        out: list[Check] = []

        rng = float(prev["high"]) - float(prev["low"])
        rp = rng / float(prev["close"]) * 100 if prev["close"] else 0
        need = float(self.p["min_range_pct"])
        out.append(Check(f"전일 변동폭 >= {need}%", rp >= need, f"{rp:.2f}%"))

        ma = int(self.p.get("ma_filter") or 0)
        if ma > 0:
            m = ind.last(ind.sma(ind.closes(hist), ma))
            out.append(Check(f"전일종가 > MA{ma}", bool(m and float(prev["close"]) >= m),
                             f"{float(prev['close']):,.0f} vs {m:,.0f}" if m else "-"))

        vf = float(self.p.get("vol_filter") or 0)
        if vf > 0:
            vavg = ind.last(ind.sma(ind.volumes(hist), 20))
            v = float(prev.get("volume") or 0)
            out.append(Check(f"거래량 >= 20일평균 x{vf}", bool(vavg and v >= vavg * vf),
                             f"{v / vavg:.2f}배" if vavg else "-"))

        t = self.trigger_price(hist, float(ctx.get("today_open") or 0))
        if t is None:
            out.append(Check("돌파 목표가 돌파", False, "목표가 산출 불가 (필터 미통과)"))
        else:
            gap = (t - price) / price * 100 if price else 0
            out.append(Check("돌파 목표가 돌파", price >= t,
                             f"목표 {t:,.0f} / 현재 {price:,.0f} ({gap:+.2f}%)"))
            chase = float(self.p.get("max_chase_pct", 1.5))
            out.append(Check(f"추격 한도 {chase}% 이내", price <= t * (1 + chase / 100),
                             f"{(price / t - 1) * 100:+.2f}%" if t else "-"))
        return out

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
        "take_profit_r": 2.0,     # 목표 = 손절폭의 몇 배
        "stop_cap_atr": 3.0,      # 손절폭 상한 = ATR 의 몇 배
        "hard_stop_pct": 20.0,    # 절대 상한 (%) - 이상치 방어용
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
        stop, target = self.levels(price, hist, price - float(self.p["atr_stop"]) * a)
        return Signal("BUY", 1.0,
                      f"정배열 눌림목: MA{fast}>{slow} 유지, 최근 {n}봉 내 MA{fast} 터치 후 "
                      f"반등 (RSI {r[-1]:.1f})",
                      stop=stop, target=target)

    def checklist(self, hist: list[dict], price: float, ctx: dict) -> list[Check]:
        if not self._ok(hist):
            return [Check("일봉 데이터", False, f"{len(hist)}/{self.warmup}봉")]
        cs, ls = ind.closes(hist), ind.lows(hist)
        fast, slow = int(self.p["fast"]), int(self.p["slow"])
        f = ind.sma(cs, fast)
        sm = ind.sma(cs, slow)
        r = ind.rsi(cs, int(self.p["rsi_period"]))
        if f[-1] is None or sm[-1] is None or r[-1] is None:
            return [Check("지표 계산", False, "이평/RSI 산출 불가")]

        out = [Check(f"정배열 (MA{fast}>MA{slow})", f[-1] > sm[-1],
                     f"{f[-1]:,.0f} vs {sm[-1]:,.0f}"),
               Check(f"종가 > MA{slow}", cs[-1] > sm[-1],
                     f"{cs[-1]:,.0f} vs {sm[-1]:,.0f}"),
               Check(f"MA{slow} 우상향", bool(sm[-6] is not None and sm[-1] > sm[-6]),
                     f"{(sm[-1] / sm[-6] - 1) * 100:+.2f}% (5봉)" if sm[-6] else "-")]

        n = int(self.p["pullback_lookback"])
        touched = any(ls[i] <= f[i] for i in range(max(len(hist) - n, 0), len(hist))
                      if f[i] is not None)
        out.append(Check(f"최근 {n}봉 내 MA{fast} 터치", touched,
                         f"현재가 MA{fast} 대비 {(cs[-1] / f[-1] - 1) * 100:+.2f}%"))
        out.append(Check(f"MA{fast} 위로 반등", cs[-1] > f[-1] and cs[-1] > cs[-2],
                         f"전일대비 {(cs[-1] / cs[-2] - 1) * 100:+.2f}%"))
        rmax = float(self.p["rsi_max"])
        out.append(Check(f"RSI < {rmax}", r[-1] < rmax, f"RSI {r[-1]:.1f}"))
        return out

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
        "take_profit_r": 1.5,     # 목표 = 손절폭의 몇 배
        "stop_cap_atr": 0.8,      # 손절폭 상한 = 오늘 시가레인지 폭의 몇 배
        "min_stop_atr": 0.35,     # 손절폭 하한 = 레인지 폭의 몇 배 (노이즈 방어)
        "hard_stop_pct": 6.0,     # 절대 상한 (%) - 이상치 방어용
        "trail_atr": 2.0,
        "breakeven_pct": 1.0,
        "max_chase_pct": 0.4,
        "entry_deadline": "13:30",  # 이 시각 이후에는 신규 진입 안 함
    }

    def vol_pct(self, hist: list[dict], price: float) -> float:
        """1분봉 ATR 은 너무 작아서 기준이 못 된다. 오늘 시가레인지 폭을 쓴다."""
        rg = self._range(hist)
        if not rg or price <= 0:
            return 0.0
        return (rg[0] - rg[1]) / price * 100

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
        stop, target = self.levels(price, hist,
                                   max(lo, price - float(self.p["atr_stop"]) * a))
        return Signal("BUY", 1.0,
                      f"시가레인지({self.p['range_min']}분) 고가 {hi:,.0f} 상향돌파",
                      stop=stop, target=target)

    def checklist(self, hist: list[dict], price: float, ctx: dict) -> list[Check]:
        if len(hist) < self.warmup:
            return [Check("분봉 데이터", False, f"{len(hist)}/{self.warmup}봉")]
        now = str(ctx.get("now_hhmm") or hist[-1]["ts"][11:16])
        dl = str(self.p["entry_deadline"])
        out = [Check(f"진입 마감 {dl} 이전", now < dl, f"현재 {now}")]

        rg = self._range(hist)
        if not rg:
            out.append(Check(f"시가 {self.p['range_min']}분 레인지 형성", False,
                             "오늘 분봉 부족"))
            return out
        hi, lo = rg
        out.append(Check(f"시가 {self.p['range_min']}분 레인지 형성", True,
                         f"{lo:,.0f} ~ {hi:,.0f} (폭 {(hi - lo) / price * 100:.2f}%)"))
        gap = (hi - price) / price * 100 if price else 0
        out.append(Check("레인지 고가 돌파", price >= hi,
                         f"고가 {hi:,.0f} / 현재 {price:,.0f} ({gap:+.2f}%)"))
        chase = float(self.p["max_chase_pct"])
        out.append(Check(f"추격 한도 {chase}% 이내", price <= hi * (1 + chase / 100),
                         f"{(price / hi - 1) * 100:+.2f}%"))
        return out

    def entry_gap_pct(self, hist: list[dict], price: float, ctx: dict) -> float | None:
        rg = self._range(hist)
        if not rg or price <= 0:
            return None
        return (rg[0] - price) / price * 100

    def effective_pcts(self, hist: list[dict], price: float) -> dict:
        """이 전략의 손절은 레인지 저가가 기준이다. 표시값도 거기 맞춘다."""
        rg = self._range(hist)
        a = ind.last(ind.atr(hist, 14)) or (price * 0.005)
        base = price - float(self.p["atr_stop"]) * a
        if rg:
            base = max(rg[1], base)
        stop, target = self.levels(price, hist, base)
        return {
            "atr_pct": self.vol_pct(hist, price),
            "stop_pct": ((price - stop) / price * 100) if price > 0 and stop > 0 else 0.0,
            "target_pct": ((target - price) / price * 100) if price > 0 and target > 0 else 0.0,
        }

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
        "take_profit_r": 0.0,     # 장투는 목표를 두지 않는다 (트레일링으로만 따라감)
        "stop_cap_atr": 4.0,      # 손절폭 상한 = ATR 의 몇 배
        "hard_stop_pct": 40.0,    # 절대 상한 (%) - 이상치 방어용
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
        stop, target = self.levels(price, hist, price - float(self.p["atr_stop"]) * a)
        # 장기이평 아래로는 어차피 청산하므로 손절을 그보다 낮게 둘 이유가 없다
        stop = max(stop, tma[-1] * 0.97)
        return Signal("BUY", 1.0,
                      f"장기추세 진입: {int(self.p['trend_ma'])}일선 위 "
                      f"(+{ext:.1f}%), {n}일 모멘텀 {mom:+.1f}%",
                      stop=stop, target=target)

    def checklist(self, hist: list[dict], price: float, ctx: dict) -> list[Check]:
        if not self._ok(hist):
            return [Check("일봉 데이터", False, f"{len(hist)}/{self.warmup}봉")]
        cs = ind.closes(hist)
        tw, ew = int(self.p["trend_ma"]), int(self.p["entry_ma"])
        tma, ema_ = ind.sma(cs, tw), ind.sma(cs, ew)
        if tma[-1] is None or ema_[-1] is None:
            return [Check("지표 계산", False, "이평 산출 불가")]

        look = min(20, len(tma) - 1)
        ext = (cs[-1] / tma[-1] - 1) * 100
        out = [Check(f"종가 > MA{tw}", cs[-1] > tma[-1], f"{ext:+.1f}%"),
               Check(f"MA{tw} 우상향",
                     bool(tma[-1 - look] and tma[-1] > tma[-1 - look]),
                     f"{(tma[-1] / tma[-1 - look] - 1) * 100:+.2f}% ({look}봉)"
                     if tma[-1 - look] else "-"),
               Check(f"MA{ew} > MA{tw}", ema_[-1] > tma[-1],
                     f"{ema_[-1]:,.0f} vs {tma[-1]:,.0f}")]

        n = int(self.p["momentum_days"])
        if len(cs) > n:
            mom = (cs[-1] / cs[-1 - n] - 1) * 100
            out.append(Check(f"{n}일 모멘텀 >= {self.p['min_momentum_pct']}%",
                             mom >= float(self.p["min_momentum_pct"]), f"{mom:+.1f}%"))
        else:
            out.append(Check(f"{n}일 모멘텀", False, "기간 부족"))

        mx = float(self.p["max_extension_pct"])
        out.append(Check(f"MA{tw} 대비 과열 아님 (<{mx}%)", ext <= mx, f"{ext:+.1f}%"))
        return out

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
