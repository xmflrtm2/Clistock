"""운용 랩 - 여러 투자방식을 동시에 가상으로 굴리고 비교한다.

"장투로 하면 얼마 벌고 단타로 하면 얼마 벌까"를 알려면 같은 기간, 같은 시세로
동시에 굴려봐야 한다. 순서대로 하나씩 돌리면 시장 상황이 달라져서 비교가 안 된다.

프로필마다:
  - 독립된 가상계좌 (예수금을 서로 공유하지 않음)
  - 독립된 TradingEngine (mode = "LAB:이름" 으로 DB에서 분리)
  - 시작 시각을 기록해 실제 경과 시간 기준으로 성과를 잰다

주의: 짧은 기간의 수익률을 연으로 환산한 값은 통계적으로 의미가 없다.
      그래서 표본이 부족하면 예상금액에 경고를 붙여서 내보낸다.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from dataclasses import asdict

from .broker import PaperBroker
from .collector import Collector
from .market import MarketCalendar
from .profiles import Profile
from .settings import AppConfig, CostConfig, ExecConfig, RiskConfig
from .storage import Store
from .trader import TradingEngine

log = logging.getLogger(__name__)

MODE_PREFIX = "LAB:"


def lab_mode(name: str) -> str:
    return f"{MODE_PREFIX}{name}"


class LabRun:
    """프로필 하나의 가상운용 인스턴스."""

    def __init__(self, profile: Profile, store: Store, quote_client,
                 calendar: MarketCalendar, collector: Collector | None,
                 base_cfg: AppConfig, cost: CostConfig, on_event=None):
        self.profile = profile
        self.store = store
        self.mode = lab_mode(profile.name)

        cfg = AppConfig()
        cfg.mode = self.mode
        cfg.watchlist = list(profile.watchlist or base_cfg.watchlist)
        cfg.paper_initial_cash = profile.initial_cash
        cfg.strategies = [dict(s) for s in profile.strategies]
        cfg.ai = base_cfg.ai
        cfg.data = base_cfg.data
        cfg.cost = cost

        cfg.risk = RiskConfig(**{**asdict(base_cfg.risk), **(profile.risk or {})})
        cfg.execution = ExecConfig(**{**asdict(base_cfg.execution),
                                      **(profile.execution or {})})
        self.cfg = cfg

        self.broker = PaperBroker(store, quote_client, cost, profile.initial_cash,
                                  key=f"paper_account::{profile.name}")
        self.engine = TradingEngine(cfg, store, self.broker, calendar, collector,
                                    on_event=on_event, ai=None)
        self.engine.mode = self.mode
        self.engine.risk.mode = self.mode

    @property
    def running(self) -> bool:
        return self.engine.running

    def start(self) -> None:
        self.engine.start()

    def stop(self) -> None:
        self.engine.stop()


class Lab:
    """여러 LabRun을 관리하고 성과를 집계한다."""

    def __init__(self, core):
        self.core = core
        self.store: Store = core.store
        self.runs: dict[str, LabRun] = {}
        self._ensure_table()

    def _ensure_table(self) -> None:
        with self.store.conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS lab_runs (
                name TEXT PRIMARY KEY, profile TEXT, started_at TEXT,
                initial_cash REAL, active INTEGER DEFAULT 1, stopped_at TEXT)""")

    # -- 수명주기 -----------------------------------------------------------
    def _build(self, profile: Profile) -> LabRun:
        return LabRun(profile, self.store, self.core.quote_client,
                      self.core.calendar, self.core.collector,
                      self.core.cfg, self.core.cfg.cost, self.core._on_event)

    def start(self, profile: Profile) -> tuple[bool, str]:
        run = self.runs.get(profile.name)
        if run and run.running:
            return False, f"'{profile.name}'는 이미 돌고 있습니다."
        run = self._build(profile)
        if not run.engine.strategies:
            return False, f"'{profile.name}'에 활성화된 전략이 없습니다."
        self.runs[profile.name] = run

        row = self.store.one("SELECT started_at FROM lab_runs WHERE name=?", (profile.name,))
        started = row["started_at"] if row else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.store.conn() as c:
            c.execute("INSERT INTO lab_runs(name,profile,started_at,initial_cash,active,stopped_at) "
                      "VALUES(?,?,?,?,1,NULL) ON CONFLICT(name) DO UPDATE SET "
                      "profile=excluded.profile, active=1, stopped_at=NULL",
                      (profile.name, json.dumps(asdict(profile), ensure_ascii=False),
                       started, profile.initial_cash))
        run.start()
        return True, f"'{profile.name}' 가상운용 시작 (전략: {profile.strategy_labels()})"

    def stop(self, name: str) -> tuple[bool, str]:
        run = self.runs.get(name)
        if not run:
            return False, f"'{name}'는 실행 중이 아닙니다."
        run.stop()
        with self.store.conn() as c:
            c.execute("UPDATE lab_runs SET active=0, stopped_at=? WHERE name=?",
                      (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), name))
        return True, f"'{name}' 가상운용 정지"

    def stop_all(self) -> None:
        for name in list(self.runs):
            self.stop(name)

    def reset(self, name: str) -> tuple[bool, str]:
        """가상계좌와 기록을 지우고 처음부터 다시."""
        run = self.runs.get(name)
        if run and run.running:
            return False, "먼저 정지하세요."
        mode = lab_mode(name)
        prof = self.core.profiles.get(name)
        cash = prof.initial_cash if prof else 10_000_000
        pb = PaperBroker(self.store, self.core.quote_client, self.core.cfg.cost,
                         cash, key=f"paper_account::{name}")
        # mode 를 명시해야 한다. 안 넘기면 PaperBroker 기본값인 "PAPER" 모드의
        # 기록(메인 가상계좌의 거래/주문/자산 이력)이 통째로 지워진다.
        pb.reset(cash, mode=mode)
        with self.store.conn() as c:
            for t in ("trades", "orders", "signals", "equity"):
                c.execute(f"DELETE FROM {t} WHERE mode=?", (mode,))
            c.execute("DELETE FROM lab_runs WHERE name=?", (name,))
        self.runs.pop(name, None)
        return True, f"'{name}' 기록 초기화 완료 (초기자금 {cash:,}원)"

    def running_names(self) -> list[str]:
        return [n for n, r in self.runs.items() if r.running]

    def tick_all_once(self) -> None:
        """장중이 아닐 때도 계좌를 갱신해 화면 숫자를 살아 있게 한다."""
        for r in self.runs.values():
            try:
                r.engine.refresh_account()
            except Exception as e:
                log.debug("lab 계좌 갱신 실패 %s: %s", r.profile.name, e)

    # -- 성과 -------------------------------------------------------------
    def performance(self, name: str) -> dict | None:
        row = self.store.one("SELECT * FROM lab_runs WHERE name=?", (name,))
        if not row:
            return None
        prof = self.core.profiles.get(name)
        run = self.runs.get(name)
        mode = lab_mode(name)
        initial = float(row["initial_cash"] or 0) or 1.0

        started = _parse(row["started_at"])
        end = _parse(row["stopped_at"]) if row["stopped_at"] else datetime.now()
        elapsed_sec = max((end - started).total_seconds(), 1.0) if started else 1.0

        # 현재 평가금액: 엔진이 돌고 있으면 그 캐시, 아니면 가상계좌를 직접 읽는다
        if run is not None:
            acct = run.engine.account or {}
            if not acct:
                try:
                    acct = run.engine.refresh_account()
                except Exception:
                    acct = {}
        else:
            pb = PaperBroker(self.store, self.core.quote_client, self.core.cfg.cost,
                             int(initial), key=f"paper_account::{name}")
            try:
                acct = pb.account()
            except Exception:
                acct = {}

        equity = float(acct.get("total_eval") or 0)
        if equity <= 0:
            eq = self.store.equity_series(mode, 1)
            equity = float(eq[-1]["total_eval"]) if eq else initial

        realized = sum(float(t["pnl"] or 0) for t in self.store.closed_trades(mode, 100000))
        unrealized = float(acct.get("pnl") or 0)
        total_pnl = equity - initial
        ret_pct = total_pnl / initial * 100

        closed = self.store.closed_trades(mode, 100000)
        wins = [t for t in closed if (t["pnl"] or 0) > 0]
        gross_win = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in closed if (t["pnl"] or 0) <= 0))

        # 자산곡선에서 MDD
        series = self.store.equity_series(mode, 20000)
        peak = mdd = 0.0
        for p in series:
            v = float(p["total_eval"] or 0)
            peak = max(peak, v)
            if peak > 0:
                mdd = max(mdd, (peak - v) / peak * 100)

        days = elapsed_sec / 86400
        # 연/월 환산 - 짧은 기간에서는 신뢰할 수 없다
        proj_year = proj_month = 0.0
        if days >= 0.02 and ret_pct > -100:
            proj_year = ((1 + ret_pct / 100) ** (365 / max(days, 0.02)) - 1) * 100
            proj_month = ((1 + ret_pct / 100) ** (30 / max(days, 0.02)) - 1) * 100
            proj_year = max(min(proj_year, 99999), -100)
            proj_month = max(min(proj_month, 99999), -100)

        reliable = days >= 5 and len(closed) >= 20
        caveat = ""
        if not reliable:
            need = []
            if days < 5:
                need.append(f"{5 - days:.1f}일 더")
            if len(closed) < 20:
                need.append(f"{20 - len(closed)}거래 더")
            caveat = f"표본 부족 ({' / '.join(need)}) - 예상금액은 참고만"

        return {
            "name": name,
            "horizon": prof.horizon_label if prof else "-",
            "strategies": prof.strategy_labels() if prof else "-",
            "running": bool(run and run.running),
            "started_at": row["started_at"] or "-",
            "elapsed_sec": elapsed_sec,
            "elapsed": _human(elapsed_sec),
            "trading_days": days,
            "initial": initial,
            "equity": equity,
            "cash": float(acct.get("cash") or 0),
            "realized": realized,
            "unrealized": unrealized,
            "total_pnl": total_pnl,
            "return_pct": ret_pct,
            "proj_month_pct": proj_month,
            "proj_month_amt": initial * proj_month / 100,
            "proj_year_pct": proj_year,
            "proj_year_amt": initial * proj_year / 100,
            "trades": len(closed),
            "open_positions": len(self.store.open_trades(mode)),
            "win_rate": (len(wins) / len(closed) * 100) if closed else 0.0,
            "profit_factor": (gross_win / gross_loss) if gross_loss else 0.0,
            "mdd_pct": mdd,
            "reliable": reliable,
            "caveat": caveat,
        }

    def all_performance(self) -> list[dict]:
        rows = self.store.query("SELECT name FROM lab_runs ORDER BY started_at")
        out = []
        for r in rows:
            p = self.performance(r["name"])
            if p:
                out.append(p)
        return out

    def equity_curves(self) -> dict[str, list[tuple[str, float]]]:
        out = {}
        for r in self.store.query("SELECT name FROM lab_runs"):
            name = r["name"]
            pts = self.store.equity_series(lab_mode(name), 5000)
            if len(pts) >= 2:
                out[name] = [(p["ts"], float(p["total_eval"] or 0)) for p in pts]
        return out


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _human(sec: float) -> str:
    d, rem = divmod(int(sec), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}일 {h}시간"
    if h:
        return f"{h}시간 {m}분"
    return f"{m}분"
