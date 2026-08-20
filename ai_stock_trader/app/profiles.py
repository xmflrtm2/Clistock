"""투자 성향 프로필.

"장투로 하면 얼마 벌까, 단타로 하면 얼마 벌까"를 비교하려면
전략만 바꿔선 안 된다. 보유기간이 다르면 손절폭·비중·재진입 간격·청산시각이
전부 달라져야 한다. 그 한 벌을 묶은 것이 프로필이다.

프로필 하나 = 가상계좌 하나. 서로 예수금을 공유하지 않으므로
포지션 사이징이 섞이지 않는다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .settings import ROOT
from .strategies import REGISTRY

PROFILE_PATH = ROOT / "config" / "profiles.json"

HORIZONS = {
    "long": "장기투자",
    "swing": "스윙(중기)",
    "day": "단타(당일)",
    "scalp": "분봉 단타",
    "custom": "사용자 정의",
}


@dataclass
class Profile:
    name: str = "새 프로필"
    horizon: str = "custom"
    description: str = ""
    initial_cash: int = 10_000_000
    # 빈 리스트면 전역 관심종목을 그대로 쓴다
    watchlist: list = field(default_factory=list)
    strategies: list = field(default_factory=list)
    risk: dict = field(default_factory=dict)         # RiskConfig 덮어쓰기
    execution: dict = field(default_factory=dict)    # ExecConfig 덮어쓰기

    def strategy_labels(self) -> str:
        out = []
        for s in self.strategies:
            if not s.get("enabled"):
                continue
            cls = REGISTRY.get(s["name"])
            out.append(cls.label if cls else s["name"])
        return ", ".join(out) or "없음"

    @property
    def horizon_label(self) -> str:
        return HORIZONS.get(self.horizon, self.horizon)


def _strat(name: str, **params) -> dict:
    cls = REGISTRY[name]
    p = dict(cls.default_params)
    p.update(params)
    return {"name": name, "enabled": True, "params": p}


def builtin_profiles() -> list[Profile]:
    """기본 제공 프리셋. 여기서 값을 조금씩 바꿔가며 비교하면 된다."""
    return [
        Profile(
            name="장기투자",
            horizon="long",
            description="200일선 위 추세를 끝까지 들고 간다. 익절 목표 없이 트레일링만.",
            initial_cash=10_000_000,
            strategies=[_strat("long_term_trend")],
            risk={
                "max_loss_per_trade_pct": 1.0,   # 손절이 머니까 단타보다는 크게
                "max_daily_loss_pct": 5.0,       # 하루 변동에 잘 반응하지 않는다
                "max_drawdown_pct": 20.0,
                "max_positions": 6,
                "max_position_weight_pct": 25.0,
                "reentry_cooldown_min": 1440,    # 하루
                "max_orders_per_day": 10,
                "min_cash_reserve_pct": 5.0,
                "max_order_amount": 3_000_000,
                "max_consecutive_losses": 5,
            },
            execution={"force_exit_at": "", "entry_start": "09:30", "entry_end": "15:00",
                       "loop_interval_sec": 120},
        ),
        Profile(
            name="스윙(중기)",
            horizon="swing",
            description="정배열 눌림목을 며칠~2주 보유. 오버나이트 허용.",
            initial_cash=10_000_000,
            strategies=[_strat("trend_pullback")],
            risk={
                "max_loss_per_trade_pct": 0.8,
                "max_daily_loss_pct": 3.0,
                "max_drawdown_pct": 12.0,
                "max_positions": 5,
                "max_position_weight_pct": 20.0,
                "reentry_cooldown_min": 240,
                "max_orders_per_day": 20,
            },
            execution={"force_exit_at": "", "entry_start": "09:10", "entry_end": "15:00",
                       "loop_interval_sec": 60},
        ),
        Profile(
            name="단타(당일)",
            horizon="day",
            description="변동성 돌파로 진입해 당일 청산. 오버나이트 리스크 없음.",
            initial_cash=10_000_000,
            strategies=[_strat("volatility_breakout")],
            risk={
                "max_loss_per_trade_pct": 0.4,
                "max_daily_loss_pct": 1.5,
                "max_drawdown_pct": 8.0,
                "max_positions": 3,
                "max_position_weight_pct": 15.0,
                "reentry_cooldown_min": 60,
                "max_orders_per_day": 30,
            },
            execution={"force_exit_at": "15:15", "entry_start": "09:05",
                       "entry_end": "14:30", "loop_interval_sec": 30},
        ),
        Profile(
            name="분봉 단타",
            horizon="scalp",
            description="장 초반 레인지 돌파를 분봉으로 잡는다. 분봉 데이터가 있어야 동작.",
            initial_cash=10_000_000,
            strategies=[_strat("opening_range_breakout")],
            risk={
                "max_loss_per_trade_pct": 0.3,
                "max_daily_loss_pct": 1.0,
                "max_drawdown_pct": 6.0,
                "max_positions": 2,
                "max_position_weight_pct": 12.0,
                "reentry_cooldown_min": 30,
                "max_orders_per_day": 40,
            },
            execution={"force_exit_at": "15:10", "entry_start": "09:30",
                       "entry_end": "13:30", "loop_interval_sec": 30},
        ),
    ]


class ProfileStore:
    def __init__(self, path: Path = PROFILE_PATH):
        self.path = path
        self.profiles: list[Profile] = []
        self.load()

    def load(self) -> list[Profile]:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.profiles = [_from_dict(d) for d in raw]
                if self.profiles:
                    return self.profiles
            except Exception:
                pass
        self.profiles = builtin_profiles()
        self.save()
        return self.profiles

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([asdict(p) for p in self.profiles], indent=2, ensure_ascii=False),
            encoding="utf-8")

    def names(self) -> list[str]:
        return [p.name for p in self.profiles]

    def get(self, name: str) -> Profile | None:
        return next((p for p in self.profiles if p.name == name), None)

    def upsert(self, profile: Profile) -> None:
        for i, p in enumerate(self.profiles):
            if p.name == profile.name:
                self.profiles[i] = profile
                break
        else:
            self.profiles.append(profile)
        self.save()

    def remove(self, name: str) -> bool:
        n = len(self.profiles)
        self.profiles = [p for p in self.profiles if p.name != name]
        if len(self.profiles) != n:
            self.save()
            return True
        return False

    def duplicate(self, name: str, new_name: str) -> Profile | None:
        src = self.get(name)
        if not src:
            return None
        d = asdict(src)
        d["name"] = new_name
        d["horizon"] = "custom"
        p = _from_dict(d)
        self.upsert(p)
        return p

    def reset_builtin(self) -> None:
        """기본 프리셋만 초기화. 사용자가 만든 프로필은 남긴다."""
        builtin_names = {p.name for p in builtin_profiles()}
        mine = [p for p in self.profiles if p.name not in builtin_names]
        self.profiles = builtin_profiles() + mine
        self.save()


def _from_dict(d: dict) -> Profile:
    p = Profile()
    for k, v in d.items():
        if hasattr(p, k):
            setattr(p, k, v)
    return p
