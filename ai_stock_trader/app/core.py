"""애플리케이션 조립.

모드(PAPER / MOCK / REAL)에 따라 클라이언트와 브로커를 갈아끼우고,
나머지 부품(수집기, 달력, 백테스터, AI, 엔진)을 연결한다.
GUI는 이 객체 하나만 들고 있으면 된다.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .ai_advisor import AIAdvisor
from .backtest import Backtester
from .broker import Broker, KISBroker, PaperBroker
from .collector import Collector
from .kis_client import KISClient
from .master import StockMaster
from .notifier import Notifier
from .profiles import ProfileStore
from .market import MarketCalendar
from .settings import (AppConfig, LOG_DIR, load_config, load_credentials,
                       save_config)
from .storage import Store, get_store
from .trader import TradingEngine

log = logging.getLogger(__name__)


def setup_logging(level: int = logging.INFO) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(LOG_DIR / "trader.log", maxBytes=5_000_000,
                             backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if getattr(sys, "stderr", None) is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    for noisy in ("httpx", "urllib3", "google_genai", "google_genai.models"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class AppCore:
    def __init__(self, on_event=None):
        setup_logging()
        self.cfg: AppConfig = load_config()
        self.store: Store = get_store()
        self._on_event = on_event

        self.quote_client: KISClient | None = None
        self.trade_client: KISClient | None = None
        self.broker: Broker | None = None
        self.collector: Collector | None = None
        self.calendar: MarketCalendar | None = None
        self.engine: TradingEngine | None = None
        self.ai = AIAdvisor(self.cfg.ai, self.store)
        self.backtester = Backtester(self.store, self.cfg.cost, self.cfg.risk)
        self.master = StockMaster(self.store)
        self.profiles = ProfileStore()
        self.notifier = Notifier()
        self.lab = None          # rebuild() 에서 생성
        self.status_msg = ""

        self.rebuild()

    # ------------------------------------------------------------------
    def emit(self, kind: str, msg: str, data: dict | None = None) -> None:
        if self._on_event:
            try:
                self._on_event(kind, msg, data or {})
            except Exception:
                pass

    # ------------------------------------------------------------------
    def rebuild(self) -> None:
        """설정이 바뀌면 전체 부품을 다시 조립한다."""
        if self.engine and self.engine.running:
            self.engine.stop()

        mode = (self.cfg.mode or "PAPER").upper()
        mock_creds = load_credentials("MOCK")
        real_creds = load_credentials("REAL")

        # 시세 클라이언트: 실전 도메인이 분봉/휴장일 등 지원 범위가 넓다.
        # 실전 키는 read_only=True 로 잠가서 주문이 절대 나가지 못하게 한다.
        self.quote_client = None
        if self.cfg.data.use_real_for_quotes and real_creds.ok:
            self.quote_client = KISClient(real_creds, read_only=True, label="시세(실전)")
        elif mock_creds.ok:
            self.quote_client = KISClient(mock_creds, read_only=True, label="시세(모의)")
        elif real_creds.ok:
            self.quote_client = KISClient(real_creds, read_only=True, label="시세(실전)")

        # 주문 클라이언트
        self.trade_client = None
        notes = []
        if mode == "REAL":
            if not self.cfg.real_trading_confirmed:
                notes.append("실전 모드가 승인되지 않아 모의투자로 대체했습니다.")
                mode = "MOCK"
            elif not real_creds.ok:
                notes.append("실전 키가 없어 모의투자로 대체했습니다.")
                mode = "MOCK"
        if mode == "MOCK" and not mock_creds.ok:
            notes.append("모의투자 키가 없어 로컬 페이퍼 모드로 대체했습니다.")
            mode = "PAPER"

        if mode == "REAL":
            self.trade_client = KISClient(real_creds, label="주문(실전)")
        elif mode == "MOCK":
            self.trade_client = KISClient(mock_creds, label="주문(모의)")

        # 브로커
        if self.trade_client:
            self.broker = KISBroker(
                self.store, self.trade_client, self.quote_client or self.trade_client,
                self.cfg.cost, self.cfg.execution.order_type,
                self.cfg.execution.limit_slippage_pct,
                self.cfg.execution.order_timeout_sec,
            )
        else:
            self.broker = PaperBroker(self.store, self.quote_client, self.cfg.cost,
                                      self.cfg.paper_initial_cash)

        self.calendar = MarketCalendar(self.store, self.quote_client)
        self.collector = Collector(self.store, self.quote_client) if self.quote_client else None
        self.ai = AIAdvisor(self.cfg.ai, self.store)
        self.backtester = Backtester(self.store, self.cfg.cost, self.cfg.risk)
        self.notifier.kinds = set(self.cfg.notify.kinds or [])
        note = self.notifier if self.cfg.notify.enabled else None
        self.engine = TradingEngine(self.cfg, self.store, self.broker, self.calendar,
                                    self.collector, self._on_event, self.ai, note)

        from .lab import Lab
        if self.lab is None:
            self.lab = Lab(self)
        else:
            self.lab.core = self

        self.status_msg = " ".join(notes)
        for n in notes:
            self.emit("warn", n)
        self.emit("engine", f"모드: {self.broker.mode}"
                            + (f" | 시세: {self.quote_client.label}" if self.quote_client else ""))

    # ------------------------------------------------------------------
    def save(self, rebuild: bool = True) -> None:
        save_config(self.cfg)
        if rebuild:
            self.rebuild()

    def diagnostics(self) -> list[tuple[str, bool, str]]:
        """설정 점검 - GUI 상단에 그대로 뿌린다."""
        from .version import __version__
        from .updater import repo_name, is_frozen
        out: list[tuple[str, bool, str]] = []
        repo = repo_name(self.cfg)
        out.append(("버전", True,
                    f"v{__version__}" + (" (exe)" if is_frozen() else " (소스 실행)")
                    + (f" / 업데이트 {repo}" if repo else " / 업데이트 저장소 미설정")))
        mock, real = load_credentials("MOCK"), load_credentials("REAL")
        out.append(("모의투자 키", mock.ok,
                    f"계좌 {mock.cano}-{mock.acnt_prdt_cd}" if mock.ok else ".env 확인 필요"))
        out.append(("실전투자 키", real.ok,
                    f"계좌 {real.cano}-{real.acnt_prdt_cd}" if real.ok else "미설정"))
        from .settings import gemini_key
        out.append(("Gemini API", bool(gemini_key()),
                    self.cfg.ai.model if gemini_key() else "미설정 (AI 기능 비활성)"))
        if self.quote_client:
            ok, msg = True, self.quote_client.label
        else:
            ok, msg = False, "시세 클라이언트 없음"
        out.append(("시세 연결", ok, msg))
        out.append(("주문 대상", True,
                    {"REAL": "실전계좌 (실제 돈)", "MOCK": "모의투자 계좌",
                     "PAPER": "로컬 가상계좌"}.get(self.broker.mode, "-")))
        out.append(("알림", self.cfg.notify.enabled and self.notifier.enabled,
                    "텔레그램 연결됨" if (self.cfg.notify.enabled and self.notifier.enabled)
                    else ("켜짐이나 토큰 없음" if self.cfg.notify.enabled else "꺼짐")))
        out.append(("종목 DB", self.master.count > 0,
                    f"{self.master.count:,}종목 검색 가능" if self.master.count
                    else "[종목] 탭에서 종목DB 갱신 필요"))
        s = self.store.stats()
        out.append(("수집 데이터", s["daily_candles"] > 0,
                    f"일봉 {s['daily_candles']:,} / 분봉 {s['minute_candles']:,} "
                    f"/ {s['symbols']}종목 / {s['db_mb']}MB"))
        return out

    def connection_test(self) -> str:
        lines = []
        if self.quote_client:
            try:
                q = self.quote_client.current_price(self.cfg.watchlist[0]
                                                    if self.cfg.watchlist else "005930")
                lines.append(f"[시세] OK - {q['symbol']} {q['price']:,}원 ({q['change_pct']:+.2f}%)")
            except Exception as e:
                lines.append(f"[시세] 실패 - {e}")
        else:
            lines.append("[시세] 클라이언트 없음 - .env에 KIS 키를 넣어주세요")
        ok, msg = self.broker.ping()
        lines.append(("[주문] " if ok else "[주문] 실패 - ") + msg)
        if not ok and self.broker.mode == "MOCK":
            lines.append("        └ 모의투자 서버가 응답하지 않을 때가 있습니다 "
                         "(설정/키 문제가 아닐 수 있음).")
            lines.append("          시세는 실전 도메인을 쓰므로 데이터 수집과 "
                         "백테스트는 계속 됩니다.")
            lines.append("          잠시 후 재시도하거나 [설정]에서 PAPER 모드로 "
                         "검증을 이어가세요.")
        if self.calendar:
            lines.append(f"[장상태] {self.calendar.describe()}")
        return "\n".join(lines)
