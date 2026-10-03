"""설정 로더.

- 비밀정보(앱키/시크릿/계좌번호/제미나이키)는 .env
- 전략/리스크/운영 파라미터는 config/settings.json  (GUI에서 수정 가능)
"""
from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path

from dotenv import load_dotenv, set_key


def _root() -> Path:
    """설정/DB/로그를 둘 곳.

    PyInstaller onefile 로 묶으면 __file__ 은 실행할 때마다 새로 풀리는 임시폴더
    (_MEIxxxxx) 를 가리킨다. 거기에 DB를 만들면 종료와 함께 사라진다.
    그래서 frozen 일 때는 exe 가 놓인 폴더를 쓴다.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_path(*parts: str) -> Path:
    """프로그램에 같이 묶여 나가는 파일(아이콘 등)의 경로.

    _root() 와 다르다. 설정/DB 는 exe 옆에 두지만, 아이콘 같은 리소스는
    PyInstaller onefile 이 임시폴더(_MEIPASS)에 풀어놓는다.
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "_MEIPASS", "") else None
        if base:
            cand = base.joinpath(*parts)
            if cand.exists():
                return cand
    return Path(__file__).resolve().parent.parent.joinpath(*parts)


def icon_file() -> Path:
    return resource_path("app", "icon.ico")


def icon_png() -> Path:
    return resource_path("app", "icon_256.png")


# PyInstaller onefile 부트로더가 자식 프로세스에 물려주는 런타임 변수들.
# 이 값들이 남아 있으면 새로 띄운 exe가 "나는 이미 압축해제된 자식이다" 라고
# 착각해서 사라진 _MEIxxxxx 폴더를 찾거나, 부모 프로세스 검사에 걸려 죽는다.
#   Security validation failure: parent process has different executable!
_PYI_RUNTIME_VARS = (
    "_PYI_ARCHIVE_FILE",
    "_PYI_APPLICATION_HOME_DIR",
    "_PYI_PARENT_PROCESS_LEVEL",
    "_PYI_SPLASH_IPC",
    "_PYI_LINUX_PROCESS_NAME",
    "_MEIPASS2",
    "_PYIBoot_SPLASH",
)


def child_env() -> dict:
    """exe를 새 인스턴스로 띄울 때 쓸 깨끗한 환경변수.

    업데이트 후 재실행, 백그라운드 운용 실행처럼 '앱이 앱을 띄우는' 자리에서
    반드시 이걸 써야 한다. 그냥 물려주면 새 프로세스가 이전 프로세스의
    임시 압축해제 폴더를 물고 들어가 실행에 실패한다.
    """
    env = dict(os.environ)
    for k in _PYI_RUNTIME_VARS:
        env.pop(k, None)
    for k in [k for k in env if k.startswith("_PYI_")]:
        env.pop(k, None)
    # 최신 부트로더는 이 값만 봐도 환경을 초기화한다(구버전은 무시).
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


ROOT = _root()
ENV_PATH = ROOT / ".env"
CONFIG_PATH = ROOT / "config" / "settings.json"
DB_PATH = ROOT / "data" / "trader.db"
LOG_DIR = ROOT / "logs"

load_dotenv(ENV_PATH)

# KIS 도메인
REAL_REST = "https://openapi.koreainvestment.com:9443"
MOCK_REST = "https://openapivts.koreainvestment.com:29443"
REAL_WS = "ws://ops.koreainvestment.com:21000"
MOCK_WS = "ws://ops.koreainvestment.com:31000"


# --------------------------------------------------------------------------
# 비밀정보 (.env)
# --------------------------------------------------------------------------
@dataclass
class Credentials:
    app_key: str = ""
    app_secret: str = ""
    cano: str = ""
    acnt_prdt_cd: str = "01"
    rest_base: str = MOCK_REST
    ws_base: str = MOCK_WS
    is_mock: bool = True

    @property
    def ok(self) -> bool:
        return bool(self.app_key and self.app_secret and self.cano)


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip().strip('"').strip("'")


