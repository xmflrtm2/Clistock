"""전략 견고성 분석.

백테스트 수익률 한 숫자는 거의 아무것도 말해주지 않는다.
"이 설정이 진짜인가, 우연인가"를 가르는 건 세 가지다.

  1. 민감도  - 파라미터를 조금 바꿨을 때 결과가 얼마나 흔들리나?
               최고점만 뾰족하게 솟아 있으면 그건 과최적화다.
               주변값도 같이 좋아야 실전에서 재현된다.

  2. 기간분할 - 특정 구간에서만 벌었나, 여러 해에 걸쳐 꾸준한가?
               한 해가 전체 수익을 다 만들었다면 그 해가 특이했던 것뿐이다.

  3. 몬테카를로 - 같은 거래들이 다른 순서로 일어났다면 결과가 어땠을까?
               운 좋은 순서 하나를 보고 판단하지 않기 위해서다.
               최악 구간(MDD)의 분포가 실제로 버틸 수 있는 수준인지 본다.

  4. 워크포워드 - 위 셋을 다 통과해도 남는 함정이 하나 있다.
               "전 구간을 보고 고른 파라미터"는 그 구간을 이미 알고 있다.
               실전에서는 미래를 모르는 채로 골라야 한다.
               그래서 앞 구간에서만 고르고, 뒤 구간 성적만 채점한다.
               이 성적이 전 구간 최적화 성적보다 훨씬 나쁘면,
               앞에서 본 수익률은 재현되지 않는 숫자였다는 뜻이다.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field

from .backtest import Backtester
from .settings import CostConfig, RiskConfig
from .storage import Store

log = logging.getLogger(__name__)


@dataclass
class SweepPoint:
    value: object
    return_pct: float = 0.0
    gross_pct: float = 0.0
    mdd_pct: float = 0.0
    trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    ret_over_mdd: float = 0.0


@dataclass
class Sensitivity:
    param: str
    points: list = field(default_factory=list)
    best_value: object = None
    best_return: float = 0.0
    spread: float = 0.0            # 최고 - 최저 수익률 (%p)
    neighbor_gap: float = 0.0      # 최고점과 이웃값의 차이 (%p)
    flatness: float = 0.0          # 0~1, 1에 가까울수록 평탄(견고)
    verdict: str = ""
    error: str = ""


@dataclass
class WFFold:
    idx: int = 0
    train_start: str = ""
    train_end: str = ""
    test_start: str = ""
    test_end: str = ""
    value: object = None          # 학습구간에서 고른 파라미터 값
    is_return: float = 0.0        # 학습구간 성적 (고른 값 기준)
    oos_return: float = 0.0       # 검증구간 성적 - 이게 진짜 점수다
    oos_mdd: float = 0.0
    oos_trades: int = 0
    bh_return: float = 0.0        # 같은 검증구간 바이앤홀드
    note: str = ""


@dataclass
class WalkForward:
    param: str = ""
    folds: list = field(default_factory=list)
    is_return_pct: float = 0.0     # 학습구간 수익률을 복리로 이은 값
    oos_return_pct: float = 0.0    # 검증구간 수익률을 복리로 이은 값
    bh_return_pct: float = 0.0
    efficiency: float = 0.0        # OOS / IS. 1에 가까울수록 재현성 높음
    positive_folds: int = 0
    param_stability: float = 0.0   # 폴드마다 같은 값이 뽑혔나 (0~1)
    best_value: object = None
    verdict: str = ""
    error: str = ""


class Analyzer:
    def __init__(self, store: Store, cost: CostConfig):
        self.store = store
        self.cost = cost

    def _bt(self, risk: RiskConfig) -> Backtester:
        return Backtester(self.store, self.cost, risk)

    # ------------------------------------------------------------------
    def sensitivity(self, strategy: str, base_params: dict, risk: RiskConfig,
                    symbols: list[str], param: str, values: list,
                    cash: int = 10_000_000, log_fn=None) -> Sensitivity:
        """파라미터 하나를 훑으며 결과가 얼마나 흔들리는지 본다."""
        out = Sensitivity(param=param)
        bt = self._bt(risk)
        for v in values:
            p = {**base_params, param: v}
            r = bt.run(strategy, p, symbols, cash)
            if r.error:
                out.error = r.error
                return out
            m = r.metrics
            out.points.append(SweepPoint(
                value=v,
                return_pct=m.get("total_return_pct", 0),
                gross_pct=m.get("gross_return_pct", 0),
                mdd_pct=m.get("mdd_pct", 0),
                trades=m.get("trades", 0),
                win_rate=m.get("win_rate", 0),
                profit_factor=m.get("profit_factor", 0),
                ret_over_mdd=m.get("return_over_mdd", 0)))
            if log_fn:
                log_fn(f"  {param}={v} -> 수익 {m.get('total_return_pct', 0):+.2f}% / "
                       f"MDD {m.get('mdd_pct', 0):.2f}% / {m.get('trades', 0)}건")

        if not out.points:
            out.error = "결과 없음"
            return out

        rets = [p.return_pct for p in out.points]
        out.spread = max(rets) - min(rets)
        bi = rets.index(max(rets))
        out.best_value = out.points[bi].value
        out.best_return = rets[bi]

        neigh = [rets[i] for i in (bi - 1, bi + 1) if 0 <= i < len(rets)]
        out.neighbor_gap = (out.best_return - sum(neigh) / len(neigh)) if neigh else 0.0

        # 평탄도: 이웃과의 차이가 전체 폭에 비해 작을수록 1에 가깝다
        if out.spread > 1e-9:
            out.flatness = max(0.0, 1.0 - abs(out.neighbor_gap) / out.spread)
        else:
            out.flatness = 1.0

        pos = sum(1 for r in rets if r > 0)
        # 이웃과의 차이만 보면 안 된다. 전체 폭 자체가 크면 그 파라미터에
        # 결과가 통째로 휘둘린다는 뜻이라 견고하다고 할 수 없다.
        wide = out.spread >= 15.0
        if pos <= len(rets) * 0.3:
            out.verdict = (f"{len(rets)}개 값 중 {pos}개만 수익입니다. "
                           f"최고값 하나만 보고 고르면 거의 확실히 과최적화입니다.")
        elif wide:
            out.verdict = (f"값에 따라 수익률이 {out.spread:.1f}%p나 벌어집니다 "
                           f"({min(rets):+.1f}% ~ {max(rets):+.1f}%). "
                           f"이 파라미터에 결과가 크게 휘둘리므로 실전에서 재현되기 어렵습니다.")
        elif out.flatness >= 0.7 and pos >= len(rets) * 0.7:
            out.verdict = (f"견고함 - 폭이 {out.spread:.1f}%p로 좁고 {len(rets)}개 중 "
                           f"{pos}개 구간에서 수익입니다.")
        elif out.flatness >= 0.7:
            out.verdict = ("표면은 평탄하지만 수익 구간이 좁습니다. "
                           "이 파라미터로는 개선이 어렵습니다.")
        else:
            out.verdict = (f"최고점이 뾰족합니다 (이웃과 {out.neighbor_gap:+.1f}%p 차이). "
                           f"우연일 가능성이 높으니 최고값을 그대로 쓰지 마세요.")
        return out

    # ------------------------------------------------------------------
    def period_split(self, strategy: str, params: dict, risk: RiskConfig,
                     symbols: list[str], cash: int = 10_000_000,
                     log_fn=None) -> list[dict]:
        """연도별로 잘라서 성과를 본다. 꾸준한지 한 해에 몰렸는지 확인용.

        주의 - 연초부터 데이터를 넣으면 워밍업(200일선이면 220봉)이 그 해를
        거의 다 먹어버려서 30봉만 평가하게 된다. 그래서 워밍업 몫만큼
        앞에서 더 읽어 들이고, 자산곡선에서 해당 연도 구간만 잘라 계산한다.
        """
        from datetime import date, timedelta
        from .strategies import build

        strat = build(strategy, params)
        warm_days = int(strat.warmup * 1.6) + 20      # 거래일 -> 달력일

        years = set()
        for s in symbols:
            a, b = self.store.candle_range(s, "D")
            if a and b:
                years.update(range(int(a[:4]), int(b[:4]) + 1))

        bt = self._bt(risk)
        out = []
        for y in sorted(years):
            y_start, y_end = f"{y}-01-01", f"{y}-12-31"
            warm_start = (date(y, 1, 1) - timedelta(days=warm_days)).isoformat()
            r = bt.run(strategy, params, symbols, cash, start=warm_start, end=y_end)
            if r.error:
                continue
            seg = [(d, v) for d, v in r.equity if d >= y_start]
            if len(seg) < 30:                 # 그 해 데이터가 사실상 없음
                continue
            base = seg[0][1] or cash
            ret = (seg[-1][1] / base - 1) * 100
            peak, mdd = base, 0.0
            for _d, v in seg:
                peak = max(peak, v)
                if peak > 0:
                    mdd = max(mdd, (peak - v) / peak * 100)
            tr = [t for t in r.trades if (t.get("exit_ts") or "") >= y_start]
            wins = sum(1 for t in tr if t["pnl"] > 0)
            out.append({
                "period": str(y),
                "return_pct": ret,
                "mdd_pct": mdd,
                "trades": len(tr),
                "win_rate": (wins / len(tr) * 100) if tr else 0.0,
                "bars": len(seg),
            })
            if log_fn:
                log_fn(f"  {y}년 -> {ret:+.2f}% (MDD {mdd:.1f}%, {len(tr)}건, {len(seg)}일)")
        return out

    @staticmethod
    def period_summary(rows: list[dict]) -> dict:
        if not rows:
            return {}
        rets = [r["return_pct"] for r in rows]
        n = len(rets)
        mean = sum(rets) / n
        sd = (sum((x - mean) ** 2 for x in rets) / n) ** 0.5
        pos = sum(1 for r in rets if r > 0)
        return {
            "years": n,
            "mean": mean,
            "stdev": sd,
            "best": max(rets),
            "worst": min(rets),
            "positive_years": pos,
            "consistency": pos / n * 100,
            "verdict": (
                f"{n}년 중 {pos}년 수익. 연평균 {mean:+.1f}% (표준편차 {sd:.1f}%p). "
                + ("해마다 결과가 크게 달라 한 해 성적으로 판단하면 안 됩니다."
                   if sd > abs(mean) * 1.5 or sd > 15
                   else "연도별 편차가 비교적 작습니다.")
            ),
        }

    # ------------------------------------------------------------------
    def walk_forward(self, strategy: str, base_params: dict, risk: RiskConfig,
                     symbols: list[str], param: str, values: list,
                     folds: int = 4, cash: int = 10_000_000,
                     log_fn=None) -> WalkForward:
        """앞 구간에서만 파라미터를 고르고, 뒤 구간 성적으로만 채점한다.

        전체 구간을 (folds+1) 등분해서 확장형(anchored)으로 민다.

            [--- 학습 ---][검증]
            [------ 학습 ------][검증]
            [-------- 학습 --------][검증]

        검증구간은 고를 때 한 번도 보지 않은 데이터다. 여기 성적만 이어붙인
        것이 "실전에서 이 방식대로 운용했다면"에 가장 가까운 숫자다.
        """
        from datetime import date, timedelta
        from .strategies import build

        out = WalkForward(param=param)
        vals = [v for v in (values or []) if v is not None]
        if not vals:
            out.error = "탐색할 파라미터 값이 없습니다."
            return out
        if folds < 2:
            folds = 2

        strat = build(strategy, base_params)
        warm_days = int(strat.warmup * 1.6) + 20

        lo = hi = None
        for sym in symbols:
            a, b = self.store.candle_range(sym, strat.timeframe)
            if not a or not b:
                continue
            a, b = a[:10], b[:10]
            lo = a if lo is None or a < lo else lo
            hi = b if hi is None or b > hi else hi
        if not lo or not hi:
            out.error = (f"{strat.timeframe} 데이터가 없습니다. "
                         f"[데이터] 탭에서 먼저 수집하세요.")
            return out

        d0, d1 = date.fromisoformat(lo), date.fromisoformat(hi)
        span = (d1 - d0).days
        seg = span // (folds + 1)
        if seg < 60:
            out.error = (f"기간이 {span}일뿐입니다. {folds}폴드로 나누면 구간당 "
                         f"{seg}일이라 검증이 무의미합니다 (구간당 60일 이상 필요).")
            return out

        bt = self._bt(risk)
        is_c = oos_c = bh_c = 1.0
        picks: list = []

        for i in range(folds):
            tr_end = d0 + timedelta(days=seg * (i + 1))
            te_end = d1 if i == folds - 1 else d0 + timedelta(days=seg * (i + 2))
            f = WFFold(idx=i + 1, train_start=d0.isoformat(),
                       train_end=tr_end.isoformat(),
                       test_start=tr_end.isoformat(), test_end=te_end.isoformat())

            # --- 학습구간: 여기서만 고른다 ---
            best = None
            for v in vals:
                r = bt.run(strategy, {**base_params, param: v}, symbols, cash,
                           start=f.train_start, end=f.train_end)
                if r.error or r.metrics.get("trades", 0) < 10:
                    continue
                key = (r.metrics.get("return_over_mdd", 0),
                       r.metrics.get("total_return_pct", 0))
                if best is None or key > best[0]:
                    best = (key, v, r.metrics.get("total_return_pct", 0))
            if best is None:
                f.note = "학습구간에 쓸 만한 표본이 없음 (거래 10건 미만)"
                out.folds.append(f)
                if log_fn:
                    log_fn(f"  폴드 {f.idx}: {f.note}")
                continue

            f.value, f.is_return = best[1], best[2]
            picks.append(best[1])

            # --- 검증구간: 고를 때 보지 않은 데이터 ---
            warm_start = (tr_end - timedelta(days=warm_days)).isoformat()
            r = bt.run(strategy, {**base_params, param: f.value}, symbols, cash,
                       start=warm_start, end=f.test_end)
            if r.error:
                f.note = r.error[:80]
                out.folds.append(f)
                continue
            seg_eq = [(d, v) for d, v in r.equity if d >= f.test_start]
            if len(seg_eq) < 10:
                f.note = "검증구간 데이터 부족"
                out.folds.append(f)
                continue

            base = seg_eq[0][1] or cash
            f.oos_return = (seg_eq[-1][1] / base - 1) * 100
            peak, mdd = base, 0.0
            for _d, v in seg_eq:
                peak = max(peak, v)
                if peak > 0:
                    mdd = max(mdd, (peak - v) / peak * 100)
            f.oos_mdd = mdd
            f.oos_trades = sum(1 for t in r.trades
                               if (t.get("exit_ts") or "") >= f.test_start)
            f.bh_return = self._bh_window(symbols, strat.timeframe,
                                          f.test_start, f.test_end)

            is_c *= (1 + f.is_return / 100)
            oos_c *= (1 + f.oos_return / 100)
            bh_c *= (1 + f.bh_return / 100)
            out.folds.append(f)
            if log_fn:
                log_fn(f"  폴드 {f.idx} [{f.test_start}~{f.test_end}] "
                       f"{param}={f.value} -> 학습 {f.is_return:+.1f}% / "
                       f"검증 {f.oos_return:+.1f}% (보유 {f.bh_return:+.1f}%, "
                       f"{f.oos_trades}건)")

        scored = [f for f in out.folds if f.value is not None and not f.note]
        if not scored:
            out.error = ("채점할 수 있는 폴드가 없습니다. "
                         "데이터 기간을 늘리거나 폴드 수를 줄이세요.")
            return out

        out.is_return_pct = (is_c - 1) * 100
        out.oos_return_pct = (oos_c - 1) * 100
        out.bh_return_pct = (bh_c - 1) * 100
        out.positive_folds = sum(1 for f in scored if f.oos_return > 0)
        out.efficiency = (out.oos_return_pct / out.is_return_pct
                          if out.is_return_pct > 0 else 0.0)
        if picks:
            keys = [str(v) for v in picks]
            top = max(set(keys), key=keys.count)
            out.param_stability = keys.count(top) / len(keys)
            out.best_value = picks[keys.index(top)]

        out.verdict = self._wf_verdict(out, len(scored))
        return out

    # ------------------------------------------------------------------
    def _bh_window(self, symbols: list[str], tf: str, start: str,
                   end: str) -> float:
        """그 구간을 그냥 사서 들고 있었을 때의 수익률(%). 동일가중."""
        rets = []
        for sym in symbols:
            bars = self.store.get_candles(sym, tf, limit=200_000,
                                          start=start, end=end)
            if len(bars) < 2:
                continue
            a, b = float(bars[0]["close"] or 0), float(bars[-1]["close"] or 0)
            if a > 0 and b > 0:
                rets.append((b / a - 1) * 100)
        return sum(rets) / len(rets) if rets else 0.0

    @staticmethod
    def _wf_verdict(w: "WalkForward", n: int) -> str:
        eff, oos = w.efficiency, w.oos_return_pct
        head = (f"{n}개 검증구간 중 {w.positive_folds}개에서 수익. "
                f"학습 {w.is_return_pct:+.1f}% -> 검증 {oos:+.1f}% "
                f"(같은 기간 그냥 보유 {w.bh_return_pct:+.1f}%). ")
        if oos <= 0:
            body = ("검증구간 합계가 손실입니다. 학습구간 성적이 아무리 좋아도 "
                    "그건 답을 보고 맞춘 것이라 실전에 넣을 근거가 못 됩니다.")
        elif oos <= w.bh_return_pct:
            body = ("검증구간에서 벌긴 했지만 그냥 사서 들고 있는 것보다 못합니다. "
                    "매매 자체가 가치를 만들지 못하고 있습니다.")
        elif eff < 0.3:
            body = (f"검증 성적이 학습 성적의 {eff * 100:.0f}%밖에 안 됩니다. "
                    f"파라미터가 과거에 맞춰져 있다는 신호입니다.")
        elif w.param_stability < 0.5:
            body = (f"폴드마다 최적값이 달라집니다 "
                    f"(일치율 {w.param_stability * 100:.0f}%). "
                    f"어떤 값을 써야 할지 데이터가 말해주지 못하는 상태입니다.")
        else:
            body = (f"검증구간에서도 재현됐고 (효율 {eff * 100:.0f}%), "
                    f"최적값도 {w.param_stability * 100:.0f}% 일치합니다. "
                    f"실전 후보로 볼 만합니다.")
        return head + body

    # ------------------------------------------------------------------
    def monte_carlo(self, trades: list[dict], initial: float,
                    runs: int = 1000, seed: int = 7) -> dict:
        """거래를 복원추출로 다시 뽑아 결과 분포를 본다 (부트스트랩).

        순서만 섞으면 안 된다 - 손익을 절대금액으로 더하면 순서를 바꿔도
        합계가 같아서 분포가 생기지 않는다. 그래서:
          1. 손익을 자산 대비 수익률로 환산하고 복리로 굴린다 (순서가 의미를 갖는다)
          2. 거래를 복원추출로 다시 뽑는다 (표본 자체의 불확실성을 반영)

        이렇게 해야 "운이 나빴다면 어디까지 갔을까"에 답할 수 있다.
        """
        rets = [float(t.get("pnl") or 0) / initial for t in trades]
        if len(rets) < 5:
            return {"error": f"거래 {len(rets)}건 - 분포를 볼 만큼이 안 됩니다 (최소 5건)."}

        rnd = random.Random(seed)
        n = len(rets)
        finals, mdds, ruins = [], [], 0
        for _ in range(runs):
            eq = 1.0
            peak = 1.0
            mdd = 0.0
            busted = False
            for _ in range(n):
                eq *= (1 + rets[rnd.randrange(n)])
                if eq <= 0.5:
                    busted = True
                if eq <= 0:
                    eq = 1e-9
                    break
                peak = max(peak, eq)
                mdd = max(mdd, (peak - eq) / peak * 100)
            finals.append((eq - 1) * 100)
            mdds.append(mdd)
            ruins += busted

        finals.sort()
        mdds.sort()

        def pct(arr, q):
            return arr[min(int(len(arr) * q), len(arr) - 1)]

        median_ret = pct(finals, 0.5)
        return {
            "runs": runs,
            "trades": n,
            "return_median_raw": median_ret,
            "return_p05": pct(finals, 0.05),
            "return_p25": pct(finals, 0.25),
            "return_median": median_ret,
            "return_p75": pct(finals, 0.75),
            "return_p95": pct(finals, 0.95),
            "mdd_median": pct(mdds, 0.5),
            "mdd_p95": pct(mdds, 0.95),
            "mdd_worst": mdds[-1],
            "loss_prob": sum(1 for f in finals if f < 0) / len(finals) * 100,
            "ruin_prob": ruins / runs * 100,
            "verdict": (
                f"같은 거래를 복원추출로 다시 뽑으면 수익률이 {pct(finals, 0.05):+.1f}% ~ "
                f"{pct(finals, 0.95):+.1f}% 사이에서 움직입니다. "
                f"손실로 끝날 확률 {sum(1 for f in finals if f < 0) / len(finals) * 100:.0f}%, "
                f"최대낙폭은 나쁜 경우 {pct(mdds, 0.95):.1f}%까지 갑니다."
            ),
        }


def suggest_values(param: str, current) -> list:
    """파라미터 이름에 맞는 탐색 범위를 자동으로 잡아준다."""
    try:
        cur = float(current)
    except (TypeError, ValueError):
        return []
    presets = {
        "k": [0.3, 0.4, 0.5, 0.6, 0.7, 0.8],
        "ma_filter": [0, 5, 10, 20, 40, 60],
        "atr_stop": [1.0, 1.5, 2.0, 2.5, 3.0, 3.5],
        "max_stop_pct": [1.5, 2.0, 2.5, 3.0, 4.0, 6.0],
        "take_profit_pct": [2.0, 3.0, 4.0, 6.0, 8.0, 12.0],
        "trail_atr": [0.0, 1.5, 2.0, 2.5, 3.0, 4.0],
        "fast": [5, 10, 15, 20, 30, 40],
        "slow": [40, 50, 60, 80, 100, 120],
        "pullback_lookback": [2, 3, 5, 8, 12],
        "rsi_max": [55, 60, 65, 70, 75],
        "max_hold_bars": [5, 10, 15, 25, 40],
        "trend_ma": [100, 120, 150, 200, 250],
        "entry_ma": [20, 40, 60, 90],
        "momentum_days": [60, 90, 120, 180, 250],
        "min_momentum_pct": [0.0, 3.0, 5.0, 10.0, 15.0],
        "range_min": [10, 15, 30, 45, 60],
        "vol_filter": [0.0, 0.8, 1.0, 1.3, 1.8],
        "min_range_pct": [0.0, 0.5, 0.8, 1.2, 2.0],
        "dip_min_pct": [10.0, 15.0, 20.0, 25.0, 30.0],
        "dip_max_pct": [30.0, 35.0, 40.0, 50.0],
    }
    if param in presets:
        return presets[param]
    if cur == 0:
        return [0, 1, 2, 5, 10]
    return [round(cur * m, 4) for m in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)]
