# -*- coding: utf-8 -*-
"""창 없이 모의투자 엔진을 돌리는 무인 러너.

  python tools/auto_trade.py            오늘 16:40 까지만 (작업 스케줄러용)
  python tools/auto_trade.py --forever  끌 때까지 계속 (상시 실행용)
  python tools/auto_trade.py --paper    PAPER(로컬 가상계좌) 모드 허용

스케줄러 등록 예 (평일 08:50):
  schtasks /create /tn ClistockAutoTrade /sc weekly /d MON,TUE,WED,THU,FRI
           /st 08:50 /tr "pythonw <경로>\tools\auto_trade.py"

무인 실행의 안전장치:
  * REAL 모드는 절대 자동 시작하지 않는다 (GUI에서 직접 승인해야 함)
  * 모의투자 키가 죽어 PAPER 로 조용히 대체된 경우도 기본적으로 거부한다
    - 몇 주간 "모의 검증"이라 믿었는데 로컬 가상체결이었던 사고 방지.
    PAPER 로 돌리려면 --paper 를 명시할 것.
  * 포트 잠금(47603)으로 러너 이중 실행을 막는다. GUI 는 이 포트로 러너를
    감지해 API 호출 한도를 나눠 쓴다. 단, GUI 의 [모의투자 시작] 버튼과
    동시에 쓰면 주문이 두 번 나가므로 러너 가동 중엔 GUI 엔진을 켜지 말 것.
  * pythonw 에서는 print 가 보이지 않으므로 기록은 logs/trader.log 로 남긴다.
"""
from __future__ import annotations

import logging
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

LOCK_PORT = 47603          # 이중 실행 방지 + GUI의 러너 감지용
STOP_AT = "16:40"          # --forever 가 아니면 이 시각에 정지 후 종료

log = logging.getLogger("auto_trade")


def say(msg: str) -> None:
    log.info(msg)
    try:
        print(msg)
    except Exception:
        pass                       # pythonw: stdout 없음


def main() -> int:
    from app.core import AppCore, setup_logging
    setup_logging()                # pythonw 에서도 logs/trader.log 에 남게 먼저

    # -- 이중 실행 방지 -------------------------------------------------
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
        lock.listen(1)
    except OSError:
        say("이미 자동매매 러너가 돌고 있습니다. 종료.")
        return 0

    core = AppCore()
    forever = "--forever" in sys.argv
    mode = core.broker.mode if core.broker else "?"
    if core.status_msg:
        say(f"[알림] {core.status_msg}")

    # -- 무인 실행 모드 검증 ---------------------------------------------
    if mode == "REAL":
        say("REAL 모드에서는 자동 시작하지 않습니다. GUI에서 직접 승인하세요.")
        return 1
    if mode != "MOCK" and "--paper" not in sys.argv:
        say(f"MOCK 계좌로 연결되지 않았습니다 (현재 {mode}). "
            f".env 의 모의투자 키를 확인하세요. PAPER 로 돌리려면 --paper 를 붙이세요.")
        return 1

    # GUI 가 함께 떠 있을 수 있으므로 appkey 호출 한도를 절반만 쓴다
    for c in (core.quote_client, core.trade_client):
        if c:
            c.throttle_share(2)

    core.engine.start()
    time.sleep(1.5)
    if not core.engine.running:
        say("엔진이 시작되지 않았습니다. [설정]에서 활성 전략을 확인하세요.")
        return 1
    say(f"엔진 시작 (모드 {mode}, 관심종목 {len(core.cfg.watchlist)}개). "
        + ("끌 때까지 계속 운용." if forever else f"{STOP_AT} 까지 운용."))

    halted_said = False
    try:
        while forever or datetime.now().strftime("%H:%M") < STOP_AT:
            time.sleep(30)
            if not core.engine.running:   # 엔진 스레드가 죽은 경우
                say("엔진이 정지 상태입니다. 러너를 종료합니다.")
                return 2
            halted = bool(getattr(core.engine.risk.state, "halted", False))
            if halted and not halted_said:
                say(f"[주의] 리스크 가드 발동으로 매매가 중단된 상태입니다: "
                    f"{getattr(core.engine.risk.state, 'halt_reason', '')} "
                    f"(러너는 계속 돌며, 낙폭 사유는 다음 거래일에 자동 해제됩니다)")
            halted_said = halted
    except KeyboardInterrupt:
        say("중단 요청.")
    finally:
        if core.engine.running:
            say("엔진 정지 중 - 진행 중인 주문/기록이 끝나길 기다립니다 (최대 90초)…")
            core.engine.stop(join=True, timeout=90)
        say("엔진 정지, 러너 종료.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
