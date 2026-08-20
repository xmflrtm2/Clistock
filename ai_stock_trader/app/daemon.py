"""백그라운드 가상운용 데몬.

GUI를 켜둬야만 랩이 돌면 불편하다. 이 모듈은 창 없이 프로필들을 굴린다.

  KIS자동매매.exe --lab              저장된 목록대로 실행
  KIS자동매매.exe --lab 장기투자 단타(당일)   지정한 프로필만 실행

GUI와는 DB(kv 테이블)로만 통신한다. 서로 프로세스를 몰라도 되고,
GUI를 껐다 켜도 데몬은 계속 돈다.

  lab_daemon_heartbeat  데몬이 살아 있음을 알리는 시각 (60초 넘으면 죽은 것으로 본다)
  lab_daemon_stop       "1" 이면 데몬이 스스로 정리하고 종료
  lab_daemon_profiles   실행 중인 프로필 목록 (JSON)
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime

log = logging.getLogger(__name__)

HEARTBEAT = "lab_daemon_heartbeat"
STOP = "lab_daemon_stop"
PROFILES = "lab_daemon_profiles"
STARTED = "lab_daemon_started"
ALIVE_SEC = 60


# --------------------------------------------------------------------------
# GUI 쪽에서 쓰는 조회/제어
# --------------------------------------------------------------------------
def status(store) -> dict:
    hb = store.kv_get(HEARTBEAT)
    alive, ago = False, None
    if hb:
        try:
            t = datetime.strptime(hb, "%Y-%m-%d %H:%M:%S")
            ago = (datetime.now() - t).total_seconds()
            alive = ago <= ALIVE_SEC
        except ValueError:
            pass
    try:
        names = json.loads(store.kv_get(PROFILES) or "[]")
    except Exception:
        names = []
    return {
        "alive": alive,
        "heartbeat": hb or "-",
        "seconds_ago": ago,
        "profiles": names,
        "started": store.kv_get(STARTED) or "-",
        "stopping": store.kv_get(STOP) == "1",
    }


def request_stop(store) -> None:
    store.kv_set(STOP, "1")


def clear_flags(store) -> None:
    store.kv_set(STOP, "")
    store.kv_set(HEARTBEAT, "")
    store.kv_set(PROFILES, "[]")


# --------------------------------------------------------------------------
# 데몬 본체
# --------------------------------------------------------------------------
def run_daemon(names: list[str] | None = None, interval: float = 10.0) -> int:
    from .core import AppCore

    core = AppCore()
    store = core.store

    st = status(store)
    if st["alive"]:
        log.error("이미 백그라운드 데몬이 돌고 있습니다 (마지막 신호 %s). 중복 실행 취소.",
                  st["heartbeat"])
        return 1

    wanted = names or [p.name for p in core.profiles.profiles]
    started = []
    for name in wanted:
        pr = core.profiles.get(name)
        if not pr:
            log.warning("프로필 없음: %s", name)
            continue
        ok, msg = core.lab.start(pr)
        log.info(msg)
        if ok:
            started.append(name)

    if not started:
        log.error("실행할 프로필이 없습니다.")
        clear_flags(store)
        return 1

    store.kv_set(STOP, "")
    store.kv_set(PROFILES, json.dumps(started, ensure_ascii=False))
    store.kv_set(STARTED, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("백그라운드 가상운용 시작: %s", ", ".join(started))

    try:
        while True:
            store.kv_set(HEARTBEAT, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            if store.kv_get(STOP) == "1":
                log.info("정지 요청 수신")
                break
            # 엔진이 멈춰 있으면(오류 등) 다시 살린다
            for name in started:
                run = core.lab.runs.get(name)
                if run and not run.running:
                    log.warning("%s 엔진이 멈춰 있어 재시작합니다.", name)
                    run.start()
            core.lab.tick_all_once()
            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("중단됨")
    finally:
        core.lab.stop_all()
        clear_flags(store)
        log.info("백그라운드 가상운용 종료")
    return 0
