"""단일 GUI 애플리케이션.

exe 하나로 끝나야 한다는 요구에 맞춰, 대시보드/전략/리스크/백테스트/데이터/AI/설정을
한 창의 탭으로 묶었다. 무거운 작업(수집, 백테스트, 연결테스트)은 전부 별도 스레드에서
돌리고 큐로 결과만 받아 UI를 갱신한다.
"""
from __future__ import annotations

import json
import queue
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

import customtkinter as ctk
from dataclasses import asdict

from .core import AppCore
from .settings import icon_file, icon_png, save_env
from .strategies import REGISTRY, default_params

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

BG = "#1a1a1a"
CARD = "#242424"
UP = "#ff5252"        # 국내 관행: 상승 빨강
DOWN = "#4d9fff"
MUTED = "#8a8a8a"
OK = "#4caf50"
WARN = "#ffb300"
BAD = "#ef5350"


def apply_icon(win, default: bool = False) -> None:
    """창 아이콘을 exe 아이콘과 같은 파일로 맞춘다.

    이걸 안 하면 작업표시줄에는 exe 아이콘, 창 제목줄에는 Tk 기본 깃털
    아이콘이 나와서 서로 달라 보인다.
    default=True 로 주면 이후 열리는 하위 창에도 같은 아이콘이 적용된다.
    """
    try:
        path = icon_file()
        if path.exists():
            if default:
                win.iconbitmap(default=str(path))
            win.iconbitmap(str(path))
    except Exception:
        pass        # 아이콘 때문에 프로그램이 안 뜨면 안 된다
    try:
        # iconbitmap 은 작은 크기만 읽어 Alt-Tab 등에서 흐리게 확대된다.
        # 큰 PNG 를 함께 걸어야 선명해진다. 참조를 붙들어야 GC 로 사라지지 않는다.
        png = icon_png()
        if png.exists():
            img = tk.PhotoImage(file=str(png), master=win)
            win._icon_ref = img
            win.iconphoto(bool(default), img)
    except Exception:
        pass


def money(v) -> str:
    try:
        return f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return "-"


def pct(v) -> str:
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return "-"


