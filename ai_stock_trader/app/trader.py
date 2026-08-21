"""자동매매 엔진.

한 번의 루프에서 하는 일:
    1. 장이 열려 있나 확인
    2. 계좌 동기화 -> 리스크 상태 갱신 (일일손실/낙폭/쿨다운)
    3. 보유 포지션 청산 조건 확인 -> 매도
    4. 관심종목 전체를 전략별로 평가 (감시 스캔) -> 결과를 DB에 축적
    5. 그중 매수 신호가 난 종목만 리스크 관문에 태워 매수
    6. 자산곡선 기록

    4번은 리스크로 진입이 막혀 있어도 항상 돈다.
    "왜 안 샀는지"가 남아야 전략을 고칠 수 있기 때문이다.

엔진이 관리하는 포지션은 "엔진이 연 것"뿐이다.
사용자가 직접 산 종목은 건드리지 않는다 (원하면 GUI에서 편입할 수 있다).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, date

from .broker import Broker, OrderResult
from .collector import Collector
from .market import MarketCalendar, hhmm
from .risk import RiskManager, avg_turnover, norm_market
from .settings import AppConfig
from .storage import Store
from .strategies import Check, Position, Strategy, build

log = logging.getLogger(__name__)


class TradingEngine:
    def __init__(self, cfg: AppConfig, store: Store, broker: Broker,
                 calendar: MarketCalendar, collector: Collector | None,
                 on_event=None, ai=None, notifier=None):
        self.cfg = cfg
        self.store = store
        self.broker = broker
        self.cal = calendar
        self.collector = collector
        self.ai = ai
        self.notifier = notifier
        self.mode = broker.mode
        self.risk = RiskManager(cfg.risk, store, self.mode, cfg.cost)

        self.strategies: list[Strategy] = []
        for s in cfg.strategies:
            if s.get("enabled"):
                try:
                    self.strategies.append(build(s["name"], s.get("params") or {}))
                except ValueError as e:
                    log.warning("전략 로드 실패: %s", e)

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._on_event = on_event
        self._hist_cache: dict[tuple[str, str], tuple[str, list[dict]]] = {}
        self._last_daily_sync = ""
        self._last_review = ""
        self.last_error = ""
        self.last_loop: datetime | None = None
        self.account: dict = {}
        self.account_ts: datetime | None = None
        self.account_error = ""
        self._filled_this_tick = 0
        # 감시 스캔 - 진입이 막혀 있어도 계속 도는 관측 루프
        self.last_scan: dict[str, dict] = {}
        self.last_scan_ts: datetime | None = None
        self._eval_mark: dict[tuple, tuple] = {}
        self._last_block_msg = ("", 0.0)

    # -- 이벤트 -------------------------------------------------------------
    def emit(self, kind: str, msg: str, data: dict | None = None) -> None:
        log.log(logging.WARNING if kind in ("error", "risk") else logging.INFO, msg)
        if self.notifier is not None:
            try:
                self.notifier.push(kind, f"[{self.mode}] {msg}")
            except Exception:
                pass
        if self._on_event:
            try:
                self._on_event(kind, msg, data or {})
            except Exception:
                pass

    # -- 수명주기 -----------------------------------------------------------
    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def start(self) -> None:
        if self.running:
            return
        if not self.strategies:
            self.emit("error", "활성화된 전략이 없습니다. [전략] 탭에서 하나 이상 켜주세요.")
            return
        self._warn_missing_data()
        self._stop.clear()
        self.risk.resume()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="engine")
        self._thread.start()
        self.emit("engine", f"엔진 시작 ({self.mode} / 전략 "
                            f"{', '.join(s.label for s in self.strategies)})")

    def _warn_missing_data(self) -> None:
        """일봉이 부족한 관심종목을 미리 알려준다.

        전략은 워밍업(수십~90봉) 없이는 신호를 만들지 못한다.
        모르고 켜두면 "왜 아무것도 안 사지?" 가 된다.
        """
        need = max((s.warmup for s in self.strategies), default=60)
        thin = []
        for sym in self.cfg.watchlist:
            n = len(self.store.get_candles(sym, "D", limit=need + 5))
            if n < need:
                thin.append(f"{sym}({n}봉)")
        if thin:
            self.emit("warn",
                      f"일봉 부족으로 신호가 안 나올 종목 {len(thin)}개: "
                      f"{', '.join(thin[:8])}{' 외' if len(thin) > 8 else ''} "
                      f"- [데이터] 탭에서 일봉을 먼저 수집하세요 (필요 {need}봉)")

    def stop(self) -> None:
        self._stop.set()
        self.emit("engine", "엔진 정지 요청")

    def panic_close_all(self) -> None:
        """킬 스위치 - 엔진이 연 포지션 전량 시장가 청산."""
        self.risk.halt("사용자 킬스위치")
        for t in self.store.open_trades(self.mode):
            try:
                q = self.broker.quote(t["symbol"])
                self._close(t, q["price"], "킬스위치 강제청산")
            except Exception as e:
                self.emit("error", f"{t['symbol']} 청산 실패: {e}")

    # -- 메인 루프 ----------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.tick()
                self.last_error = ""
            except Exception as e:
                self.last_error = str(e)
                self.emit("error", f"루프 오류: {e}")
                log.exception("engine loop")
            wait = max(self.cfg.execution.loop_interval_sec - (time.monotonic() - started), 1)
            self._stop.wait(wait)
        self.emit("engine", "엔진 정지됨")

    def tick(self) -> None:
        now = datetime.now()
        self.last_loop = now
        session = self.cal.session(now)

        if session in ("closed", "pre_auction"):
            self._after_hours(now, session)
            return

        self._filled_this_tick = 0

        # 1) 계좌 - 못 읽으면 이번 루프는 건너뛴다 (엔진은 계속 돈다)
        try:
            acct = self.refresh_account()
        except Exception as e:
            self.emit("risk", f"계좌를 읽지 못해 이번 회차를 건너뜁니다: {str(e)[:120]}")
            return
        equity = float(acct["total_eval"] or 0)
        cash = float(acct["orderable_cash"] or acct["cash"] or 0)
        self.store.add_equity(self.mode, equity, cash,
                              float(acct.get("stock_eval") or 0),
                              equity - (self.risk.state.day_start_equity or equity))

        ok, why = self.risk.check_global(equity)
        if not ok:
            self._block_note(why)

        # 2) 강제 청산 시각
        fx = (self.cfg.execution.force_exit_at or "").strip()
        if fx and now.time() >= hhmm(fx):
            for t in self.store.open_trades(self.mode):
                try:
                    q = self.broker.quote(t["symbol"])
                    self._close(t, q["price"], f"장마감 강제청산 ({fx})")
                except Exception as e:
                    self.emit("error", f"{t['symbol']} 청산 실패: {e}")
            if self._filled_this_tick:
                self._settle_account()
            return

        # 3) 포지션 관리
        open_trades = self.store.open_trades(self.mode)
        held = {t["symbol"] for t in open_trades}
        for t in open_trades:
            try:
                self._manage(t, acct)
            except Exception as e:
                self.emit("error", f"{t['symbol']} 관리 실패: {e}")

        # 4) 감시 스캔 - 진입이 막혀 있어도 항상 돈다.
        #    막혔다고 안 보면 "왜 안 샀는지"에 대한 기록이 영영 남지 않는다.
        in_window = (hhmm(self.cfg.execution.entry_start) <= now.time()
                     <= hhmm(self.cfg.execution.entry_end))
        snaps = self.scan(held, blocked="" if ok else why, acct=acct)

        # 5) 신규 진입 - 스캔에서 이미 BUY 가 난 종목만 대상
        if not ok or not in_window:
            return
        for symbol in self.cfg.watchlist:
            if self._stop.is_set():
                break
            if symbol in held:
                continue
            snap = snaps.get(symbol) or {}
            hit = next((r for r in snap.get("strategies", [])
                        if r.get("verdict") == "BUY"), None)
            if not hit:
                continue
            try:
                self._enter(symbol, snap, hit, acct, len(held))
                if self.store.get_open_trade(symbol, self.mode):
                    held.add(symbol)
            except Exception as e:
                self.emit("error", f"{symbol} 진입 검토 실패: {e}")

        # 6) 체결이 있었으면 계좌를 다시 읽는다.
        #    안 그러면 대시보드가 매수 직전 금액에 머물러 "금액이 안 변한다"로 보인다.
        if self._filled_this_tick:
            self._settle_account()

    # -- 장외 처리 ----------------------------------------------------------
    def _after_hours(self, now: datetime, session: str) -> None:
        today = now.strftime("%Y-%m-%d")
        # 장 마감 후 1회: 일봉 갱신 + 분봉 수집 + AI 리뷰
        if now.hour >= 16 and self._last_daily_sync != today and self.collector:
            self._last_daily_sync = today
            self.emit("data", "장 마감 - 오늘 데이터 수집 시작")
            try:
                n = self.cal.prefetch_holidays()
                if n:
                    self.emit("data", f"휴장일 {n}건 캐시 갱신")
                self.collector.sync_daily_all(self.cfg.watchlist, 30,
                                              lambda m: self.emit("data", m))
                if self.cfg.data.collect_minute:
                    self.collector.sync_minute_all(self.cfg.watchlist,
                                                   lambda m: self.emit("data", m))
            except Exception as e:
                self.emit("error", f"데이터 수집 실패: {e}")

        if (now.hour >= 16 and self._last_review != today
                and self.ai and self.cfg.ai.enabled and self.cfg.ai.daily_review):
            self._last_review = today
            try:
                note = self.ai.daily_review(self.mode)
                if note:
                    self.emit("ai", "AI 일일 리뷰 생성됨")
            except Exception as e:
                self.emit("error", f"AI 리뷰 실패: {e}")

    # -- 히스토리 -----------------------------------------------------------
    def _hist(self, symbol: str, tf: str) -> list[dict]:
        today = date.today().isoformat()
        key = (symbol, tf)
        cached = self._hist_cache.get(key)

        if tf == "D":
            if cached and cached[0] == today:
                return cached[1]
            bars = self.store.get_candles(symbol, "D", limit=500)
            bars = [b for b in bars if b["ts"] < today]     # 오늘 봉 제외
            if len(bars) < 60 and self.collector:
                self.collector.sync_daily(symbol, self.cfg.data.daily_history_days)
                bars = [b for b in self.store.get_candles(symbol, "D", limit=500)
                        if b["ts"] < today]
            self._hist_cache[key] = (today, bars)
            return bars

        # 1분봉은 장중 계속 갱신
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        if cached and cached[0] == stamp:
            return cached[1]
        if self.collector:
            try:
                self.collector.sync_minute_day(symbol, datetime.now().strftime("%H%M%S"),
                                               max_calls=3)
            except Exception as e:
                log.debug("분봉 갱신 실패: %s", e)
        bars = self.store.get_candles(symbol, "1m", limit=2000)
        self._hist_cache[key] = (stamp, bars)
        return bars

    def _strategy_for(self, name: str) -> Strategy | None:
        for s in self.strategies:
            if s.name == name:
                return s
        return None

    # -- 포지션 관리 --------------------------------------------------------
    def _manage(self, trade: dict, acct: dict) -> None:
        symbol = trade["symbol"]
        strat = self._strategy_for(trade.get("strategy") or "")
        q = self.broker.quote(symbol)
        price = float(q["price"])
        if price <= 0:
            return
        self.store.add_quote(symbol, price, q.get("change_pct", 0), q.get("volume", 0))

        pos = Position(symbol, float(trade["entry_price"]), int(trade["qty"]),
                       float(trade.get("stop_price") or 0),
                       float(trade.get("target_price") or 0),
                       float(trade.get("peak_price") or trade["entry_price"]),
                       0, trade.get("strategy") or "")

        # 하드 스톱/타깃은 전략 없이도 항상 작동한다
        if pos.stop > 0 and price <= pos.stop:
            self._close(trade, price, f"손절 (기준 {pos.stop:,.0f})")
            return
        if pos.target > 0 and price >= pos.target:
            self._close(trade, price, f"익절 (기준 {pos.target:,.0f})")
            return

        if strat is None:
            return

        hist = self._hist(symbol, strat.timeframe)
        if hist:
            try:
                entry_day = (trade.get("entry_ts") or "")[:10]
                pos.bars_held = sum(1 for b in hist if b["ts"][:10] > entry_day) \
                    if strat.timeframe == "D" else 0
                sig = strat.exit(hist, price, pos,
                                 {"now_hhmm": datetime.now().strftime("%H:%M")})
                if sig.side == "SELL":
                    self._close(trade, price, sig.reason)
                    return
            except Exception as e:
                log.debug("청산 판단 실패 %s: %s", symbol, e)

        # 트레일링 스톱 갱신
        from . import indicators as ind
        a = ind.last(ind.atr(hist, 14)) if hist else None
        new_stop = strat.update_stop(pos, price, a)
        if new_stop > (trade.get("stop_price") or 0):
            self.store.update_trade_levels(trade["id"], stop=new_stop, peak=pos.peak)
            self.emit("position", f"{symbol} 트레일링 스톱 상향 -> {new_stop:,.0f}")

    def _close(self, trade: dict, price: float, reason: str) -> OrderResult | None:
        symbol = trade["symbol"]
        if not self.risk.lock(symbol):
            return None
        try:
            qty = int(trade["qty"])
            avail = (self.account.get("holdings", {}).get(symbol, {}) or {}).get("sellable", qty)
            qty = min(qty, int(avail) or qty)
            if qty <= 0:
                self.emit("error", f"{symbol} 매도가능 수량 0 - 청산 보류")
                return None

            r = self.broker.sell(symbol, qty, price)
            oid = self.store.add_order(self.mode, symbol, "SELL", qty, price,
                                       "-", r.odno, r.org_no,
                                       "FILLED" if r.ok else "FAILED", r.message,
                                       trade.get("strategy") or "")
            if not r.ok:
                self.emit("error", f"{symbol} 매도 실패: {r.message}")
                return r
            self.store.add_fill(oid, symbol, "SELL", r.filled_qty, r.avg_price, r.fee, r.tax)
            self._filled_this_tick += 1
            self.broker.invalidate_account()
            closed = self.store.close_trade(trade["id"], r.avg_price, reason, r.fee, r.tax)
            if closed:
                pnl = closed["pnl"]
                self.risk.on_trade_closed(pnl)
                sign = "+" if pnl >= 0 else ""
                self.emit("trade",
                          f"매도 {symbol} {r.filled_qty}주 @{r.avg_price:,.0f} | {reason} | "
                          f"손익 {sign}{pnl:,.0f}원 ({sign}{closed['pnl_pct']:.2f}%)")
            return r
        finally:
            self.risk.unlock(symbol)

    def _sector_exposure(self, sector: str, acct: dict, equity: float) -> float:
        """지금 이 업종에 자산의 몇 %가 들어가 있는가."""
        if not sector or equity <= 0:
            return 0.0
        amt = 0.0
        for sym, h in (acct.get("holdings") or {}).items():
            if self.store.get_sector(sym) == sector:
                amt += float(h.get("eval_amt") or 0)
        return amt / equity * 100

    # -- 감시 스캔 ----------------------------------------------------------
    def scan(self, held: set[str] | None = None, blocked: str = "",
             acct: dict | None = None) -> dict[str, dict]:
        """관심종목 전체를 전략별로 평가한다.

        매수 여부와 무관하게 항상 돈다. 여기서 나온 것이
        화면의 '감시 현황'이고, DB(watch_eval)에 쌓이는 관측 데이터다.
        """
        if held is None:
            held = {t["symbol"] for t in self.store.open_trades(self.mode)}
        if acct is None:
            acct = self.account or {}
        equity = float(acct.get("total_eval") or 0)
        cash = float(acct.get("orderable_cash") or acct.get("cash") or 0)
        now = datetime.now()
        out: dict[str, dict] = {}
        rows: list[dict] = []
        buys: list[str] = []
        nears: list[str] = []
        gated: list[str] = []

        for symbol in self.cfg.watchlist:
            if self._stop.is_set():
                break
            try:
                snap = self._evaluate(symbol, now, held, blocked, equity, cash)
            except Exception as e:
                snap = {"symbol": symbol, "ts": now.strftime("%H:%M:%S"),
                        "price": 0.0, "change_pct": 0.0, "atr_pct": 0.0,
                        "error": str(e)[:100], "strategies": []}
            out[symbol] = snap
            for r in snap.get("strategies", []):
                if r["verdict"] == "BUY":
                    buys.append(f"{symbol}/{r['label']}")
                elif r["verdict"] == "GATED":
                    gated.append(f"{symbol}/{r['label']}")
                elif r["verdict"] == "NEAR":
                    nears.append(f"{symbol}/{r['label']} {r['gap_pct']:+.2f}%")
                if self._should_record(symbol, r, now):
                    rows.append({
                        "ts": now.strftime("%Y-%m-%d %H:%M:%S"), "mode": self.mode,
                        "symbol": symbol, "strategy": r["strategy"],
                        "verdict": r["verdict"], "score": r["score"],
                        "gap_pct": r["gap_pct"], "price": snap.get("price") or 0,
                        "atr_pct": r.get("atr_pct") or 0,
                        "change_pct": snap.get("change_pct") or 0,
                        "passed": r["passed"], "total": r["total"],
                        "detail": r["checks"], "blocked_by": blocked,
                    })

        self.last_scan = out
        self.last_scan_ts = now
        if rows:
            try:
                self.store.add_evals(rows)
            except Exception as e:
                log.debug("스캔 기록 실패: %s", e)

        msg = (f"감시 {len(out)}종목 x 전략 {len(self.strategies)}개 평가 - "
               f"신호 {len(buys)} / 근접 {len(nears)}")
        if gated:
            msg += f" / 관문차단 {len(gated)}"
        if buys:
            msg += f" | 신호: {', '.join(buys[:4])}"
        elif gated:
            msg += f" | 차단: {', '.join(gated[:3])}"
        elif nears:
            msg += f" | 가장 가까움: {', '.join(nears[:3])}"
        self.emit("scan", msg, {"buys": buys, "nears": nears, "gated": gated})
        return out

    def _evaluate(self, symbol: str, now: datetime, held: set[str],
                  blocked: str, equity: float = 0, cash: float = 0) -> dict:
        """한 종목을 모든 전략으로 평가한 스냅샷."""
        q = self.broker.quote(symbol)
        price = float(q["price"])
        if price > 0:
            self.store.add_quote(symbol, price, q.get("change_pct", 0), q.get("volume", 0))
        sector = (q.get("sector") or "").strip()
        if sector:
            self.store.set_sector(symbol, sector)

        market = norm_market(q.get("market") or "")
        turnover = self._turnover(symbol)

        snap = {
            "symbol": symbol,
            "name": self._name(symbol),
            "ts": now.strftime("%H:%M:%S"),
            "price": price,
            "change_pct": float(q.get("change_pct") or 0),
            "volume": float(q.get("volume") or 0),
            "sector": sector,
            "held": symbol in held,
            "halt": q.get("halt") == "Y",
            "warn": str(q.get("market_warn", "00")) not in ("00", ""),
            "blocked": blocked,
            "atr_pct": 0.0,
            "market": market,
            "turnover": turnover,
            "strategies": [],
        }
        ctx = {"today_open": float(q.get("open") or price),
               "now_hhmm": now.strftime("%H:%M")}

        for strat in self.strategies:
            r = {"strategy": strat.name, "label": strat.label, "verdict": "WAIT",
                 "score": 0.0, "gap_pct": 0.0, "passed": 0, "total": 0,
                 "checks": [], "reason": "", "atr_pct": 0.0,
                 "stop_pct": 0.0, "target_pct": 0.0, "has_gap": False,
                 "qty": 0, "size_note": "",
                 "cost_pct": 0.0, "edge_ratio": 0.0, "gate": ""}
            hist = self._hist(symbol, strat.timeframe)
            if len(hist) < strat.warmup:
                r["verdict"] = "NODATA"
                r["reason"] = f"{strat.timeframe} {len(hist)}/{strat.warmup}봉 - 데이터 부족"
                snap["strategies"].append(r)
                continue

            try:
                checks = strat.checklist(hist, price, ctx)
                r["checks"] = [c.to_dict() for c in checks]
                r["total"] = len(checks)
                r["passed"] = sum(1 for c in checks if c.ok)
                r["score"] = (r["passed"] / r["total"]) if r["total"] else 0.0
                gap = strat.entry_gap_pct(hist, price, ctx)
                r["has_gap"] = gap is not None
                r["gap_pct"] = float(gap) if gap is not None else 0.0
                eff = strat.effective_pcts(hist, price)
                r["atr_pct"] = eff["atr_pct"]
                r["stop_pct"] = eff["stop_pct"]
                r["target_pct"] = eff["target_pct"]
                snap["atr_pct"] = max(snap["atr_pct"], eff["atr_pct"])

                # 이 거래가 거래비용을 이길 폭을 가지고 있는가.
                # 신호가 난 뒤가 아니라 나기 전에 계산해서 화면에 드러낸다.
                _edge, cpct, ratio = self.risk.edge_ratio(
                    price, eff["stop_pct"], eff["target_pct"], market)
                r["cost_pct"] = cpct
                r["edge_ratio"] = ratio

                # 지금 신호가 나면 실제로 몇 주나 살 수 있는가.
                # 손절폭이 넓어질수록 수량은 줄어든다. 0주면 신호가 나도 못 산다 -
                # 그 사실을 신호가 난 뒤가 아니라 미리 보여준다.
                if equity > 0 and eff["stop_pct"] > 0:
                    try:
                        q0, note = self.risk.position_size(
                            price, price * (1 - eff["stop_pct"] / 100), equity, cash)
                        r["qty"] = q0
                        r["size_note"] = note
                    except Exception:
                        pass

                sig = strat.entry(hist, price, ctx)
                if sig.side == "BUY":
                    # 전략은 사자고 하지만 시스템이 막을 수 있다.
                    # 막힌 사실과 사유를 BUY 와 구분해서 남긴다.
                    gate_ok, gate_why = self._entry_gates(
                        price, eff, turnover, market)
                    if not gate_ok:
                        r["verdict"] = "GATED"
                        r["gate"] = gate_why
                        r["reason"] = f"{sig.reason} / 관문차단: {gate_why}"
                    else:
                        r["verdict"] = "BUY"
                        r["reason"] = sig.reason
                        r["_sig"] = sig
                        r["_strategy"] = strat
                elif snap["held"]:
                    r["verdict"] = "HELD"
                    r["reason"] = "이미 보유 중"
                else:
                    miss = [c.label for c in checks if not c.ok]
                    near = (len(miss) <= 1) or (gap is not None and abs(gap) <= 1.0)
                    r["verdict"] = "NEAR" if near else "WAIT"
                    r["reason"] = sig.reason or (
                        f"미충족: {', '.join(miss[:3])}" if miss else "조건 대기")
            except Exception as e:
                r["verdict"] = "ERROR"
                r["reason"] = str(e)[:100]
                log.debug("평가 실패 %s/%s: %s", symbol, strat.name, e)
            snap["strategies"].append(r)
        return snap

    def _entry_gates(self, price: float, eff: dict, turnover: float,
                     market: str) -> tuple[bool, str]:
        """전략 신호가 나도 이 관문을 못 넘으면 주문하지 않는다."""
        ok, why = self.risk.check_liquidity(price, turnover)
        if not ok:
            return False, why
        return self.risk.check_cost_edge(price, eff["stop_pct"],
                                         eff["target_pct"], market)

    def _turnover(self, symbol: str) -> float:
        """평균 거래대금. 일봉 기준이라 장 초반에도 값이 흔들리지 않는다."""
        if not float(getattr(self.cfg.risk, "min_turnover_amount", 0) or 0):
            return 0.0
        try:
            return avg_turnover(self._hist(symbol, "D"),
                                int(getattr(self.cfg.risk, "turnover_lookback", 20) or 20))
        except Exception:
            return 0.0

    def _name(self, symbol: str) -> str:
        try:
            return self.store.stock_name(symbol) or symbol
        except Exception:
            return symbol

    def _should_record(self, symbol: str, r: dict, now: datetime) -> bool:
        """매 30초마다 전부 적재하면 하루 수만 행이 된다.

        상태가 바뀐 순간과 10분 간격 스냅샷만 남긴다.
        추세를 보기에는 충분하고 DB는 가볍게 유지된다.
        """
        key = (symbol, r["strategy"])
        cur = (r["verdict"], r["passed"], round(r["gap_pct"], 1))
        prev = self._eval_mark.get(key)
        if prev is None or prev[0] != cur:
            self._eval_mark[key] = (cur, now)
            return True
        if (now - prev[1]).total_seconds() >= 600:
            self._eval_mark[key] = (cur, now)
            return True
        return False

    def _block_note(self, why: str) -> None:
        """같은 차단 사유로 로그를 도배하지 않는다 (5분에 한 번)."""
        last, ts = self._last_block_msg
        nowm = time.monotonic()
        if why == last and nowm - ts < 300:
            return
        self._last_block_msg = (why, nowm)
        self.emit("risk", f"신규진입 차단: {why} (감시 스캔은 계속 돕니다)")

    # -- 진입 ---------------------------------------------------------------
    def _enter(self, symbol: str, snap: dict, hit: dict,
               acct: dict, open_count: int) -> None:
        """스캔에서 BUY 가 난 종목을 리스크 관문에 태우고 주문한다."""
        strat: Strategy | None = hit.get("_strategy")
        sig = hit.get("_sig")
        if strat is None or sig is None:
            return
        price = float(snap.get("price") or 0)
        if price <= 0 or snap.get("halt"):
            return
        if snap.get("warn"):
            self.emit("risk", f"{symbol} 시장경보 상태 - 진입 제외")
            return

        equity = float(acct["total_eval"] or 0)
        cash = float(acct["orderable_cash"] or acct["cash"] or 0)
        sector = snap.get("sector") or ""
        q = {"price": price, "change_pct": snap.get("change_pct", 0),
             "volume": snap.get("volume", 0)}

        sid = self.store.add_signal(symbol, strat.name, "BUY", price,
                                    sig.strength, sig.reason, self.mode)

        ok, why = self.risk.check_entry(
            symbol, equity, cash, open_count, sector,
            self._sector_exposure(sector, acct, equity),
            price=price, stop_pct=hit.get("stop_pct") or 0,
            target_pct=hit.get("target_pct") or 0,
            turnover=snap.get("turnover") or 0,
            market=snap.get("market") or "KOSPI")
        if not ok:
            self.store.mark_signal(sid, False, why)
            self.emit("risk", f"{symbol} 매수신호 차단: {why}")
            return

        qty, note = self.risk.position_size(price, sig.stop, equity, cash, sig.strength)
        if qty <= 0:
            self.store.mark_signal(sid, False, note)
            self.emit("risk", f"{symbol} 수량 산출 실패: {note}")
            return

        # AI 리스크 필터 - 진입을 막을 수만 있고, 만들 수는 없다
        if self.ai and self.cfg.ai.enabled and self.cfg.ai.veto_filter:
            try:
                veto, vreason = self.ai.veto(symbol, q, sig.reason)
                if veto:
                    self.store.mark_signal(sid, False, f"AI veto: {vreason}")
                    self.emit("ai", f"{symbol} AI 진입거부: {vreason}")
                    return
            except Exception as e:
                log.debug("AI veto 실패: %s", e)

        if not self.risk.lock(symbol):
            self.store.mark_signal(sid, False, "중복 주문 방지")
            return
        try:
            self.emit("signal", f"{symbol} 매수신호 [{strat.label}] {sig.reason} | {note}")
            r = self.broker.buy(symbol, qty, price)
            oid = self.store.add_order(self.mode, symbol, "BUY", qty, price, "-",
                                       r.odno, r.org_no,
                                       "FILLED" if r.ok else "FAILED",
                                       r.message, strat.name, sid)
            if not r.ok:
                self.store.mark_signal(sid, False, r.message)
                self.emit("error", f"{symbol} 매수 실패: {r.message}")
                return
            self.store.add_fill(oid, symbol, "BUY", r.filled_qty, r.avg_price,
                                r.fee, r.tax)
            stop = sig.stop if sig.stop > 0 else r.avg_price * 0.97
            target = sig.target if sig.target > 0 else 0
            self.store.open_trade(self.mode, symbol, strat.name, r.avg_price,
                                  r.filled_qty, r.fee, stop, target)
            self._filled_this_tick += 1
            self.broker.invalidate_account()
            self.store.mark_signal(sid, True)
            self.emit("trade",
                      f"매수 {symbol} {r.filled_qty}주 @{r.avg_price:,.0f} "
                      f"| 손절 {stop:,.0f} / 목표 {target:,.0f}")
        finally:
            self.risk.unlock(symbol)


    # -- 외부 보유 편입 ------------------------------------------------------
    def adopt_holdings(self) -> int:
        """계좌에 있지만 엔진이 모르는 보유종목을 엔진 관리로 편입."""
        acct = self.broker.account()
        n = 0
        strat = self.strategies[0] if self.strategies else None
        for sym, h in (acct.get("holdings") or {}).items():
            if self.store.get_open_trade(sym, self.mode):
                continue
            avg = float(h["avg_price"] or h["price"])
            stop = avg * (1 - self.cfg.risk.max_loss_per_trade_pct * 4 / 100)
            self.store.open_trade(self.mode, sym, strat.name if strat else "adopted",
                                  avg, int(h["qty"]), 0, stop, 0)
            n += 1
            self.emit("position", f"{sym} {h['qty']}주 엔진 편입 (손절 {stop:,.0f})")
        return n

    def _settle_account(self) -> None:
        """체결 직후 잔고를 다시 읽어 화면/자산곡선에 반영한다."""
        try:
            acct = self.refresh_account()
            equity = float(acct.get("total_eval") or 0)
            self.store.add_equity(
                self.mode, equity, float(acct.get("orderable_cash") or acct.get("cash") or 0),
                float(acct.get("stock_eval") or 0),
                equity - (self.risk.state.day_start_equity or equity))
        except Exception as e:
            log.debug("체결 후 잔고 갱신 실패: %s", e)

    # -- 계좌 갱신 ----------------------------------------------------------
    def refresh_account(self) -> dict:
        """엔진이 멈춰 있어도 계좌를 조회한다.

        예전에는 tick() 안에서만 계좌를 읽어서, 자동매매를 시작하기 전에는
        대시보드 금액이 전부 0으로 보였다.
        """
        try:
            acct = self.broker.account()
        except Exception as e:
            self.account_error = str(e)
            if not self.account:
                raise
            return self.account
        self.account_error = getattr(self.broker, "account_error", "")
        self.account = acct
        self.account_ts = datetime.now()
        try:
            self.risk.roll_day(float(acct.get("total_eval") or 0))
        except Exception as e:
            log.debug("리스크 일자 갱신 실패: %s", e)
        return acct

    # -- 상태 요약 ----------------------------------------------------------
    def status(self) -> dict:
        acct = self.account or {}
        equity = float(acct.get("total_eval") or 0)
        rs = self.risk.state
        return {
            "running": self.running,
            "mode": self.mode,
            "session": self.cal.describe(),
            "market_open": self.cal.is_open(),
            "last_loop": self.last_loop.strftime("%H:%M:%S") if self.last_loop else "-",
            "account_ts": self.account_ts.strftime("%H:%M:%S") if self.account_ts else "-",
            "account_error": self.account_error,
            "account_stale": bool(self.account_error and self.account_ts),
            "holdings": (self.account or {}).get("holdings", {}),
            "equity": equity,
            "cash": float(acct.get("cash") or 0),
            "day_pnl": equity - (rs.day_start_equity or equity),
            "realized_today": self.store.realized_pnl_today(self.mode),
            "open_positions": len(self.store.open_trades(self.mode)),
            "orders_today": self.store.orders_today(self.mode),
            "consecutive_losses": self.store.consecutive_losses(self.mode),
            "risk": rs.to_dict(equity),
            "strategies": [s.label for s in self.strategies],
            "error": self.last_error,
        }