def load_credentials(mode: str) -> Credentials:
    """mode: 'MOCK' | 'REAL'"""
    if mode.upper() == "REAL":
        return Credentials(
            app_key=_env("KIS_REAL_APP_KEY"),
            app_secret=_env("KIS_REAL_APP_SECRET"),
            cano=_env("KIS_REAL_CANO"),
            acnt_prdt_cd=_env("KIS_REAL_ACNT_PRDT_CD", "01"),
            rest_base=REAL_REST,
            ws_base=REAL_WS,
            is_mock=False,
        )
    return Credentials(
        app_key=_env("KIS_MOCK_APP_KEY"),
        app_secret=_env("KIS_MOCK_APP_SECRET"),
        cano=_env("KIS_MOCK_CANO"),
        acnt_prdt_cd=_env("KIS_MOCK_ACNT_PRDT_CD", "01"),
        rest_base=MOCK_REST,
        ws_base=MOCK_WS,
        is_mock=True,
    )


def gemini_key() -> str:
    return _env("GEMINI_API_KEY")


def save_env(key: str, value: str) -> None:
    ENV_PATH.touch(exist_ok=True)
    set_key(str(ENV_PATH), key, value)
    os.environ[key] = value


# --------------------------------------------------------------------------
# 운영 설정 (config/settings.json)
# --------------------------------------------------------------------------
@dataclass
class RiskConfig:
    """자금관리 안전장치. 이 값들이 계좌를 지키는 마지막 방어선이다."""

    # 1회 거래에서 감수할 최대 손실 (총자산 대비 %)
    max_loss_per_trade_pct: float = 0.5
    # 하루 최대 손실 (총자산 대비 %). 초과하면 당일 신규진입 중단.
    max_daily_loss_pct: float = 2.0
    # 누적 최대 낙폭 (시작자산 대비 %). 초과하면 엔진 전체 정지.
    max_drawdown_pct: float = 10.0
    # 연속 손절 N회 시 쿨다운
    max_consecutive_losses: int = 3
    consecutive_loss_cooldown_min: int = 60
    # 동시 보유 종목 수
    max_positions: int = 5
    # 종목 1개당 최대 비중 (총자산 대비 %)
    max_position_weight_pct: float = 20.0
    # 1회 주문 금액 하한/상한 (원)
    min_order_amount: int = 50_000
    max_order_amount: int = 2_000_000
    # 같은 종목 재진입 금지 시간 (분) - 중복/과매매 방지
    reentry_cooldown_min: int = 30
    # 하루 최대 주문 건수
    max_orders_per_day: int = 40
    # 같은 업종(섹터)에 몰릴 수 있는 최대 비중 (%). 반도체 동반 급락 같은 상황 방어.
    max_sector_weight_pct: float = 40.0
    # 현금 최소 보유 비율 (%) - 전액 몰빵 방지
    min_cash_reserve_pct: float = 10.0

    # -- 거래비용 / 체결현실성 관문 -----------------------------------------
    # 기대이익이 왕복 거래비용의 몇 배 이상일 때만 진입하는가 (0 = 관문 끔).
    # 5년 백테스트에서 전략 대부분은 시장에 진 게 아니라 거래비용에 졌다.
    # 그 손실은 전략을 고쳐서 되찾는 게 아니라 "이 거래는 애초에 할 값어치가
    # 없다"를 진입 전에 가려내서 막아야 한다.
    min_edge_cost_ratio: float = 3.0
    # 최근 평균 거래대금 하한 (원, 0 = 관문 끔).
    # 살 때는 아무 문제 없다가 팔 때 못 빠져나오는 종목을 미리 거른다.
    min_turnover_amount: int = 500_000_000
    # 평균 거래대금을 낼 봉 수
    turnover_lookback: int = 20


@dataclass
class StrategyConfig:
    name: str = "volatility_breakout"
    enabled: bool = True
    params: dict = field(default_factory=dict)


@dataclass
class ExecConfig:
    # 주문 방식: market(시장가) | best(최유리지정가) | limit(지정가)
    order_type: str = "best"
    # 지정가 사용 시 현재가 대비 허용 슬리피지 (%)
    limit_slippage_pct: float = 0.3
    # 매수 체결 미확인 시 취소까지 대기 (초)
    order_timeout_sec: int = 60
    # 정규장 진입 허용 시간대
    entry_start: str = "09:05"
    entry_end: str = "15:00"
    # 장 마감 전 전량 청산 시각 (없으면 빈 문자열 = 오버나이트 보유)
    force_exit_at: str = "15:15"
    # 엔진 루프 주기 (초)
    loop_interval_sec: int = 30


