"""전략 자동 탐색.

종목을 주면 전략·파라미터 조합을 훑어서 "이걸로 돌려보시겠습니까"를 제안한다.

여기에는 큰 함정이 하나 있다. **조합을 많이 돌려놓고 1등을 고르면 그건 과최적화다.**
50개를 돌리면 그중 하나는 순전히 운으로 좋아 보인다. 그 하나를 골라 실전에 넣으면
백테스트에서 본 수익은 재현되지 않는다.

그래서 이 스캐너는 수익률로 줄 세우지 않는다. 통과 관문을 먼저 두고:

  - 거래 30건 이상            (표본)
  - 비용까지 뺀 뒤에도 수익    (거래비용이 이 시스템의 최대 적)
  - 몬테카를로 손실확률 45% 미만 (운이 나빠도 버티나)
  - 연도별 수익난 해 절반 이상  (특정 해에만 벌었나)
  - 최대낙폭 35% 이하         (실제로 들고 있을 수 있나)
  - 그냥 사서 들고 있는 것보다 나음 (매매가 값어치를 만들었나)

관문을 통과한 것만 점수를 매기고, 하나도 통과 못 하면 "추천할 게 없습니다"라고 말한다.
그게 억지로 하나 고르는 것보다 훨씬 쓸모 있다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict

from .analysis import Analyzer, suggest_values
from .backtest import Backtester
from .settings import CostConfig, RiskConfig
from .storage import Store
from .strategies import REGISTRY

log = logging.getLogger(__name__)

# 전략별로 어떤 성향/자금관리와 짝을 지을지
STYLE_RISK = {
    "long_term_trend": ("장기투자", {
        "max_loss_per_trade_pct": 1.0, "max_daily_loss_pct": 5.0,
        "max_drawdown_pct": 20.0, "max_positions": 6,
        "max_position_weight_pct": 25.0, "reentry_cooldown_min": 1440,
        "max_orders_per_day": 10, "min_cash_reserve_pct": 5.0,
        "max_order_amount": 3_000_000, "max_consecutive_losses": 5}),
    "trend_pullback": ("스윙(중기)", {
        "max_loss_per_trade_pct": 0.8, "max_daily_loss_pct": 3.0,
        "max_drawdown_pct": 12.0, "max_positions": 5,
        "max_position_weight_pct": 20.0, "reentry_cooldown_min": 240,
        "max_orders_per_day": 20}),
    "volatility_breakout": ("단타(당일)", {
        "max_loss_per_trade_pct": 0.4, "max_daily_loss_pct": 1.5,
        "max_drawdown_pct": 8.0, "max_positions": 3,
        "max_position_weight_pct": 15.0, "reentry_cooldown_min": 60,
        "max_orders_per_day": 30}),
    "opening_range_breakout": ("분봉 단타", {
        "max_loss_per_trade_pct": 0.3, "max_daily_loss_pct": 1.0,
        "max_drawdown_pct": 6.0, "max_positions": 2,
        "max_position_weight_pct": 12.0, "reentry_cooldown_min": 30,
        "max_orders_per_day": 40}),
}

STYLE_EXEC = {
    "장기투자": {"force_exit_at": "", "entry_start": "09:30", "entry_end": "15:00",
                 "loop_interval_sec": 120},
    "스윙(중기)": {"force_exit_at": "", "entry_start": "09:10", "entry_end": "15:00",
                   "loop_interval_sec": 60},
    "단타(당일)": {"force_exit_at": "15:15", "entry_start": "09:05",
                   "entry_end": "14:30", "loop_interval_sec": 30},
    "분봉 단타": {"force_exit_at": "15:10", "entry_start": "09:30",
                  "entry_end": "13:30", "loop_interval_sec": 30},
}

# 2차 탐색에서 건드릴 핵심 파라미터 (전부 훑으면 과최적화라 하나씩만)
TUNE_PARAM = {
    "long_term_trend": "trend_ma",
    "trend_pullback": "pullback_lookback",
    "volatility_breakout": "k",
    "opening_range_breakout": "range_min",
}

GATES = {
    "min_trades": 30,
    "max_loss_prob": 45.0,
    "min_consistency": 50.0,
    "max_mdd": 35.0,
    # 바이앤홀드를 최소 몇 %p 이겨야 하는가.
    # 그냥 사서 들고만 있어도 나오는 수익을, 매매 위험을 지고 겨우 따라잡은
    # 전략은 쓸 이유가 없다. 0%p 로 두면 "동점이면 탈락"이 된다.
    "min_alpha_pp": 0.0,
}


@dataclass
class Candidate:
    strategy: str
    label: str = ""
    style: str = ""
    params: dict = field(default_factory=dict)
    risk_over: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    mc: dict = field(default_factory=dict)
    periods: list = field(default_factory=list)
    period_sum: dict = field(default_factory=dict)
    sens: object = None
    score: float = 0.0
    passed: bool = False
    fails: list = field(default_factory=list)
    subsets: list = field(default_factory=list)   # 종목 일부만 썼을 때 수익률
    subset_fail: str = ""

    @property
    def net(self) -> float:
        return self.metrics.get("total_return_pct", 0)

    @property
    def loss_prob(self) -> float:
        return self.mc.get("loss_prob", 100.0)

    @property
    def consistency(self) -> float:
        return self.period_sum.get("consistency", 0.0)

    @property
    def alpha(self) -> float:
        """바이앤홀드 대비 초과수익(%p)."""
        return self.metrics.get("alpha_pct", 0.0)


@dataclass
class ScanResult:
    symbols: list = field(default_factory=list)
    tested: int = 0
    candidates: list = field(default_factory=list)   # 관문 통과, 점수순
    rejected: list = field(default_factory=list)     # 탈락 (사유 포함)
    best: Candidate | None = None
    summary: str = ""
    error: str = ""


class Scanner:
    def __init__(self, store: Store, cost: CostConfig, base_risk: RiskConfig,
                 cash: int = 10_000_000):
        self.store = store
        self.cost = cost
        self.base_risk = base_risk
        self.cash = cash
        self.an = Analyzer(store, cost)

    # ------------------------------------------------------------------
    def _risk(self, over: dict) -> RiskConfig:
        rc = RiskConfig()
        for k, v in asdict(self.base_risk).items():
            setattr(rc, k, v)
        for k, v in (over or {}).items():
            if hasattr(rc, k):
                setattr(rc, k, v)
        return rc

    def _usable(self, symbols: list[str], strategy: str) -> list[str]:
        need = REGISTRY[strategy].warmup + 30
        tf = REGISTRY[strategy].timeframe
        return [s for s in symbols
                if len(self.store.get_candles(s, tf, limit=need + 5)) >= need]

    # ------------------------------------------------------------------
    def scan(self, symbols: list[str], progress=None, log_fn=None) -> ScanResult:
        res = ScanResult(symbols=list(symbols))
        if not symbols:
            res.error = "종목이 없습니다."
            return res

        def say(m):
            log.info(m)
            if log_fn:
                log_fn(m)

        def step(frac, msg):
            if progress:
                progress(frac, msg)

        cands: list[Candidate] = []
        strategies = [n for n in REGISTRY if TUNE_PARAM.get(n)]

        # ---------- 1차: 기본 파라미터로 전략별 성적 ----------
        step(0.05, "1단계: 전략별 기본 성적")
        say(f"1단계 - 전략 {len(strategies)}종을 기본 설정으로 검증")
        stage1 = []
        for i, name in enumerate(strategies):
            usable = self._usable(symbols, name)
            if len(usable) < 2:
                say(f"  {REGISTRY[name].label}: 데이터 있는 종목 {len(usable)}개 - 건너뜀")
                continue
            style, over = STYLE_RISK[name]
            rc = self._risk(over)
            r = Backtester(self.store, self.cost, rc).run(name, {}, usable, self.cash)
            if r.error:
                say(f"  {REGISTRY[name].label}: {r.error[:50]}")
                continue
            m = r.metrics
            say(f"  {REGISTRY[name].label}: {m['total_return_pct']:+.2f}% "
                f"(비용전 {m['gross_return_pct']:+.2f}%) / MDD {m['mdd_pct']:.1f}% / "
                f"{m['trades']}건 / {len(usable)}종목")
            res.tested += 1
            # 거래가 몇 건뿐이면 MDD가 0으로 나와 순위가 왜곡된다.
            # 표본이 없는 것을 "낙폭 없는 안전한 전략"으로 착각하면 안 된다.
            if m.get("trades", 0) < GATES["min_trades"] * 0.6:
                say(f"    -> 거래 {m.get('trades', 0)}건뿐이라 순위에서 제외 "
                    f"(데이터가 부족하거나 조건이 너무 빡빡함)")
                continue
            stage1.append((m.get("return_over_mdd", -99), name, usable, r))
            step(0.05 + 0.2 * (i + 1) / len(strategies), f"1단계 {i + 1}/{len(strategies)}")

        if not stage1:
            res.error = ("검증할 수 있는 전략이 없습니다. "
                         "[데이터] 탭에서 일봉을 먼저 수집하세요.")
            return res

        stage1.sort(reverse=True, key=lambda x: x[0])
        top = stage1[:2]

        # ---------- 2차: 상위 전략의 핵심 파라미터 훑기 ----------
        step(0.28, "2단계: 핵심 파라미터 탐색")
        say("")
        say(f"2단계 - 상위 {len(top)}개 전략의 핵심 파라미터를 훑음")
        variants: list[tuple[str, dict, list]] = []
        for si, (_s, name, usable, _r) in enumerate(top):
            param = TUNE_PARAM[name]
            cur = REGISTRY[name].default_params.get(param)
            values = suggest_values(param, cur)[:5]
            style, over = STYLE_RISK[name]
            rc = self._risk(over)
            bt = Backtester(self.store, self.cost, rc)
            say(f"  {REGISTRY[name].label} / {param} = {values}")
            best_local = None
            for vi, v in enumerate(values):
                p = {param: v}
                r = bt.run(name, p, usable, self.cash)
                res.tested += 1
                if r.error:
                    continue
                m = r.metrics
                say(f"    {param}={v} -> {m['total_return_pct']:+.2f}% / "
                    f"MDD {m['mdd_pct']:.1f}% / {m['trades']}건")
                key = m.get("return_over_mdd", -99)
                if best_local is None or key > best_local[0]:
                    best_local = (key, p, r, usable)
                step(0.28 + 0.32 * ((si * len(values) + vi + 1) /
                                    (len(top) * max(len(values), 1))),
                     f"2단계 {si + 1}/{len(top)}")
            if best_local:
                variants.append((name, best_local[1], best_local[3]))
                # 기본값도 후보에 남긴다 (튜닝값이 우연일 수 있으므로)
                variants.append((name, {}, usable))

        # ---------- 3차: 견고성 검증 ----------
        step(0.62, "3단계: 견고성 검증")
        say("")
        say(f"3단계 - 후보 {len(variants)}개를 몬테카를로/연도별/민감도로 검증")
        for ci, (name, params, usable) in enumerate(variants):
            style, over = STYLE_RISK[name]
            rc = self._risk(over)
            bt = Backtester(self.store, self.cost, rc)
            r = bt.run(name, params, usable, self.cash)
            if r.error:
                continue
            c = Candidate(strategy=name, label=REGISTRY[name].label, style=style,
                          params=dict(params), risk_over=dict(over),
                          metrics=r.metrics)
            c.mc = self.an.monte_carlo(r.trades, self.cash, runs=1500)
            c.periods = self.an.period_split(name, params, rc, usable, self.cash)
            c.period_sum = self.an.period_summary(c.periods)
            param = TUNE_PARAM[name]
            c.sens = self.an.sensitivity(
                name, params, rc, usable, param,
                suggest_values(param, params.get(param,
                                                 REGISTRY[name].default_params.get(param)))[:5],
                self.cash)
            res.tested += 1 + len(c.periods) + len(getattr(c.sens, "points", []))
            self._judge(c)
            cands.append(c)
            tag = "통과" if c.passed else "탈락"
            say(f"  [{tag}] {c.label} {params or '기본'} -> "
                f"{c.net:+.2f}% / 손실확률 {c.loss_prob:.0f}% / "
                f"연도일관성 {c.consistency:.0f}% / 점수 {c.score:.1f}"
                + (f"  ({', '.join(c.fails)})" if c.fails else ""))
            step(0.62 + 0.35 * (ci + 1) / max(len(variants), 1),
                 f"3단계 {ci + 1}/{len(variants)}")

        # ---------- 4차: 종목을 바꿔도 유지되나 ----------
        passed_pre = [c for c in cands if c.passed]
        passed_pre.sort(key=lambda c: c.score, reverse=True)
        if passed_pre:
            step(0.97, "4단계: 종목 교체 검증")
            say("")
            say("4단계 - 상위 후보를 종목 일부만으로 다시 검증")
            for c in passed_pre[:2]:
                self._symbol_robustness(c, symbols, say)
                res.tested += 3
                if c.subset_fail:
                    c.passed = False
                    c.fails.append(c.subset_fail)

        step(1.0, "완료")
        passed = [c for c in cands if c.passed]
        passed.sort(key=lambda c: c.score, reverse=True)
        res.candidates = passed
        # 점수로만 줄 세우면 "거래 1건이라 낙폭 0" 같은 껍데기가 위로 온다.
        # 관문을 적게 어긴 것을 먼저 보여준다.
        res.rejected = sorted([c for c in cands if not c.passed],
                              key=lambda c: (len(c.fails), -c.score))
        res.best = passed[0] if passed else None

        if res.best:
            b = res.best
            res.summary = (
                f"{res.tested}개 조합을 검증해 {len(passed)}개가 관문을 통과했습니다.\n"
                f"가장 나은 것은 [{b.label} / {b.style}] 입니다.\n\n"
                f"  백테스트 수익률   {b.net:+.2f}%  (비용 전 "
                f"{b.metrics.get('gross_return_pct', 0):+.2f}%)\n"
                f"  몬테카를로 중앙값 {b.mc.get('return_median', 0):+.2f}%\n"
                f"  90% 구간          {b.mc.get('return_p05', 0):+.1f}% ~ "
                f"{b.mc.get('return_p95', 0):+.1f}%\n"
                f"  손실로 끝날 확률  {b.loss_prob:.0f}%\n"
                f"  연도별 수익 비율  {b.consistency:.0f}%  "
                f"({b.period_sum.get('positive_years', 0)}/{b.period_sum.get('years', 0)}년)\n"
                f"  최대낙폭          {b.metrics.get('mdd_pct', 0):.1f}%  "
                f"(나쁜 경우 {b.mc.get('mdd_p95', 0):.1f}%)\n"
                f"  거래 / 승률       {b.metrics.get('trades', 0)}건 / "
                f"{b.metrics.get('win_rate', 0)}%\n"
                f"  그냥 보유했다면   {b.metrics.get('bh_return_pct', 0):+.2f}%  "
                f"(초과수익 {b.alpha:+.2f}%p)\n\n"
                f"주의 - {res.tested}개를 훑어서 고른 1등입니다. 많이 훑을수록 "
                f"우연히 좋아 보이는 것이 섞이기 쉬우므로, 모의투자로 충분히 "
                f"확인한 뒤에 실전을 생각하세요."
            )
        else:
            worst = res.rejected[0] if res.rejected else None
            res.summary = (
                f"{res.tested}개 조합을 검증했지만 **추천할 만한 것이 없습니다.**\n\n"
                "관문을 하나도 통과하지 못했습니다. 억지로 1등을 고르면 그건 "
                "우연히 좋아 보인 것을 고르는 것이라 실전에서 재현되지 않습니다.\n\n"
                + (f"가장 근접했던 것: [{worst.label}] "
                   f"{worst.net:+.2f}% / 손실확률 {worst.loss_prob:.0f}%\n"
                   f"  걸린 관문: {', '.join(worst.fails)}\n\n" if worst else "")
                + "해볼 것:\n"
                  "  - 종목을 바꾸거나 늘려보기 (거래대금 큰 종목이 슬리피지에 유리)\n"
                  "  - [데이터] 탭에서 일봉을 더 수집하기\n"
                  "  - 거래 횟수가 적은 장기 전략 위주로 보기"
            )
        return res

    # ------------------------------------------------------------------
    def _symbol_robustness(self, c: Candidate, symbols: list[str], say) -> None:
        """종목을 몇 개 빼고 돌려도 결과가 유지되는지 본다.

        파라미터 민감도만 보면 놓치는 게 있다. 종목 몇 개를 바꿨을 뿐인데
        수익이 절반이 되거나 손실로 뒤집힌다면, 그건 그 종목들이 우연히
        잘 맞았던 것이지 전략이 좋았던 게 아니다.
        """
        import random
        usable = self._usable(symbols, c.strategy)
        if len(usable) < 5:
            c.subsets = []
            return
        rc = self._risk(c.risk_over)
        bt = Backtester(self.store, self.cost, rc)
        keep = max(3, int(len(usable) * 0.7))
        rnd = random.Random(11)
        rets = []
        for i in range(3):
            sub = rnd.sample(usable, keep)
            r = bt.run(c.strategy, c.params, sub, self.cash)
            if r.error:
                continue
            rets.append(r.metrics.get("total_return_pct", 0))
            say(f"    {c.label} {c.params or '기본'} / 종목 {keep}개 표본{i + 1} "
                f"-> {rets[-1]:+.2f}%")
        c.subsets = rets
        if not rets:
            return
        losers = sum(1 for x in rets if x <= 0)
        if losers >= 2:
            c.subset_fail = (f"종목을 바꾸면 {losers}/{len(rets)}번 손실 "
                             f"({min(rets):+.1f}% ~ {max(rets):+.1f}%)")
        elif max(rets) - min(rets) > max(abs(c.net), 10) * 1.5:
            c.subset_fail = (f"종목에 따라 {min(rets):+.1f}% ~ {max(rets):+.1f}% 로 "
                             f"편차가 큼")

    # ------------------------------------------------------------------
    def _judge(self, c: Candidate) -> None:
        m, mc, ps = c.metrics, c.mc, c.period_sum
        fails = []
        if m.get("trades", 0) < GATES["min_trades"]:
            fails.append(f"거래 {m.get('trades', 0)}건 < {GATES['min_trades']}")
        if c.net <= 0:
            fails.append(f"비용 후 수익 {c.net:+.1f}%")
        if mc.get("error"):
            fails.append("분포 계산 불가")
        elif c.loss_prob > GATES["max_loss_prob"]:
            fails.append(f"손실확률 {c.loss_prob:.0f}% > {GATES['max_loss_prob']:.0f}%")
        if ps and c.consistency < GATES["min_consistency"]:
            fails.append(f"연도일관성 {c.consistency:.0f}% < {GATES['min_consistency']:.0f}%")
        if m.get("mdd_pct", 99) > GATES["max_mdd"]:
            fails.append(f"MDD {m.get('mdd_pct', 0):.0f}% > {GATES['max_mdd']:.0f}%")
        # 그냥 사서 들고 있는 것보다 못하면, 매매를 한 이유가 없다.
        if m.get("bh_symbols") and c.alpha <= GATES["min_alpha_pp"]:
            fails.append(f"바이앤홀드 대비 {c.alpha:+.1f}%p "
                         f"(보유 {m.get('bh_return_pct', 0):+.1f}%)")
        c.fails = fails
        c.passed = not fails

        # 점수 = 기대수익 x 성공확률 x 일관성 - 꼬리위험.
        # 수익률만으로 줄 세우면 우연히 튄 조합이 1등을 한다.
        med = mc.get("return_median", 0) if not mc.get("error") else c.net
        succ = max(0.0, 1 - c.loss_prob / 100)
        tail = mc.get("mdd_p95", m.get("mdd_pct", 0)) if not mc.get("error") else m.get("mdd_pct", 0)
        flat = getattr(c.sens, "flatness", 0.5) or 0.5
        cons = c.consistency / 100 if ps else 0.5
        c.score = round(med * succ * (0.5 + 0.5 * cons) * (0.7 + 0.3 * flat) - tail * 0.3, 2)


def to_profile(c: Candidate, name: str, symbols: list[str], cash: int):
    """추천 결과를 그대로 실행 가능한 프로필로 만든다."""
    from .profiles import Profile
    cls = REGISTRY[c.strategy]
    params = dict(cls.default_params)
    params.update(c.params or {})
    horizon = {"장기투자": "long", "스윙(중기)": "swing",
               "단타(당일)": "day", "분봉 단타": "scalp"}.get(c.style, "custom")
    return Profile(
        name=name,
        horizon=horizon,
        description=(f"자동 탐색 결과 ({cls.label}). "
                     f"백테스트 {c.net:+.1f}% / 손실확률 {c.loss_prob:.0f}% / "
                     f"연도일관성 {c.consistency:.0f}%"),
        initial_cash=cash,
        watchlist=list(symbols),
        strategies=[{"name": c.strategy, "enabled": True, "params": params}],
        risk=dict(c.risk_over),
        execution=dict(STYLE_EXEC.get(c.style, {})),
    )
