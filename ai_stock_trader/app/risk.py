"""자금관리 / 안전장치.

전략이 아무리 좋아도 이 파일이 부실하면 계좌는 죽는다.
"전략이 실패해도 계좌는 살아남게" 하는 것이 여기 있는 모든 코드의 목적이다.

막는 것:
  - 1회 거래 과다손실   -> 손절폭 기준 포지션 사이징
  - 하루 과다손실       -> 일일 손실한도 도달 시 당일 신규진입 차단
  - 계좌 파괴           -> 누적 낙폭 한도 도달 시 엔진 정지
  - 연속 손실 후 뇌동매매 -> 쿨다운
  - 중복/과매매         -> 종목별 주문 잠금 + 재진입 쿨다운 + 일일 주문수 상한
  - 몰빵                -> 종목당 비중 상한 + 현금 최소보유
  - 비용에 지는 매매     -> 기대이익이 왕복 거래비용의 N배 미만이면 진입 자체를 취소
  - 못 빠져나오는 종목   -> 평균 거래대금 하한 / 1주 가격 대비 주문상한 확인
"""
from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .kis_client import tick_size
from .settings import CostConfig, RiskConfig
from .storage import Store

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 거래비용 계산 - 백테스터와 실거래 엔진이 같은 식을 쓴다.
# --------------------------------------------------------------------------
def norm_market(name: str) -> str:
    """KIS가 돌려주는 시장명을 호가단위 계산용 코드로 정규화한다."""
    s = (name or "").upper()
    return "KOSDAQ" if ("KOSDAQ" in s or "코스닥" in s) else "KOSPI"


def round_trip_cost_pct(cost: CostConfig, price: float = 0.0,
                        market: str = "KOSPI") -> float:
    """한 번 사고 파는 데 확정적으로 빠져나가는 비용(%).

    수수료는 양방향, 증권거래세는 매도에만, 슬리피지는 양방향이다.
    가격을 주면 최소 호가단위(1틱)를 함께 본다. 저가주는 1틱이 슬리피지
    가정보다 큰 경우가 있는데, 1틱은 어떤 실력으로도 줄일 수 없는 비용이라
    둘 중 큰 쪽을 슬리피지로 쓴다.
    """
    slip = float(cost.slippage_pct)
    if price > 0:
        slip = max(slip, tick_size(price, market) / price * 100)
    return cost.commission_pct * 2 + cost.sell_tax_pct + slip * 2


def avg_turnover(bars: list[dict], lookback: int = 20) -> float:
    """최근 N봉의 평균 거래대금(원). 캔들에 금액이 없으면 종가 x 거래량."""
    if not bars:
        return 0.0
    seg = bars[-lookback:] if lookback > 0 else bars
    vals = []
    for b in seg:
        v = float(b.get("value") or 0)
        if v <= 0:
            v = float(b.get("close") or 0) * float(b.get("volume") or 0)
        if v > 0:
            vals.append(v)
    return sum(vals) / len(vals) if vals else 0.0


@dataclass
class RiskState:
    day: str = ""
    day_start_equity: float = 0.0
    peak_equity: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    daily_block: bool = False
    daily_block_reason: str = ""
    cooldown_until: datetime | None = None
    pending: set = field(default_factory=set)

    def to_dict(self, cur_equity: float = 0.0) -> dict:
        dd = 0.0
        if self.peak_equity > 0 and cur_equity > 0:
            dd = (self.peak_equity - cur_equity) / self.peak_equity * 100
        day_pnl_pct = 0.0
        if self.day_start_equity > 0 and cur_equity > 0:
            day_pnl_pct = (cur_equity - self.day_start_equity) / self.day_start_equity * 100
        return {
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "daily_block": self.daily_block,
            "daily_block_reason": self.daily_block_reason,
            "drawdown_pct": dd,
            "day_pnl_pct": day_pnl_pct,
            "cooldown_until": self.cooldown_until.strftime("%H:%M") if self.cooldown_until else "",
            "pending": sorted(self.pending),
        }


