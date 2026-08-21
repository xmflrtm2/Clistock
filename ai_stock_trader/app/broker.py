"""브로커 추상화.

  PaperBroker - 실제 시세를 쓰되 체결은 로컬에서 흉내낸다. 키 없이도 돌아간다.
  KISBroker   - 한국투자증권 모의/실전 계좌에 실제 주문을 낸다.

둘 다 같은 인터페이스라서 trader.py는 어느 쪽인지 몰라도 된다.
모의계좌에서 검증한 코드가 그대로 실전으로 간다.
"""
from __future__ import annotations

import json
import logging
import time as _time
from dataclasses import dataclass

from .kis_client import KISClient, KISError, round_to_tick
from .settings import CostConfig
from .storage import Store

log = logging.getLogger(__name__)


@dataclass
class OrderResult:
    ok: bool
    side: str = ""
    symbol: str = ""
    qty: int = 0
    filled_qty: int = 0
    avg_price: float = 0.0
    fee: float = 0.0
    tax: float = 0.0
    odno: str = ""
    org_no: str = ""
    message: str = ""


class Broker:
    mode = "PAPER"
    account_error = ""

    def account(self, max_age: float = 5.0) -> dict:
        raise NotImplementedError

    @property
    def account_age(self) -> float:
        return 0.0

    def invalidate_account(self) -> None:
        pass

    def quote(self, symbol: str) -> dict:
        raise NotImplementedError

    def buy(self, symbol: str, qty: int, price: float) -> OrderResult:
        raise NotImplementedError

    def sell(self, symbol: str, qty: int, price: float) -> OrderResult:
        raise NotImplementedError

    def ping(self) -> tuple[bool, str]:
        return True, "OK"


def _costs(amount: float, side: str, cost: CostConfig) -> tuple[float, float]:
    fee = round(amount * cost.commission_pct / 100)
    tax = round(amount * cost.sell_tax_pct / 100) if side == "SELL" else 0
    return float(fee), float(tax)