@dataclass
class CostConfig:
    """백테스트/시뮬레이션 비용. 과최적화 방지의 핵심."""
    commission_pct: float = 0.015   # 위탁수수료 (편도)
    sell_tax_pct: float = 0.15      # 매도 시 세금 (증권거래세+농특세)
    slippage_pct: float = 0.10      # 체결 슬리피지 (편도)


@dataclass
class DataConfig:
    # 시세 조회에 실전 도메인을 쓸지 (모의 도메인은 일부 시세 API 미지원)
    use_real_for_quotes: bool = True
    # 수집할 일봉 과거 일수(거래일 기준).
    # 200일선 전략은 워밍업만 220봉이라 400일로는 검증 구간이 8개월밖에 안 남는다.
    daily_history_days: int = 1250
    # 분봉 수집 여부
    collect_minute: bool = True


@dataclass
class AIConfig:
    enabled: bool = True
    model: str = "gemini-2.5-flash"
    # AI 뉴스/공시 리스크 필터 (진입 거부만 가능, 진입 생성은 불가)
    veto_filter: bool = False
    # 장 마감 후 일일 리뷰 생성
    daily_review: bool = True


def _default_strategies() -> list:
    """전략 기본 파라미터는 전략 클래스가 유일한 출처다 (설정과 코드가 어긋나지 않게)."""
    from .strategies import REGISTRY
    enabled_by_default = {"volatility_breakout"}
    return [
        asdict(StrategyConfig(name, name in enabled_by_default, dict(cls.default_params)))
        for name, cls in REGISTRY.items()
    ]


@dataclass
class UpdateConfig:
    # GitHub 저장소 (owner/repo). 비우면 업데이트 확인 안 함
    repo: str = ""
    # 시작할 때 새 버전 확인
    check_on_start: bool = True
    # True면 확인 없이 내려받아 설치까지 (기본 꺼짐 - 무엇이 설치되는지 보고 결정하는 게 낫다)
    auto_install: bool = False


@dataclass
class NotifyConfig:
    enabled: bool = False
    kinds: list = field(default_factory=lambda: ["trade", "risk", "error"])


@dataclass
class AppConfig:
    mode: str = "MOCK"                 # MOCK | REAL | PAPER
    real_trading_confirmed: bool = False
    watchlist: list = field(default_factory=lambda: ["005930", "000660", "035420"])
    paper_initial_cash: int = 10_000_000
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecConfig = field(default_factory=ExecConfig)
    cost: CostConfig = field(default_factory=CostConfig)
    data: DataConfig = field(default_factory=DataConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    update: UpdateConfig = field(default_factory=UpdateConfig)
    strategies: list = field(default_factory=lambda: _default_strategies())


_lock = threading.Lock()
_cache: AppConfig | None = None


def _from_dict(data: dict) -> AppConfig:
    cfg = AppConfig()
    for k, v in data.items():
        if not hasattr(cfg, k):
            continue
        cur = getattr(cfg, k)
        if isinstance(cur, (RiskConfig, ExecConfig, CostConfig, DataConfig,
                            AIConfig, NotifyConfig, UpdateConfig)) and isinstance(v, dict):
            for kk, vv in v.items():
                if hasattr(cur, kk):
                    setattr(cur, kk, vv)
        else:
            setattr(cfg, k, v)
    return cfg


def load_config(force: bool = False) -> AppConfig:
    global _cache
    with _lock:
        if _cache is not None and not force:
            return _cache
        if CONFIG_PATH.exists():
            try:
                _cache = _from_dict(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
            except Exception:
                _cache = AppConfig()
        else:
            _cache = AppConfig()
            _write(_cache)
        # 예전 기본값이 존재하지 않는 모델명이었다. 설정 파일에 남아 있으면
        # 모든 AI 호출이 조용히 실패하므로 여기서 바로잡는다.
        if _cache.ai.model == "gemini-3.5-flash":
            _cache.ai.model = "gemini-2.5-flash"
        return _cache


def _write(cfg: AppConfig) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(asdict(cfg), indent=2, ensure_ascii=False), encoding="utf-8"
    )


def save_config(cfg: AppConfig) -> None:
    global _cache
    with _lock:
        _write(cfg)
        _cache = cfg