class RiskManager:
    # 매매 없이 자산이 이만큼 변하면 입출금/초기화로 본다 (%)
    CAPITAL_JUMP_PCT = 12.0

    def __init__(self, cfg: RiskConfig, store: Store, mode: str,
                 cost: CostConfig | None = None):
        self.cfg = cfg
        self.store = store
        self.mode = mode
        self.cost = cost or CostConfig()
        self.state = RiskState()
        self._lock = threading.RLock()

    # -- 자산 기준선 --------------------------------------------------------
    def rebaseline(self, equity: float, reason: str) -> None:
        """낙폭 기준을 지금 자산으로 다시 잡는다.

        계좌 초기화나 입출금은 손실이 아니다. 그런데 예전 최고치를 그대로
        들고 있으면 낙폭이 부풀려져 엔진이 즉시 멈추고, 재시작해도 DB에서
        같은 값을 다시 읽어와 영원히 못 돌아간다.
        """
        ts = self.store.set_equity_epoch(self.mode)
        with self._lock:
            self.state.peak_equity = equity
            self.state.day_start_equity = equity
            if self.state.halted and "낙폭" in (self.state.halt_reason or ""):
                self.state.halted = False
                self.state.halt_reason = ""
            self.state.daily_block = False
            self.state.daily_block_reason = ""
        log.warning("[리스크] 자산 기준선 재설정 (%s) - 기준 %s원, 기준시각 %s",
                    reason, f"{equity:,.0f}", ts)

    def _detect_capital_change(self, equity: float) -> str:
        """매매가 없었는데 자산이 크게 변했으면 입출금/초기화로 본다."""
        last = self.store.last_equity(self.mode)
        if not last or not last.get("total_eval"):
            return ""
        prev = float(last["total_eval"])
        if prev <= 0 or equity <= 0:
            return ""
        change = abs(equity - prev) / prev * 100
        if change < self.CAPITAL_JUMP_PCT:
            return ""
        # 그 사이에 주문이 있었으면 실제 매매로 인한 변동이다
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if self.store.orders_between(self.mode, last["ts"], now) > 0:
            return ""
        return (f"매매 없이 자산이 {prev:,.0f} -> {equity:,.0f} "
                f"({'+' if equity > prev else '-'}{change:.1f}%) 변동")

    # -- 하루 경계 ----------------------------------------------------------
    def roll_day(self, equity: float) -> None:
        today = datetime.now().strftime("%Y-%m-%d")

        # 입출금/계좌초기화 감지가 먼저다. 아니면 아래에서 가짜 낙폭이 잡힌다.
        why = self._detect_capital_change(equity)
        if why:
            self.rebaseline(equity, why)

        with self._lock:
            if self.state.day != today:
                self.state.day = today
                self.state.day_start_equity = self.store.day_start_equity(self.mode) or equity
                self.state.daily_block = False
                self.state.daily_block_reason = ""
                self.state.pending.clear()
                log.info("[리스크] 새 거래일 %s 시작. 기준자산 %s원",
                         today, f"{self.state.day_start_equity:,.0f}")
            peak = max(self.store.peak_equity(self.mode), equity)
            self.state.peak_equity = peak

    # -- 강제 정지 ----------------------------------------------------------
    def halt(self, reason: str) -> None:
        with self._lock:
            self.state.halted = True
            self.state.halt_reason = reason
        log.error("[리스크] 엔진 정지: %s", reason)

    def resume(self) -> None:
        with self._lock:
            self.state.halted = False
            self.state.halt_reason = ""
            self.state.daily_block = False
            self.state.daily_block_reason = ""
            self.state.cooldown_until = None

    # -- 주문 잠금 (중복 주문 방지) -----------------------------------------
    def lock(self, symbol: str) -> bool:
        with self._lock:
            if symbol in self.state.pending:
                return False
            self.state.pending.add(symbol)
            return True

    def unlock(self, symbol: str) -> None:
        with self._lock:
            self.state.pending.discard(symbol)

    def is_locked(self, symbol: str) -> bool:
        return symbol in self.state.pending

    # -- 관문 ---------------------------------------------------------------
    def check_global(self, equity: float) -> tuple[bool, str]:
        """엔진 전체에 걸리는 관문. 신규 진입 가능한 상태인가."""
        c = self.cfg
        st = self.state

        if st.halted:
            return False, f"엔진 정지 상태 ({st.halt_reason})"

        # 누적 낙폭
        if st.peak_equity > 0:
            dd = (st.peak_equity - equity) / st.peak_equity * 100
            if dd >= c.max_drawdown_pct:
                self.halt(f"누적 낙폭 {dd:.2f}% >= 한도 {c.max_drawdown_pct}%")
                return False, st.halt_reason

        # 일일 손실
        if st.day_start_equity > 0:
            day_pnl_pct = (equity - st.day_start_equity) / st.day_start_equity * 100
            if day_pnl_pct <= -c.max_daily_loss_pct:
                if not st.daily_block:
                    st.daily_block = True
                    st.daily_block_reason = (f"일일 손실 {day_pnl_pct:.2f}% "
                                             f"<= 한도 -{c.max_daily_loss_pct}%")
                    log.warning("[리스크] %s -> 당일 신규진입 중단", st.daily_block_reason)
                return False, st.daily_block_reason
        if st.daily_block:
            return False, st.daily_block_reason

        # 연속 손실 쿨다운
        if st.cooldown_until and datetime.now() < st.cooldown_until:
            return False, f"연속손실 쿨다운 ({st.cooldown_until.strftime('%H:%M')}까지)"

        # 일일 주문 건수
        n = self.store.orders_today(self.mode)
        if n >= c.max_orders_per_day:
            return False, f"일일 주문 상한 도달 ({n}/{c.max_orders_per_day})"

        return True, ""

    # -- 거래비용 관문 -------------------------------------------------------
    def cost_pct(self, price: float = 0.0, market: str = "KOSPI") -> float:
        return round_trip_cost_pct(self.cost, price, market)

    def edge_ratio(self, price: float, stop_pct: float, target_pct: float,
                   market: str = "KOSPI") -> tuple[float, float, float]:
        """(기대이익%, 왕복비용%, 배수)를 돌려준다.

        목표가가 있으면 목표폭이 기대이익이다. 목표 없이 추세를 끝까지 타는
        전략은 손절폭을 1R로 보고 그걸 기대이익의 하한으로 쓴다.
        손절폭조차 비용 몇 배가 안 되면, 이겨도 남는 게 없는 거래다.
        """
        edge = target_pct if target_pct > 0 else stop_pct
        c = self.cost_pct(price, market)
        return edge, c, (edge / c if c > 0 else 0.0)

    def check_cost_edge(self, price: float, stop_pct: float, target_pct: float,
                        market: str = "KOSPI") -> tuple[bool, str]:
        need = float(getattr(self.cfg, "min_edge_cost_ratio", 0) or 0)
        if need <= 0:
            return True, ""
        edge, c, ratio = self.edge_ratio(price, stop_pct, target_pct, market)
        if edge <= 0 or c <= 0:
            return True, ""          # 폭을 못 내놓는 전략은 다른 관문에 맡긴다
        if ratio < need:
            kind = "목표" if target_pct > 0 else "손절폭"
            return False, (f"기대이익({kind} {edge:.2f}%) / 왕복비용 {c:.2f}% "
                           f"= {ratio:.1f}배 < 최소 {need:.1f}배 - 이겨도 비용이 먹는 거래")
        return True, ""

    def check_liquidity(self, price: float, turnover: float = 0.0) -> tuple[bool, str]:
        c = self.cfg
        if price > 0 and price > c.max_order_amount:
            return False, (f"1주 {price:,.0f}원 > 1회 주문상한 {c.max_order_amount:,}원 "
                           f"- 1주도 살 수 없음")
        need = float(getattr(c, "min_turnover_amount", 0) or 0)
        if need > 0 and 0 < turnover < need:
            return False, (f"평균 거래대금 {turnover / 1e8:.1f}억 < 하한 "
                           f"{need / 1e8:.1f}억 - 팔고 싶을 때 못 빠져나올 수 있음")
        return True, ""

    def check_entry(self, symbol: str, equity: float, cash: float,
                    open_positions: int, sector: str = "",
                    sector_exposure_pct: float = 0.0,
                    price: float = 0.0, stop_pct: float = 0.0,
                    target_pct: float = 0.0, turnover: float = 0.0,
                    market: str = "KOSPI") -> tuple[bool, str]:
        """종목별 진입 관문."""
        c = self.cfg
        ok, why = self.check_global(equity)
        if not ok:
            return False, why

        if self.is_locked(symbol):
            return False, "이미 주문 처리 중 (중복 방지)"

        if open_positions >= c.max_positions:
            return False, f"보유 종목 수 상한 ({open_positions}/{c.max_positions})"

        # 재진입 쿨다운
        last = self.store.last_exit_time(symbol, self.mode)
        if last:
            try:
                t = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
                if datetime.now() - t < timedelta(minutes=c.reentry_cooldown_min):
                    left = c.reentry_cooldown_min - int((datetime.now() - t).total_seconds() / 60)
                    return False, f"재진입 쿨다운 {left}분 남음"
            except ValueError:
                pass

        # 섹터 집중도 - 같은 업종이 한꺼번에 무너지는 상황 방어
        cap = float(getattr(c, "max_sector_weight_pct", 0) or 0)
        if sector and cap > 0 and sector_exposure_pct >= cap:
            return False, (f"'{sector}' 업종 비중 {sector_exposure_pct:.1f}% "
                           f">= 한도 {cap}%")

        # 현금 여력
        reserve = equity * c.min_cash_reserve_pct / 100
        if cash - reserve < c.min_order_amount:
            return False, (f"현금 부족 (가용 {max(cash - reserve, 0):,.0f}원 "
                           f"< 최소주문 {c.min_order_amount:,}원)")

        # 체결 현실성 - 팔 수 있는 종목인가, 1주라도 살 수 있는가
        if price > 0:
            ok, why = self.check_liquidity(price, turnover)
            if not ok:
                return False, why
            ok, why = self.check_cost_edge(price, stop_pct, target_pct, market)
            if not ok:
                return False, why

        return True, ""

    # -- 포지션 사이징 -------------------------------------------------------
    def position_size(self, price: float, stop: float, equity: float,
                      cash: float, strength: float = 1.0) -> tuple[int, str]:
        """손절폭 기준으로 수량을 정한다.

        핵심: "얼마 살까"가 아니라 "틀렸을 때 얼마 잃을까"에서 역산한다.
        """
        c = self.cfg
        if price <= 0:
            return 0, "가격 오류"

        stop_dist = price - stop if stop > 0 else 0
        if stop_dist <= 0:
            stop_dist = price * 0.03           # 손절 미지정 시 3% 가정
        # 손절이 비정상적으로 멀면 자르지 않고 수량으로 흡수 (사이징이 알아서 줄인다)

        risk_amount = equity * c.max_loss_per_trade_pct / 100 * max(min(strength, 1.0), 0.1)
        qty_risk = risk_amount / stop_dist

        qty_weight = (equity * c.max_position_weight_pct / 100) / price
        reserve = equity * c.min_cash_reserve_pct / 100
        qty_cash = max(cash - reserve, 0) / price
        qty_cap = c.max_order_amount / price

        qty = int(math.floor(min(qty_risk, qty_weight, qty_cash, qty_cap)))
        if qty <= 0:
            return 0, (f"수량 0 (리스크기준 {qty_risk:.1f} / 비중 {qty_weight:.1f} / "
                       f"현금 {qty_cash:.1f} / 상한 {qty_cap:.1f})")

        amount = qty * price
        if amount < c.min_order_amount:
            need = math.ceil(c.min_order_amount / price)
            if need <= min(qty_weight, qty_cash, qty_cap):
                qty = need
            else:
                return 0, f"주문금액 {amount:,.0f}원 < 최소 {c.min_order_amount:,}원"

        binding = min([(qty_risk, "손실한도"), (qty_weight, "종목비중"),
                       (qty_cash, "현금"), (qty_cap, "1회주문상한")])[1]
        return qty, (f"{qty}주 x {price:,.0f}원 = {qty * price:,.0f}원 "
                     f"(제약: {binding}, 손절시 -{qty * stop_dist:,.0f}원 "
                     f"= 자산의 {qty * stop_dist / equity * 100:.2f}%)")

    # -- 체결/청산 이벤트 ----------------------------------------------------
    def on_trade_closed(self, pnl: float) -> None:
        c = self.cfg
        if pnl >= 0:
            return
        n = self.store.consecutive_losses(self.mode)
        if n >= c.max_consecutive_losses:
            until = datetime.now() + timedelta(minutes=c.consecutive_loss_cooldown_min)
            with self._lock:
                self.state.cooldown_until = until
            log.warning("[리스크] 연속 손절 %d회 -> %s까지 쿨다운",
                        n, until.strftime("%H:%M"))