# --------------------------------------------------------------------------
class PaperBroker(Broker):
    """로컬 모의체결. 실제 시세 + 슬리피지 + 수수료/세금 반영."""

    mode = "PAPER"
    KEY = "paper_account"

    def __init__(self, store: Store, quote_client: KISClient | None,
                 cost: CostConfig, initial_cash: int = 10_000_000,
                 key: str | None = None):
        self.store = store
        self.qc = quote_client
        self.cost = cost
        self.initial_cash = initial_cash
        # 프로필마다 다른 키를 주면 가상계좌가 서로 분리된다 (예수금 공유 안 함)
        self.key = key or self.KEY
        self._acct = self._load()

    def _load(self) -> dict:
        raw = self.store.kv_get(self.key)
        if raw:
            try:
                return json.loads(raw)
            except Exception:
                pass
        a = {"cash": self.initial_cash, "holdings": {}, "initial": self.initial_cash}
        self.store.kv_set(self.key, json.dumps(a))
        return a

    def _save(self) -> None:
        self.store.kv_set(self.key, json.dumps(self._acct, ensure_ascii=False))

    def reset(self, cash: int | None = None, mode: str | None = None) -> None:
        """가상계좌 초기화. 자산 기록도 함께 지워야 가짜 낙폭이 안 생긴다."""
        self._acct = {"cash": cash or self.initial_cash, "holdings": {},
                      "initial": cash or self.initial_cash}
        self._save()
        self.store.clear_mode_history(mode or self.mode)
        log.info("가상계좌 초기화: %s 자금 %s원", mode or self.mode,
                 f"{cash or self.initial_cash:,}")

    def quote(self, symbol: str) -> dict:
        if self.qc is None:
            raise KISError("시세 클라이언트가 없습니다. .env에 KIS 키를 넣어주세요.")
        return self.qc.current_price(symbol)

    def account(self, max_age: float = 5.0) -> dict:
        holdings = {}
        stock_eval = 0.0
        for sym, h in list(self._acct["holdings"].items()):
            try:
                px = self.quote(sym)["price"]
            except Exception:
                px = h.get("avg_price", 0)
            amt = px * h["qty"]
            stock_eval += amt
            pnl = amt - h["avg_price"] * h["qty"]
            holdings[sym] = {
                "symbol": sym, "name": h.get("name", sym), "qty": h["qty"],
                "sellable": h["qty"], "avg_price": h["avg_price"], "price": px,
                "eval_amt": amt, "pnl": pnl,
                "pnl_pct": (pnl / (h["avg_price"] * h["qty"]) * 100) if h["qty"] else 0,
            }
        cash = self._acct["cash"]
        return {
            "cash": cash, "orderable_cash": cash, "stock_eval": stock_eval,
            "total_eval": cash + stock_eval, "net_asset": cash + stock_eval,
            "pnl": sum(h["pnl"] for h in holdings.values()), "holdings": holdings,
        }

    def buy(self, symbol: str, qty: int, price: float) -> OrderResult:
        fill = price * (1 + self.cost.slippage_pct / 100)
        amount = fill * qty
        fee, tax = _costs(amount, "BUY", self.cost)
        if self._acct["cash"] < amount + fee:
            return OrderResult(False, "BUY", symbol, qty,
                               message=f"현금 부족 ({self._acct['cash']:,.0f} < {amount + fee:,.0f})")
        self._acct["cash"] -= amount + fee
        h = self._acct["holdings"].get(symbol, {"qty": 0, "avg_price": 0.0})
        total_qty = h["qty"] + qty
        h["avg_price"] = (h["avg_price"] * h["qty"] + fill * qty) / total_qty
        h["qty"] = total_qty
        self._acct["holdings"][symbol] = h
        self._save()
        return OrderResult(True, "BUY", symbol, qty, qty, fill, fee, tax,
                           message="모의체결")

    def sell(self, symbol: str, qty: int, price: float) -> OrderResult:
        h = self._acct["holdings"].get(symbol)
        if not h or h["qty"] < qty:
            return OrderResult(False, "SELL", symbol, qty, message="보유 수량 부족")
        fill = price * (1 - self.cost.slippage_pct / 100)
        amount = fill * qty
        fee, tax = _costs(amount, "SELL", self.cost)
        self._acct["cash"] += amount - fee - tax
        h["qty"] -= qty
        if h["qty"] <= 0:
            self._acct["holdings"].pop(symbol, None)
        self._save()
        return OrderResult(True, "SELL", symbol, qty, qty, fill, fee, tax,
                           message="모의체결")

    def ping(self) -> tuple[bool, str]:
        a = self.account()
        return True, (f"[PAPER] 로컬 모의계좌 | 예수금 {a['cash']:,.0f}원 "
                      f"| 총평가 {a['total_eval']:,.0f}원")