def num(v, signed: bool = False) -> str:
    """국내주식은 정수, 해외주식은 소수점이 의미가 있으므로 나눠서 표기."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "-"
    if f == int(f) or abs(f) >= 1000:
        return f"{f:+,.0f}" if signed else f"{f:,.0f}"
    return f"{f:+,.2f}" if signed else f"{f:,.2f}"


def extra_fmt(label: str, value) -> str:
    """순위 API의 부가 컬럼을 사람이 읽는 단위로."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return ""
    if not label or f == 0:
        return ""
    if label == "시가총액(억)":
        return f"시총 {f / 10000:,.1f}조" if f >= 10000 else f"시총 {f:,.0f}억"
    if label == "거래증가율":
        return f"거래증가 {f:,.1f}%"
    if label == "거래대금":
        return f"대금 {f:,.0f}"
    return f"{label} {f:,.0f}"


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        from .version import __version__
        self.title(f"KIS 자동매매 시스템  v{__version__}")
        apply_icon(self, default=True)
        self._fit_to_screen()

        self._q: queue.Queue = queue.Queue()
        self._busy = False
        self._acct_busy = False
        self._acct_at = None
        self._lab_tick = 0
        self._lab_busy = False
        self.core = AppCore(on_event=self._event)

        self._style_tree()
        self._build_header()
        self._build_tabs()

        self.after(50, self._maximize)
        if self.core.cfg.update.check_on_start:
            self.after(3000, lambda: self._check_update(silent=True))
        self.after(300, self._pump)
        self.after(100, lambda: self._pull_account(force=True))
        self.after(1000, self._refresh)
        self.protocol("WM_DELETE_WINDOW", self._quit)
        self._log("시스템 준비 완료. [설정] 탭에서 연결을 먼저 점검하세요.")
        if self.core.status_msg:
            self._log(f"[알림] {self.core.status_msg}")

    # ==================================================================
    # 공통
    # ==================================================================
    # 표와 카드가 잘리지 않는 최소 논리 크기
    NEED_W, NEED_H = 1340, 850

    def _fit_to_screen(self) -> None:
        """고DPI 화면에서 창이 잘리지 않게 크기와 위젯 배율을 맞춘다.

        customtkinter는 위젯/좌표에 화면 DPI 배율을 자동으로 한 번 곱한다.
        150% 배율 노트북에서는 그 상태로 기본값을 쓰면 우측 버튼이 화면 밖으로 나간다.
        """
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()

        # 작은 화면에서는 위젯을 줄여야 8개 탭과 표가 다 들어간다.
        # (customtkinter가 윈도우 DPI 배율을 이미 한 번 곱하므로 여기선 보정만 한다)
        if sh <= 800 or sw <= 1400:
            ctk.set_widget_scaling(0.75)
        elif sh <= 1080:
            ctk.set_widget_scaling(0.85)

        self.geometry(f"{self.NEED_W}x{self.NEED_H}")   # 최대화 해제했을 때 크기
        self.minsize(940, 600)

    def _maximize(self) -> None:
        """시작 시 최대화.

        배율/해상도가 어떻든 화면 밖으로 나가지 않는 가장 확실한 방법.
        단, 위젯을 다 만든 뒤에 걸어야 한다. 생성 도중의 geometry 호출이 zoom을 풀어버린다.
        """
        try:
            self.state("zoomed")
        except tk.TclError:
            try:
                self.attributes("-zoomed", True)
            except tk.TclError:
                pass

    def _event(self, kind: str, msg: str, data: dict) -> None:
        self._q.put((kind, msg, data))

    def _pump(self) -> None:
        try:
            while True:
                kind, msg, _d = self._q.get_nowait()
                self._log(msg, kind)
        except queue.Empty:
            pass
        self.after(300, self._pump)

    def _log(self, msg: str, kind: str = "info") -> None:
        tag = {"error": "[오류]", "risk": "[리스크]", "trade": "[체결]",
               "signal": "[신호]", "ai": "[AI]", "data": "[데이터]",
               "engine": "[엔진]", "warn": "[알림]", "position": "[포지션]"}.get(kind, "")
        box = getattr(self, "log_box", None)
        if box is None:
            return
        box.configure(state="normal")
        box.insert("end", f"{datetime.now():%H:%M:%S} {tag} {msg}\n")
        box.see("end")
        box.configure(state="disabled")

    def _thread(self, fn, *a, **kw) -> None:
        if self._busy:
            messagebox.showinfo("작업 중", "이미 실행 중인 작업이 있습니다.")
            return

        def run():
            self._busy = True
            try:
                fn(*a, **kw)
            except Exception as e:
                self._event("error", f"{fn.__name__} 실패: {e}", {})
            finally:
                self._busy = False
        threading.Thread(target=run, daemon=True).start()

    def _style_tree(self) -> None:
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("Treeview", background=CARD, foreground="#e8e8e8",
                     fieldbackground=CARD, borderwidth=0, rowheight=26)
        st.configure("Treeview.Heading", background="#2f2f2f", foreground="#cfcfcf",
                     borderwidth=0, relief="flat")
        st.map("Treeview", background=[("selected", "#3a5f8a")])
        st.map("Treeview.Heading", background=[("active", "#3a3a3a")])

    @staticmethod
    def _tree(parent, cols: list[tuple[str, str, int]], height: int = 8) -> ttk.Treeview:
        t = ttk.Treeview(parent, columns=[c[0] for c in cols],
                         show="headings", height=height)
        for key, title, w in cols:
            t.heading(key, text=title)
            t.column(key, width=w, anchor="e" if w < 110 else "w")
        return t

    # ==================================================================
    # 헤더
    # ==================================================================
    def _build_header(self) -> None:
        h = ctk.CTkFrame(self, height=64, fg_color=CARD)
        h.pack(fill="x", padx=10, pady=(10, 6))

        self.lbl_mode = ctk.CTkLabel(h, text="MODE", font=("", 18, "bold"))
        self.lbl_mode.pack(side="left", padx=(16, 12))

        self.lbl_session = ctk.CTkLabel(h, text="", text_color=MUTED)
        self.lbl_session.pack(side="left", padx=6)

        self.lbl_engine = ctk.CTkLabel(h, text="● 정지", text_color=MUTED,
                                       font=("", 13, "bold"))
        self.lbl_engine.pack(side="left", padx=16)

        self.btn_panic = ctk.CTkButton(h, text="긴급 전량청산", width=120,
                                       fg_color="#7a1f1f", hover_color="#9b2626",
                                       command=self._panic)
        self.btn_panic.pack(side="right", padx=(6, 16))
        self.btn_run = ctk.CTkButton(h, text="자동매매 시작", width=140,
                                     command=self._toggle)
        self.btn_run.pack(side="right", padx=6)

    def _build_tabs(self) -> None:
        self.tabs = ctk.CTkTabview(self, anchor="nw")
        self.tabs.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        for name in ("대시보드", "종목", "전략", "리스크", "운용랩",
                     "백테스트", "데이터", "AI", "설정"):
            self.tabs.add(name)
        self._tab_dashboard(self.tabs.tab("대시보드"))
        self._tab_stocks(self.tabs.tab("종목"))
        self._tab_strategy(self.tabs.tab("전략"))
        self._tab_risk(self.tabs.tab("리스크"))
        self._tab_lab(self.tabs.tab("운용랩"))
        self._tab_backtest(self.tabs.tab("백테스트"))
        self._tab_data(self.tabs.tab("데이터"))
        self._tab_ai(self.tabs.tab("AI"))
        self._tab_settings(self.tabs.tab("설정"))
        self.tabs.set("대시보드")      # add() 순서상 마지막 탭이 선택된 채로 뜬다

    # ==================================================================
    # 대시보드
    # ==================================================================
    def _tab_dashboard(self, p) -> None:
        cards = ctk.CTkFrame(p, fg_color="transparent")
        cards.pack(fill="x", pady=(6, 8))
        self.cards = {}
        for key, title in (("equity", "총 평가금액"), ("cash", "주문가능 현금"),
                           ("day", "당일 손익"), ("pos", "보유 / 오늘 주문")):
            f = ctk.CTkFrame(cards, fg_color=CARD, corner_radius=8)
            f.pack(side="left", fill="both", expand=True, padx=5)
            ctk.CTkLabel(f, text=title, text_color=MUTED, font=("", 12)).pack(pady=(12, 2))
            v = ctk.CTkLabel(f, text="-", font=("", 22, "bold"))
            v.pack(pady=(0, 4))
            s = ctk.CTkLabel(f, text="", text_color=MUTED, font=("", 11))
            s.pack(pady=(0, 12))
            self.cards[key] = (v, s)

        self.lbl_risk = ctk.CTkLabel(p, text="", text_color=MUTED, anchor="w",
                                     justify="left", font=("", 12))
        self.lbl_risk.pack(fill="x", padx=8, pady=(0, 6))

        body = ctk.CTkFrame(p, fg_color="transparent")
        body.pack(fill="both", expand=True)

        col = ctk.CTkFrame(body, fg_color="transparent")
        col.pack(side="left", fill="both", expand=True, padx=(4, 5))

        left = ctk.CTkFrame(col, fg_color=CARD, corner_radius=8)
        left.pack(fill="both", expand=True, pady=(0, 6))
        ctk.CTkLabel(left, text="보유 포지션 (엔진 관리)", anchor="w",
                     font=("", 13, "bold")).pack(fill="x", padx=12, pady=(10, 4))
        self.tv_pos = self._tree(left, [
            ("sym", "종목", 80), ("qty", "수량", 60), ("avg", "평단", 90),
            ("cur", "현재가", 90), ("stop", "손절가", 90), ("target", "목표가", 90),
            ("pnl", "평가손익", 100), ("pct", "수익률", 80), ("st", "전략", 120)], 6)
        self.tv_pos.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        row = ctk.CTkFrame(left, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(0, 10))
        ctk.CTkButton(row, text="계좌 보유종목 엔진에 편입", width=200,
                      fg_color="#3a3a3a", hover_color="#4a4a4a",
                      command=self._adopt).pack(side="left")
        ctk.CTkButton(row, text="새로고침", width=90, fg_color="#3a3a3a",
                      hover_color="#4a4a4a", command=self._refresh_now).pack(side="left", padx=6)

        # --- 감시 현황: 엔진이 지금 무엇을 보고 있는가 ---
        watch = ctk.CTkFrame(col, fg_color=CARD, corner_radius=8)
        watch.pack(fill="both", expand=True)
        wh = ctk.CTkFrame(watch, fg_color="transparent")
        wh.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(wh, text="감시 현황 (실시간 종목 분석)", anchor="w",
                     font=("", 13, "bold")).pack(side="left")
        self.lbl_scan = ctk.CTkLabel(wh, text="엔진을 시작하면 관심종목을 전략별로 훑습니다.",
                                     text_color=MUTED, font=("", 11))
        self.lbl_scan.pack(side="left", padx=10)
        ctk.CTkButton(wh, text="지금 한 번 훑기", width=110, height=26,
                      fg_color="#3a3a3a", hover_color="#4a4a4a",
                      command=self._scan_once).pack(side="right")

        self.tv_watch_live = self._tree(watch, [
            ("sym", "종목", 130), ("price", "현재가", 85), ("chg", "등락", 70),
            ("atr", "변동성", 70), ("st", "전략", 110), ("vd", "판정", 60),
            ("cond", "조건", 60), ("gap", "트리거까지", 90),
            ("lv", "손절/목표", 100), ("edge", "비용대비", 75),
            ("qty", "예상수량", 75),
            ("why", "상태", 260)], 9)
        self.tv_watch_live.pack(fill="both", expand=True, padx=10, pady=(0, 4))
        self.tv_watch_live.tag_configure("buy", foreground=OK)
        self.tv_watch_live.tag_configure("near", foreground=WARN)
        self.tv_watch_live.tag_configure("held", foreground="#7fb3ff")
        self.tv_watch_live.tag_configure("dim", foreground=MUTED)
        self.tv_watch_live.tag_configure("gated", foreground="#c98a5a")
        self.tv_watch_live.bind("<Double-1>", lambda _e: self._open_watch_detail())
        ctk.CTkLabel(watch, text="행을 더블클릭하면 조건별 판정을 자세히 볼 수 있습니다. "
                                 "변동성(ATR%)이 큰 종목일수록 손절/목표 폭이 자동으로 넓어집니다.",
                     text_color=MUTED, anchor="w",
                     font=("", 10)).pack(fill="x", padx=12, pady=(0, 8))

        right = ctk.CTkFrame(body, fg_color=CARD, corner_radius=8, width=470)
        right.pack(side="right", fill="both", padx=(5, 4))
        right.pack_propagate(False)
        ctk.CTkLabel(right, text="실시간 로그", anchor="w",
                     font=("", 13, "bold")).pack(fill="x", padx=12, pady=(10, 4))
        self.log_box = ctk.CTkTextbox(right, font=("Consolas", 11), wrap="word")
        self.log_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.log_box.configure(state="disabled")

    # ==================================================================
    # 종목 - 검색 / 관심 / 보유 / 최근조회 / 순위
    # ==================================================================
    QUOTE_COLS = [("rank", "#", 45), ("sym", "코드", 70), ("name", "종목명", 170),
                  ("price", "현재가", 100), ("chg", "대비", 90),
                  ("pct", "등락률", 80), ("vol", "거래량", 110),
                  ("extra", "비고", 150)]

    def _tab_stocks(self, p) -> None:
        bar = ctk.CTkFrame(p, fg_color=CARD, corner_radius=8)
        bar.pack(fill="x", padx=4, pady=(6, 6))
        r = ctk.CTkFrame(bar, fg_color="transparent")
        r.pack(fill="x", padx=12, pady=10)
        ctk.CTkLabel(r, text="종목 검색", font=("", 13, "bold")).pack(side="left", padx=(0, 10))
        self.e_search = ctk.CTkEntry(r, width=280, height=34,
                                     placeholder_text="종목명 또는 6자리 코드  (예: 삼성전자, 005930)")
        self.e_search.pack(side="left")
        self.e_search.bind("<Return>", lambda _e: self._do_search())
        ctk.CTkButton(r, text="검색", width=80, command=self._do_search).pack(side="left", padx=6)
        self.lbl_master = ctk.CTkLabel(r, text="", text_color=MUTED, font=("", 11))
        self.lbl_master.pack(side="left", padx=12)
        ctk.CTkButton(r, text="종목DB 갱신", width=110, fg_color="#3a3a3a",
                      hover_color="#4a4a4a",
                      command=self._refresh_master).pack(side="right", padx=(6, 0))
        ctk.CTkButton(r, text="⚡ 전략 자동 탐색", width=150, fg_color="#3a5f8a",
                      hover_color="#46709c",
                      command=self._open_scan).pack(side="right", padx=6)

        body = ctk.CTkFrame(p, fg_color="transparent")
        body.pack(fill="both", expand=True)

        # --- 좌: 검색 결과 ---
        left = ctk.CTkFrame(body, fg_color=CARD, corner_radius=8, width=340)
        left.pack(side="left", fill="both", padx=(4, 5))
        left.pack_propagate(False)
        ctk.CTkLabel(left, text="검색 결과", anchor="w",
                     font=("", 13, "bold")).pack(fill="x", padx=12, pady=(10, 4))
        self.tv_search = self._tree(left, [("sym", "코드", 75), ("name", "종목명", 165),
                                           ("mk", "시장", 70)], 16)
        self.tv_search.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self.tv_search.bind("<Double-1>", lambda _e: self._add_selected(self.tv_search))
        ctk.CTkButton(left, text="＋ 관심종목에 추가  (더블클릭도 가능)",
                      command=lambda: self._add_selected(self.tv_search)
                      ).pack(fill="x", padx=10, pady=(0, 10))

        # --- 우: 목록 ---
        right = ctk.CTkFrame(body, fg_color=CARD, corner_radius=8)
        right.pack(side="right", fill="both", expand=True, padx=(5, 4))
        self.sub = ctk.CTkTabview(right, anchor="nw", height=430)
        self.sub.pack(fill="both", expand=True, padx=6, pady=6)
        for n in ("관심종목", "감시분석", "보유종목", "최근조회", "국내순위", "해외순위"):
            self.sub.add(n)
        self._tab_watch_stats(self.sub.tab("감시분석"))

        self.tv_watch = self._quote_tree(self.sub.tab("관심종목"))
        w = ctk.CTkFrame(self.sub.tab("관심종목"), fg_color="transparent")
        w.pack(fill="x", pady=(0, 6))
        ctk.CTkButton(w, text="새로고침", width=90, command=lambda: self._load_list("관심종목")
                      ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(w, text="선택 종목 삭제", width=120, fg_color="#7a1f1f",
                      hover_color="#9b2626", command=self._remove_selected).pack(side="left")
        self.lbl_watch = ctk.CTkLabel(w, text="", text_color=MUTED, font=("", 11))
        self.lbl_watch.pack(side="left", padx=12)

        self.tv_hold = self._tree(self.sub.tab("보유종목"), [
            ("sym", "코드", 70), ("name", "종목명", 160), ("qty", "수량", 70),
            ("avg", "평균단가", 100), ("price", "현재가", 100),
            ("amt", "평가금액", 120), ("pnl", "평가손익", 110), ("pct", "수익률", 80)], 14)
        self.tv_hold.pack(fill="both", expand=True, pady=(4, 4))
        self.tv_hold.bind("<Double-1>", lambda _e: self._add_selected(self.tv_hold))
        h = ctk.CTkFrame(self.sub.tab("보유종목"), fg_color="transparent")
        h.pack(fill="x", pady=(0, 6))
        ctk.CTkButton(h, text="새로고침", width=90,
                      command=lambda: self._load_list("보유종목")).pack(side="left", padx=(0, 6))
        ctk.CTkButton(h, text="＋ 관심종목에 추가", width=140, fg_color="#3a3a3a",
                      hover_color="#4a4a4a",
                      command=lambda: self._add_selected(self.tv_hold)).pack(side="left")

        self.tv_recent = self._quote_tree(self.sub.tab("최근조회"))
        rc = ctk.CTkFrame(self.sub.tab("최근조회"), fg_color="transparent")
        rc.pack(fill="x", pady=(0, 6))
        ctk.CTkButton(rc, text="새로고침", width=90,
                      command=lambda: self._load_list("최근조회")).pack(side="left", padx=(0, 6))
        ctk.CTkButton(rc, text="＋ 관심종목에 추가", width=140, fg_color="#3a3a3a",
                      hover_color="#4a4a4a",
                      command=lambda: self._add_selected(self.tv_recent)).pack(side="left")

        # 국내순위
        dm = self.sub.tab("국내순위")
        d1 = ctk.CTkFrame(dm, fg_color="transparent")
        d1.pack(fill="x", pady=(4, 2))
        self.opt_rank = ctk.CTkOptionMenu(d1, width=150, values=[
            "거래량", "거래증가율", "상승률", "하락률", "시가총액"],
            command=lambda _v: self._load_list("국내순위"))
        self.opt_rank.pack(side="left")
        self.opt_market = ctk.CTkOptionMenu(d1, width=110, values=["전체", "코스피", "코스닥"],
                                            command=lambda _v: self._load_list("국내순위"))
        self.opt_market.pack(side="left", padx=6)
        ctk.CTkButton(d1, text="새로고침", width=90,
                      command=lambda: self._load_list("국내순위")).pack(side="left", padx=6)
        ctk.CTkButton(d1, text="＋ 관심종목에 추가", width=140, fg_color="#3a3a3a",
                      hover_color="#4a4a4a",
                      command=lambda: self._add_selected(self.tv_rank)).pack(side="left")
        self.tv_rank = self._quote_tree(dm)
        self.tv_rank.bind("<Double-1>", lambda _e: self._add_selected(self.tv_rank))

        # 해외순위
        om = self.sub.tab("해외순위")
        o1 = ctk.CTkFrame(om, fg_color="transparent")
        o1.pack(fill="x", pady=(4, 2))
        self.opt_orank = ctk.CTkOptionMenu(o1, width=150,
                                           values=["상승률", "하락률", "거래량", "거래대금"],
                                           command=lambda _v: self._load_list("해외순위"))
        self.opt_orank.pack(side="left")
        self.opt_excd = ctk.CTkOptionMenu(
            o1, width=130,
            values=["나스닥", "뉴욕", "아멕스", "도쿄", "홍콩", "상해", "심천", "베트남"],
            command=lambda _v: self._load_list("해외순위"))
        self.opt_excd.pack(side="left", padx=6)
        ctk.CTkButton(o1, text="새로고침", width=90,
                      command=lambda: self._load_list("해외순위")).pack(side="left", padx=6)
        ctk.CTkLabel(o1, text="해외는 조회 전용 (이 프로그램의 자동매매는 국내주식만)",
                     text_color=MUTED, font=("", 11)).pack(side="left", padx=10)
        self.tv_orank = self._quote_tree(om)

        self._update_master_label()
        self.after(1200, lambda: self._load_list("관심종목"))

    # ------------------------------------------------------------------
    # 감시 분석 - 관측이 쌓일수록 "어디서 막히는지"가 드러난다
    # ------------------------------------------------------------------
    def _tab_watch_stats(self, p) -> None:
        bar = ctk.CTkFrame(p, fg_color="transparent")
        bar.pack(fill="x", pady=(6, 4))
        ctk.CTkLabel(bar, text="기간", font=("", 12)).pack(side="left", padx=(4, 6))
        self.opt_eval_days = ctk.CTkOptionMenu(
            bar, width=100, values=["1일", "7일", "30일", "90일"],
            command=lambda _v: self._refresh_eval_stats())
        self.opt_eval_days.set("7일")
        self.opt_eval_days.pack(side="left")
        ctk.CTkButton(bar, text="새로고침", width=90,
                      command=self._refresh_eval_stats).pack(side="left", padx=6)
        self.lbl_eval = ctk.CTkLabel(bar, text="", text_color=MUTED, font=("", 11))
        self.lbl_eval.pack(side="left", padx=10)

        ctk.CTkLabel(p, text="종목 × 전략 - 얼마나 자주 조건에 근접했는가", anchor="w",
                     font=("", 12, "bold")).pack(fill="x", padx=4, pady=(6, 2))
        self.tv_eval = self._tree(p, [
            ("sym", "종목", 130), ("st", "전략", 120), ("n", "관측", 70),
            ("buy", "신호", 60), ("near", "근접", 60), ("score", "평균 충족률", 100),
            ("gap", "최소 거리", 90), ("atr", "변동성", 80),
            ("last", "최근", 130)], 11)
        self.tv_eval.pack(fill="both", expand=True, padx=4, pady=(0, 6))

        ctk.CTkLabel(p, text="조건별 발목잡기 - 이 조건 때문에 몇 번이나 못 샀는가",
                     anchor="w", font=("", 12, "bold")).pack(fill="x", padx=4, pady=(4, 2))
        self.tv_eval_block = self._tree(p, [
            ("st", "전략", 130), ("cond", "조건", 330), ("n", "관측", 80),
            ("fail", "미충족", 80), ("pct", "미충족률", 100)], 8)
        self.tv_eval_block.pack(fill="both", expand=True, padx=4, pady=(0, 8))
        self.after(1500, self._refresh_eval_stats)

    def _refresh_eval_stats(self) -> None:
        days = int(self.opt_eval_days.get().replace("일", "")) if getattr(
            self, "opt_eval_days", None) else 7
        mode = self.core.engine.mode if self.core.engine else None

        def job():
            try:
                stats = self.core.store.eval_stats(days, mode)
                blocks = self.core.store.eval_block_stats(days, mode)
            except Exception as e:
                self.after(0, lambda: self._log(f"감시 통계 조회 실패: {e}", "error"))
                return
            self.after(0, lambda: self._paint_eval_stats(stats, blocks, days))
        # 자동 갱신이라 _thread 의 '작업 중' 팝업을 띄우면 안 된다
        threading.Thread(target=job, daemon=True).start()

    def _paint_eval_stats(self, stats: list, blocks: list, days: int) -> None:
        for i in self.tv_eval.get_children():
            self.tv_eval.delete(i)
        for r in stats:
            self.tv_eval.insert("", "end", values=(
                f"{self.core.store.stock_name(r['symbol']) or r['symbol']}",
                r["strategy"], f"{r['n']:,}", r["buys"], r["nears"],
                f"{(r['avg_score'] or 0) * 100:.0f}%",
                f"{r['best_gap']:.2f}%" if r.get("best_gap") is not None else "-",
                f"{r['atr_pct'] or 0:.2f}%", (r.get("last_ts") or "")[5:16]))

        for i in self.tv_eval_block.get_children():
            self.tv_eval_block.delete(i)
        for b in blocks[:60]:
            self.tv_eval_block.insert("", "end", values=(
                b["strategy"], b["label"], f"{b['n']:,}", f"{b['fail']:,}",
                f"{b['fail_pct']:.0f}%"))

        total = sum(r["n"] for r in stats)
        buys = sum(r["buys"] for r in stats)
        self.lbl_eval.configure(
            text=(f"최근 {days}일 관측 {total:,}건 · 매수신호 {buys}건 "
                  f"· 종목x전략 조합 {len(stats)}개"
                  if total else
                  "아직 관측 기록이 없습니다 - 엔진을 켜두면 30초마다 쌓입니다"))

    def _quote_tree(self, parent) -> ttk.Treeview:
        t = self._tree(parent, self.QUOTE_COLS, 14)
        t.pack(fill="both", expand=True, pady=(4, 4))
        t.tag_configure("up", foreground=UP)
        t.tag_configure("down", foreground=DOWN)
        return t

    def _update_master_label(self) -> None:
        n = self.core.master.count
        self.lbl_master.configure(
            text=f"검색 가능 {n:,}종목" if n else "종목DB가 비어 있습니다 → [종목DB 갱신]")

    def _refresh_master(self) -> None:
        def job():
            self.core.master.refresh(lambda m: self._event("data", m, {}))
            self.after(0, self._update_master_label)
        self._thread(job)

    def _do_search(self) -> None:
        q = self.e_search.get().strip()
        if not q:
            return
        if self.core.master.count == 0:
            if messagebox.askyesno("종목DB 없음",
                                   "검색하려면 종목 목록을 먼저 받아야 합니다.\n지금 받을까요?"):
                self._refresh_master()
            return
        rows = self.core.master.search(q)
        self.tv_search.delete(*self.tv_search.get_children())
        for r in rows:
            self.tv_search.insert("", "end", values=(r["symbol"], r["name"], r["market"]))
        if not rows:
            self._log(f"'{q}' 검색 결과 없음")

    # -- 목록 채우기 --------------------------------------------------------
    def _selected_symbols(self, tree: ttk.Treeview) -> list[str]:
        out = []
        for i in tree.selection():
            v = tree.item(i, "values")
            for cand in v[:2]:
                s = str(cand).strip()
                if s.isdigit() and len(s) == 6:
                    out.append(s)
                    break
        return out

    def _add_selected(self, tree: ttk.Treeview) -> None:
        syms = self._selected_symbols(tree)
        if not syms:
            messagebox.showinfo("안내", "추가할 종목을 목록에서 선택하세요.")
            return
        wl = list(self.core.cfg.watchlist)
        added = [s for s in syms if s not in wl]
        if not added:
            self._log("이미 관심종목에 있습니다.")
            return
        wl.extend(added)
        self.core.cfg.watchlist = wl
        self.core.save()
        names = ", ".join(f"{s}({self.core.master.name(s)})" for s in added)
        self._log(f"관심종목 추가: {names}  (총 {len(wl)}종목)")
        self.sub.set("관심종목")
        self._load_list("관심종목")

    def _remove_selected(self) -> None:
        syms = self._selected_symbols(self.tv_watch)
        if not syms:
            messagebox.showinfo("안내", "삭제할 종목을 선택하세요.")
            return
        wl = [s for s in self.core.cfg.watchlist if s not in syms]
        if not wl:
            messagebox.showerror("오류", "관심종목을 전부 지울 수는 없습니다. 최소 1개는 필요합니다.")
            return
        self.core.cfg.watchlist = wl
        self.core.save()
        self._log(f"관심종목 삭제: {', '.join(syms)}  (총 {len(wl)}종목)")
        self._load_list("관심종목")

    def _load_list(self, kind: str) -> None:
        """네트워크 호출이 들어가므로 항상 백그라운드에서."""
        def job():
            try:
                rows, tree = self._fetch_list(kind)
            except Exception as e:
                self._event("error", f"{kind} 조회 실패: {e}", {})
                return
            self.after(0, lambda: self._fill_quotes(tree, rows, kind))
        threading.Thread(target=job, daemon=True).start()

    def _fetch_list(self, kind: str):
        qc = self.core.quote_client
        if kind == "관심종목":
            if not qc:
                return [], self.tv_watch
            rows = qc.multi_price(self.core.cfg.watchlist)
            self._annotate_sizing(rows)
            return rows, self.tv_watch
        if kind == "보유종목":
            acct = self.core.broker.account(max_age=15)
            rows = []
            for h in (acct.get("holdings") or {}).values():
                rows.append({"symbol": h["symbol"], "name": h["name"] or
                             self.core.master.name(h["symbol"]),
                             "qty": h["qty"], "avg": h["avg_price"], "price": h["price"],
                             "amt": h["eval_amt"], "pnl": h["pnl"], "pct": h["pnl_pct"]})
            return rows, self.tv_hold
        if kind == "최근조회":
            syms = self.core.store.recent_views(30)
            if not syms or not qc:
                return [], self.tv_recent
            return qc.multi_price(syms), self.tv_recent
        if kind == "국내순위":
            if not qc:
                return [], self.tv_rank
            mk = {"전체": "0000", "코스피": "0001", "코스닥": "1001"}[self.opt_market.get()]
            sel = self.opt_rank.get()
            if sel == "거래량":
                return qc.volume_rank(mk, "0"), self.tv_rank
            if sel == "거래증가율":
                return qc.volume_rank(mk, "1"), self.tv_rank
            if sel == "상승률":
                return qc.fluctuation_rank(mk, False), self.tv_rank
            if sel == "하락률":
                return qc.fluctuation_rank(mk, True), self.tv_rank
            return qc.market_cap_rank(mk), self.tv_rank
        if kind == "해외순위":
            if not qc:
                return [], self.tv_orank
            excd = {"나스닥": "NAS", "뉴욕": "NYS", "아멕스": "AMS", "도쿄": "TSE",
                    "홍콩": "HKS", "상해": "SHS", "심천": "SZS",
                    "베트남": "HSX"}[self.opt_excd.get()]
            sel = self.opt_orank.get()
            kindmap = {"상승률": ("updown", False), "하락률": ("updown", True),
                       "거래량": ("volume", False), "거래대금": ("amount", False)}
            k, falling = kindmap[sel]
            return qc.overseas_rank(k, excd, falling), self.tv_orank
        return [], self.tv_watch

    def _annotate_sizing(self, rows: list) -> None:
        """관심종목별로 지금 몇 주까지 살 수 있는지 계산해 붙인다.

        고가주는 손절폭이 커서 1주도 못 사는 경우가 많은데, 화면에 안 보이면
        "신호는 뜨는데 왜 안 사지?" 가 된다.
        """
        from . import indicators as ind
        eng = self.core.engine
        acct = eng.account or {}
        equity = float(acct.get("total_eval") or 0)
        cash = float(acct.get("orderable_cash") or acct.get("cash") or 0)
        if equity <= 0:
            for r in rows:
                r["extra_label"], r["extra"] = "", 0
            return
        atr_mult = 2.0
        for st in self.core.cfg.strategies:
            if st.get("enabled"):
                atr_mult = float((st.get("params") or {}).get("atr_stop", 2.0) or 2.0)
                break
        rc = self.core.cfg.risk
        for r in rows:
            sym, px = r.get("symbol", ""), float(r.get("price") or 0)
            if px <= 0:
                continue
            bars = self.core.store.get_candles(sym, "D", limit=60)
            a = ind.last(ind.atr(bars, 14)) if len(bars) > 15 else None
            stop = px - atr_mult * (a or px * 0.02)
            qty, _note = eng.risk.position_size(px, stop, equity, cash)
            r["_qty"] = qty
            r["_bars"] = len(bars)
            if qty > 0:
                continue
            # 왜 못 사는지 - 가장 먼저 걸리는 제약을 이름으로 알려준다
            stop_dist = max(px - stop, px * 0.03)
            cands = [
                ((equity * rc.max_loss_per_trade_pct / 100) / stop_dist, "손절폭 과대"),
                ((equity * rc.max_position_weight_pct / 100) / px, "종목비중 한도"),
                (max(cash - equity * rc.min_cash_reserve_pct / 100, 0) / px, "현금 부족"),
                (rc.max_order_amount / px, "1회주문 상한"),
            ]
            r["_why"] = min(cands)[1]

    def _fill_quotes(self, tree: ttk.Treeview, rows: list, kind: str) -> None:
        tree.delete(*tree.get_children())
        if kind == "보유종목":
            for r in rows:
                tag = "up" if r["pnl"] > 0 else ("down" if r["pnl"] < 0 else "")
                tree.insert("", "end", tags=(tag,), values=(
                    r["symbol"], r["name"], f"{r['qty']:,}", money(r["avg"]),
                    money(r["price"]), money(r["amt"]), money(r["pnl"]),
                    f"{r['pct']:+.2f}%"))
            if not rows:
                tree.insert("", "end", values=("", "보유 종목 없음", "", "", "", "", "", ""))
            return

        for i, r in enumerate(rows, 1):
            cp = r.get("change_pct", 0)
            tag = "up" if cp > 0 else ("down" if cp < 0 else "")
            name = r.get("name") or self.core.master.name(r.get("symbol", ""))
            if kind == "관심종목" and "_qty" in r:
                if r.get("_bars", 0) < 60:
                    note = f"일봉 {r['_bars']}봉 - 수집 필요"
                elif r["_qty"] > 0:
                    note = f"매수가능 {r['_qty']}주"
                else:
                    note = f"매수불가 - {r.get('_why', '자금부족')}"
            else:
                note = extra_fmt(r.get("extra_label", ""), r.get("extra", 0))
            tree.insert("", "end", tags=(tag,), values=(
                r.get("rank") or i, r.get("symbol", ""), name,
                num(r.get("price", 0)), num(r.get("change", 0), signed=True),
                f"{cp:+.2f}%", f"{r.get('volume', 0):,}", note))
        if not rows:
            msg = ("시세 클라이언트가 없습니다 (.env 확인)" if not self.core.quote_client
                   else "조회 결과 없음")
            tree.insert("", "end", values=("", "", msg, "", "", "", "", ""))
        if kind == "관심종목":
            can = sum(1 for r in rows if r.get("_qty", 0) > 0)
            thin = sum(1 for r in rows if r.get("_bars", 99) < 60)
            msg = f"{len(rows)}종목  |  매수가능 {can}"
            if len(rows) - can:
                msg += f" / 불가 {len(rows) - can}"
            if thin:
                msg += f"  |  일봉부족 {thin}종목"
            self.lbl_watch.configure(
                text=msg, text_color=WARN if (len(rows) - can or thin) else MUTED)
            for r in rows:
                self.core.store.touch_recent(r.get("symbol", ""))

    # ==================================================================
    # 전략
    # ==================================================================
    # 영문 키만 보고 값을 정하기는 어렵다. 특히 '비율' 계열은
    # 무엇에 대한 비율인지가 이름에 안 드러난다.
    PARAM_LABEL = {
        "k": "돌파계수 k",
        "ma_filter": "추세필터 MA",
        "vol_filter": "거래량 배수",
        "min_range_pct": "최소 전일변동폭%",
        "max_chase_pct": "추격 허용%",
        "atr_stop": "손절 = ATR ×",
        "stop_cap_atr": "손절폭 상한 = ATR ×",
        "min_stop_atr": "손절폭 하한 = ATR ×",
        "hard_stop_pct": "손절 절대상한 %",
        "max_stop_pct": "(구)고정 상한 %",
        "take_profit_r": "목표 = 손절폭 × R",
        "take_profit_pct": "(구)고정 목표 %",
        "trail_atr": "트레일링 = ATR ×",
        "breakeven_pct": "본전방어 시작 %",
        "fast": "단기 이평",
        "slow": "장기 이평",
        "pullback_lookback": "눌림 탐색 봉수",
        "rsi_period": "RSI 기간",
        "rsi_max": "진입 RSI 상한",
        "rsi_exit": "청산 RSI",
        "max_hold_bars": "최대 보유 봉수",
        "range_min": "레인지 구성 분",
        "entry_deadline": "진입 마감시각",
        "trend_ma": "추세 이평",
        "entry_ma": "진입 확인 이평",
        "momentum_days": "모멘텀 기간(일)",
        "min_momentum_pct": "최소 모멘텀 %",
        "exit_ma": "청산 이평",
        "max_extension_pct": "과열 제외 %",
    }

    def _tab_strategy(self, p) -> None:
        ctk.CTkLabel(p, anchor="w", justify="left", wraplength=1150, text_color=MUTED,
                     text=("켜져 있는 전략이 [종목] 탭의 관심종목 전체에 대해 매수 신호를 만듭니다. "
                           "수량과 차단은 전략이 아니라 [리스크] 탭이 결정합니다.\n"
                           "손절·목표는 고정 %가 아니라 종목의 변동성(ATR) 배수로 정해집니다. "
                           "같은 설정이라도 잘 움직이는 종목은 자동으로 폭이 넓어집니다 - "
                           "종목별 실제 적용값은 [대시보드]의 감시 현황에서 행을 더블클릭해 확인하세요."),
                     ).pack(fill="x", padx=12, pady=(10, 6))
        sc = ctk.CTkScrollableFrame(p, fg_color="transparent")
        sc.pack(fill="both", expand=True, padx=4)

        self.strat_widgets: dict[str, dict] = {}
        for entry in self.core.cfg.strategies:
            name = entry["name"]
            cls = REGISTRY.get(name)
            if cls is None:
                continue
            f = ctk.CTkFrame(sc, fg_color=CARD, corner_radius=8)
            f.pack(fill="x", pady=5, padx=4)

            head = ctk.CTkFrame(f, fg_color="transparent")
            head.pack(fill="x", padx=12, pady=(10, 2))
            sw = ctk.CTkSwitch(head, text=f"{cls.label}  ({cls.timeframe})",
                               font=("", 14, "bold"))
            sw.pack(side="left")
            if entry.get("enabled"):
                sw.select()
            ctk.CTkButton(head, text="기본값 복원", width=100, height=26,
                          fg_color="#3a3a3a", hover_color="#4a4a4a",
                          command=lambda n=name: self._reset_params(n)).pack(side="right")

            ctk.CTkLabel(f, text=cls.description, anchor="w", justify="left",
                         text_color=MUTED, wraplength=1050,
                         font=("", 11)).pack(fill="x", padx=12, pady=(0, 6))

            grid = ctk.CTkFrame(f, fg_color="transparent")
            grid.pack(fill="x", padx=12, pady=(0, 12))
            saved = entry.get("params") or {}
            # 전략 클래스에 없는 옛 파라미터는 버린다 (설정 파일이 오래됐을 때)
            params = {k: saved.get(k, v) for k, v in cls.default_params.items()}
            fields = {}
            for i, (k, v) in enumerate(params.items()):
                col = i % 4
                rw = i // 4
                cell = ctk.CTkFrame(grid, fg_color="transparent")
                cell.grid(row=rw, column=col, sticky="w", padx=(0, 18), pady=3)
                ctk.CTkLabel(cell, text=self.PARAM_LABEL.get(k, k), width=150,
                             anchor="w", text_color=MUTED,
                             font=("", 11)).pack(side="left")
                e = ctk.CTkEntry(cell, width=80, height=28)
                e.insert(0, str(v))
                e.pack(side="left")
                fields[k] = e
            self.strat_widgets[name] = {"switch": sw, "fields": fields}

        ctk.CTkButton(p, text="전략 설정 저장", height=38,
                      command=self._save_strategies).pack(pady=10)

    def _reset_params(self, name: str) -> None:
        d = default_params(name)
        w = self.strat_widgets.get(name)
        if not w:
            return
        for k, e in w["fields"].items():
            if k in d:
                e.delete(0, "end")
                e.insert(0, str(d[k]))
        self._log(f"{name} 파라미터를 기본값으로 되돌렸습니다.")

    def _save_strategies(self) -> None:
        out = []
        for entry in self.core.cfg.strategies:
            name = entry["name"]
            w = self.strat_widgets.get(name)
            if not w:
                out.append(entry)
                continue
            params = {}
            for k, e in w["fields"].items():
                params[k] = _coerce(e.get())
            out.append({"name": name, "enabled": bool(w["switch"].get()), "params": params})
        self.core.cfg.strategies = out
        self.core.save()
        on = [o["name"] for o in out if o["enabled"]]
        self._log(f"전략 저장 완료. 활성: {', '.join(on) if on else '없음'}")

    # ==================================================================
    # 리스크
    # ==================================================================
    def _tab_risk(self, p) -> None:
        ctk.CTkLabel(
            p, anchor="w", justify="left", wraplength=1150, text_color=WARN,
            text=("전략이 실패해도 계좌가 살아남게 하는 설정입니다. "
                  "여기 값들이 전략보다 중요합니다. 모의투자에서 충분히 검증하기 전엔 느슨하게 풀지 마세요."),
        ).pack(fill="x", padx=12, pady=(10, 8))

        sc = ctk.CTkScrollableFrame(p, fg_color="transparent")
        sc.pack(fill="both", expand=True, padx=4)

        groups = [
            ("손실 한도", [
                ("max_loss_per_trade_pct", "1회 거래 최대손실 (%)",
                 "총자산 대비. 이 값으로 매수 수량이 역산된다."),
                ("max_daily_loss_pct", "하루 최대손실 (%)",
                 "초과하면 당일 신규진입 전면 중단."),
                ("max_drawdown_pct", "누적 최대낙폭 (%)",
                 "최고자산 대비. 초과하면 엔진 전체 정지."),
            ]),
            ("과매매 차단", [
                ("max_consecutive_losses", "연속 손절 허용 횟수", "초과 시 쿨다운 진입."),
                ("consecutive_loss_cooldown_min", "연속손실 쿨다운 (분)", ""),
                ("reentry_cooldown_min", "같은 종목 재진입 금지 (분)", "손절 직후 재매수 방지."),
                ("max_orders_per_day", "하루 최대 주문 건수", ""),
            ]),
            ("분산 / 금액", [
                ("max_positions", "동시 보유 종목 수", ""),
                ("max_position_weight_pct", "종목당 최대 비중 (%)", "총자산 대비."),
                ("min_order_amount", "1회 최소 주문금액 (원)", "수수료 대비 너무 작은 주문 방지."),
                ("max_order_amount", "1회 최대 주문금액 (원)", ""),
                ("min_cash_reserve_pct", "최소 현금 보유 (%)", "몰빵 방지."),
            ]),
            ("거래비용 / 체결현실성  (백테스트에도 똑같이 적용)", [
                ("min_edge_cost_ratio", "기대이익 / 왕복비용 최소배수",
                 "왕복비용은 수수료x2 + 매도세 + 슬리피지x2. 3배 미만이면 "
                 "이겨도 비용이 먹는다. 0을 넣으면 관문을 끈다."),
                ("min_turnover_amount", "평균 거래대금 하한 (원)",
                 "이보다 적게 거래되는 종목은 팔고 싶을 때 못 판다. 0 = 끔."),
                ("turnover_lookback", "거래대금 평균 기간 (봉)", "기본 20."),
            ]),
        ]
        self.risk_fields: dict[str, ctk.CTkEntry] = {}
        for title, items in groups:
            f = ctk.CTkFrame(sc, fg_color=CARD, corner_radius=8)
            f.pack(fill="x", pady=5, padx=4)
            ctk.CTkLabel(f, text=title, anchor="w",
                         font=("", 13, "bold")).pack(fill="x", padx=12, pady=(10, 6))
            for key, label, hint in items:
                row = ctk.CTkFrame(f, fg_color="transparent")
                row.pack(fill="x", padx=12, pady=3)
                ctk.CTkLabel(row, text=label, width=250, anchor="w").pack(side="left")
                e = ctk.CTkEntry(row, width=120, height=28)
                e.insert(0, str(getattr(self.core.cfg.risk, key)))
                e.pack(side="left")
                if hint:
                    ctk.CTkLabel(row, text=hint, text_color=MUTED,
                                 anchor="w", font=("", 11)).pack(side="left", padx=12)
                self.risk_fields[key] = e
            ctk.CTkFrame(f, height=6, fg_color="transparent").pack()

        # 실행/비용
        f = ctk.CTkFrame(sc, fg_color=CARD, corner_radius=8)
        f.pack(fill="x", pady=5, padx=4)
        ctk.CTkLabel(f, text="주문 실행 / 비용 가정", anchor="w",
                     font=("", 13, "bold")).pack(fill="x", padx=12, pady=(10, 6))
        self.exec_fields: dict[str, ctk.CTkEntry] = {}
        for key, label, hint in [
            ("entry_start", "신규진입 시작 시각", "HH:MM"),
            ("entry_end", "신규진입 종료 시각", "HH:MM"),
            ("force_exit_at", "강제 전량청산 시각", "비우면 오버나이트 보유"),
            ("limit_slippage_pct", "지정가 허용 슬리피지 (%)", ""),
            ("order_timeout_sec", "미체결 취소 대기 (초)", ""),
            ("loop_interval_sec", "엔진 루프 주기 (초)", ""),
        ]:
            row = ctk.CTkFrame(f, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=3)
            ctk.CTkLabel(row, text=label, width=250, anchor="w").pack(side="left")
            e = ctk.CTkEntry(row, width=120, height=28)
            e.insert(0, str(getattr(self.core.cfg.execution, key)))
            e.pack(side="left")
            ctk.CTkLabel(row, text=hint, text_color=MUTED, anchor="w",
                         font=("", 11)).pack(side="left", padx=12)
            self.exec_fields[key] = e

        row = ctk.CTkFrame(f, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=3)
        ctk.CTkLabel(row, text="주문 방식", width=250, anchor="w").pack(side="left")
        self.opt_order = ctk.CTkOptionMenu(row, width=200,
                                           values=["best", "limit", "market"])
        self.opt_order.set(self.core.cfg.execution.order_type)
        self.opt_order.pack(side="left")
        ctk.CTkLabel(row, text="best=최유리지정가 / limit=지정가 / market=시장가",
                     text_color=MUTED, font=("", 11)).pack(side="left", padx=12)

        self.cost_fields: dict[str, ctk.CTkEntry] = {}
        for key, label, hint in [
            ("commission_pct", "위탁수수료 (%, 편도)", "백테스트 비용 가정"),
            ("sell_tax_pct", "매도 세금 (%)", "증권거래세+농특세"),
            ("slippage_pct", "슬리피지 (%, 편도)", "낙관적으로 잡으면 백테스트가 거짓말을 한다"),
        ]:
            row = ctk.CTkFrame(f, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=3)
            ctk.CTkLabel(row, text=label, width=250, anchor="w").pack(side="left")
            e = ctk.CTkEntry(row, width=120, height=28)
            e.insert(0, str(getattr(self.core.cfg.cost, key)))
            e.pack(side="left")
            ctk.CTkLabel(row, text=hint, text_color=MUTED, anchor="w",
                         font=("", 11)).pack(side="left", padx=12)
            self.cost_fields[key] = e
        ctk.CTkFrame(f, height=6, fg_color="transparent").pack()

        ctk.CTkButton(p, text="리스크 설정 저장", height=38,
                      command=self._save_risk).pack(pady=10)

    def _save_risk(self) -> None:
        try:
            for k, e in self.risk_fields.items():
                cur = getattr(self.core.cfg.risk, k)
                setattr(self.core.cfg.risk, k,
                        int(float(e.get())) if isinstance(cur, int) else float(e.get()))
            for k, e in self.exec_fields.items():
                cur = getattr(self.core.cfg.execution, k)
                v = e.get().strip()
                setattr(self.core.cfg.execution, k,
                        v if isinstance(cur, str) else
                        (int(float(v)) if isinstance(cur, int) else float(v)))
            self.core.cfg.execution.order_type = self.opt_order.get()
            for k, e in self.cost_fields.items():
                setattr(self.core.cfg.cost, k, float(e.get()))
        except ValueError as ex:
            messagebox.showerror("입력 오류", f"숫자를 확인해주세요: {ex}")
            return
        self.core.save()
        self._log("리스크/실행 설정 저장 완료")

    # ==================================================================
    # 백테스트
    # ==================================================================
    def _tab_backtest(self, p) -> None:
        bar = ctk.CTkFrame(p, fg_color=CARD, corner_radius=8)
        bar.pack(fill="x", padx=4, pady=(6, 8))
        r1 = ctk.CTkFrame(bar, fg_color="transparent")
        r1.pack(fill="x", padx=12, pady=(10, 4))

        ctk.CTkLabel(r1, text="전략", width=40).pack(side="left")
        self.bt_strat = ctk.CTkOptionMenu(r1, width=180,
                                          values=[c.label for c in REGISTRY.values()])
        self.bt_strat.pack(side="left", padx=(0, 14))

        ctk.CTkLabel(r1, text="종목", width=40).pack(side="left")
        self.bt_syms = ctk.CTkEntry(r1, width=260)
        self.bt_syms.insert(0, ",".join(self.core.cfg.watchlist))
        self.bt_syms.pack(side="left", padx=(0, 14))

        ctk.CTkLabel(r1, text="기간").pack(side="left")
        self.bt_start = ctk.CTkEntry(r1, width=100, placeholder_text="2024-01-01")
        self.bt_start.pack(side="left", padx=4)
        ctk.CTkLabel(r1, text="~").pack(side="left")
        self.bt_end = ctk.CTkEntry(r1, width=100, placeholder_text="비우면 최근까지")
        self.bt_end.pack(side="left", padx=4)

        ctk.CTkLabel(r1, text="초기자금").pack(side="left", padx=(14, 4))
        self.bt_cash = ctk.CTkEntry(r1, width=110)
        self.bt_cash.insert(0, "10000000")
        self.bt_cash.pack(side="left")

        r2 = ctk.CTkFrame(bar, fg_color="transparent")
        r2.pack(fill="x", padx=12, pady=(4, 4))
        ctk.CTkButton(r2, text="백테스트 실행", width=130,
                      command=self._run_backtest).pack(side="left")
        ctk.CTkButton(r2, text="파라미터 최적화", width=130, fg_color="#3a3a3a",
                      hover_color="#4a4a4a", command=self._run_sweep).pack(side="left", padx=6)
        self.bt_prog = ctk.CTkProgressBar(r2, width=160)
        self.bt_prog.set(0)
        self.bt_prog.pack(side="left", padx=10)
        ctk.CTkLabel(r2, text="[전략] 탭에 저장된 파라미터로 실행됩니다",
                     text_color=MUTED, font=("", 11)).pack(side="left", padx=6)

        r3 = ctk.CTkFrame(bar, fg_color="transparent")
        r3.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkLabel(r3, text="견고성 분석", font=("", 12, "bold")).pack(side="left", padx=(0, 10))
        ctk.CTkLabel(r3, text="민감도 대상").pack(side="left")
        self.opt_param = ctk.CTkOptionMenu(r3, width=170, values=["(전략 선택)"])
        self.opt_param.pack(side="left", padx=6)
        ctk.CTkButton(r3, text="민감도", width=90, fg_color="#3a5f8a",
                      hover_color="#46709c", command=self._run_sensitivity).pack(side="left", padx=4)
        ctk.CTkButton(r3, text="연도별 성과", width=110, fg_color="#3a5f8a",
                      hover_color="#46709c", command=self._run_period).pack(side="left", padx=4)
        ctk.CTkButton(r3, text="몬테카를로", width=110, fg_color="#3a5f8a",
                      hover_color="#46709c", command=self._run_mc).pack(side="left", padx=4)
        ctk.CTkButton(r3, text="워크포워드", width=110, fg_color="#5a4a8a",
                      hover_color="#6a5a9c",
                      command=self._run_walkforward).pack(side="left", padx=4)
        ctk.CTkLabel(r3, text="워크포워드 = 앞 구간에서만 고르고 뒤 구간으로만 채점 "
                              "(과최적화 최종 검증)",
                     text_color=MUTED, font=("", 11)).pack(side="left", padx=8)
        self.bt_strat.configure(command=lambda _v: self._refresh_param_list())

        body = ctk.CTkFrame(p, fg_color="transparent")
        body.pack(fill="both", expand=True)

        left = ctk.CTkFrame(body, fg_color=CARD, corner_radius=8)
        left.pack(side="left", fill="both", expand=True, padx=(4, 5))
        ctk.CTkLabel(left, text="자산 곡선", anchor="w",
                     font=("", 13, "bold")).pack(fill="x", padx=12, pady=(10, 2))
        self.canvas = tk.Canvas(left, bg=CARD, highlightthickness=0, height=250)
        self.canvas.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        ctk.CTkLabel(left, text="지표", anchor="w",
                     font=("", 13, "bold")).pack(fill="x", padx=12, pady=(4, 2))
        self.bt_metrics = ctk.CTkTextbox(left, height=170, font=("Consolas", 12))
        self.bt_metrics.pack(fill="both", expand=False, padx=10, pady=(0, 10))
        self.bt_metrics.configure(state="disabled")
        self._refresh_param_list()

        right = ctk.CTkFrame(body, fg_color=CARD, corner_radius=8, width=470)
        right.pack(side="right", fill="both", padx=(5, 4))
        right.pack_propagate(False)
        ctk.CTkLabel(right, text="거래 내역 / 로그", anchor="w",
                     font=("", 13, "bold")).pack(fill="x", padx=12, pady=(10, 4))
        self.bt_log = ctk.CTkTextbox(right, font=("Consolas", 11))
        self.bt_log.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.bt_log.configure(state="disabled")

    def _strategy_key(self) -> str:
        label = self.bt_strat.get()
        for name, cls in REGISTRY.items():
            if cls.label == label:
                return name
        return list(REGISTRY)[0]

    def _bt_params(self, name: str) -> dict:
        for e in self.core.cfg.strategies:
            if e["name"] == name:
                return e.get("params") or {}
        return {}

    def _btlog(self, msg: str) -> None:
        self.bt_log.configure(state="normal")
        self.bt_log.insert("end", msg + "\n")
        self.bt_log.see("end")
        self.bt_log.configure(state="disabled")

    def _run_backtest(self) -> None:
        name = self._strategy_key()
        syms = [s.strip() for s in self.bt_syms.get().replace(" ", ",").split(",") if s.strip()]
        if not syms:
            messagebox.showerror("오류", "종목을 입력하세요.")
            return
        try:
            cash = int(float(self.bt_cash.get()))
        except ValueError:
            messagebox.showerror("오류", "초기자금은 숫자로 입력하세요.")
            return
        start, end = self.bt_start.get().strip(), self.bt_end.get().strip()
        self.bt_log.configure(state="normal")
        self.bt_log.delete("1.0", "end")
        self.bt_log.configure(state="disabled")
        self._btlog(f"백테스트 시작: {name} / {', '.join(syms)}")
        self.bt_prog.configure(mode="indeterminate")
        self.bt_prog.start()

        def job():
            r = self.core.backtester.run(name, self._bt_params(name), syms,
                                         cash, start, end)
            self.after(0, lambda: self._show_backtest(r))
        self._thread(job)

    def _show_backtest(self, r) -> None:
        self.bt_prog.stop()
        self.bt_prog.configure(mode="determinate")
        self.bt_prog.set(1)
        if r.error:
            self._btlog(f"[실패] {r.error}")
            messagebox.showerror("백테스트 실패", r.error)
            return

        m = r.metrics
        lines = [
            f"기간              {r.start} ~ {r.end}  ({m.get('bars', 0)} 거래일)",
            f"초기자금          {money(m.get('initial'))} 원",
            f"최종자산          {money(m.get('final'))} 원",
            f"누적수익률        {m.get('total_return_pct', 0):+.2f} %",
            f"  비용 전         {m.get('gross_return_pct', 0):+.2f} %",
            f"  거래비용        -{m.get('cost_drag_pct', 0):.2f} %p "
            f"({money(m.get('total_cost'))}원)",
            f"    수수료/세금/슬리피지  {money(m.get('fee_total'))} / "
            f"{money(m.get('tax_total'))} / {money(m.get('slip_total'))}",
            f"연환산(CAGR)      {m.get('cagr_pct', 0):+.2f} %",
            "",
            f"그냥 보유했다면   {m.get('bh_return_pct', 0):+.2f} % "
            f"(MDD {m.get('bh_mdd_pct', 0):.2f} %)   <- 같은 기간·같은 종목 동일가중",
            f"초과수익(알파)    {m.get('alpha_pct', 0):+.2f} %p"
            + ("   <- 매매해서 오히려 깎였다" if m.get('alpha_pct', 0) < 0 else ""),
            "",
            f"최대낙폭(MDD)     {m.get('mdd_pct', 0):.2f} %      <- 실계좌에서 견딜 수 있는가",
            f"수익/MDD          {m.get('return_over_mdd', 0)}",
            f"Sharpe            {m.get('sharpe', 0)}",
            "",
            f"총 거래           {m.get('trades', 0)} 건",
            f"승률              {m.get('win_rate', 0)} %",
            f"손익비(Payoff)    {m.get('payoff', 0)}",
            f"Profit Factor     {m.get('profit_factor', 0)}",
            f"평균수익 / 평균손실  {money(m.get('avg_win'))} / -{money(m.get('avg_loss'))}",
            f"기대값(1거래당)   {money(m.get('expectancy'))} 원",
            f"최대 연속손실     {m.get('max_consecutive_losses', 0)} 회",
            f"평균 보유         {m.get('avg_bars_held', 0)} 봉",
            f"관문에서 취소     {m.get('gated_cost', 0) + m.get('gated_liquidity', 0)} 건 "
            f"(비용 {m.get('gated_cost', 0)} / 유동성 {m.get('gated_liquidity', 0)})",
        ]
        self.bt_metrics.configure(state="normal")
        self.bt_metrics.delete("1.0", "end")
        self.bt_metrics.insert("end", "\n".join(lines))
        self.bt_metrics.configure(state="disabled")

        self._draw_equity(r.equity)

        self._btlog("")
        for w in r.warnings:
            self._btlog(f"[경고] {w}")
        self._btlog("")
        self._btlog(f"{'종목':<8}{'진입일':<12}{'청산일':<12}{'손익':>12}  사유")
        for t in r.trades[-120:]:
            self._btlog(f"{t['symbol']:<8}{t['entry_ts'][:10]:<12}{t['exit_ts'][:10]:<12}"
                        f"{t['pnl']:>12,.0f}  {t['reason']}")

        self.core.store.save_backtest(r.strategy, r.params, r.symbols,
                                      r.start, r.end, r.metrics)
        self._log(f"백테스트 완료: {r.strategy} {m.get('total_return_pct', 0):+.2f}% "
                  f"/ MDD {m.get('mdd_pct', 0):.1f}% / {m.get('trades', 0)}건")

    def _draw_equity(self, series: list) -> None:
        c = self.canvas
        c.delete("all")
        if len(series) < 2:
            c.create_text(200, 60, text="데이터 없음", fill=MUTED, anchor="w")
            return
        w = max(c.winfo_width(), 400)
        h = max(c.winfo_height(), 200)
        pad = 40
        vals = [v for _d, v in series]
        lo, hi = min(vals), max(vals)
        if hi == lo:
            hi = lo + 1
        base = vals[0]

        def xy(i, v):
            x = pad + (w - pad * 1.4) * i / (len(vals) - 1)
            y = h - pad - (h - pad * 1.8) * (v - lo) / (hi - lo)
            return x, y

        c.create_line(pad, h - pad, w - pad * 0.4, h - pad, fill="#3a3a3a")
        c.create_line(pad, pad * 0.5, pad, h - pad, fill="#3a3a3a")
        bx, by = xy(0, base)
        c.create_line(pad, by, w - pad * 0.4, by, fill="#4a4a4a", dash=(3, 3))
        c.create_text(pad - 6, by, text="0%", fill=MUTED, anchor="e", font=("", 9))

        pts = []
        for i, v in enumerate(vals):
            pts.extend(xy(i, v))
        color = UP if vals[-1] >= base else DOWN
        c.create_line(*pts, fill=color, width=2, smooth=True)
        c.create_text(w - pad * 0.5, pad * 0.6,
                      text=f"{(vals[-1] - base) / base * 100:+.2f}%",
                      fill=color, anchor="e", font=("", 14, "bold"))
        c.create_text(pad, h - pad + 14, text=series[0][0], fill=MUTED,
                      anchor="w", font=("", 9))
        c.create_text(w - pad * 0.4, h - pad + 14, text=series[-1][0], fill=MUTED,
                      anchor="e", font=("", 9))

    # -- 견고성 분석 --------------------------------------------------------
    def _refresh_param_list(self) -> None:
        name = self._strategy_key()
        cls = REGISTRY.get(name)
        keys = list(cls.default_params) if cls else []
        self.opt_param.configure(values=keys or ["(없음)"])
        if keys:
            self.opt_param.set(keys[0])

    def _analysis_risk(self):
        """분석에 쓸 자금관리. 프로필이 있으면 그 설정을 따른다."""
        from .settings import RiskConfig
        rc = RiskConfig()
        for k, v in asdict(self.core.cfg.risk).items():
            setattr(rc, k, v)
        name = self._strategy_key()
        for pr in self.core.profiles.profiles:
            if any(s["name"] == name and s.get("enabled") for s in pr.strategies):
                for k, v in (pr.risk or {}).items():
                    if hasattr(rc, k):
                        setattr(rc, k, v)
                return rc, pr.name, pr.initial_cash
        return rc, "전역 설정", int(float(self.bt_cash.get() or 10_000_000))

    def _bt_inputs(self):
        name = self._strategy_key()
        syms = [s.strip() for s in self.bt_syms.get().replace(" ", ",").split(",") if s.strip()]
        if not syms:
            messagebox.showerror("오류", "종목을 입력하세요.")
            return None
        rc, pname, cash = self._analysis_risk()
        return name, syms, rc, pname, cash

    def _analysis_header(self, title: str, pname: str, cash: int, syms: list) -> None:
        self.bt_log.configure(state="normal")
        self.bt_log.delete("1.0", "end")
        self.bt_log.configure(state="disabled")
        self._btlog(f"=== {title} ===")
        self._btlog(f"자금관리: {pname} 프로필 / 자금 {cash:,}원 / {len(syms)}종목")
        self._btlog("")
        self.bt_prog.configure(mode="indeterminate")
        self.bt_prog.start()

    def _analysis_done(self, lines: list[str]) -> None:
        self.bt_prog.stop()
        self.bt_prog.configure(mode="determinate")
        self.bt_prog.set(1)
        self.bt_metrics.configure(state="normal")
        self.bt_metrics.delete("1.0", "end")
        self.bt_metrics.insert("end", "\n".join(lines))
        self.bt_metrics.configure(state="disabled")

    def _run_sensitivity(self) -> None:
        got = self._bt_inputs()
        if not got:
            return
        name, syms, rc, pname, cash = got
        param = self.opt_param.get()
        cls = REGISTRY.get(name)
        if not cls or param not in cls.default_params:
            messagebox.showinfo("안내", "민감도를 볼 파라미터를 고르세요.")
            return
        from .analysis import Analyzer, suggest_values
        base = self._bt_params(name)
        cur = base.get(param, cls.default_params[param])
        values = suggest_values(param, cur)
        if not values:
            messagebox.showinfo("안내", f"'{param}'은 숫자 파라미터가 아니라 훑을 수 없습니다.")
            return
        self._analysis_header(f"민감도 분석 - {cls.label} / {param}", pname, cash, syms)
        self._btlog(f"현재값 {cur} / 탐색 {values}")
        self._btlog("")

        def job():
            an = Analyzer(self.core.store, self.core.cfg.cost)
            r = an.sensitivity(name, base, rc, syms, param, values, cash,
                               lambda m: self.after(0, self._btlog, m))
            self.after(0, lambda: self._show_sensitivity(param, r))
        self._thread(job)

    def _show_sensitivity(self, param: str, r) -> None:
        if r.error:
            self._analysis_done([f"실패: {r.error}"])
            return
        self._btlog("")
        self._btlog(f"{'값':>10}{'수익률':>10}{'비용전':>10}{'MDD':>9}{'수익/MDD':>10}{'거래':>7}{'승률':>7}")
        for p in r.points:
            self._btlog(f"{str(p.value):>10}{p.return_pct:>9.2f}%{p.gross_pct:>9.2f}%"
                        f"{p.mdd_pct:>8.2f}%{p.ret_over_mdd:>10.2f}{p.trades:>7}{p.win_rate:>6.1f}%")
        lines = [
            f"민감도 대상       {param}",
            f"최고 성적 값      {r.best_value}  ({r.best_return:+.2f}%)",
            f"수익률 폭         {r.spread:.2f} %p",
            f"이웃값과 차이     {r.neighbor_gap:+.2f} %p",
            f"평탄도            {r.flatness:.2f}  (1에 가까울수록 견고)",
            "",
            "판정:",
        ] + _wrap(r.verdict, 46)
        self._analysis_done(lines)
        self._log(f"민감도 분석 완료: {param} 폭 {r.spread:.1f}%p")

    def _run_period(self) -> None:
        got = self._bt_inputs()
        if not got:
            return
        name, syms, rc, pname, cash = got
        cls = REGISTRY.get(name)
        self._analysis_header(f"연도별 성과 - {cls.label}", pname, cash, syms)
        self._btlog("각 연도마다 워밍업 구간을 앞에서 따로 읽어 실제 그 해 성과만 계산합니다.")
        self._btlog("")

        def job():
            from .analysis import Analyzer
            an = Analyzer(self.core.store, self.core.cfg.cost)
            rows = an.period_split(name, self._bt_params(name), rc, syms, cash,
                                   lambda m: self.after(0, self._btlog, m))
            self.after(0, lambda: self._show_period(an, rows))
        self._thread(job)

    def _show_period(self, an, rows: list) -> None:
        if not rows:
            self._analysis_done(["연도별로 나눌 만한 데이터가 없습니다.",
                                 "[데이터] 탭에서 일봉을 더 수집하세요."])
            return
        s = an.period_summary(rows)
        lines = [
            f"{'연도':<8}{'수익률':>10}{'MDD':>9}{'거래':>7}{'승률':>8}",
        ]
        for r in rows:
            lines.append(f"{r['period']:<8}{r['return_pct']:>9.2f}%{r['mdd_pct']:>8.1f}%"
                         f"{r['trades']:>7}{r['win_rate']:>7.0f}%")
        lines += [
            "",
            f"연평균            {s['mean']:+.2f} %",
            f"표준편차          {s['stdev']:.2f} %p",
            f"최고 / 최저       {s['best']:+.1f}% / {s['worst']:+.1f}%",
            f"수익난 해         {s['positive_years']}/{s['years']}",
            "",
            "판정:",
        ] + _wrap(s["verdict"], 46)
        self._analysis_done(lines)
        self._log(f"연도별 분석 완료: {s['years']}년 중 {s['positive_years']}년 수익")

    def _run_walkforward(self) -> None:
        """앞 구간에서만 파라미터를 고르고, 뒤 구간 성적으로만 채점한다."""
        got = self._bt_inputs()
        if not got:
            return
        name, syms, rc, pname, cash = got
        param = self.opt_param.get()
        if param.startswith("("):
            messagebox.showinfo("안내", "검증할 파라미터를 고르세요.")
            return
        from .analysis import suggest_values
        cls = REGISTRY.get(name)
        base = self._bt_params(name)
        values = suggest_values(param, base.get(param))
        if not values:
            messagebox.showinfo("안내", f"'{param}'는 자동 탐색 범위를 잡을 수 없습니다.")
            return

        self._analysis_header(f"워크포워드 검증 - {cls.label} / {param}",
                              pname, cash, syms)
        self._btlog("전 구간을 보고 고른 파라미터는 이미 답을 알고 있습니다.")
        self._btlog("여기서는 앞 구간에서만 고르고, 고를 때 보지 않은 뒤 구간으로만 채점합니다.")
        self._btlog(f"탐색값: {values}")
        self._btlog("")

        def job():
            from .analysis import Analyzer
            an = Analyzer(self.core.store, self.core.cfg.cost)
            try:
                w = an.walk_forward(name, base, rc, syms, param, values,
                                    folds=4, cash=cash,
                                    log_fn=lambda m: self.after(0, self._btlog, m))
            except Exception as ex:
                self.after(0, lambda: self._analysis_done([f"실패: {ex}"]))
                return
            self.after(0, lambda: self._show_walkforward(param, w))
        self._thread(job)

    def _show_walkforward(self, param: str, w) -> None:
        if w.error:
            self._analysis_done([f"워크포워드 실패", "", *_wrap(w.error, 46)])
            self._btlog(f"[실패] {w.error}")
            return

        self._btlog("")
        self._btlog(f"{'폴드':<6}{'검증구간':<24}{param:>10}{'학습':>9}{'검증':>9}"
                    f"{'보유':>9}{'거래':>6}")
        for f in w.folds:
            if f.note:
                self._btlog(f"{f.idx:<6}{f.test_start}~{f.test_end}   {f.note}")
                continue
            self._btlog(f"{f.idx:<6}{f.test_start}~{f.test_end:<12}"
                        f"{str(f.value):>10}{f.is_return:>8.1f}%{f.oos_return:>8.1f}%"
                        f"{f.bh_return:>8.1f}%{f.oos_trades:>6}")

        scored = [f for f in w.folds if f.value is not None and not f.note]
        lines = [
            f"검증 폴드          {len(scored)} 개",
            f"수익난 폴드        {w.positive_folds}/{len(scored)}",
            "",
            f"학습구간 누적      {w.is_return_pct:+.2f} %   <- 답을 보고 고른 성적",
            f"검증구간 누적      {w.oos_return_pct:+.2f} %   <- 실전에 가까운 성적",
            f"그냥 보유했다면    {w.bh_return_pct:+.2f} %",
            "",
            f"재현 효율          {w.efficiency * 100:.0f} %   (검증/학습)",
            f"최적값 일치율      {w.param_stability * 100:.0f} %   "
            f"(폴드마다 같은 값이 뽑혔나)",
            f"가장 많이 뽑힌 값  {param} = {w.best_value}",
            "",
            "판정:",
        ] + _wrap(w.verdict, 46)
        self._analysis_done(lines)
        self._btlog("")
        self._btlog(f"[판정] {w.verdict}")
        self._log(f"워크포워드 완료: 검증 {w.oos_return_pct:+.1f}% / "
                  f"효율 {w.efficiency * 100:.0f}%")

    def _run_mc(self) -> None:
        got = self._bt_inputs()
        if not got:
            return
        name, syms, rc, pname, cash = got
        cls = REGISTRY.get(name)
        self._analysis_header(f"몬테카를로 - {cls.label}", pname, cash, syms)
        self._btlog("같은 거래들을 복원추출로 3,000번 다시 뽑아 결과 분포를 구합니다.")
        self._btlog("백테스트 수익률 한 개는 '일어날 수 있었던 여러 결과 중 하나'일 뿐입니다.")
        self._btlog("")

        def job():
            from .analysis import Analyzer
            from .backtest import Backtester
            bt = Backtester(self.core.store, self.core.cfg.cost, rc)
            r = bt.run(name, self._bt_params(name), syms, cash)
            if r.error:
                self.after(0, lambda: self._analysis_done([f"실패: {r.error}"]))
                return
            an = Analyzer(self.core.store, self.core.cfg.cost)
            mc = an.monte_carlo(r.trades, cash, runs=3000)
            self.after(0, lambda: self._show_mc(r, mc))
        self._thread(job)

    def _show_mc(self, r, mc: dict) -> None:
        if mc.get("error"):
            self._analysis_done([mc["error"]])
            return
        actual = r.metrics.get("total_return_pct", 0)
        self._btlog(f"실제 백테스트 결과: {actual:+.2f}%  ({mc['trades']}거래)")
        self._btlog("")
        self._btlog("수익률 분포")
        for lbl, key in (("하위 5%", "return_p05"), ("하위 25%", "return_p25"),
                         ("중앙값", "return_median"), ("상위 25%", "return_p75"),
                         ("상위 5%", "return_p95")):
            self._btlog(f"   {lbl:<9}{mc[key]:+9.2f}%")
        self._btlog("")
        self._btlog("최대낙폭 분포")
        for lbl, key in (("중앙값", "mdd_median"), ("나쁜 경우(95%)", "mdd_p95"),
                         ("최악", "mdd_worst")):
            self._btlog(f"   {lbl:<14}{mc[key]:8.2f}%")
        lines = [
            f"시행               {mc['runs']:,} 회",
            f"거래 표본          {mc['trades']} 건",
            "",
            f"실제 결과          {actual:+.2f} %",
            f"분포 중앙값        {mc['return_median']:+.2f} %",
            f"90% 구간           {mc['return_p05']:+.1f}% ~ {mc['return_p95']:+.1f}%",
            "",
            f"손실로 끝날 확률   {mc['loss_prob']:.0f} %",
            f"반토막 날 확률     {mc['ruin_prob']:.1f} %",
            f"나쁜 경우 MDD      {mc['mdd_p95']:.1f} %",
            "",
            "판정:",
        ] + _wrap(mc["verdict"], 46)
        if actual > mc["return_p75"]:
            lines += [""] + _wrap(
                "실제 백테스트 결과가 분포 상위 25%에 있습니다. "
                "운이 좋았던 경로일 수 있으니 중앙값 기준으로 판단하세요.", 46)
        self._analysis_done(lines)
        self._log(f"몬테카를로 완료: 손실확률 {mc['loss_prob']:.0f}%, "
                  f"90% 구간 {mc['return_p05']:+.1f}~{mc['return_p95']:+.1f}%")

    def _run_sweep(self) -> None:
        name = self._strategy_key()
        syms = [s.strip() for s in self.bt_syms.get().replace(" ", ",").split(",") if s.strip()]
        grid = _SWEEP_GRID.get(name)
        if not grid:
            messagebox.showinfo("안내", "이 전략에는 기본 탐색 격자가 없습니다.")
            return
        start, end = self.bt_start.get().strip(), self.bt_end.get().strip()
        try:
            cash = int(float(self.bt_cash.get()))
        except ValueError:
            cash = 10_000_000
        n = 1
        for v in grid.values():
            n *= len(v)
        if not messagebox.askyesno("파라미터 최적화",
                                   f"{n}개 조합을 백테스트합니다. 수십 초 걸릴 수 있습니다.\n계속할까요?"):
            return
        self._btlog(f"그리드 탐색 시작: {n}개 조합")
        self.bt_prog.configure(mode="indeterminate")
        self.bt_prog.start()

        def job():
            res = self.core.backtester.sweep(name, self._bt_params(name), grid, syms,
                                             cash, start, end,
                                             lambda m: self.after(0, self._btlog, m))
            self.after(0, lambda: self._show_sweep(name, res))
        self._thread(job)

    def _show_sweep(self, name: str, res: list) -> None:
        self.bt_prog.stop()
        self.bt_prog.configure(mode="determinate")
        self.bt_prog.set(1)
        if not res:
            self._btlog("[실패] 유효한 결과 없음. 데이터를 먼저 수집하세요.")
            return
        self._btlog("")
        self._btlog("=== 상위 10개 (수익/MDD 기준) ===")
        for r in res[:10]:
            m = r["metrics"]
            self._btlog(f"{json.dumps(r['params'], ensure_ascii=False)} -> "
                        f"수익 {m['total_return_pct']:+.1f}% / MDD {m['mdd_pct']:.1f}% / "
                        f"{m['trades']}건 / PF {m['profit_factor']}")
        self._last_sweep = (name, res)
        if self.core.ai.available:
            self._btlog("")
            self._btlog("AI에게 해석을 요청합니다...")

            def job():
                out = self.core.ai.suggest_params(name, res, self._bt_params(name))
                self.after(0, lambda: self._sweep_ai(out))
            self._thread(job)

    def _sweep_ai(self, out: dict | None) -> None:
        if not out:
            self._btlog("AI 응답 없음 (키 미설정이거나 호출 실패)")
            return
        self._btlog(f"[AI 추천] {json.dumps(out.get('recommended', {}), ensure_ascii=False)}")
        self._btlog(f"[신뢰도] {out.get('confidence')}")
        self._btlog(f"[근거] {out.get('reasoning')}")
        self._btlog(f"[과최적화 위험] {out.get('overfit_risk')}")
        self._refresh_ai()

    # ==================================================================
    # 운용 랩 - 여러 투자방식을 동시에 가상으로 굴려 비교
    # ==================================================================
    def _tab_lab(self, p) -> None:
        ctk.CTkLabel(
            p, anchor="w", justify="left", wraplength=1400, text_color=MUTED,
            text=("장투·스윙·단타를 같은 기간, 같은 시세로 동시에 가상 운용하고 비교합니다. "
                  "프로필마다 가상계좌가 따로 있어서 예수금이 섞이지 않습니다.\n"
                  "실제 주문은 나가지 않습니다 - 여기서 이긴 방식을 [설정]에서 모의투자로 옮기세요."),
        ).pack(fill="x", padx=12, pady=(10, 6))

        top = ctk.CTkFrame(p, fg_color=CARD, corner_radius=8)
        top.pack(fill="x", padx=4, pady=(0, 6))
        head = ctk.CTkFrame(top, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(head, text="투자 프로필", anchor="w",
                     font=("", 13, "bold")).pack(side="left")
        for txt, cmd, w, col in (
                ("＋ 새 프로필", self._lab_new, 110, "#3a3a3a"),
                ("복제", self._lab_dup, 70, "#3a3a3a"),
                ("편집", self._lab_edit, 70, "#3a3a3a"),
                ("삭제", self._lab_delete, 70, "#7a1f1f"),
                ("기본 프리셋 복원", self._lab_restore, 140, "#3a3a3a")):
            ctk.CTkButton(head, text=txt, width=w, fg_color=col,
                          hover_color="#4a4a4a" if col == "#3a3a3a" else "#9b2626",
                          command=cmd).pack(side="right", padx=3)

        self.tv_profile = self._tree(top, [
            ("name", "프로필", 130), ("hz", "성향", 100), ("st", "전략", 200),
            ("cash", "초기자금", 110), ("risk", "1회손실", 80),
            ("pos", "최대보유", 80), ("exit", "청산", 90), ("state", "상태", 80)], 5)
        self.tv_profile.pack(fill="x", padx=10, pady=(0, 6))
        self.tv_profile.tag_configure("run", foreground=OK)

        row = ctk.CTkFrame(top, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(0, 10))
        ctk.CTkButton(row, text="▶ 선택 프로필 가상운용 시작", width=200,
                      command=self._lab_start).pack(side="left")
        ctk.CTkButton(row, text="■ 정지", width=90, fg_color="#7a4a1f",
                      hover_color="#96601f", command=self._lab_stop).pack(side="left", padx=6)
        ctk.CTkButton(row, text="기록 초기화", width=110, fg_color="#7a1f1f",
                      hover_color="#9b2626", command=self._lab_reset).pack(side="left", padx=6)
        ctk.CTkButton(row, text="전부 정지", width=90, fg_color="#3a3a3a",
                      hover_color="#4a4a4a", command=self._lab_stop_all).pack(side="left", padx=6)
        ctk.CTkButton(row, text="⚡ 즉시 검증 (과거 데이터)", width=180, fg_color="#3a5f8a",
                      hover_color="#46709c",
                      command=self._lab_quick_test).pack(side="left", padx=(16, 6))
        self.lbl_lab = ctk.CTkLabel(row, text="", text_color=MUTED, font=("", 11))
        self.lbl_lab.pack(side="left", padx=12)

        bgrow = ctk.CTkFrame(top, fg_color="transparent")
        bgrow.pack(fill="x", padx=10, pady=(0, 10))
        ctk.CTkLabel(bgrow, text="백그라운드", font=("", 12, "bold")).pack(side="left", padx=(0, 8))
        ctk.CTkButton(bgrow, text="창 없이 실행", width=110,
                      command=self._daemon_start).pack(side="left", padx=3)
        ctk.CTkButton(bgrow, text="⚡ 전략 자동 탐색", width=150, fg_color="#3a5f8a",
                      hover_color="#46709c",
                      command=self._open_scan).pack(side="left", padx=(20, 3))
        ctk.CTkButton(bgrow, text="백그라운드 정지", width=120, fg_color="#7a4a1f",
                      hover_color="#96601f", command=self._daemon_stop).pack(side="left", padx=3)
        self.lbl_daemon = ctk.CTkLabel(bgrow, text="", text_color=MUTED, font=("", 11))
        self.lbl_daemon.pack(side="left", padx=12)
        ctk.CTkLabel(bgrow, text="이 프로그램을 꺼도 계속 돕니다. 결과는 다시 켰을 때 그대로 보입니다.",
                     text_color=MUTED, font=("", 10)).pack(side="right")

        mid = ctk.CTkFrame(p, fg_color=CARD, corner_radius=8)
        mid.pack(fill="both", expand=True, padx=4, pady=(0, 6))
        ctk.CTkLabel(mid, text="성과 비교  (실제 경과시간 기준)", anchor="w",
                     font=("", 13, "bold")).pack(fill="x", padx=12, pady=(10, 4))
        self.tv_perf = self._tree(mid, [
            ("name", "프로필", 120), ("state", "상태", 70), ("elapsed", "경과", 90),
            ("init", "초기자금", 105), ("equity", "현재평가", 105),
            ("pnl", "총손익", 100), ("ret", "수익률", 80),
            ("real", "실현", 95), ("unreal", "평가", 95),
            ("pm", "예상 월", 105), ("py", "예상 연", 110),
            ("tr", "거래", 55), ("win", "승률", 65), ("mdd", "MDD", 65)], 8)
        self.tv_perf.pack(fill="both", expand=True, padx=10, pady=(0, 4))
        self.tv_perf.tag_configure("up", foreground=UP)
        self.tv_perf.tag_configure("down", foreground=DOWN)
        self.lbl_caveat = ctk.CTkLabel(mid, text="", text_color=WARN, anchor="w",
                                       justify="left", font=("", 11), wraplength=1400)
        self.lbl_caveat.pack(fill="x", padx=12, pady=(0, 8))

        bot = ctk.CTkFrame(p, fg_color=CARD, corner_radius=8, height=230)
        bot.pack(fill="x", padx=4, pady=(0, 8))
        bot.pack_propagate(False)
        ctk.CTkLabel(bot, text="자산곡선 비교", anchor="w",
                     font=("", 13, "bold")).pack(fill="x", padx=12, pady=(8, 2))
        self.lab_canvas = tk.Canvas(bot, bg=CARD, highlightthickness=0)
        self.lab_canvas.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        self._lab_refresh()

    # -- 프로필 목록 --------------------------------------------------------
    def _lab_selected(self) -> str | None:
        sel = self.tv_profile.selection()
        if not sel:
            messagebox.showinfo("안내", "프로필을 목록에서 선택하세요.")
            return None
        return str(self.tv_profile.item(sel[0], "values")[0])

    def _lab_refresh(self) -> None:
        lab = self.core.lab
        d = self._daemon_status()
        # 백그라운드 데몬이 돌리는 프로필은 이 프로세스가 모른다. 합쳐서 보여준다.
        bg = set(d["profiles"]) if d["alive"] else set()
        running = set(lab.running_names()) | bg
        self.tv_profile.delete(*self.tv_profile.get_children())
        for pr in self.core.profiles.profiles:
            fx = (pr.execution or {}).get("force_exit_at", "")
            self.tv_profile.insert("", "end", tags=("run",) if pr.name in running else (), values=(
                pr.name, pr.horizon_label, pr.strategy_labels(),
                f"{pr.initial_cash:,}",
                f"{(pr.risk or {}).get('max_loss_per_trade_pct', '-')}%",
                (pr.risk or {}).get("max_positions", "-"),
                fx if fx else "보유",
                ("백그라운드" if pr.name in bg else
                 "운용중" if pr.name in running else "정지")))
        self.lbl_lab.configure(
            text=(f"운용중 {len(running)}개: {', '.join(running)}" if running
                  else "운용중인 프로필 없음"))

        if d["alive"]:
            ago = int(d["seconds_ago"] or 0)
            self.lbl_daemon.configure(
                text=f"● 백그라운드 실행중 ({', '.join(d['profiles'])}) "
                     f"- {ago}초 전 신호, {d['started']} 시작",
                text_color=OK)
        elif d["stopping"]:
            self.lbl_daemon.configure(text="정지 처리 중…", text_color=WARN)
        else:
            self.lbl_daemon.configure(text="● 백그라운드 정지됨", text_color=MUTED)

        rows = lab.all_performance()
        self.tv_perf.delete(*self.tv_perf.get_children())
        caveats = []
        for r in rows:
            tag = "up" if r["total_pnl"] > 0 else ("down" if r["total_pnl"] < 0 else "")
            state = ("백그라운드" if r["name"] in bg
                     else "운용중" if r["running"] else "정지")
            self.tv_perf.insert("", "end", tags=(tag,), values=(
                r["name"], state, r["elapsed"],
                money(r["initial"]), money(r["equity"]),
                f"{r['total_pnl']:+,.0f}", f"{r['return_pct']:+.2f}%",
                f"{r['realized']:+,.0f}", f"{r['unrealized']:+,.0f}",
                f"{r['proj_month_amt']:+,.0f}" if r["reliable"] else "—",
                f"{r['proj_year_amt']:+,.0f}" if r["reliable"] else "—",
                r["trades"], f"{r['win_rate']:.0f}%", f"{r['mdd_pct']:.1f}%"))
            if r["caveat"]:
                caveats.append(f"{r['name']}: {r['caveat']}")
        self.lbl_caveat.configure(
            text=("  |  ".join(caveats[:3]) + ("   (예상 월/연 금액은 표본이 쌓이면 표시됩니다)"
                                               if caveats else ""))
            if caveats else "")
        self._draw_lab_curves()

    def _draw_lab_curves(self) -> None:
        c = self.lab_canvas
        c.delete("all")
        curves = self.core.lab.equity_curves()
        curves = {k: v for k, v in curves.items() if len(v) >= 2}
        if not curves:
            c.create_text(20, 20, text="가상운용을 시작하면 자산곡선이 그려집니다.",
                          fill=MUTED, anchor="nw")
            return
        w = max(c.winfo_width(), 600)
        h = max(c.winfo_height(), 150)
        pad = 46
        # 각 곡선을 초기자금 대비 % 로 정규화해야 자금이 달라도 비교가 된다
        norm = {}
        for name, pts in curves.items():
            base = pts[0][1] or 1
            norm[name] = [(v / base - 1) * 100 for _t, v in pts]
        lo = min(min(v) for v in norm.values())
        hi = max(max(v) for v in norm.values())
        if hi - lo < 0.5:
            lo, hi = lo - 0.5, hi + 0.5
        colors = ["#ff5252", "#4d9fff", "#4caf50", "#ffb300", "#ab47bc", "#26c6da"]

        y0 = h - pad - (h - pad * 1.7) * (0 - lo) / (hi - lo)
        c.create_line(pad, y0, w - 10, y0, fill="#4a4a4a", dash=(3, 3))
        c.create_text(pad - 6, y0, text="0%", fill=MUTED, anchor="e", font=("", 9))
        c.create_text(pad - 6, pad * 0.6, text=f"{hi:+.1f}%", fill=MUTED,
                      anchor="e", font=("", 9))
        c.create_text(pad - 6, h - pad, text=f"{lo:+.1f}%", fill=MUTED,
                      anchor="e", font=("", 9))

        for i, (name, vals) in enumerate(norm.items()):
            col = colors[i % len(colors)]
            pts = []
            n = len(vals)
            for j, v in enumerate(vals):
                x = pad + (w - pad - 20) * (j / max(n - 1, 1))
                y = h - pad - (h - pad * 1.7) * (v - lo) / (hi - lo)
                pts.extend([x, y])
            if len(pts) >= 4:
                c.create_line(*pts, fill=col, width=2)
            c.create_text(pad + 8 + i * 150, pad * 0.5,
                          text=f"● {name} {vals[-1]:+.2f}%", fill=col,
                          anchor="w", font=("", 10, "bold"))

    # -- 프로필 조작 --------------------------------------------------------
    def _lab_start(self) -> None:
        name = self._lab_selected()
        if not name:
            return
        pr = self.core.profiles.get(name)
        if not pr:
            return
        ok, msg = self.core.lab.start(pr)
        self._log(msg, "engine" if ok else "error")
        if not ok:
            messagebox.showerror("시작 실패", msg)
        self.after(500, self._lab_refresh)

    def _lab_stop(self) -> None:
        name = self._lab_selected()
        if not name:
            return
        ok, msg = self.core.lab.stop(name)
        self._log(msg, "engine" if ok else "error")
        self.after(300, self._lab_refresh)

    def _lab_stop_all(self) -> None:
        self.core.lab.stop_all()
        self._log("모든 가상운용 정지", "engine")
        self.after(300, self._lab_refresh)

    def _lab_reset(self) -> None:
        name = self._lab_selected()
        if not name:
            return
        if not messagebox.askyesno(
                "기록 초기화",
                f"'{name}'의 가상계좌와 거래기록을 전부 지우고 처음부터 다시 시작합니다.\n계속할까요?"):
            return
        ok, msg = self.core.lab.reset(name)
        self._log(msg, "engine" if ok else "error")
        if not ok:
            messagebox.showerror("초기화 실패", msg)
        self.after(300, self._lab_refresh)

    def _lab_new(self) -> None:
        from .profiles import Profile
        base = self.core.profiles.get("스윙(중기)") or self.core.profiles.profiles[0]
        import copy
        p = copy.deepcopy(base)
        p.name = self._unique_name("내 전략")
        p.horizon = "custom"
        p.description = ""
        ProfileDialog(self, p, self._lab_saved)

    def _lab_dup(self) -> None:
        name = self._lab_selected()
        if not name:
            return
        new = self.core.profiles.duplicate(name, self._unique_name(f"{name} 복사"))
        if new:
            self._log(f"프로필 복제: {new.name}")
            self._lab_refresh()
            ProfileDialog(self, new, self._lab_saved)

    def _lab_edit(self) -> None:
        name = self._lab_selected()
        if not name:
            return
        pr = self.core.profiles.get(name)
        if not pr:
            return
        if name in self.core.lab.running_names():
            messagebox.showinfo("안내", "운용 중인 프로필은 정지 후 수정하세요.")
            return
        ProfileDialog(self, pr, self._lab_saved)

    def _lab_delete(self) -> None:
        name = self._lab_selected()
        if not name:
            return
        if name in self.core.lab.running_names():
            messagebox.showinfo("안내", "운용 중인 프로필은 정지 후 삭제하세요.")
            return
        if not messagebox.askyesno("삭제", f"프로필 '{name}'을 삭제할까요?\n(거래기록도 함께 지웁니다)"):
            return
        self.core.lab.reset(name)
        self.core.profiles.remove(name)
        self._log(f"프로필 삭제: {name}")
        self._lab_refresh()

    def _lab_restore(self) -> None:
        if not messagebox.askyesno("기본 프리셋 복원",
                                   "장기투자 / 스윙 / 단타 / 분봉단타 프리셋을 기본값으로 되돌립니다.\n"
                                   "직접 만든 프로필은 그대로 남습니다. 계속할까요?"):
            return
        self.core.profiles.reset_builtin()
        self._log("기본 프리셋 복원됨")
        self._lab_refresh()

    # -- 즉시 검증 / 백그라운드 --------------------------------------------
    def _lab_quick_test(self) -> None:
        """실시간으로 며칠 기다리지 않고 과거 데이터로 같은 설정을 즉시 돌려본다."""
        name = self._lab_selected()
        if not name:
            return
        pr = self.core.profiles.get(name)
        if not pr:
            return
        st = [x for x in pr.strategies if x.get("enabled")]
        if not st:
            messagebox.showerror("오류", "이 프로필에 켜진 전략이 없습니다.")
            return
        sname = st[0]["name"]
        params = st[0].get("params") or {}
        syms = pr.watchlist or self.core.cfg.watchlist
        from .settings import RiskConfig
        rc = RiskConfig()
        for k, v in asdict(self.core.cfg.risk).items():
            setattr(rc, k, v)
        for k, v in (pr.risk or {}).items():
            if hasattr(rc, k):
                setattr(rc, k, v)

        self._log(f"'{name}' 즉시 검증 시작 (과거 데이터, {len(syms)}종목)")

        def job():
            from .backtest import Backtester
            from .analysis import Analyzer
            r = Backtester(self.core.store, self.core.cfg.cost, rc).run(
                sname, params, syms, pr.initial_cash)
            mc = None
            if not r.error:
                mc = Analyzer(self.core.store, self.core.cfg.cost).monte_carlo(
                    r.trades, pr.initial_cash, runs=2000)
            self.after(0, lambda: self._show_quick(name, r, mc))
        self._thread(job)

    def _show_quick(self, name: str, r, mc) -> None:
        if r.error:
            messagebox.showerror("즉시 검증 실패", r.error)
            return
        m = r.metrics
        lines = [
            f"[{name}] 과거 데이터 검증 결과",
            f"기간            {r.start} ~ {r.end}  ({m.get('bars', 0)}일)",
            "",
            f"수익률          {m.get('total_return_pct', 0):+.2f} %   "
            f"(비용 전 {m.get('gross_return_pct', 0):+.2f} %)",
            f"거래비용        -{m.get('cost_drag_pct', 0):.2f} %p",
            f"연환산          {m.get('cagr_pct', 0):+.2f} %",
            f"최대낙폭        {m.get('mdd_pct', 0):.2f} %",
            f"거래 / 승률     {m.get('trades', 0)}건 / {m.get('win_rate', 0)}%",
            f"평균 보유       {m.get('avg_bars_held', 0)}봉",
        ]
        if mc and not mc.get("error"):
            lines += [
                "",
                "같은 거래를 다시 뽑았을 때 (몬테카를로 2,000회)",
                f"  90% 구간      {mc['return_p05']:+.1f}% ~ {mc['return_p95']:+.1f}%",
                f"  중앙값        {mc['return_median']:+.1f}%",
                f"  손실 확률     {mc['loss_prob']:.0f}%",
                f"  나쁜 경우 MDD {mc['mdd_p95']:.1f}%",
            ]
        if r.warnings:
            lines += ["", "경고:"] + [f"  - {w}" for w in r.warnings]
        messagebox.showinfo(f"즉시 검증 - {name}", "\n".join(lines))
        self._log(f"'{name}' 즉시 검증: {m.get('total_return_pct', 0):+.2f}% / "
                  f"MDD {m.get('mdd_pct', 0):.1f}% / {m.get('trades', 0)}건"
                  + (f" / 손실확률 {mc['loss_prob']:.0f}%"
                     if mc and not mc.get("error") else ""))

    def _open_scan(self) -> None:
        """선택된 종목(없으면 관심종목)으로 자동 탐색 창을 연다."""
        syms = []
        for tv in (getattr(self, "tv_search", None), getattr(self, "tv_watch", None)):
            if tv is not None:
                syms += self._selected_symbols(tv)
        seen, picked = set(), []
        for x in syms or self.core.cfg.watchlist:
            if x not in seen:
                seen.add(x)
                picked.append(x)
        if self.core.master.count == 0:
            self._log("종목DB가 비어 있어 종목명 검색은 안 됩니다 (코드는 가능).", "warn")
        ScanDialog(self, picked)

    def _daemon_status(self) -> dict:
        from .daemon import status
        return status(self.core.store)

    def _daemon_start(self) -> None:
        import subprocess
        st = self._daemon_status()
        if st["alive"]:
            messagebox.showinfo("안내",
                                f"이미 백그라운드에서 돌고 있습니다.\n"
                                f"프로필: {', '.join(st['profiles'])}")
            return
        names = self.core.lab.running_names() or self.core.profiles.names()
        if not messagebox.askyesno(
                "백그라운드 실행",
                f"창 없이 다음 프로필을 굴립니다.\n\n  {', '.join(names)}\n\n"
                "이 프로그램을 꺼도 계속 돕니다.\n"
                "(현재 창에서 돌고 있는 운용은 중복을 피하려고 정지합니다)\n\n계속할까요?"):
            return
        self.core.lab.stop_all()

        exe = sys.executable
        if getattr(sys, "frozen", False):
            cmd = [exe, "--lab"] + names
        else:
            pyw = str(Path(exe).with_name("pythonw.exe"))
            runner = str(Path(__file__).resolve().parent.parent / "run.py")
            cmd = [pyw if Path(pyw).exists() else exe, runner, "--lab"] + names
        try:
            from .settings import child_env
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            # 같은 exe를 새 인스턴스로 띄우는 자리다. onefile 런타임 변수를 물려주면
            # 새 프로세스가 이 창의 임시 압축해제 폴더를 자기 것으로 쓰다가,
            # 창을 닫는 순간 그 폴더가 지워져 백그라운드 운용이 함께 죽는다.
            subprocess.Popen(cmd, cwd=str(Path(__file__).resolve().parent.parent),
                             env=child_env(), creationflags=flags)
        except Exception as e:
            messagebox.showerror("실행 실패", str(e))
            return
        self._log(f"백그라운드 실행 요청: {', '.join(names)}", "engine")
        self.after(4000, self._lab_refresh)

    def _daemon_stop(self) -> None:
        from .daemon import request_stop
        st = self._daemon_status()
        if not st["alive"]:
            messagebox.showinfo("안내", "돌고 있는 백그라운드 운용이 없습니다.")
            return
        request_stop(self.core.store)
        self._log("백그라운드 정지 요청 (최대 10초 소요)", "engine")
        self.after(12000, self._lab_refresh)

    def _lab_saved(self, profile) -> None:
        self.core.profiles.upsert(profile)
        self._log(f"프로필 저장: {profile.name}")
        self._lab_refresh()

    def _unique_name(self, base: str) -> str:
        names = set(self.core.profiles.names())
        if base not in names:
            return base
        i = 2
        while f"{base} {i}" in names:
            i += 1
        return f"{base} {i}"

    # ==================================================================
    # 데이터
    # ==================================================================
    def _tab_data(self, p) -> None:
        bar = ctk.CTkFrame(p, fg_color=CARD, corner_radius=8)
        bar.pack(fill="x", padx=4, pady=(6, 8))
        ctk.CTkLabel(bar, anchor="w", justify="left", wraplength=1150, text_color=MUTED,
                     text=("백테스트는 여기 쌓인 데이터로만 돌아갑니다. 처음 실행이라면 "
                           "일봉부터 수집하세요. 분봉은 KIS가 30건씩만 주기 때문에 종목당 "
                           "여러 번 호출합니다 (당일치만 가능).")
                     ).pack(fill="x", padx=12, pady=(10, 6))
        r = ctk.CTkFrame(bar, fg_color="transparent")
        r.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkLabel(r, text="일봉 수집 일수").pack(side="left")
        self.e_days = ctk.CTkEntry(r, width=80)
        self.e_days.insert(0, str(self.core.cfg.data.daily_history_days))
        self.e_days.pack(side="left", padx=6)
        ctk.CTkButton(r, text="일봉 수집", width=110,
                      command=lambda: self._collect("daily")).pack(side="left", padx=6)
        ctk.CTkButton(r, text="분봉 수집 (당일)", width=140, fg_color="#3a3a3a",
                      hover_color="#4a4a4a",
                      command=lambda: self._collect("minute")).pack(side="left", padx=6)
        ctk.CTkButton(r, text="현재가 스냅샷", width=120, fg_color="#3a3a3a",
                      hover_color="#4a4a4a",
                      command=lambda: self._collect("snap")).pack(side="left", padx=6)
        ctk.CTkButton(r, text="보유현황 새로고침", width=140, fg_color="#3a3a3a",
                      hover_color="#4a4a4a", command=self._refresh_coverage).pack(side="left", padx=6)

        f = ctk.CTkFrame(p, fg_color=CARD, corner_radius=8)
        f.pack(fill="both", expand=True, padx=4, pady=(0, 8))
        ctk.CTkLabel(f, text="종목별 데이터 보유 현황", anchor="w",
                     font=("", 13, "bold")).pack(fill="x", padx=12, pady=(10, 4))
        self.tv_data = self._tree(f, [
            ("sym", "종목", 90), ("d", "일봉", 70), ("df", "일봉 시작", 110),
            ("dt", "일봉 끝", 110), ("m", "분봉", 80),
            ("mf", "분봉 시작", 140), ("mt", "분봉 끝", 140)], 10)
        self.tv_data.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        self.data_log = ctk.CTkTextbox(f, height=150, font=("Consolas", 11))
        self.data_log.pack(fill="x", padx=10, pady=(0, 10))
        self.data_log.configure(state="disabled")

    def _dlog(self, msg: str) -> None:
        self.data_log.configure(state="normal")
        self.data_log.insert("end", msg + "\n")
        self.data_log.see("end")
        self.data_log.configure(state="disabled")

    def _collect(self, kind: str) -> None:
        if not self.core.collector:
            messagebox.showerror("오류", "시세 클라이언트가 없습니다. .env에 KIS 키를 넣어주세요.")
            return
        syms = self.core.cfg.watchlist
        say = lambda m: self.after(0, self._dlog, m)

        def job():
            if kind == "daily":
                try:
                    days = int(float(self.e_days.get()))
                except ValueError:
                    days = 400
                self.core.cfg.data.daily_history_days = days
                self.core.save(rebuild=False)
                self.core.collector.sync_daily_all(syms, days, say)
            elif kind == "minute":
                self.core.collector.sync_minute_all(syms, say)
            else:
                snap = self.core.collector.snapshot(syms)
                for s, q in snap.items():
                    say(f"  {s} {q['price']:,}원 ({q['change_pct']:+.2f}%) "
                        f"거래량 {q['volume']:,}")
            self.after(0, self._refresh_coverage)
        self._dlog(f"--- {kind} 수집 시작 ({len(syms)}종목) ---")
        self._thread(job)

    def _refresh_coverage(self) -> None:
        if not self.core.collector:
            return
        for i in self.tv_data.get_children():
            self.tv_data.delete(i)
        for row in self.core.collector.coverage(self.core.cfg.watchlist):
            self.tv_data.insert("", "end", values=(
                row["symbol"], row["daily"], row["daily_from"], row["daily_to"],
                row["minute"], row["minute_from"], row["minute_to"]))

    # ==================================================================
    # AI
    # ==================================================================
    def _tab_ai(self, p) -> None:
        bar = ctk.CTkFrame(p, fg_color=CARD, corner_radius=8)
        bar.pack(fill="x", padx=4, pady=(6, 8))
        ctk.CTkLabel(bar, anchor="w", justify="left", wraplength=1150,
                     text=("AI는 매매 결정을 직접 내리지 않습니다. "
                           "같은 질문에 매번 다른 답이 나오면 백테스트가 불가능해지고, "
                           "그러면 그 전략이 돈을 버는지 확인할 방법이 사라지기 때문입니다.\n"
                           "AI가 하는 일: ① 하루 거래 기록 리뷰  ② 백테스트 결과 해석과 "
                           "파라미터 제안  ③ 진입 거부권(악재 필터).")
                     ).pack(fill="x", padx=12, pady=(10, 8))
        r = ctk.CTkFrame(bar, fg_color="transparent")
        r.pack(fill="x", padx=12, pady=(0, 12))
        self.sw_ai = ctk.CTkSwitch(r, text="AI 기능 사용")
        if self.core.cfg.ai.enabled:
            self.sw_ai.select()
        self.sw_ai.pack(side="left")
        self.sw_veto = ctk.CTkSwitch(r, text="진입 거부권(악재 필터) 사용")
        if self.core.cfg.ai.veto_filter:
            self.sw_veto.select()
        self.sw_veto.pack(side="left", padx=18)
        self.sw_review = ctk.CTkSwitch(r, text="장 마감 후 일일 리뷰")
        if self.core.cfg.ai.daily_review:
            self.sw_review.select()
        self.sw_review.pack(side="left", padx=18)
        ctk.CTkLabel(r, text="모델").pack(side="left", padx=(18, 4))
        self.e_model = ctk.CTkEntry(r, width=170)
        self.e_model.insert(0, self.core.cfg.ai.model)
        self.e_model.pack(side="left")
        ctk.CTkButton(r, text="저장", width=70, command=self._save_ai).pack(side="left", padx=8)
        ctk.CTkButton(r, text="지금 리뷰 생성", width=130, fg_color="#3a3a3a",
                      hover_color="#4a4a4a", command=self._run_review).pack(side="left", padx=6)

        f = ctk.CTkFrame(p, fg_color=CARD, corner_radius=8)
        f.pack(fill="both", expand=True, padx=4, pady=(0, 8))
        ctk.CTkLabel(f, text="AI 노트 (일일 리뷰 / 파라미터 제안)", anchor="w",
                     font=("", 13, "bold")).pack(fill="x", padx=12, pady=(10, 4))
        self.ai_box = ctk.CTkTextbox(f, font=("Consolas", 12), wrap="word")
        self.ai_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.ai_box.configure(state="disabled")
        self._refresh_ai()

    def _save_ai(self) -> None:
        self.core.cfg.ai.enabled = bool(self.sw_ai.get())
        self.core.cfg.ai.veto_filter = bool(self.sw_veto.get())
        self.core.cfg.ai.daily_review = bool(self.sw_review.get())
        self.core.cfg.ai.model = self.e_model.get().strip() or "gemini-3.5-flash"
        self.core.save()
        self._log("AI 설정 저장 완료")

    def _run_review(self) -> None:
        if not self.core.ai.available:
            messagebox.showerror("오류", "GEMINI_API_KEY가 없거나 AI가 꺼져 있습니다.")
            return

        def job():
            out = self.core.ai.daily_review(self.core.broker.mode)
            self.after(0, self._refresh_ai)
            self.after(0, lambda: self._log("AI 리뷰 생성됨" if out else "리뷰할 기록이 없습니다."))
        self._thread(job)

    def _refresh_ai(self) -> None:
        notes = self.core.store.recent_ai_notes(10)
        self.ai_box.configure(state="normal")
        self.ai_box.delete("1.0", "end")
        if not notes:
            self.ai_box.insert("end", "아직 AI 노트가 없습니다.\n"
                                      "장 마감 후 자동 생성되거나, 위 [지금 리뷰 생성]을 누르세요.")
        for n in notes:
            self.ai_box.insert("end", f"── {n['ts']}  [{n['kind']}] "
                                      f"{'─' * 40}\n")
            try:
                d = json.loads(n["content"])
                self.ai_box.insert("end", json.dumps(d, ensure_ascii=False, indent=2) + "\n\n")
            except Exception:
                self.ai_box.insert("end", n["content"] + "\n\n")
        self.ai_box.configure(state="disabled")

    # ==================================================================
    # 설정
    # ==================================================================
    def _tab_settings(self, p) -> None:
        f = ctk.CTkFrame(p, fg_color=CARD, corner_radius=8)
        f.pack(fill="x", padx=4, pady=(6, 8))
        ctk.CTkLabel(f, text="거래 모드", anchor="w",
                     font=("", 13, "bold")).pack(fill="x", padx=12, pady=(10, 6))
        self.seg_mode = ctk.CTkSegmentedButton(
            f, values=["PAPER", "MOCK", "REAL"], command=self._mode_changed)
        self.seg_mode.set(self.core.cfg.mode)
        self.seg_mode.pack(padx=12, pady=(0, 6), anchor="w")
        ctk.CTkLabel(f, anchor="w", justify="left", text_color=MUTED, wraplength=1100,
                     text=("PAPER = 실제 시세 + 로컬 가상체결 (주문이 나가지 않음)\n"
                           "MOCK  = 한국투자증권 모의투자 계좌에 실제 주문\n"
                           "REAL  = 실전 계좌. 진짜 돈이 나갑니다.")
                     ).pack(fill="x", padx=12, pady=(0, 6))

        self.sw_real = ctk.CTkSwitch(
            f, text="실전투자 사용을 승인합니다 (체크하지 않으면 REAL 선택 시 MOCK으로 대체)",
            command=self._confirm_real)
        if self.core.cfg.real_trading_confirmed:
            self.sw_real.select()
        self.sw_real.pack(padx=12, pady=(0, 6), anchor="w")

        row = ctk.CTkFrame(f, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkLabel(row, text="PAPER 초기자금").pack(side="left")
        self.e_paper = ctk.CTkEntry(row, width=130)
        self.e_paper.insert(0, str(self.core.cfg.paper_initial_cash))
        self.e_paper.pack(side="left", padx=6)
        ctk.CTkButton(row, text="PAPER 계좌 초기화", width=150, fg_color="#3a3a3a",
                      hover_color="#4a4a4a", command=self._reset_paper).pack(side="left", padx=6)
        self.sw_realquote = ctk.CTkSwitch(row, text="시세는 실전 도메인 사용 (권장)")
        if self.core.cfg.data.use_real_for_quotes:
            self.sw_realquote.select()
        self.sw_realquote.pack(side="left", padx=18)
        ctk.CTkButton(row, text="적용", width=80, command=self._apply_settings).pack(side="left")
        ctk.CTkButton(row, text="낙폭 기준 재설정", width=140, fg_color="#7a4a1f",
                      hover_color="#96601f",
                      command=self._rebaseline).pack(side="left", padx=(18, 0))

        g = ctk.CTkFrame(p, fg_color=CARD, corner_radius=8)
        g.pack(fill="x", padx=4, pady=(0, 8))
        ctk.CTkLabel(g, text="Gemini API 키", anchor="w",
                     font=("", 13, "bold")).pack(fill="x", padx=12, pady=(10, 4))
        row = ctk.CTkFrame(g, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(0, 12))
        self.e_gem = ctk.CTkEntry(row, width=420, show="*",
                                  placeholder_text="비워두면 기존 키 유지")
        self.e_gem.pack(side="left")
        ctk.CTkButton(row, text=".env에 저장", width=120,
                      command=self._save_gemini).pack(side="left", padx=8)
        ctk.CTkLabel(row, text="KIS 앱키/시크릿/계좌번호는 .env 파일에서 직접 관리합니다.",
                     text_color=MUTED, font=("", 11)).pack(side="left", padx=8)

        u = ctk.CTkFrame(p, fg_color=CARD, corner_radius=8)
        u.pack(fill="x", padx=4, pady=(0, 8))
        from .version import __version__
        ctk.CTkLabel(u, text=f"자동 업데이트   (현재 v{__version__})", anchor="w",
                     font=("", 13, "bold")).pack(fill="x", padx=12, pady=(10, 2))
        ctk.CTkLabel(u, anchor="w", justify="left", text_color=MUTED, wraplength=1100,
                     font=("", 11),
                     text=("GitHub 릴리즈에 새 버전이 올라오면 받아서 교체합니다. "
                           "저장소를 owner/repo 형식으로 넣어주세요 (예: myid/kis-trader)."),
                     ).pack(fill="x", padx=12, pady=(0, 6))
        ur = ctk.CTkFrame(u, fg_color="transparent")
        ur.pack(fill="x", padx=12, pady=(0, 6))
        ctk.CTkLabel(ur, text="저장소", width=60, anchor="w").pack(side="left")
        self.e_repo = ctk.CTkEntry(ur, width=250, placeholder_text="owner/repo")
        self.e_repo.insert(0, self.core.cfg.update.repo)
        self.e_repo.pack(side="left", padx=(0, 12))
        self.sw_upd_start = ctk.CTkSwitch(ur, text="시작할 때 확인")
        if self.core.cfg.update.check_on_start:
            self.sw_upd_start.select()
        self.sw_upd_start.pack(side="left", padx=6)
        self.sw_upd_auto = ctk.CTkSwitch(ur, text="확인 없이 자동 설치")
        if self.core.cfg.update.auto_install:
            self.sw_upd_auto.select()
        self.sw_upd_auto.pack(side="left", padx=10)
        ctk.CTkButton(ur, text="저장", width=70,
                      command=self._save_update).pack(side="left", padx=6)
        ctk.CTkButton(ur, text="지금 확인", width=100, fg_color="#3a5f8a",
                      hover_color="#46709c",
                      command=lambda: self._check_update(False)).pack(side="left")
        ur2 = ctk.CTkFrame(u, fg_color="transparent")
        ur2.pack(fill="x", padx=12, pady=(0, 12))
        self.lbl_update = ctk.CTkLabel(ur2, text="", anchor="w", text_color=MUTED,
                                       font=("", 11), justify="left")
        self.lbl_update.pack(side="left")
        self.upd_prog = ctk.CTkProgressBar(ur2, width=180)
        self.upd_prog.set(0)

        n = ctk.CTkFrame(p, fg_color=CARD, corner_radius=8)
        n.pack(fill="x", padx=4, pady=(0, 8))
        ctk.CTkLabel(n, text="텔레그램 알림", anchor="w",
                     font=("", 13, "bold")).pack(fill="x", padx=12, pady=(10, 2))
        ctk.CTkLabel(n, anchor="w", justify="left", text_color=MUTED, wraplength=1100,
                     font=("", 11),
                     text=("@BotFather 로 봇을 만들어 토큰을 받고, 그 봇에게 아무 메시지나 한 번 보낸 뒤 "
                           "https://api.telegram.org/bot<토큰>/getUpdates 에서 chat id를 확인하세요."),
                     ).pack(fill="x", padx=12, pady=(0, 6))
        row = ctk.CTkFrame(n, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(0, 6))
        self.sw_notify = ctk.CTkSwitch(row, text="알림 사용")
        if self.core.cfg.notify.enabled:
            self.sw_notify.select()
        self.sw_notify.pack(side="left")
        self.notify_kinds = {}
        for k, label in (("trade", "체결"), ("risk", "리스크"),
                         ("error", "오류"), ("ai", "AI 리뷰")):
            sw = ctk.CTkSwitch(row, text=label, width=70)
            if k in (self.core.cfg.notify.kinds or []):
                sw.select()
            sw.pack(side="left", padx=8)
            self.notify_kinds[k] = sw
        row2 = ctk.CTkFrame(n, fg_color="transparent")
        row2.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkLabel(row2, text="봇 토큰", width=60, anchor="w").pack(side="left")
        self.e_tg_token = ctk.CTkEntry(row2, width=300, show="*",
                                       placeholder_text="비우면 기존 값 유지")
        self.e_tg_token.pack(side="left", padx=(0, 10))
        ctk.CTkLabel(row2, text="chat id", width=60, anchor="w").pack(side="left")
        self.e_tg_chat = ctk.CTkEntry(row2, width=150,
                                      placeholder_text="비우면 기존 값 유지")
        self.e_tg_chat.pack(side="left", padx=(0, 10))
        ctk.CTkButton(row2, text="저장", width=80,
                      command=self._save_notify).pack(side="left", padx=4)
        ctk.CTkButton(row2, text="테스트 전송", width=110, fg_color="#3a3a3a",
                      hover_color="#4a4a4a", command=self._test_notify).pack(side="left")

        d = ctk.CTkFrame(p, fg_color=CARD, corner_radius=8)
        d.pack(fill="both", expand=True, padx=4, pady=(0, 8))
        head = ctk.CTkFrame(d, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(head, text="진단", anchor="w", font=("", 13, "bold")).pack(side="left")
        ctk.CTkButton(head, text="연결 테스트", width=110,
                      command=self._test_conn).pack(side="right")
        ctk.CTkButton(head, text="다시 점검", width=90, fg_color="#3a3a3a",
                      hover_color="#4a4a4a", command=self._refresh_diag).pack(side="right", padx=6)
        self.diag_box = ctk.CTkTextbox(d, font=("Consolas", 12))
        self.diag_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.diag_box.configure(state="disabled")
        self._refresh_diag()

    def _mode_changed(self, value: str) -> None:
        if value == "REAL" and not self.core.cfg.real_trading_confirmed:
            messagebox.showwarning(
                "실전투자 미승인",
                "실전 모드를 쓰려면 아래 '실전투자 사용을 승인합니다'를 먼저 체크하세요.\n"
                "지금은 모의투자로 동작합니다.")
        self.core.cfg.mode = value
        self.core.save()
        self._refresh_diag()
        self._log(f"거래 모드 변경 -> {self.core.broker.mode}")

    def _confirm_real(self) -> None:
        if self.sw_real.get():
            ok = messagebox.askyesno(
                "실전투자 승인",
                "실전 계좌에 실제 주문이 나갑니다.\n\n"
                "모의투자에서 최소 수십 거래 이상 검증하고, 백테스트 결과를 확인한 뒤에 "
                "켜는 것을 강력히 권합니다.\n\n정말 승인하시겠습니까?")
            if not ok:
                self.sw_real.deselect()
                return
        self.core.cfg.real_trading_confirmed = bool(self.sw_real.get())
        self.core.save()
        self._refresh_diag()

    def _apply_settings(self) -> None:
        try:
            self.core.cfg.paper_initial_cash = int(float(self.e_paper.get()))
        except ValueError:
            pass
        self.core.cfg.data.use_real_for_quotes = bool(self.sw_realquote.get())
        self.core.save()
        self._refresh_diag()
        self._log("설정 적용됨")

    def _reset_paper(self) -> None:
        if not messagebox.askyesno("초기화", "로컬 가상계좌를 초기화합니다. 계속할까요?"):
            return
        from .broker import PaperBroker
        pb = PaperBroker(self.core.store, self.core.quote_client,
                         self.core.cfg.cost, self.core.cfg.paper_initial_cash)
        # reset() 이 자산/주문/거래 기록과 낙폭 기준시각까지 함께 정리한다.
        # 이력을 남겨두면 예전 예수금이 최고점으로 남아 새 예수금이 곧바로
        # '낙폭 초과'로 계산되고, 재시작해도 DB에서 같은 값을 읽어와 영원히 멈춘다.
        pb.reset(self.core.cfg.paper_initial_cash, mode="PAPER")
        self.core.rebuild()
        self._log(f"PAPER 계좌 초기화 완료 (예수금 "
                  f"{self.core.cfg.paper_initial_cash:,}원, 자산이력 리셋)")

    def _rebaseline(self) -> None:
        """입출금이나 계좌 변경으로 낙폭이 부풀려졌을 때 기준을 지금 자산으로 되돌린다."""
        eng = self.core.engine
        st = eng.status()
        eq = st.get("equity") or 0
        if eq <= 0:
            messagebox.showinfo("안내", "계좌 조회가 끝난 뒤에 눌러주세요.")
            return
        dd = (st.get("risk") or {}).get("drawdown_pct", 0)
        msg = "\n".join([
            f"지금 자산 {eq:,.0f}원을 새 기준으로 잡습니다.",
            f"현재 계산된 낙폭 {dd:.2f}% 가 0% 가 됩니다.",
            "",
            "입금·출금·계좌 초기화처럼 매매와 무관한 변동 때문에",
            "낙폭이 잘못 잡혔을 때만 쓰세요.",
            "실제 손실을 지우는 데 쓰면 안전장치가 무력해집니다.",
            "",
            "계속할까요?",
        ])
        if not messagebox.askyesno("낙폭 기준 재설정", msg):
            return
        eng.risk.rebaseline(float(eq), "사용자 수동 재설정")
        self._log(f"낙폭 기준을 {eq:,.0f}원으로 재설정했습니다.", "risk")
        self._refresh_now()

    def _save_gemini(self) -> None:
        v = self.e_gem.get().strip()
        if not v:
            return
        save_env("GEMINI_API_KEY", v)
        self.e_gem.delete(0, "end")
        self.core.rebuild()
        self._refresh_diag()
        self._log("Gemini API 키 저장됨")

    # -- 자동 업데이트 ------------------------------------------------------
    def _save_update(self) -> None:
        repo = self.e_repo.get().strip()
        if repo and repo.count("/") != 1:
            messagebox.showerror("오류", "owner/repo 형식으로 넣어주세요. 예: myid/kis-trader")
            return
        self.core.cfg.update.repo = repo
        self.core.cfg.update.check_on_start = bool(self.sw_upd_start.get())
        self.core.cfg.update.auto_install = bool(self.sw_upd_auto.get())
        self.core.save()
        self._refresh_diag()
        self._log(f"업데이트 설정 저장 (저장소 {repo or '미설정'})")

    def _check_update(self, silent: bool = True) -> None:
        """silent=True면 시작 시 자동 확인 (없으면 조용히 넘어간다)."""
        from .updater import repo_name
        if not repo_name(self.core.cfg):
            if not silent:
                messagebox.showinfo("안내", "먼저 저장소를 owner/repo 형식으로 넣고 저장하세요.")
            return
        if not silent:
            self.lbl_update.configure(text="확인 중…", text_color=MUTED)

        def job():
            from .updater import has_update
            newer, rel = has_update(self.core.cfg)
            self.after(0, lambda: self._show_update(newer, rel, silent))
        threading.Thread(target=job, daemon=True).start()

    def _show_update(self, newer: bool, rel, silent: bool) -> None:
        from .version import __version__
        if rel.error:
            self.lbl_update.configure(text=rel.error, text_color=BAD if not silent else MUTED)
            if not silent:
                messagebox.showerror("업데이트 확인 실패", rel.error)
            return
        if not newer:
            msg = f"최신 버전입니다 (v{__version__}, 릴리즈 {rel.version})"
            self.lbl_update.configure(text=msg, text_color=MUTED)
            if not silent:
                messagebox.showinfo("업데이트", msg)
            return

        self._pending_release = rel
        self.lbl_update.configure(
            text=f"새 버전 {rel.version} 이 있습니다 (현재 v{__version__})", text_color=OK)
        self._log(f"새 버전 발견: {rel.version}", "warn")

        from .updater import is_frozen
        if not is_frozen():
            self.lbl_update.configure(
                text=f"새 버전 {rel.version} - 소스 실행 중이라 자동교체는 안 됩니다 (git pull 하세요)",
                text_color=WARN)
            return

        if self.core.cfg.update.auto_install:
            self._install_update()
            return

        notes = (rel.notes or "").strip()
        if len(notes) > 700:
            notes = notes[:700] + "\n…"
        if messagebox.askyesno(
                "새 버전이 있습니다",
                f"현재 v{__version__}  →  새 버전 {rel.version}\n"
                f"{rel.name}  ({rel.published})\n"
                f"크기 {rel.size / 1024 / 1024:.1f} MB\n\n"
                + (f"{notes}\n\n" if notes else "")
                + "지금 받아서 설치할까요?\n"
                  "(프로그램이 종료됐다가 새 버전으로 다시 열립니다)"):
            self._install_update()

    def _install_update(self) -> None:
        rel = getattr(self, "_pending_release", None)
        if not rel:
            return
        if self.core.lab and self.core.lab.running_names():
            self.core.lab.stop_all()
        if self.core.engine and self.core.engine.running:
            self.core.engine.stop()

        self.upd_prog.pack(side="left", padx=12)
        self.upd_prog.set(0)
        self.lbl_update.configure(text="내려받는 중…", text_color=MUTED)

        def prog(frac, done, total):
            self.after(0, lambda: (
                self.upd_prog.set(frac),
                self.lbl_update.configure(
                    text=f"내려받는 중… {done / 1024 / 1024:.1f} / {total / 1024 / 1024:.1f} MB")))

        def job():
            from .updater import download, stage_install, current_exe
            try:
                path = download(rel, current_exe().parent, prog)
            except Exception as e:
                self.after(0, lambda: (
                    self.lbl_update.configure(text=f"다운로드 실패: {e}", text_color=BAD),
                    self.upd_prog.pack_forget(),
                    messagebox.showerror("업데이트 실패", str(e))))
                return
            self.after(0, lambda: self._finish_update(path))
        threading.Thread(target=job, daemon=True).start()

    def _finish_update(self, path) -> None:
        from .updater import stage_install
        self.lbl_update.configure(text="교체 준비 완료 - 프로그램을 종료합니다.", text_color=OK)
        try:
            stage_install(path)
        except Exception as e:
            messagebox.showerror("업데이트 실패", f"교체 스크립트 실행 실패: {e}")
            return
        self.after(600, self.destroy)

    def _save_notify(self) -> None:
        tok = self.e_tg_token.get().strip()
        chat = self.e_tg_chat.get().strip()
        if tok:
            save_env("TELEGRAM_BOT_TOKEN", tok)
            self.e_tg_token.delete(0, "end")
        if chat:
            save_env("TELEGRAM_CHAT_ID", chat)
            self.e_tg_chat.delete(0, "end")
        self.core.cfg.notify.enabled = bool(self.sw_notify.get())
        self.core.cfg.notify.kinds = [k for k, sw in self.notify_kinds.items() if sw.get()]
        self.core.save()
        self._refresh_diag()
        self._log(f"알림 설정 저장 (사용 {self.core.cfg.notify.enabled}, "
                  f"항목 {', '.join(self.core.cfg.notify.kinds) or '없음'})")

    def _test_notify(self) -> None:
        def job():
            ok, msg = self.core.notifier.send(
                "KIS 자동매매 알림 테스트입니다.\n이 메시지가 보이면 연결 성공입니다.")
            self.after(0, lambda: (
                messagebox.showinfo("알림 테스트", msg) if ok
                else messagebox.showerror("알림 테스트 실패", msg)))
        self._thread(job)

    def _refresh_diag(self) -> None:
        lines = []
        for name, ok, msg in self.core.diagnostics():
            lines.append(f"{'OK ' if ok else '-- '} {name:<12} {msg}")
        self.diag_box.configure(state="normal")
        self.diag_box.delete("1.0", "end")
        self.diag_box.insert("end", "\n".join(lines))
        self.diag_box.configure(state="disabled")

    def _test_conn(self) -> None:
        def job():
            out = self.core.connection_test()
            self.after(0, lambda: self._show_conn(out))
        self._log("연결 테스트 중...")
        self._thread(job)

    def _show_conn(self, out: str) -> None:
        self.diag_box.configure(state="normal")
        self.diag_box.insert("end", "\n\n--- 연결 테스트 ---\n" + out)
        self.diag_box.see("end")
        self.diag_box.configure(state="disabled")
        self._log("연결 테스트 완료")

    # ==================================================================
    # 엔진 제어 / 주기 갱신
    # ==================================================================
    def _toggle(self) -> None:
        e = self.core.engine
        if e.running:
            e.stop()
        else:
            if self.core.broker.mode == "REAL" and not self._confirm_real_start():
                return
            e.start()
        self.after(300, self._refresh_now)

    def _confirm_real_start(self) -> bool:
        """실전 시작 전 마지막 관문 - 무엇이 어떻게 나가는지 숫자로 보여준다."""
        from tkinter import simpledialog
        cfg, st = self.core.cfg, self.core.engine.status()
        creds = self.core.trade_client.creds if self.core.trade_client else None
        acct = f"{creds.cano}-{creds.acnt_prdt_cd}" if creds else "?"
        r = cfg.risk
        lines = [
            f"실전 계좌 {acct} 에 실제 주문이 나갑니다.",
            "",
            f"  총평가금액      {st['equity']:,.0f} 원",
            f"  관심종목        {len(cfg.watchlist)} 개",
            f"  활성 전략       {', '.join(st['strategies']) or '없음'}",
            f"  1회 주문 상한   {r.max_order_amount:,} 원",
            f"  1회 최대손실    {r.max_loss_per_trade_pct}%  "
            f"(약 {st['equity'] * r.max_loss_per_trade_pct / 100:,.0f} 원)",
            f"  하루 최대손실   {r.max_daily_loss_pct}%  "
            f"(약 {st['equity'] * r.max_daily_loss_pct / 100:,.0f} 원)",
            f"  동시 보유       최대 {r.max_positions} 종목",
            "",
            "모의투자에서 충분히 검증하지 않았다면 지금 중단하세요.",
            "",
            "계속하려면 아래에 '실전' 이라고 입력하세요.",
        ]
        detail = "\n".join(lines)
        answer = simpledialog.askstring("실전 자동매매 시작 - 최종 확인", detail, parent=self)
        if (answer or "").strip() != "실전":
            self._log("실전 시작 취소됨")
            return False
        return True

    def _panic(self) -> None:
        if not messagebox.askyesno(
                "긴급 전량청산",
                "엔진이 보유한 모든 포지션을 시장가로 즉시 청산하고 엔진을 정지합니다.\n계속할까요?"):
            return
        self.core.engine.stop()
        self._thread(self.core.engine.panic_close_all)

    def _adopt(self) -> None:
        if not messagebox.askyesno(
                "보유종목 편입",
                "계좌에 있는 보유종목을 엔진 관리 대상으로 편입합니다.\n"
                "편입 후에는 엔진이 손절/청산 규칙에 따라 매도할 수 있습니다.\n계속할까요?"):
            return
        self._thread(lambda: self.core.engine.adopt_holdings())

    def _lab_bg_refresh(self) -> None:
        if self._lab_busy:
            return
        self._lab_busy = True
        try:
            self.core.lab.tick_all_once()
            self.after(0, self._lab_refresh)
        except Exception as e:
            log_msg = f"랩 갱신 실패: {e}"
            self.after(0, lambda: self._log(log_msg, "error"))
        finally:
            self._lab_busy = False

    def _refresh_now(self) -> None:
        self._pull_account(force=True)
        self._refresh(loop=False)

    def _pull_account(self, force: bool = False) -> None:
        """계좌를 백그라운드에서 조회한다.

        엔진이 돌 때는 엔진이 알아서 갱신하지만, 정지 상태에서도 대시보드에
        실제 잔고가 보여야 한다. (예전에는 엔진을 켜기 전까지 전부 0으로 보였다)
        """
        if self._acct_busy:
            return
        e = self.core.engine
        if not force and e.running:
            return                      # 엔진이 이미 30초마다 갱신 중
        now = datetime.now()
        if not force and self._acct_at and (now - self._acct_at).total_seconds() < 20:
            return
        self._acct_busy = True
        self._acct_at = now

        def job():
            try:
                e.refresh_account()
            except Exception as ex:
                self._event("error", f"계좌 조회 실패: {ex}", {})
            finally:
                self._acct_busy = False
        threading.Thread(target=job, daemon=True).start()

    def _refresh(self, loop: bool = True) -> None:
        self._pull_account()
        self._lab_tick += 1
        if self._lab_tick % 4 == 0 and getattr(self, "tv_perf", None) is not None:
            # 랩은 3초마다 다 갱신할 필요가 없다 (12초 주기)
            threading.Thread(target=self._lab_bg_refresh, daemon=True).start()
        try:
            self._paint_status()
        except Exception as e:
            # 화면 갱신 실패가 조용히 묻히면 원인을 못 찾는다
            self._log(f"화면 갱신 실패: {e}", "error")
        if loop:
            self.after(3000, self._refresh)

    def _paint_status(self) -> None:
        e = self.core.engine
        st = e.status()
        mode = st["mode"]
        color = {"REAL": BAD, "MOCK": WARN, "PAPER": MUTED}.get(mode, MUTED)
        self.lbl_mode.configure(text=f"● {mode}", text_color=color)
        self.lbl_session.configure(text=st["session"])
        if st["running"]:
            self.lbl_engine.configure(text=f"● 가동중  (최근 {st['last_loop']})", text_color=OK)
            self.btn_run.configure(text="자동매매 정지", fg_color="#7a4a1f",
                                   hover_color="#96601f")
        else:
            self.lbl_engine.configure(
                text=f"● 정지  (계좌 {st['account_ts']})", text_color=MUTED)
            self.btn_run.configure(text="자동매매 시작", fg_color=["#3a7ebf", "#1f538d"],
                                   hover_color=["#325882", "#14375e"])

        eq, cash = st["equity"], st["cash"]
        day = st["day_pnl"]
        if st["account_ts"] == "-":
            err = st.get("account_error") or ""
            for k in self.cards:
                self.cards[k][0].configure(text="조회 실패" if err else "조회 중…",
                                           text_color=BAD if err else MUTED)
            self.cards["equity"][1].configure(
                text=("증권사 서버 무응답 - 잠시 후 자동 재시도" if err
                      else "계좌 조회에 몇 초 걸립니다"),
                text_color=BAD if err else MUTED)
            if err:
                self.lbl_risk.configure(
                    text=f"계좌 조회 실패: {err[:150]}", text_color=BAD)
            # 감시 현황은 계좌와 무관하다. 계좌를 못 읽어도 계속 보여준다.
            self._paint_watch()
            return
        self.cards["equity"][0].configure(text=money(eq), text_color=["gray10", "#dce4ee"])
        self.cards["equity"][1].configure(text=f"실현손익 {money(st['realized_today'])}원")
        self.cards["cash"][0].configure(text=money(cash), text_color=["gray10", "#dce4ee"])
        self.cards["cash"][1].configure(
            text=f"비중 {cash / eq * 100:.0f}%" if eq else "")
        c = UP if day > 0 else (DOWN if day < 0 else MUTED)
        self.cards["day"][0].configure(text=f"{day:+,.0f}", text_color=c)
        self.cards["day"][1].configure(text=pct(st["risk"]["day_pnl_pct"]), text_color=c)
        self.cards["pos"][0].configure(
            text=f"{st['open_positions']} / {st['orders_today']}",
            text_color=["gray10", "#dce4ee"])
        self.cards["pos"][1].configure(
            text=f"관심 {len(self.core.cfg.watchlist)}종목 / 연속손절 {st['consecutive_losses']}회")

        r = st["risk"]
        bits = [f"낙폭 {r['drawdown_pct']:.2f}%", f"당일 {r['day_pnl_pct']:+.2f}%"]
        if mode == "REAL":
            bits.insert(0, "실전 모드 - 시작하면 실제 돈으로 주문이 나갑니다")
        if r["cooldown_until"]:
            bits.append(f"쿨다운 {r['cooldown_until']}까지")
        if r["pending"]:
            bits.append(f"주문중 {','.join(r['pending'])}")
        if st.get("account_stale"):
            bits.insert(0, f"계좌 갱신 실패 - {st['account_ts']} 기준 값 표시 중")
        txt = "  |  ".join(bits)
        tc = BAD if mode == "REAL" else (WARN if st.get("account_stale") else MUTED)
        if r["halted"]:
            txt = f"엔진 정지: {r['halt_reason']}   |   " + txt
            tc = BAD
        elif r["daily_block"]:
            txt = f"신규진입 차단: {r['daily_block_reason']}   |   " + txt
            tc = WARN
        if st["error"]:
            txt += f"   |   최근 오류: {st['error']}"
            tc = BAD
        self.lbl_risk.configure(text=txt, text_color=tc)

        holdings = st.get("holdings") or {}
        for i in self.tv_pos.get_children():
            self.tv_pos.delete(i)
        for t in self.core.store.open_trades(mode):
            h = holdings.get(t["symbol"], {})
            cur = h.get("price", 0)
            pnl = (cur - t["entry_price"]) * t["qty"] if cur else 0
            pp = (cur / t["entry_price"] - 1) * 100 if cur and t["entry_price"] else 0
            self.tv_pos.insert("", "end", values=(
                t["symbol"], t["qty"], money(t["entry_price"]), money(cur),
                money(t.get("stop_price")), money(t.get("target_price")),
                money(pnl), f"{pp:+.2f}%", t.get("strategy", "")))

        self._paint_watch()

    # ------------------------------------------------------------------
    # 감시 현황
    # ------------------------------------------------------------------
    VERDICT_LABEL = {"BUY": "신호", "NEAR": "근접", "WAIT": "대기", "HELD": "보유",
                     "GATED": "관문차단", "NODATA": "자료부족", "ERROR": "오류"}

    def _paint_watch(self) -> None:
        e = self.core.engine
        scan = getattr(e, "last_scan", None) or {}
        tv = self.tv_watch_live
        for i in tv.get_children():
            tv.delete(i)

        if not scan:
            self.lbl_scan.configure(
                text="아직 스캔 기록이 없습니다 - 엔진을 시작하거나 [지금 한 번 훑기]",
                text_color=MUTED)
            return

        rows = []
        for sym, snap in scan.items():
            for r in snap.get("strategies", []):
                rows.append((snap, r))
        order = {"BUY": 0, "GATED": 1, "NEAR": 2, "HELD": 3, "WAIT": 4,
                 "ERROR": 5, "NODATA": 6}
        rows.sort(key=lambda x: (order.get(x[1]["verdict"], 9),
                                 abs(x[1].get("gap_pct") or 0) if x[1].get("gap_pct")
                                 else (1 - (x[1].get("score") or 0)) * 100))

        for snap, r in rows:
            v = r["verdict"]
            tag = {"BUY": "buy", "NEAR": "near", "HELD": "held",
                   "GATED": "gated"}.get(v, "dim")
            if v in ("NODATA", "ERROR"):
                gap_s = "-"
            elif r.get("has_gap"):
                gap_s = f"{r['gap_pct']:+.2f}%"
            else:
                gap_s = "조건형"
            lv = (f"-{r['stop_pct']:.1f}/+{r['target_pct']:.1f}%"
                  if r.get("stop_pct") else "-")
            q = r.get("qty") or 0
            qty_s = f"{q}주" if q else ("0주" if r.get("size_note") else "-")
            # 기대이익이 왕복 거래비용의 몇 배인가. 1배 미만이면 이겨도 남는 게 없다.
            er = r.get("edge_ratio") or 0
            edge_s = f"{er:.1f}배" if er else "-"
            row_tag = tag
            if v != "GATED" and not q and r.get("size_note"):
                row_tag = "dim"
            tv.insert("", "end", tags=(row_tag,), values=(
                f"{snap.get('name') or snap['symbol']}",
                money(snap.get("price")), f"{snap.get('change_pct', 0):+.2f}%",
                f"{r.get('atr_pct') or snap.get('atr_pct', 0):.2f}%",
                r["label"], self.VERDICT_LABEL.get(v, v),
                f"{r['passed']}/{r['total']}" if r["total"] else "-",
                gap_s, lv, edge_s, qty_s, r.get("reason", "")[:110]))

        n_buy = sum(1 for _, r in rows if r["verdict"] == "BUY")
        n_near = sum(1 for _, r in rows if r["verdict"] == "NEAR")
        n_gate = sum(1 for _, r in rows if r["verdict"] == "GATED")
        # 신호가 떠도 0주면 못 산다. 그 사실을 미리 드러낸다.
        n_zero = sum(1 for _, r in rows
                     if r.get("size_note") and not r.get("qty")
                     and r["verdict"] in ("BUY", "NEAR", "WAIT"))
        ts = getattr(e, "last_scan_ts", None)
        txt = (f"{len(scan)}종목 x {len(rows) // max(len(scan), 1)}전략 = {len(rows)}건 "
               f"| 신호 {n_buy} · 근접 {n_near}"
               + (f" · 관문차단 {n_gate}" if n_gate else "")
               + f" | {ts.strftime('%H:%M:%S') if ts else '-'} 기준")
        color = OK if n_buy else MUTED
        if n_zero:
            txt += (f"   ※ {n_zero}건은 신호가 나도 0주 "
                    f"(손절폭 대비 예수금·1회손실한도 부족)")
            color = WARN
        self.lbl_scan.configure(text=txt, text_color=color)

    def _scan_once(self) -> None:
        """엔진이 꺼져 있어도 지금 한 번 훑어본다."""
        def job():
            try:
                self.core.engine.scan()
            except Exception as ex:
                self._log(f"스캔 실패: {ex}", "error")
            self.after(0, self._paint_watch)
            self._scanning = False
        if getattr(self, "_scanning", False):
            return
        self._scanning = True
        self._log("감시 스캔 수동 실행")
        threading.Thread(target=job, daemon=True).start()

    def _open_watch_detail(self) -> None:
        sel = self.tv_watch_live.selection()
        if not sel:
            return
        vals = self.tv_watch_live.item(sel[0], "values")
        name, label = vals[0], vals[4]
        scan = getattr(self.core.engine, "last_scan", None) or {}
        for sym, snap in scan.items():
            if (snap.get("name") or sym) != name:
                continue
            for r in snap.get("strategies", []):
                if r["label"] == label:
                    WatchDetailDialog(self, snap, r)
                    return

    def _quit(self) -> None:
        if self.core.lab and self.core.lab.running_names():
            if not messagebox.askyesno(
                    "종료", f"가상운용 {len(self.core.lab.running_names())}개가 돌고 있습니다. "
                            "정지하고 종료할까요?"):
                return
            self.core.lab.stop_all()
        if self.core.engine and self.core.engine.running:
            if not messagebox.askyesno("종료", "엔진이 가동 중입니다. 정지하고 종료할까요?"):
                return
            self.core.engine.stop()
        self.destroy()


class WatchDetailDialog(ctk.CTkToplevel):
    """한 종목 x 한 전략이 지금 어떤 상태인지 조건별로 펼쳐 보여준다."""

    def __init__(self, app, snap: dict, r: dict):
        super().__init__(app)
        self.app = app
        sym = snap["symbol"]
        name = snap.get("name") or sym
        self.title(f"{name} - {r['label']}")
        self.geometry("760x620")
        self.transient(app)

        ctk.CTkLabel(self, text=f"{name} ({sym})  ·  {r['label']}", anchor="w",
                     font=("", 16, "bold")).pack(fill="x", padx=16, pady=(14, 2))

        head = (f"현재가 {money(snap.get('price'))}원  "
                f"({snap.get('change_pct', 0):+.2f}%)   |   "
                f"변동성 ATR {r.get('atr_pct', 0):.2f}%   |   "
                f"판정 {App.VERDICT_LABEL.get(r['verdict'], r['verdict'])} "
                f"({r['passed']}/{r['total']} 조건 충족)")
        ctk.CTkLabel(self, text=head, anchor="w", text_color=MUTED,
                     font=("", 12)).pack(fill="x", padx=16, pady=(0, 8))

        lv = ctk.CTkFrame(self, fg_color=CARD, corner_radius=8)
        lv.pack(fill="x", padx=14, pady=(0, 8))
        ctk.CTkLabel(lv, text="이 종목에 지금 적용되는 값 (변동성에 맞춰 자동 환산)",
                     anchor="w", font=("", 12, "bold")).pack(fill="x", padx=12, pady=(10, 2))
        if r.get("stop_pct"):
            lv_txt = (f"손절폭 -{r['stop_pct']:.2f}%   ·   "
                      f"목표폭 +{r.get('target_pct', 0):.2f}%   ·   "
                      f"손익비 {(r.get('target_pct') or 0) / r['stop_pct']:.2f}R")
        else:
            lv_txt = "손절폭 산출 불가"
        cpct, er = r.get("cost_pct") or 0, r.get("edge_ratio") or 0
        if cpct:
            lv_txt += (f"\n왕복 거래비용 {cpct:.2f}%   ·   "
                       f"기대이익은 그 {er:.1f}배"
                       + ("   ← 이겨도 비용이 먹는다" if 0 < er < 2 else ""))
        ctk.CTkLabel(lv, anchor="w", justify="left", text_color=MUTED, font=("", 11),
                     text=lv_txt).pack(fill="x", padx=12, pady=(0, 4))
        if r.get("gate"):
            ctk.CTkLabel(lv, anchor="w", justify="left", font=("", 11),
                         text_color=WARN, wraplength=700,
                         text=f"전략은 매수 신호를 냈지만 시스템이 막았습니다: "
                              f"{r['gate']}").pack(fill="x", padx=12, pady=(0, 4))
        if r.get("size_note"):
            ctk.CTkLabel(lv, anchor="w", justify="left", font=("", 11),
                         text_color=OK if r.get("qty") else BAD,
                         text=f"지금 신호가 나면: {r['size_note']}",
                         wraplength=700).pack(fill="x", padx=12, pady=(0, 10))
        else:
            ctk.CTkLabel(lv, text="", height=4).pack()

        ctk.CTkLabel(self, text="진입 조건 판정", anchor="w",
                     font=("", 13, "bold")).pack(fill="x", padx=16, pady=(4, 4))
        box = ctk.CTkScrollableFrame(self, fg_color=CARD, corner_radius=8, height=230)
        box.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        for c in (r.get("checks") or []):
            row = ctk.CTkFrame(box, fg_color="transparent")
            row.pack(fill="x", pady=3)
            ok = bool(c.get("ok"))
            ctk.CTkLabel(row, text="●", width=18,
                         text_color=OK if ok else BAD).pack(side="left")
            ctk.CTkLabel(row, text=c.get("label", ""), width=300, anchor="w",
                         font=("", 12)).pack(side="left")
            ctk.CTkLabel(row, text=c.get("detail", ""), anchor="w", text_color=MUTED,
                         font=("", 11)).pack(side="left", padx=8)
        if not r.get("checks"):
            ctk.CTkLabel(box, text=r.get("reason") or "판정 항목 없음",
                         text_color=MUTED).pack(pady=10)

        ctk.CTkLabel(self, text="최근 관측 기록", anchor="w",
                     font=("", 13, "bold")).pack(fill="x", padx=16, pady=(4, 4))
        tv = App._tree(self, [("ts", "시각", 140), ("vd", "판정", 70),
                              ("cond", "조건", 70), ("gap", "트리거까지", 100),
                              ("px", "가격", 100), ("why", "차단", 220)], 7)
        tv.pack(fill="both", expand=True, padx=14, pady=(0, 6))
        try:
            for e in app.core.store.recent_evals(limit=60, symbol=sym):
                if e.get("strategy") != r["strategy"]:
                    continue
                tv.insert("", "end", values=(
                    (e.get("ts") or "")[5:], App.VERDICT_LABEL.get(e.get("verdict"), ""),
                    f"{e.get('passed')}/{e.get('total')}",
                    f"{e.get('gap_pct') or 0:+.2f}%", money(e.get("price")),
                    (e.get("blocked_by") or "")[:60]))
        except Exception as ex:
            tv.insert("", "end", values=("기록 조회 실패", str(ex)[:40], "", "", "", ""))

        ctk.CTkButton(self, text="닫기", width=100,
                      command=self.destroy).pack(pady=(0, 12))


class ScanDialog(ctk.CTkToplevel):
    """전략 자동 탐색 - 종목만 주면 조합을 훑어 추천하고, 승인하면 바로 시작한다."""

    def __init__(self, app, symbols: list[str]):
        super().__init__(app)
        self.app = app
        self.result = None
        self.title("전략 자동 탐색")
        self.geometry("900x720")
        self.transient(app)
        self.grab_set()
        self.after(220, lambda: apply_icon(self))

        ctk.CTkLabel(self, text="전략 자동 탐색", anchor="w",
                     font=("", 16, "bold")).pack(fill="x", padx=16, pady=(14, 2))
        ctk.CTkLabel(
            self, anchor="w", justify="left", wraplength=850, text_color=MUTED,
            text=("종목을 주면 전략과 파라미터 조합을 훑어서 가장 나은 것을 찾습니다.\n"
                  "단, 수익률 1등을 고르지 않습니다 — 조합을 많이 돌려놓고 1등을 뽑는 건 "
                  "우연히 좋아 보인 걸 뽑는 것이기 때문입니다.\n"
                  "거래 표본·손실확률·연도별 일관성·최대낙폭 관문을 통과한 것만 추천하고, "
                  "하나도 통과 못 하면 그렇다고 말합니다."),
        ).pack(fill="x", padx=16, pady=(0, 8))

        f = ctk.CTkFrame(self, fg_color=CARD, corner_radius=8)
        f.pack(fill="x", padx=14, pady=(0, 8))
        r = ctk.CTkFrame(f, fg_color="transparent")
        r.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(r, text="종목", width=50, anchor="w").pack(side="left")
        self.e_syms = ctk.CTkEntry(r, height=32)
        self.e_syms.pack(side="left", fill="x", expand=True)
        self.e_syms.insert(0, ", ".join(symbols))
        ctk.CTkLabel(f, text="6자리 코드 또는 종목명으로 넣을 수 있습니다 "
                             "(예: 005930, 삼성전자, SK하이닉스)",
                     anchor="w", text_color=MUTED,
                     font=("", 11)).pack(fill="x", padx=12, pady=(0, 4))
        r2 = ctk.CTkFrame(f, fg_color="transparent")
        r2.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkLabel(r2, text="자금", width=50, anchor="w").pack(side="left")
        self.e_cash = ctk.CTkEntry(r2, width=140, height=32)
        self.e_cash.insert(0, str(app.core.cfg.paper_initial_cash))
        self.e_cash.pack(side="left")
        self.btn_scan = ctk.CTkButton(r2, text="탐색 시작", width=120,
                                      command=self._start)
        self.btn_scan.pack(side="left", padx=12)
        self.prog = ctk.CTkProgressBar(r2, width=220)
        self.prog.set(0)
        self.prog.pack(side="left", padx=8)
        self.lbl_step = ctk.CTkLabel(r2, text="대기 중", text_color=MUTED, font=("", 11))
        self.lbl_step.pack(side="left", padx=6)

        self.box = ctk.CTkTextbox(self, font=("Consolas", 11))
        self.box.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        self.box.configure(state="disabled")

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=14, pady=(0, 14))
        self.btn_apply = ctk.CTkButton(
            bar, text="이 설정으로 시작", width=180, state="disabled",
            command=self._apply)
        self.btn_apply.pack(side="right")
        ctk.CTkButton(bar, text="닫기", width=90, fg_color="#3a3a3a",
                      hover_color="#4a4a4a", command=self.destroy).pack(side="right", padx=8)
        self.lbl_verdict = ctk.CTkLabel(bar, text="", anchor="w", justify="left",
                                        font=("", 12, "bold"))
        self.lbl_verdict.pack(side="left")

    # ------------------------------------------------------------------
    def _say(self, msg: str) -> None:
        self.box.configure(state="normal")
        self.box.insert("end", msg + "\n")
        self.box.see("end")
        self.box.configure(state="disabled")

    def _resolve(self, raw: str) -> tuple[list[str], list[str]]:
        """코드와 종목명을 섞어 받아 코드로 바꾼다."""
        out, bad = [], []
        for tok in [t.strip() for t in raw.replace(",", " ").split() if t.strip()]:
            if tok.isdigit() and len(tok) == 6:
                if tok not in out:
                    out.append(tok)
                continue
            hits = self.app.core.master.search(tok, limit=3)
            exact = [h for h in hits if h["name"] == tok]
            pick = (exact or hits)
            if pick:
                if pick[0]["symbol"] not in out:
                    out.append(pick[0]["symbol"])
                    self._say(f"  '{tok}' -> {pick[0]['symbol']} {pick[0]['name']}")
            else:
                bad.append(tok)
        return out, bad

    def _start(self) -> None:
        self.box.configure(state="normal")
        self.box.delete("1.0", "end")
        self.box.configure(state="disabled")
        syms, bad = self._resolve(self.e_syms.get())
        if bad:
            self._say(f"[찾을 수 없음] {', '.join(bad)}")
        if len(syms) < 2:
            messagebox.showerror("오류", "종목을 2개 이상 넣어주세요.", parent=self)
            return
        try:
            cash = int(float(self.e_cash.get()))
        except ValueError:
            messagebox.showerror("오류", "자금은 숫자로 넣어주세요.", parent=self)
            return

        self.btn_scan.configure(state="disabled", text="탐색 중…")
        self.btn_apply.configure(state="disabled")
        self.lbl_verdict.configure(text="")
        self._scan_syms, self._scan_cash = syms, cash
        self._say(f"종목 {len(syms)}개 / 자금 {cash:,}원")
        self._say("조합 수십 개를 백테스트합니다. 1~3분쯤 걸립니다.")
        self._say("")

        def prog(frac, msg):
            self.after(0, lambda: (self.prog.set(frac),
                                   self.lbl_step.configure(text=msg)))

        def job():
            from .scanner import Scanner
            sc = Scanner(self.app.core.store, self.app.core.cfg.cost,
                         self.app.core.cfg.risk, cash)
            r = sc.scan(syms, progress=prog,
                        log_fn=lambda m: self.after(0, self._say, m))
            self.after(0, lambda: self._done(r))
        threading.Thread(target=job, daemon=True).start()

    def _done(self, r) -> None:
        self.btn_scan.configure(state="normal", text="다시 탐색")
        self.prog.set(1)
        self.lbl_step.configure(text="완료")
        self.result = r
        self._say("")
        self._say("=" * 66)
        for line in (r.error or r.summary).split("\n"):
            self._say(line)
        self._say("=" * 66)

        if r.candidates:
            self._say("")
            self._say("통과한 후보")
            self._say(f"{'전략':<16}{'설정':<22}{'점수':>8}{'수익':>9}{'손실확률':>9}{'일관성':>8}")
            for c in r.candidates:
                self._say(f"{c.label:<16}{str(c.params or '기본'):<22}{c.score:>8.1f}"
                          f"{c.net:>8.1f}%{c.loss_prob:>8.0f}%{c.consistency:>7.0f}%")
        if r.rejected:
            self._say("")
            self._say("탈락한 후보 (걸린 관문)")
            for c in r.rejected[:8]:
                self._say(f"  {c.label:<16}{str(c.params or '기본'):<22} {', '.join(c.fails)}")

        if r.best:
            self.btn_apply.configure(state="normal")
            self.lbl_verdict.configure(
                text=(f"추천: {r.best.label} / {r.best.style}  "
                      f"(손실확률 {r.best.loss_prob:.0f}%, "
                      f"연도일관성 {r.best.consistency:.0f}%)"),
                text_color=OK)
        else:
            self.lbl_verdict.configure(
                text="추천할 만한 조합이 없습니다", text_color=WARN)

    def _apply(self) -> None:
        r = self.result
        if not r or not r.best:
            return
        b = r.best
        name = self.app._unique_name(f"자동 {b.label}")
        if not messagebox.askyesno(
                "이 설정으로 시작",
                f"'{name}' 프로필을 만들고 가상운용을 시작합니다.\n\n"
                f"  전략      {b.label}\n"
                f"  성향      {b.style}\n"
                f"  설정      {b.params or '기본값'}\n"
                f"  종목      {len(self._scan_syms)}개\n"
                f"  자금      {self._scan_cash:,}원 (가상)\n\n"
                f"실제 주문은 나가지 않습니다. 계속할까요?", parent=self):
            return
        from .scanner import to_profile
        pr = to_profile(b, name, self._scan_syms, self._scan_cash)
        self.app.core.profiles.upsert(pr)
        ok, msg = self.app.core.lab.start(pr)
        self.app._log(msg, "engine" if ok else "error")
        self.app._lab_refresh()
        if ok:
            self.app.tabs.set("운용랩")
            messagebox.showinfo("시작됨",
                                f"'{name}' 가상운용을 시작했습니다.\n"
                                f"[운용랩] 탭에서 성과가 쌓이는 걸 볼 수 있습니다.",
                                parent=self)
            self.destroy()
        else:
            messagebox.showerror("시작 실패", msg, parent=self)


class ProfileDialog(ctk.CTkToplevel):
    """투자 프로필 편집 - 여기서 자기 매매방식을 숫자로 정의한다."""

    RISK_FIELDS = [
        ("max_loss_per_trade_pct", "1회 거래 최대손실 (%)", "이 값에서 매수 수량이 역산된다"),
        ("max_daily_loss_pct", "하루 최대손실 (%)", "초과 시 당일 신규진입 중단"),
        ("max_drawdown_pct", "누적 최대낙폭 (%)", "초과 시 운용 정지"),
        ("max_positions", "동시 보유 종목 수", ""),
        ("max_position_weight_pct", "종목당 최대 비중 (%)", ""),
        ("max_order_amount", "1회 최대 주문금액 (원)", ""),
        ("min_order_amount", "1회 최소 주문금액 (원)", ""),
        ("reentry_cooldown_min", "재진입 금지 (분)", "장투는 1440(하루) 권장"),
        ("max_orders_per_day", "하루 최대 주문 건수", ""),
        ("min_cash_reserve_pct", "최소 현금 보유 (%)", ""),
        ("max_consecutive_losses", "연속 손절 허용", ""),
        ("min_edge_cost_ratio", "기대이익/비용 최소배수", "0 = 끔. 3배 권장"),
        ("min_turnover_amount", "평균 거래대금 하한 (원)", "0 = 끔"),
    ]
    EXEC_FIELDS = [
        ("entry_start", "신규진입 시작", "HH:MM"),
        ("entry_end", "신규진입 종료", "HH:MM"),
        ("force_exit_at", "강제 청산 시각", "비우면 오버나이트 보유 (장투/스윙)"),
        ("loop_interval_sec", "판단 주기 (초)", "장투는 120, 단타는 30"),
    ]

    def __init__(self, app, profile, on_save):
        super().__init__(app)
        self.app = app
        self.profile = profile
        self.on_save = on_save
        self.title(f"프로필 편집 - {profile.name}")
        self.geometry("880x780")
        self.transient(app)
        self.grab_set()
        self.after(220, lambda: apply_icon(self))

        from .profiles import HORIZONS
        sc = ctk.CTkScrollableFrame(self, fg_color="transparent")
        sc.pack(fill="both", expand=True, padx=10, pady=(10, 4))

        # --- 기본 ---
        f = self._box(sc, "기본")
        self.e_name = self._field(f, "프로필 이름", profile.name, 220)
        self.opt_hz = ctk.CTkOptionMenu(self._row(f, "투자 성향"), width=200,
                                        values=list(HORIZONS.values()))
        self.opt_hz.set(profile.horizon_label)
        self.opt_hz.pack(side="left")
        self._hz_map = {v: k for k, v in HORIZONS.items()}
        self.e_desc = self._field(f, "설명", profile.description, 480)
        self.e_cash = self._field(f, "가상 초기자금 (원)", str(profile.initial_cash), 160)
        self.e_wl = self._field(
            f, "관심종목 (비우면 공용)",
            ",".join(profile.watchlist), 480)

        # --- 전략 ---
        f = self._box(sc, "전략  (여러 개를 켜면 먼저 신호가 난 전략이 진입한다)")
        self.strat_rows = {}
        cur = {s["name"]: s for s in profile.strategies}
        for name, cls in REGISTRY.items():
            box = ctk.CTkFrame(f, fg_color="#2c2c2c", corner_radius=6)
            box.pack(fill="x", padx=12, pady=4)
            head = ctk.CTkFrame(box, fg_color="transparent")
            head.pack(fill="x", padx=10, pady=(8, 2))
            sw = ctk.CTkSwitch(head, text=f"{cls.label}  ({cls.timeframe}, 워밍업 {cls.warmup}봉)",
                               font=("", 12, "bold"))
            sw.pack(side="left")
            if name in cur and cur[name].get("enabled"):
                sw.select()
            ctk.CTkLabel(box, text=cls.description, anchor="w", justify="left",
                         text_color=MUTED, wraplength=780,
                         font=("", 10)).pack(fill="x", padx=10, pady=(0, 4))
            grid = ctk.CTkFrame(box, fg_color="transparent")
            grid.pack(fill="x", padx=10, pady=(0, 8))
            saved = (cur.get(name) or {}).get("params") or {}
            fields = {}
            for i, (k, dv) in enumerate(cls.default_params.items()):
                cell = ctk.CTkFrame(grid, fg_color="transparent")
                cell.grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 14), pady=2)
                ctk.CTkLabel(cell, text=k, width=125, anchor="w", text_color=MUTED,
                             font=("", 10)).pack(side="left")
                e = ctk.CTkEntry(cell, width=70, height=26)
                e.insert(0, str(saved.get(k, dv)))
                e.pack(side="left")
                fields[k] = e
            self.strat_rows[name] = {"switch": sw, "fields": fields}

        # --- 리스크 ---
        f = self._box(sc, "자금관리  (성향에 맞춰 여기를 바꾸는 게 핵심)")
        base_risk = asdict(self.app.core.cfg.risk)
        self.risk_e = {}
        for k, label, hint in self.RISK_FIELDS:
            v = (profile.risk or {}).get(k, base_risk.get(k))
            self.risk_e[k] = self._field(f, label, str(v), 120, hint)

        # --- 실행 ---
        f = self._box(sc, "실행 시간")
        base_exec = asdict(self.app.core.cfg.execution)
        self.exec_e = {}
        for k, label, hint in self.EXEC_FIELDS:
            v = (profile.execution or {}).get(k, base_exec.get(k))
            self.exec_e[k] = self._field(f, label, str(v), 120, hint)

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkButton(bar, text="저장", width=120, command=self._save).pack(side="right")
        ctk.CTkButton(bar, text="취소", width=90, fg_color="#3a3a3a",
                      hover_color="#4a4a4a", command=self.destroy).pack(side="right", padx=8)

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _box(parent, title):
        f = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=8)
        f.pack(fill="x", pady=5, padx=2)
        ctk.CTkLabel(f, text=title, anchor="w",
                     font=("", 13, "bold")).pack(fill="x", padx=12, pady=(10, 4))
        return f

    @staticmethod
    def _row(parent, label):
        r = ctk.CTkFrame(parent, fg_color="transparent")
        r.pack(fill="x", padx=12, pady=3)
        ctk.CTkLabel(r, text=label, width=190, anchor="w").pack(side="left")
        return r

    def _field(self, parent, label, value, width, hint=""):
        r = self._row(parent, label)
        e = ctk.CTkEntry(r, width=width, height=28)
        e.insert(0, value)
        e.pack(side="left")
        if hint:
            ctk.CTkLabel(r, text=hint, text_color=MUTED, anchor="w",
                         font=("", 10)).pack(side="left", padx=10)
        return e

    def _save(self) -> None:
        name = self.e_name.get().strip()
        if not name:
            messagebox.showerror("오류", "프로필 이름을 넣어주세요.", parent=self)
            return
        old = self.profile.name
        if name != old and name in self.app.core.profiles.names():
            messagebox.showerror("오류", f"'{name}'은 이미 있는 이름입니다.", parent=self)
            return
        try:
            cash = int(float(self.e_cash.get()))
        except ValueError:
            messagebox.showerror("오류", "초기자금은 숫자로 넣어주세요.", parent=self)
            return

        strategies, enabled = [], 0
        for sname, w in self.strat_rows.items():
            params = {k: _coerce(e.get()) for k, e in w["fields"].items()}
            on = bool(w["switch"].get())
            enabled += on
            strategies.append({"name": sname, "enabled": on, "params": params})
        if not enabled:
            messagebox.showerror("오류", "전략을 최소 하나 켜주세요.", parent=self)
            return

        risk, execu = {}, {}
        try:
            for k, e in self.risk_e.items():
                risk[k] = _coerce(e.get())
            for k, e in self.exec_e.items():
                execu[k] = _coerce(e.get())
        except ValueError as ex:
            messagebox.showerror("오류", f"입력값 확인: {ex}", parent=self)
            return

        wl = [s.strip() for s in self.e_wl.get().replace(" ", ",").split(",") if s.strip()]
        bad = [s for s in wl if not (s.isdigit() and len(s) == 6)]
        if bad:
            messagebox.showerror("오류", f"종목코드는 6자리 숫자여야 합니다: {', '.join(bad)}",
                                 parent=self)
            return

        p = self.profile
        if name != old:
            self.app.core.lab.reset(old)
            self.app.core.profiles.remove(old)
        p.name = name
        p.horizon = self._hz_map.get(self.opt_hz.get(), "custom")
        p.description = self.e_desc.get().strip()
        p.initial_cash = cash
        p.watchlist = wl
        p.strategies = strategies
        p.risk = risk
        p.execution = execu
        self.on_save(p)
        self.destroy()


_SWEEP_GRID = {
    "volatility_breakout": {
        "k": [0.3, 0.4, 0.5, 0.6, 0.7],
        "ma_filter": [0, 10, 20, 40],
        "take_profit_pct": [3.0, 4.0, 6.0],
    },
    "trend_pullback": {
        "fast": [10, 20, 30],
        "slow": [50, 60, 90],
        "pullback_lookback": [3, 5, 8],
        "take_profit_pct": [5.0, 8.0],
    },
    "opening_range_breakout": {
        "range_min": [15, 30, 45],
        "take_profit_pct": [1.5, 2.0, 3.0],
        "atr_stop": [1.0, 1.5, 2.0],
    },
}


def _wrap(text: str, width: int) -> list[str]:
    """긴 판정문을 지표 패널 폭에 맞춰 접는다."""
    out, line = [], ""
    for word in str(text).split():
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


def _coerce(v: str):
    s = v.strip()
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    try:
        f = float(s)
        return int(f) if f.is_integer() and "." not in s else f
    except ValueError:
        return s


def run() -> None:
    App().mainloop()
