"""백테스터.

게시글이 강조한 그대로 - 백테스트가 현실과 다르면 그 결과는 독이다. 그래서:
  * 수수료 + 매도세 + 슬리피지를 전부 뺀다
  * 같은 봉에서 손절과 익절이 동시에 걸리면 손절 우선 (최악 가정)
  * 갭으로 손절가를 뛰어넘으면 시가에 체결 (손절가 체결 아님)
  * 신호는 직전 봉까지의 데이터로만 만든다 (미래참조 차단)
  * 거래 횟수가 적으면 결과에 경고를 붙인다
  * 실거래와 똑같은 비용/유동성 관문을 진입에 적용한다
    (백테스트에서만 사는 거래가 있으면 그 수익률은 실현되지 않는다)
  * 같은 기간 같은 종목을 그냥 사서 들고 있었을 때와 비교한다

그리고 실거래와 같은 Strategy 객체, 같은 사이징 공식을 쓴다.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from .risk import avg_turnover, norm_market, round_trip_cost_pct
from .settings import CostConfig, RiskConfig
from .storage import Store
from .strategies import Position, Strategy, build

log = logging.getLogger(__name__)


@dataclass
class BTPosition:
    symbol: str
    qty: int
    entry_price: float
    entry_ts: str
    stop: float
    target: float
    peak: float
    bars_held: int = 0
    fee: float = 0.0


@dataclass
class BacktestResult:
    strategy: str = ""
    params: dict = field(default_factory=dict)
    symbols: list = field(default_factory=list)
    start: str = ""
    end: str = ""
    trades: list = field(default_factory=list)
    equity: list = field(default_factory=list)     # [(ts, value), ...]
    metrics: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    error: str = ""


class Backtester:
    def __init__(self, store: Store, cost: CostConfig, risk: RiskConfig):
        self.store = store
        self.cost = cost
        self.risk = risk

    # ------------------------------------------------------------------
    def run(self, strategy_name: str, params: dict, symbols: list[str],
            initial_cash: int = 10_000_000, start: str = "", end: str = "",
            log_fn=None) -> BacktestResult:
        strat: Strategy = build(strategy_name, params)
        res = BacktestResult(strategy=strategy_name, params=dict(strat.p),
                             symbols=list(symbols))

        data: dict[str, list[dict]] = {}
        for s in symbols:
            bars = self.store.get_candles(s, strat.timeframe, limit=200_000,
                                          start=start or None, end=end or None)
            if len(bars) > strat.warmup + 10:
                data[s] = bars
            else:
                res.warnings.append(f"{s}: 봉 {len(bars)}개 - 워밍업({strat.warmup})보다 적어 제외")

        if not data:
            res.error = (f"사용할 수 있는 {strat.timeframe} 데이터가 없습니다. "
                         f"[데이터] 탭에서 먼저 수집하세요.")
            return res

        index: dict[str, dict[str, int]] = {
            s: {b["ts"]: i for i, b in enumerate(bars)} for s, bars in data.items()
        }
        timeline = sorted({b["ts"] for bars in data.values() for b in bars})
        res.start, res.end = timeline[0], timeline[-1]

        cash = float(initial_cash)
        positions: dict[str, BTPosition] = {}
        trades: list[dict] = []
        costs = {"fee": 0.0, "tax": 0.0, "slip": 0.0}
        gated = {"cost": 0, "liquidity": 0}
        equity_curve: list[tuple[str, float]] = []
        day_mark = ""

        # 시장 구분(코스피/코스닥) - 호가단위 기반 비용 관문을 실거래와 같은
        # 기준으로 계산하기 위해. 종목 마스터가 없으면 KOSPI 로 본다.
        markets = {s: norm_market(self.store.stock_market(s)) for s in data}

        def settle_exit(sym: str, exit_px: float, ts_: str, reason: str) -> None:
            """포지션 청산 정산 - 비용 차감, 거래 기록, 포지션 제거."""
            nonlocal cash
            pos = positions[sym]
            fill = exit_px * (1 - self.cost.slippage_pct / 100)
            amount = fill * pos.qty
            fee = round(amount * self.cost.commission_pct / 100)
            tax = round(amount * self.cost.sell_tax_pct / 100)
            costs["fee"] += fee
            costs["tax"] += tax
            costs["slip"] += (exit_px - fill) * pos.qty
            cash += amount - fee - tax
            gross_in = pos.entry_price * pos.qty
            pnl = amount - gross_in - fee - tax - pos.fee
            trades.append({
                "symbol": sym, "entry_ts": pos.entry_ts, "exit_ts": ts_,
                "entry_price": pos.entry_price, "exit_price": fill,
                "qty": pos.qty, "pnl": pnl,
                "pnl_pct": (pnl / gross_in * 100) if gross_in else 0,
                "reason": reason, "bars": pos.bars_held,
            })
            del positions[sym]

        for ti, ts in enumerate(timeline):
            day = ts[:10]
            is_last_of_day = (ti == len(timeline) - 1) or (timeline[ti + 1][:10] != day)

            # ---------- 1) 청산 먼저 ----------
            for sym in list(positions.keys()):
                i = index[sym].get(ts)
                if i is None:
                    continue
                bar = data[sym][i]
                pos = positions[sym]
                pos.bars_held += 1
                o, h, l, c = (float(bar["open"]), float(bar["high"]),
                              float(bar["low"]), float(bar["close"]))

                exit_px, reason = None, ""
                if pos.stop > 0 and l <= pos.stop:
                    exit_px = min(pos.stop, o)      # 갭하락이면 시가 체결
                    reason = "손절"
                elif pos.target > 0 and h >= pos.target:
                    exit_px = max(pos.target, o)    # 갭상승이면 시가 체결
                    reason = "익절"
                else:
                    p = Position(sym, pos.entry_price, pos.qty, pos.stop,
                                 pos.target, pos.peak, pos.bars_held, strategy_name)
                    sig = strat.exit(data[sym][:i + 1], c, p, {"ts": ts})
                    if sig.side == "SELL":
                        exit_px, reason = c, sig.reason
                    elif strat.exit_on_close and is_last_of_day:
                        exit_px, reason = c, "당일 종가 청산"

                if exit_px is None:
                    # 트레일링 스톱 갱신
                    from . import indicators as ind
                    a = ind.last(ind.atr(data[sym][:i + 1], 14))
                    p = Position(sym, pos.entry_price, pos.qty, pos.stop,
                                 pos.target, pos.peak, pos.bars_held, strategy_name)
                    new_stop = strat.update_stop(p, c, a)
                    pos.stop, pos.peak = new_stop, max(pos.peak, c)
                    continue

                settle_exit(sym, exit_px, ts, reason)

            # ---------- 2) 진입 ----------
            equity = cash + sum(
                p.qty * float(data[s][index[s][ts]]["close"])
                for s, p in positions.items() if ts in index[s]
            )
            if len(positions) < self.risk.max_positions:
                for sym, bars in data.items():
                    if sym in positions or len(positions) >= self.risk.max_positions:
                        continue
                    i = index[sym].get(ts)
                    if i is None or i < strat.warmup:
                        continue
                    bar = bars[i]
                    hist = bars[:i]                      # 오늘 봉 제외 = 미래참조 차단
                    o, h = float(bar["open"]), float(bar["high"])

                    entry_px, sig = None, None
                    if strat.intrabar:
                        trig = strat.trigger_price(hist, o)
                        if trig is not None and h >= trig:
                            entry_px = max(trig, o)
                            sig = strat.entry(hist, entry_px, {"today_open": o, "ts": ts})
                            if sig.side != "BUY":
                                entry_px = None
                    else:
                        s0 = strat.entry(hist, float(hist[-1]["close"]),
                                         {"today_open": o, "ts": ts,
                                          "now_hhmm": ts[11:16] if len(ts) > 10 else ""})
                        if s0.side == "BUY":
                            entry_px, sig = o, s0

                    if entry_px is None or sig is None:
                        continue

                    fill = entry_px * (1 + self.cost.slippage_pct / 100)
                    stop = sig.stop if sig.stop > 0 else fill * 0.97
                    target = sig.target if sig.target > 0 else 0

                    # 실거래와 같은 관문. 여기서 거른 거래는 실계좌에서도 안 산다.
                    gate = self._gate(fill, stop, target, hist,
                                      markets.get(sym, "KOSPI"))
                    if gate:
                        gated[gate] += 1
                        continue

                    qty = self._size(fill, stop, equity, cash, sig.strength)
                    if qty <= 0:
                        continue
                    amount = fill * qty
                    fee = round(amount * self.cost.commission_pct / 100)
                    if cash < amount + fee:
                        continue
                    costs["fee"] += fee
                    costs["slip"] += (fill - entry_px) * qty
                    cash -= amount + fee
                    positions[sym] = BTPosition(sym, qty, fill, ts, stop, target,
                                                fill, 0, float(fee))

                    # ---------- 진입 당일(같은 봉) 청산 ----------
                    # 청산 루프는 다음 봉부터 이 포지션을 보므로, 여기서 안 보면
                    # "당일 종가 청산" 전략(일봉)이 실제로는 다음날 종가에 나가
                    # 오버나이트가 하루 더 붙는다. 실거래 엔진은 당일 안에
                    # 손절/익절/강제청산을 전부 처리하므로 백테스트도 진입 봉에서
                    # 손절 우선(최악 가정) -> 익절 -> 종가청산 순으로 본다.
                    npos = positions[sym]
                    lo_, cl_ = float(bar["low"]), float(bar["close"])
                    if npos.stop > 0 and lo_ <= npos.stop:
                        settle_exit(sym, npos.stop, ts, "손절")
                    elif npos.target > 0 and h >= npos.target:
                        settle_exit(sym, npos.target, ts, "익절")
                    elif strat.exit_on_close and is_last_of_day:
                        settle_exit(sym, cl_, ts, "당일 종가 청산")

            # ---------- 3) 자산 기록 ----------
            if is_last_of_day and day != day_mark:
                day_mark = day
                eq = cash + sum(
                    p.qty * float(data[s][index[s][ts]]["close"])
                    for s, p in positions.items() if ts in index[s]
                )
                equity_curve.append((day, eq))

        # 미청산 포지션은 마지막 종가로 평가만 (거래로는 안 셈)
        res.trades = trades
        res.equity = equity_curve
        res.metrics = self._metrics(trades, equity_curve, initial_cash, costs)
        res.metrics.update(self._buy_hold(data, timeline, index, initial_cash))
        res.metrics["gated_cost"] = gated["cost"]
        res.metrics["gated_liquidity"] = gated["liquidity"]
        res.metrics["alpha_pct"] = round(
            res.metrics.get("total_return_pct", 0)
            - res.metrics.get("bh_return_pct", 0), 2)
        res.warnings += self._warn(res.metrics, trades)
        if gated["cost"] or gated["liquidity"]:
            res.warnings.append(
                f"관문에서 진입 취소 {gated['cost'] + gated['liquidity']}건 "
                f"(비용 {gated['cost']} / 유동성 {gated['liquidity']}) - "
                f"관문을 끄면 거래는 늘지만 실계좌에서 재현되지 않는 거래가 섞인다.")
        if log_fn:
            log_fn(f"백테스트 완료: {len(trades)}거래, "
                   f"수익률 {res.metrics.get('total_return_pct', 0):.2f}%")
        return res

    # ------------------------------------------------------------------
    def _gate(self, price: float, stop: float, target: float,
              hist: list[dict], market: str = "KOSPI") -> str:
        """진입을 취소해야 하면 사유 키를, 통과면 빈 문자열을 돌려준다.

        실거래 엔진(risk.py)이 쓰는 것과 같은 식이다. 백테스트에서만 통과하는
        거래가 있으면 그 수익률은 실계좌에서 나오지 않는다.
        """
        c = self.risk
        need = float(getattr(c, "min_edge_cost_ratio", 0) or 0)
        if need > 0 and price > 0:
            edge = (((target - price) / price * 100) if target > 0
                    else ((price - stop) / price * 100))
            cpct = round_trip_cost_pct(self.cost, price, market)
            if edge > 0 and cpct > 0 and edge / cpct < need:
                return "cost"

        turn_need = float(getattr(c, "min_turnover_amount", 0) or 0)
        if price > 0 and price > c.max_order_amount:
            return "liquidity"
        if turn_need > 0:
            t = avg_turnover(hist, int(getattr(c, "turnover_lookback", 20) or 20))
            if 0 < t < turn_need:
                return "liquidity"
        return ""

    # ------------------------------------------------------------------
    def _buy_hold(self, data: dict, timeline: list[str], index: dict,
                  initial: float) -> dict:
        """같은 기간 같은 종목을 동일가중으로 사서 끝까지 들고 있었다면.

        비교 대상 없이는 수익률 숫자가 아무 뜻이 없다. 전략이 +12%라도
        그냥 들고 있는 게 +30%였다면, 그 전략은 돈을 벌어준 게 아니라
        수수료와 시간을 써서 수익을 깎은 것이다.
        매수 수수료와 마지막 매도 수수료/세금까지 똑같이 뺀다.
        """
        if not data or not timeline:
            return {"bh_return_pct": 0.0, "bh_mdd_pct": 0.0, "bh_final": 0}

        per = initial / len(data)
        holds: dict[str, int] = {}
        cash = float(initial)
        for sym, bars in data.items():
            px = float(bars[0]["close"] or 0)
            if px <= 0:
                continue
            qty = int(per // px)
            if qty <= 0:
                continue
            amount = qty * px
            cash -= amount + round(amount * self.cost.commission_pct / 100)
            holds[sym] = qty
        if not holds:
            return {"bh_return_pct": 0.0, "bh_mdd_pct": 0.0, "bh_final": 0}

        last_px = {s: float(data[s][0]["close"] or 0) for s in holds}
        curve: list[float] = []
        day_mark = ""
        for ti, ts in enumerate(timeline):
            day = ts[:10]
            for s in holds:
                i = index[s].get(ts)
                if i is not None:
                    last_px[s] = float(data[s][i]["close"] or last_px[s])
            is_last = (ti == len(timeline) - 1) or (timeline[ti + 1][:10] != day)
            if is_last and day != day_mark:
                day_mark = day
                curve.append(cash + sum(q * last_px[s] for s, q in holds.items()))

        gross = sum(q * last_px[s] for s, q in holds.items())
        exit_cost = round(gross * (self.cost.commission_pct
                                   + self.cost.sell_tax_pct) / 100)
        final = cash + gross - exit_cost
        if curve:
            curve[-1] = final

        peak, mdd = (curve[0] if curve else initial), 0.0
        for v in curve:
            peak = max(peak, v)
            if peak > 0:
                mdd = max(mdd, (peak - v) / peak * 100)

        return {
            "bh_return_pct": round((final - initial) / initial * 100, 2),
            "bh_mdd_pct": round(mdd, 2),
            "bh_final": round(final),
            "bh_symbols": len(holds),
        }

    # ------------------------------------------------------------------
    def _size(self, price: float, stop: float, equity: float,
              cash: float, strength: float) -> int:
        """실거래 사이징(risk.RiskManager.position_size)과 같은 규칙.

        여기가 실거래와 다르면 백테스트 수익률은 실계좌에서 재현되지 않는다.
        """
        c = self.risk
        stop_dist = price - stop if stop > 0 else price * 0.03
        if stop_dist <= 0:
            stop_dist = price * 0.03
        risk_amt = equity * c.max_loss_per_trade_pct / 100 * max(min(strength, 1.0), 0.1)
        qty_risk = risk_amt / stop_dist
        qty_weight = (equity * c.max_position_weight_pct / 100) / price
        qty_cash = max(cash - equity * c.min_cash_reserve_pct / 100, 0) / price
        qty_cap = c.max_order_amount / price

        q = int(math.floor(min(qty_risk, qty_weight, qty_cash, qty_cap)))
        if q <= 0:
            return 0
        if q * price < c.min_order_amount:
            # 실거래와 동일: 손실한도는 넘겨도 비중/현금/1회상한 안에서만
            # 최소주문금액을 맞춘다. 그 밖이면 사지 않는다.
            need = math.ceil(c.min_order_amount / price)
            q = need if need <= min(qty_weight, qty_cash, qty_cap) else 0
        return max(q, 0)

    # ------------------------------------------------------------------
    @staticmethod
    def _metrics(trades: list[dict], equity: list[tuple[str, float]],
                 initial: float, costs: dict | None = None) -> dict:
        if not equity:
            return {"total_return_pct": 0, "trades": 0}
        final = equity[-1][1]
        total_ret = (final - initial) / initial * 100

        peak, mdd = equity[0][1], 0.0
        for _d, v in equity:
            peak = max(peak, v)
            if peak > 0:
                mdd = max(mdd, (peak - v) / peak * 100)

        rets = []
        for i in range(1, len(equity)):
            prev = equity[i - 1][1]
            if prev > 0:
                rets.append((equity[i][1] - prev) / prev)
        sharpe = 0.0
        if len(rets) > 5:
            m = sum(rets) / len(rets)
            sd = (sum((r - m) ** 2 for r in rets) / len(rets)) ** 0.5
            if sd > 0:
                sharpe = m / sd * math.sqrt(252)

        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        gross_win = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in losses))
        avg_win = gross_win / len(wins) if wins else 0
        avg_loss = gross_loss / len(losses) if losses else 0
        win_rate = len(wins) / len(trades) * 100 if trades else 0

        streak = worst = 0
        for t in trades:
            if t["pnl"] <= 0:
                streak += 1
                worst = max(worst, streak)
            else:
                streak = 0

        days = len(equity)
        years = days / 252 if days else 0
        cagr = ((final / initial) ** (1 / years) - 1) * 100 if years > 0.2 and final > 0 else 0

        costs = costs or {}
        total_cost = costs.get("fee", 0) + costs.get("tax", 0) + costs.get("slip", 0)
        cost_drag = total_cost / initial * 100 if initial else 0

        return {
            "initial": initial, "final": round(final),
            "total_return_pct": round(total_ret, 2),
            # 거래비용을 빼기 전 수익률. 이 둘의 차이가 곧 "매매를 많이 해서 잃은 돈"이다.
            "gross_return_pct": round(total_ret + cost_drag, 2),
            "total_cost": round(total_cost),
            "cost_drag_pct": round(cost_drag, 2),
            "fee_total": round(costs.get("fee", 0)),
            "tax_total": round(costs.get("tax", 0)),
            "slip_total": round(costs.get("slip", 0)),
            "cagr_pct": round(cagr, 2),
            "mdd_pct": round(mdd, 2),
            "sharpe": round(sharpe, 2),
            "trades": len(trades),
            "win_rate": round(win_rate, 1),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else 0,
            "avg_win": round(avg_win),
            "avg_loss": round(avg_loss),
            "payoff": round(avg_win / avg_loss, 2) if avg_loss else 0,
            "expectancy": round((gross_win - gross_loss) / len(trades)) if trades else 0,
            "max_consecutive_losses": worst,
            "avg_bars_held": round(sum(t["bars"] for t in trades) / len(trades), 1) if trades else 0,
            "bars": days,
            "return_over_mdd": round(total_ret / mdd, 2) if mdd > 0.01 else 0,
        }

    @staticmethod
    def _warn(m: dict, trades: list[dict]) -> list[str]:
        w = []
        n = m.get("trades", 0)
        if n < 30:
            w.append(f"거래 {n}건 - 표본이 너무 적다. 30건 미만은 우연일 확률이 높음.")
        if m.get("mdd_pct", 0) > 25:
            w.append(f"최대낙폭 {m['mdd_pct']}% - 실계좌에서 버티기 어려운 수준.")
        if n and m.get("win_rate", 0) > 75 and m.get("payoff", 0) < 0.6:
            w.append("승률은 높지만 손익비가 낮음 - 한 번 크게 물리는 유형.")
        if m.get("profit_factor", 0) and m["profit_factor"] > 3 and n < 100:
            w.append("Profit Factor가 비정상적으로 높음 - 과최적화 의심.")
        bh = m.get("bh_return_pct")
        if bh is not None and m.get("bh_symbols"):
            net_r = m.get("total_return_pct", 0)
            if net_r <= bh:
                w.append(f"같은 기간 그냥 사서 들고 있었으면 {bh:+.1f}%인데 "
                         f"전략은 {net_r:+.1f}% - 매매를 해서 오히려 깎였다. "
                         f"MDD가 더 낮은 것도 아니라면 이 전략을 쓸 이유가 없다.")
            elif m.get("mdd_pct", 0) > m.get("bh_mdd_pct", 0) and net_r - bh < 5:
                w.append(f"바이앤홀드보다 {net_r - bh:+.1f}%p 앞서지만 "
                         f"낙폭은 더 크다 ({m.get('mdd_pct', 0):.1f}% vs "
                         f"{m.get('bh_mdd_pct', 0):.1f}%) - 위험 대비 이득이 없다.")

        gross, net = m.get("gross_return_pct", 0), m.get("total_return_pct", 0)
        if gross > 0 >= net:
            w.append(f"비용 전에는 +{gross:.1f}%인데 비용 반영 후 {net:.1f}% - "
                     f"전략이 아니라 거래비용({m.get('cost_drag_pct', 0):.1f}%p)에 지고 있음. "
                     f"거래 횟수를 줄이거나 보유기간을 늘려야 함.")
        if trades:
            syms = {t["symbol"] for t in trades}
            top = max(syms, key=lambda s: sum(t["pnl"] for t in trades if t["symbol"] == s))
            top_pnl = sum(t["pnl"] for t in trades if t["symbol"] == top)
            total = sum(t["pnl"] for t in trades)
            if total > 0 and top_pnl / total > 0.7 and len(syms) > 1:
                w.append(f"수익의 {top_pnl / total * 100:.0f}%가 {top} 한 종목에서 나옴 - 일반성 부족.")
        return w

    # ------------------------------------------------------------------
    def sweep(self, strategy_name: str, base_params: dict, grid: dict,
              symbols: list[str], initial_cash: int = 10_000_000,
              start: str = "", end: str = "", log_fn=None) -> list[dict]:
        """파라미터 그리드 탐색. 결과는 return_over_mdd 기준 정렬."""
        keys = list(grid.keys())
        combos: list[dict] = [{}]
        for k in keys:
            combos = [{**c, k: v} for c in combos for v in grid[k]]

        out = []
        for i, combo in enumerate(combos, 1):
            p = {**base_params, **combo}
            r = self.run(strategy_name, p, symbols, initial_cash, start, end)
            if r.error:
                continue
            out.append({"params": combo, "metrics": r.metrics,
                        "warnings": r.warnings})
            if log_fn:
                log_fn(f"[{i}/{len(combos)}] {combo} -> "
                       f"수익 {r.metrics.get('total_return_pct', 0):.1f}% / "
                       f"MDD {r.metrics.get('mdd_pct', 0):.1f}% / "
                       f"{r.metrics.get('trades', 0)}건")
        out.sort(key=lambda x: (x["metrics"].get("return_over_mdd", 0),
                                x["metrics"].get("total_return_pct", 0)), reverse=True)
        return out