# --------------------------------------------------------------------------
class KISBroker(Broker):
    """한국투자증권 실계좌(모의/실전) 주문."""

    def __init__(self, store: Store, trade_client: KISClient,
                 quote_client: KISClient, cost: CostConfig,
                 order_type: str = "best", limit_slippage_pct: float = 0.3,
                 order_timeout_sec: int = 60):
        self.store = store
        self.tc = trade_client
        self.qc = quote_client or trade_client
        self.cost = cost
        self.order_type = order_type
        self.limit_slippage_pct = limit_slippage_pct
        self.timeout = order_timeout_sec
        self.mode = "MOCK" if trade_client.creds.is_mock else "REAL"
        self._acct_cache: dict | None = None
        self._acct_at = 0.0
        self._fails = 0
        self._next_try = 0.0
        self.account_error = ""

    def quote(self, symbol: str) -> dict:
        return self.qc.current_price(symbol)

    def account(self, max_age: float = 5.0) -> dict:
        """잔고 조회. 실패해도 예외를 던지지 않고 마지막 성공값을 돌려준다.

        모의투자 서버가 응답을 멈추는 일이 잦은데, 그때마다 화면이 0원으로
        바뀌거나 엔진 루프가 죽으면 곤란하다. 대신:
          - 최근 조회값이 있으면 재사용 (max_age초)
          - 연속 실패하면 재시도 간격을 늘려 서버를 더 괴롭히지 않는다
          - account_error 에 사유를 남겨 화면이 '오래된 값'임을 표시하게 한다
        """
        now = _time.monotonic()
        if self._acct_cache is not None and now - self._acct_at < max_age:
            return self._acct_cache
        # 백오프 중이면 캐시가 없어도 즉시 실패시킨다.
        # (없으면 매 호출이 타임아웃 시간만큼 통째로 붙잡힌다)
        if self._fails and now < self._next_try:
            if self._acct_cache is not None:
                return self._acct_cache
            raise KISError(f"잔고 조회 대기 중 ({int(self._next_try - now)}초 후 재시도) "
                           f"- {self.account_error}")

        try:
            acct = self.tc.balance()
        except KISError as e:
            self._fails += 1
            backoff = min(15 * (2 ** (self._fails - 1)), 300)
            self._next_try = now + backoff
            self.account_error = f"{e}"
            log.warning("잔고 조회 실패 %d회 - %.0f초 후 재시도: %s", self._fails, backoff, e)
            if self._acct_cache is not None:
                return self._acct_cache
            raise
        self._fails = 0
        self.account_error = ""
        self._acct_cache = acct
        self._acct_at = now
        return acct

    @property
    def account_age(self) -> float:
        if self._acct_cache is None:
            return -1.0
        return _time.monotonic() - self._acct_at

    def invalidate_account(self) -> None:
        """주문 직후처럼 잔고가 확실히 바뀐 시점에 캐시를 버린다."""
        self._acct_at = 0.0

    # -- 주문구분/가격 결정 --------------------------------------------------
    def _dvsn_price(self, side: str, price: float) -> tuple[str, int]:
        """order_type 설정에 따라 (주문구분코드, 주문단가)."""
        if self.order_type == "market":
            return "01", 0
        if self.order_type == "best":
            # 최유리지정가: 반대편 최우선호가로 즉시 체결. 시장가보다 슬리피지 통제가 낫다.
            # 모의투자는 최유리(03) 미지원 케이스가 있어 지정가로 대체한다.
            if self.tc.creds.is_mock:
                return self._limit(side, price)
            return "03", 0
        return self._limit(side, price)

    def _limit(self, side: str, price: float) -> tuple[str, int]:
        slip = self.limit_slippage_pct / 100
        raw = price * (1 + slip) if side == "BUY" else price * (1 - slip)
        return "00", round_to_tick(raw, up=(side == "BUY"))

    # -- 체결 확인 ----------------------------------------------------------
    def _wait_fill(self, odno: str, qty: int) -> tuple[int, float]:
        """주문번호로 체결 수량/평균가를 확인. (체결수량, 평균가)"""
        deadline = _time.time() + self.timeout
        filled, avg = 0, 0.0
        while _time.time() < deadline:
            _time.sleep(1.5)
            try:
                for e in self.tc.daily_executions():
                    if e["odno"] == odno:
                        filled, avg = e["filled_qty"], e["avg_price"]
                        if filled >= qty:
                            return filled, avg
                        break
            except KISError as e:
                log.debug("체결 조회 실패: %s", e)
        return filled, avg

    def _place(self, side: str, symbol: str, qty: int, price: float) -> OrderResult:
        dvsn, unpr = self._dvsn_price(side, price)
        try:
            r = self.tc.order(symbol, side, qty, unpr, dvsn)
        except KISError as e:
            return OrderResult(False, side, symbol, qty, message=str(e))

        odno, org = r["odno"], r["org_no"]
        filled, avg = self._wait_fill(odno, qty)

        if filled < qty and odno:
            try:
                self.tc.cancel(org, odno, qty - filled, all_qty=False)
                log.info("미체결 %d주 취소 (주문 %s)", qty - filled, odno)
            except KISError as e:
                log.debug("취소 실패(이미 체결/소멸 가능): %s", e)

        if filled <= 0:
            return OrderResult(False, side, symbol, qty, odno=odno, org_no=org,
                               message=f"미체결 (주문 {odno})")

        self.invalidate_account()
        px = avg or price
        amount = px * filled
        fee, tax = _costs(amount, side, self.cost)
        return OrderResult(True, side, symbol, qty, filled, px, fee, tax,
                           odno, org, r.get("message", ""))

    def buy(self, symbol: str, qty: int, price: float) -> OrderResult:
        return self._place("BUY", symbol, qty, price)

    def sell(self, symbol: str, qty: int, price: float) -> OrderResult:
        return self._place("SELL", symbol, qty, price)

    def ping(self) -> tuple[bool, str]:
        return self.tc.ping()
