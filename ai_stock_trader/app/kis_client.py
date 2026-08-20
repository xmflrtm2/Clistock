"""한국투자증권 OpenAPI REST 클라이언트.

기존 코드 대비 고친 것:
  * 토큰 캐시  - KIS는 토큰 발급이 1분 1회 제한. 매 실행마다 새로 받으면 막힌다.
                 DB에 저장해두고 만료 전까지 재사용한다.
  * 레이트리밋 - 실전 초당 20건 / 모의 초당 2건. 초과하면 에러가 쏟아진다.
  * hashkey    - 주문 API 위변조 방지 해시.
  * read_only  - 시세 전용 클라이언트(실전키)에서는 주문 메서드 자체를 막는다.
  * 재시도     - 네트워크/일시 오류에 지수 백오프.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta

import requests

from .settings import Credentials
from .storage import get_store

log = logging.getLogger(__name__)


class KISError(Exception):
    def __init__(self, msg: str, code: str = "", body: str = ""):
        super().__init__(msg)
        self.code = code
        self.body = body


# --------------------------------------------------------------------------
# 호가 단위 (2023-01-25 개정 기준)
# --------------------------------------------------------------------------
def tick_size(price: float, market: str = "KOSPI") -> int:
    p = float(price)
    if p < 2_000:
        return 1
    if p < 5_000:
        return 5
    if p < 20_000:
        return 10
    if p < 50_000:
        return 50
    if market.upper() == "KOSDAQ":
        return 100
    if p < 200_000:
        return 100
    if p < 500_000:
        return 500
    return 1_000


def round_to_tick(price: float, market: str = "KOSPI", up: bool = False) -> int:
    t = tick_size(price, market)
    if up:
        return int((int(price) + t - 1) // t * t)
    return int(int(price) // t * t)


class _RateLimiter:
    """단순 슬라이딩 윈도우 레이트리밋."""

    def __init__(self, per_sec: float):
        self.per_sec = per_sec
        self._hits: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            while True:
                now = time.monotonic()
                while self._hits and now - self._hits[0] > 1.0:
                    self._hits.popleft()
                if len(self._hits) < self.per_sec:
                    self._hits.append(now)
                    return
                sleep = 1.0 - (now - self._hits[0]) + 0.01
                time.sleep(max(sleep, 0.01))


class KISClient:
    def __init__(self, creds: Credentials, read_only: bool = False, label: str = ""):
        self.creds = creds
        self.read_only = read_only
        self.label = label or ("MOCK" if creds.is_mock else "REAL")
        self.base = creds.rest_base
        self.session = requests.Session()
        self._token: str | None = None
        self._token_exp: datetime | None = None
        self._token_lock = threading.Lock()
        self._limiter = _RateLimiter(2 if creds.is_mock else 15)
        self._store = get_store()
        # 모의투자 서버는 정상일 때도 느리다 (잔고 4~13초, 실전은 0.05초).
        # 실전 기준으로 타임아웃을 잡으면 모의가 멀쩡한데도 계속 실패한다.
        # 어차피 백그라운드 스레드에서 도니까 길게 잡아도 화면은 멈추지 않는다.
        self.timeout = (5, 30) if creds.is_mock else (5, 10)

    # -- 토큰 ---------------------------------------------------------------
    @property
    def _token_key(self) -> str:
        return f"kis_token::{self.creds.app_key[:12]}::{'mock' if self.creds.is_mock else 'real'}"

    def _load_cached_token(self) -> bool:
        raw = self._store.kv_get(self._token_key)
        if not raw:
            return False
        try:
            d = json.loads(raw)
            exp = datetime.fromisoformat(d["expires_at"])
        except Exception:
            return False
        if exp - timedelta(minutes=10) <= datetime.now():
            return False
        self._token = d["token"]
        self._token_exp = exp
        return True

    def token(self, force: bool = False) -> str:
        with self._token_lock:
            if not force and self._token and self._token_exp and \
                    datetime.now() < self._token_exp - timedelta(minutes=10):
                return self._token
            if not force and self._load_cached_token():
                return self._token  # type: ignore[return-value]

            if not self.creds.ok:
                raise KISError(f"[{self.label}] APP KEY/SECRET/계좌번호가 비어 있습니다. .env를 확인하세요.")

            url = f"{self.base}/oauth2/tokenP"
            body = {
                "grant_type": "client_credentials",
                "appkey": self.creds.app_key,
                "appsecret": self.creds.app_secret,
            }
            r = self.session.post(url, json=body, timeout=self.timeout)
            if r.status_code != 200:
                # EGW00133 = 1분당 1회 제한. 캐시가 있으면 그거라도 쓴다.
                if self._load_cached_token():
                    log.warning("[%s] 토큰 재발급 제한 - 캐시 토큰 사용", self.label)
                    return self._token  # type: ignore[return-value]
                raise KISError(f"[{self.label}] 토큰 발급 실패: {r.text}", body=r.text)

            d = r.json()
            self._token = d["access_token"]
            secs = int(d.get("expires_in", 86400))
            self._token_exp = datetime.now() + timedelta(seconds=secs)
            self._store.kv_set(self._token_key, json.dumps({
                "token": self._token, "expires_at": self._token_exp.isoformat(),
            }))
            log.info("[%s] KIS 토큰 발급 완료 (만료 %s)",
                     self.label, self._token_exp.strftime("%m-%d %H:%M"))
            return self._token

    def _headers(self, tr_id: str, hashkey: str = "") -> dict:
        h = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.token()}",
            "appkey": self.creds.app_key,
            "appsecret": self.creds.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }
        if hashkey:
            h["hashkey"] = hashkey
        return h

    def _hashkey(self, body: dict) -> str:
        try:
            self._limiter.acquire()
            r = self.session.post(
                f"{self.base}/uapi/hashkey",
                headers={
                    "content-type": "application/json; charset=utf-8",
                    "appkey": self.creds.app_key,
                    "appsecret": self.creds.app_secret,
                },
                data=json.dumps(body),
                timeout=10,
            )
            if r.status_code == 200:
                return r.json().get("HASH", "")
        except Exception as e:  # hashkey는 실패해도 주문은 대개 통과한다
            log.debug("hashkey 실패: %s", e)
        return ""

    # -- 공통 요청 ----------------------------------------------------------
    def _request(self, method: str, path: str, tr_id: str,
                 params: dict | None = None, body: dict | None = None,
                 retries: int = 3, timeout: tuple | None = None) -> dict:
        timeout = timeout or self.timeout
        url = f"{self.base}{path}"
        last: Exception | None = None
        for attempt in range(retries):
            try:
                self._limiter.acquire()
                hk = self._hashkey(body) if (method == "POST" and body) else ""
                headers = self._headers(tr_id, hk)
                if method == "GET":
                    r = self.session.get(url, headers=headers, params=params,
                                         timeout=timeout)
                else:
                    r = self.session.post(url, headers=headers,
                                          data=json.dumps(body or {}), timeout=timeout)

                if r.status_code == 401:
                    self.token(force=True)
                    continue
                if r.status_code == 429 or r.status_code >= 500:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                if r.status_code != 200:
                    raise KISError(f"HTTP {r.status_code}: {r.text[:300]}", body=r.text)

                d = r.json()
                if str(d.get("rt_cd", "0")) != "0":
                    raise KISError(
                        f"{d.get('msg1', '').strip()} ({d.get('msg_cd', '')})",
                        code=str(d.get("msg_cd", "")), body=r.text[:500],
                    )
                return d
            except KISError:
                raise
            except Exception as e:
                last = e
                time.sleep(0.4 * (2 ** attempt))
        raise KISError(f"[{self.label}] 요청 실패 {path}: {last}")

    def _tr(self, real: str, mock: str) -> str:
        return mock if self.creds.is_mock else real

    # ======================================================================
    # 시세
    # ======================================================================
    def current_price(self, symbol: str) -> dict:
        """현재가 시세."""
        d = self._request(
            "GET", "/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
        )
        o = d.get("output", {})
        return {
            "symbol": symbol,
            # bstp_kor_isnm 은 '종목명'이 아니라 '업종명'이다 (삼성전자 -> 전기·전자).
            # 종목명은 종목 마스터에서 채운다.
            "name": "",
            "sector": (o.get("bstp_kor_isnm") or "").strip(),
            "market": (o.get("rprs_mrkt_kor_name") or "").strip(),
            "per": _f(o.get("per")),
            "pbr": _f(o.get("pbr")),
            "eps": _f(o.get("eps")),
            "w52_high": _num(o.get("w52_hgpr")),
            "w52_low": _num(o.get("w52_lwpr")),
            "market_cap": _num(o.get("hts_avls")),      # 억원
            "price": _num(o.get("stck_prpr")),
            "open": _num(o.get("stck_oprc")),
            "high": _num(o.get("stck_hgpr")),
            "low": _num(o.get("stck_lwpr")),
            "prev_close": _num(o.get("stck_sdpr")),
            "change": _num(o.get("prdy_vrss")),
            "change_pct": _f(o.get("prdy_ctrt")),
            "volume": _num(o.get("acml_vol")),
            "value": _num(o.get("acml_tr_pbmn")),
            "upper_limit": _num(o.get("stck_mxpr")),
            "lower_limit": _num(o.get("stck_llam")),
            "market_warn": o.get("mrkt_warn_cls_code", "00"),
            "halt": o.get("temp_stop_yn", "N"),
        }

    def orderbook(self, symbol: str) -> dict:
        """호가 - 최우선 매도/매수호가."""
        d = self._request(
            "GET", "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
            "FHKST01010200",
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
        )
        o = d.get("output1", {})
        return {
            "ask": _num(o.get("askp1")), "bid": _num(o.get("bidp1")),
            "ask_qty": _num(o.get("askp_rsqn1")), "bid_qty": _num(o.get("bidp_rsqn1")),
        }

    def daily_candles(self, symbol: str, start: str, end: str,
                      adjusted: bool = True) -> list[dict]:
        """일봉 (기간별). start/end 는 'YYYYMMDD'. 1회 최대 100건."""
        d = self._request(
            "GET", "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            "FHKST03010100",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_DATE_1": start,
                "FID_INPUT_DATE_2": end,
                "FID_PERIOD_DIV_CODE": "D",
                "FID_ORG_ADJ_PRC": "0" if adjusted else "1",
            },
        )
        out = []
        for row in d.get("output2") or []:
            ds = row.get("stck_bsop_date")
            if not ds or not row.get("stck_clpr"):
                continue
            out.append({
                "ts": f"{ds[:4]}-{ds[4:6]}-{ds[6:8]}",
                "open": _num(row.get("stck_oprc")), "high": _num(row.get("stck_hgpr")),
                "low": _num(row.get("stck_lwpr")), "close": _num(row.get("stck_clpr")),
                "volume": _num(row.get("acml_vol")),
            })
        out.sort(key=lambda r: r["ts"])
        return out

    def minute_candles(self, symbol: str, upto_hhmmss: str = "") -> list[dict]:
        """1분봉. 지정 시각 기준 직전 30건. 모의 도메인은 미지원 - 실전 도메인 사용 권장."""
        if not upto_hhmmss:
            upto_hhmmss = datetime.now().strftime("%H%M%S")
        d = self._request(
            "GET", "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            "FHKST03010200",
            params={
                "FID_ETC_CLS_CODE": "",
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_HOUR_1": upto_hhmmss,
                "FID_PW_DATA_INCU_YN": "N",
            },
        )
        out = []
        for row in d.get("output2") or []:
            ds, hs = row.get("stck_bsop_date"), row.get("stck_cntg_hour")
            if not ds or not hs or not row.get("stck_prpr"):
                continue
            out.append({
                "ts": f"{ds[:4]}-{ds[4:6]}-{ds[6:8]} {hs[:2]}:{hs[2:4]}",
                "open": _num(row.get("stck_oprc")), "high": _num(row.get("stck_hgpr")),
                "low": _num(row.get("stck_lwpr")), "close": _num(row.get("stck_prpr")),
                "volume": _num(row.get("cntg_vol")),
            })
        out.sort(key=lambda r: r["ts"])
        return out

    def multi_price(self, symbols: list[str]) -> list[dict]:
        """관심종목 복수시세. 1회 최대 30종목 - N번 호출하는 것보다 훨씬 싸다."""
        out: list[dict] = []
        for chunk in [symbols[i:i + 30] for i in range(0, len(symbols), 30)]:
            params: dict = {}
            for i, s in enumerate(chunk, 1):
                params[f"FID_COND_MRKT_DIV_CODE_{i}"] = "J"
                params[f"FID_INPUT_ISCD_{i}"] = s
            try:
                d = self._request(
                    "GET", "/uapi/domestic-stock/v1/quotations/intstock-multprice",
                    "FHKST11300006", params=params)
            except KISError as e:
                log.debug("복수시세 실패: %s", e)
                continue
            for row in d.get("output") or []:
                sym = (row.get("inter_shrn_iscd") or "").strip()
                if not sym:
                    continue
                out.append({
                    "symbol": sym,
                    "name": (row.get("inter_kor_isnm") or "").strip(),
                    "price": _num(row.get("inter2_prpr")),
                    "change": _num(row.get("inter2_prdy_vrss")),
                    "change_pct": _f(row.get("prdy_ctrt")),
                    "volume": _num(row.get("acml_vol")),
                    "open": _num(row.get("inter2_oprc")),
                    "high": _num(row.get("inter2_hgpr")),
                    "low": _num(row.get("inter2_lwpr")),
                    "market": (row.get("kospi_kosdaq_cls_name") or "").strip(),
                })
        return out

    # -- 순위 --------------------------------------------------------------
    def volume_rank(self, market: str = "0000", blng: str = "0") -> list[dict]:
        """거래량 순위. market 0000=전체 0001=코스피 1001=코스닥
        blng 0=평균거래량 1=거래증가율 3=거래금액순"""
        d = self._request(
            "GET", "/uapi/domestic-stock/v1/quotations/volume-rank", "FHPST01710000",
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_COND_SCR_DIV_CODE": "20171",
                    "FID_INPUT_ISCD": market, "FID_DIV_CLS_CODE": "0",
                    "FID_BLNG_CLS_CODE": blng, "FID_TRGT_CLS_CODE": "111111111",
                    "FID_TRGT_EXLS_CLS_CODE": "000000", "FID_INPUT_PRICE_1": "",
                    "FID_INPUT_PRICE_2": "", "FID_VOL_CNT": "", "FID_INPUT_DATE_1": ""})
        return [_rank_row(r, "mksc_shrn_iscd", extra=("vol_inrt", "거래증가율"))
                for r in (d.get("output") or [])]

    def fluctuation_rank(self, market: str = "0000", falling: bool = False) -> list[dict]:
        """등락률 순위. falling=True 면 하락률."""
        d = self._request(
            "GET", "/uapi/domestic-stock/v1/ranking/fluctuation", "FHPST01700000",
            params={"fid_rsfl_rate2": "", "fid_cond_mrkt_div_code": "J",
                    "fid_cond_scr_div_code": "20170", "fid_input_iscd": market,
                    "fid_rank_sort_cls_code": "1" if falling else "0",
                    "fid_input_cnt_1": "0", "fid_prc_cls_code": "0",
                    "fid_input_price_1": "", "fid_input_price_2": "", "fid_vol_cnt": "",
                    "fid_trgt_cls_code": "0", "fid_trgt_exls_cls_code": "0",
                    "fid_div_cls_code": "0", "fid_rsfl_rate1": ""})
        return [_rank_row(r, "stck_shrn_iscd") for r in (d.get("output") or [])]

    def market_cap_rank(self, market: str = "0000") -> list[dict]:
        d = self._request(
            "GET", "/uapi/domestic-stock/v1/ranking/market-cap", "FHPST01740000",
            params={"fid_input_price_2": "", "fid_cond_mrkt_div_code": "J",
                    "fid_cond_scr_div_code": "20174", "fid_div_cls_code": "0",
                    "fid_input_iscd": market, "fid_trgt_cls_code": "0",
                    "fid_trgt_exls_cls_code": "0", "fid_input_price_1": "",
                    "fid_vol_cnt": ""})
        return [_rank_row(r, "mksc_shrn_iscd", extra=("stck_avls", "시가총액(억)"))
                for r in (d.get("output") or [])]

    def overseas_rank(self, kind: str = "updown", excd: str = "NAS",
                      falling: bool = False) -> list[dict]:
        """해외주식 순위. kind: updown | volume | amount
        excd: NAS(나스닥) NYS(뉴욕) AMS(아멕스) TSE(도쿄) HKS(홍콩) SHS(상해)"""
        path, tr, params = {
            "updown": ("/uapi/overseas-stock/v1/ranking/updown-rate", "HHDFS76290000",
                       {"AUTH": "", "EXCD": excd, "NDAY": "0", "VOL_RANG": "0",
                        "KEYB": "", "GUBN": "0" if falling else "1"}),
            "volume": ("/uapi/overseas-stock/v1/ranking/trade-vol", "HHDFS76310010",
                       {"AUTH": "", "EXCD": excd, "NDAY": "0", "VOL_RANG": "0",
                        "PRC1": "", "PRC2": "", "KEYB": ""}),
            "amount": ("/uapi/overseas-stock/v1/ranking/trade-pbmn", "HHDFS76320010",
                       {"AUTH": "", "EXCD": excd, "NDAY": "0", "VOL_RANG": "0",
                        "PRC1": "", "PRC2": "", "KEYB": ""}),
        }[kind]
        d = self._request("GET", path, tr, params=params)
        out = []
        for i, r in enumerate(d.get("output2") or [], 1):
            out.append({
                "rank": i,
                "symbol": (r.get("symb") or "").strip(),
                "name": (r.get("name") or "").strip(),
                "price": _f(r.get("last")),
                "change": _f(r.get("diff")),
                "change_pct": _f(r.get("rate")),
                "volume": _num(r.get("tvol")),
                "extra_label": "거래대금",
                "extra": _num(r.get("tamt")),
                "exchange": (r.get("excd") or excd).strip(),
            })
        return out

    def is_market_open_day(self, yyyymmdd: str) -> bool | None:
        """국내 휴장일 조회. 실전 도메인 전용. 실패하면 None."""
        try:
            d = self._request(
                "GET", "/uapi/domestic-stock/v1/quotations/chk-holiday", "CTCA0903R",
                params={"BASS_DT": yyyymmdd, "CTX_AREA_NK": "", "CTX_AREA_FK": ""},
            )
            for row in d.get("output") or []:
                if row.get("bass_dt") == yyyymmdd:
                    return row.get("opnd_yn") == "Y"
        except Exception as e:
            log.debug("휴장일 조회 실패: %s", e)
        return None

    # ======================================================================
    # 계좌
    # ======================================================================
    def balance(self) -> dict:
        # 재시도는 1회만. 모의 서버가 완전히 멈춘 경우 재시도해도 소용없고,
        # 대기만 두 배가 된다. 복구는 broker의 캐시+백오프가 맡는다.
        d = self._request(
            "GET", "/uapi/domestic-stock/v1/trading/inquire-balance",
            self._tr("TTTC8434R", "VTTC8434R"),
            retries=1,
            params={
                "CANO": self.creds.cano,
                "ACNT_PRDT_CD": self.creds.acnt_prdt_cd,
                "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02",
                "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
            },
        )
        holdings = {}
        for it in d.get("output1") or []:
            qty = _num(it.get("hldg_qty"))
            if qty <= 0:
                continue
            holdings[it.get("pdno")] = {
                "symbol": it.get("pdno"),
                "name": (it.get("prdt_name") or "").strip(),
                "qty": qty,
                "sellable": _num(it.get("ord_psbl_qty")),
                "avg_price": _f(it.get("pchs_avg_pric")),
                "price": _num(it.get("prpr")),
                "eval_amt": _num(it.get("evlu_amt")),
                "pnl": _num(it.get("evlu_pfls_amt")),
                "pnl_pct": _f(it.get("evlu_pfls_rt")),
            }
        o2 = (d.get("output2") or [{}])[0]
        return {
            "cash": _num(o2.get("dnca_tot_amt")),
            "orderable_cash": _num(o2.get("prvs_rcdl_excc_amt")) or _num(o2.get("dnca_tot_amt")),
            "stock_eval": _num(o2.get("scts_evlu_amt")),
            "total_eval": _num(o2.get("tot_evlu_amt")),
            "net_asset": _num(o2.get("nass_amt")),
            "pnl": _num(o2.get("evlu_pfls_smtl_amt")),
            "holdings": holdings,
        }

    def orderable_cash(self, symbol: str, price: int) -> int:
        """현금 주문가능금액."""
        try:
            d = self._request(
                "GET", "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
                self._tr("TTTC8908R", "VTTC8908R"),
                params={
                    "CANO": self.creds.cano,
                    "ACNT_PRDT_CD": self.creds.acnt_prdt_cd,
                    "PDNO": symbol, "ORD_UNPR": str(int(price)), "ORD_DVSN": "00",
                    "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N",
                },
            )
            return _num((d.get("output") or {}).get("ord_psbl_cash"))
        except KISError as e:
            log.debug("주문가능금액 조회 실패: %s", e)
            return self.balance()["orderable_cash"]

    def daily_executions(self, yyyymmdd: str = "") -> list[dict]:
        """일별 주문/체결 조회."""
        day = yyyymmdd or datetime.now().strftime("%Y%m%d")
        d = self._request(
            "GET", "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            self._tr("TTTC8001R", "VTTC8001R"),
            params={
                "CANO": self.creds.cano, "ACNT_PRDT_CD": self.creds.acnt_prdt_cd,
                "INQR_STRT_DT": day, "INQR_END_DT": day,
                "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00", "PDNO": "",
                "CCLD_DVSN": "00", "ORD_GNO_BRNO": "", "ODNO": "",
                "INQR_DVSN_3": "00", "INQR_DVSN_1": "",
                "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
            },
        )
        out = []
        for it in d.get("output1") or []:
            out.append({
                "odno": it.get("odno"),
                "symbol": it.get("pdno"),
                "side": "BUY" if it.get("sll_buy_dvsn_cd") == "02" else "SELL",
                "ord_qty": _num(it.get("ord_qty")),
                "filled_qty": _num(it.get("tot_ccld_qty")),
                "avg_price": _f(it.get("avg_prvs")),
                "remain": _num(it.get("rmn_qty")),
                "time": it.get("ord_tmd", ""),
            })
        return out

    # ======================================================================
    # 주문
    # ======================================================================
    def _guard(self) -> None:
        if self.read_only:
            raise KISError(f"[{self.label}] 이 클라이언트는 시세 전용입니다. 주문 불가.")

    def order(self, symbol: str, side: str, qty: int,
              price: int = 0, ord_dvsn: str = "01") -> dict:
        """현금 주문.  side: BUY|SELL,  ord_dvsn: 00 지정가 / 01 시장가 / 03 최유리지정가"""
        self._guard()
        if qty <= 0:
            raise KISError("주문 수량이 0입니다.")
        tr = (self._tr("TTTC0802U", "VTTC0802U") if side == "BUY"
              else self._tr("TTTC0801U", "VTTC0801U"))
        body = {
            "CANO": self.creds.cano,
            "ACNT_PRDT_CD": self.creds.acnt_prdt_cd,
            "PDNO": symbol,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(int(qty)),
            "ORD_UNPR": str(int(price) if ord_dvsn == "00" else 0),
        }
        d = self._request("POST", "/uapi/domestic-stock/v1/trading/order-cash", tr, body=body)
        o = d.get("output", {}) or {}
        return {
            "odno": o.get("ODNO", ""),
            "org_no": o.get("KRX_FWDG_ORD_ORGNO", ""),
            "time": o.get("ORD_TMD", ""),
            "message": (d.get("msg1") or "").strip(),
        }

    def cancel(self, org_no: str, odno: str, qty: int = 0, all_qty: bool = True) -> dict:
        self._guard()
        body = {
            "CANO": self.creds.cano,
            "ACNT_PRDT_CD": self.creds.acnt_prdt_cd,
            "KRX_FWDG_ORD_ORGNO": org_no,
            "ORGN_ODNO": odno,
            "ORD_DVSN": "00",
            "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": str(int(qty)),
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y" if all_qty else "N",
        }
        d = self._request("POST", "/uapi/domestic-stock/v1/trading/order-rvsecncl",
                          self._tr("TTTC0803U", "VTTC0803U"), body=body)
        return {"message": (d.get("msg1") or "").strip()}

    def ping(self) -> tuple[bool, str]:
        """연결 점검. (성공여부, 메시지)"""
        try:
            self.token()
            b = self.balance()
            return True, (f"[{self.label}] 접속 OK | 예수금 {b['cash']:,}원 "
                          f"| 총평가 {b['total_eval']:,}원 | 보유 {len(b['holdings'])}종목")
        except Exception as e:
            return False, f"[{self.label}] 접속 실패: {e}"


def _rank_row(r: dict, code_key: str, extra: tuple[str, str] | None = None) -> dict:
    """순위 API 응답을 공통 형태로 정규화."""
    return {
        "rank": _num(r.get("data_rank")),
        "symbol": (r.get(code_key) or "").strip(),
        "name": (r.get("hts_kor_isnm") or "").strip(),
        "price": _num(r.get("stck_prpr")),
        "change": _num(r.get("prdy_vrss")),
        "change_pct": _f(r.get("prdy_ctrt")),
        "volume": _num(r.get("acml_vol")),
        "extra_label": extra[1] if extra else "",
        "extra": _f(r.get(extra[0])) if extra else 0,
    }


def _num(v) -> int:
    try:
        return int(float(str(v).replace(",", "").strip() or 0))
    except (TypeError, ValueError):
        return 0


def _f(v) -> float:
    try:
        return float(str(v).replace(",", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0
