"""KIS 자동매매 시스템 - 단일 진입점.

  python run.py          GUI 실행
  python run.py --check  연결/설정 점검만 (콘솔)
  python run.py --collect  관심종목 일봉 수집 (콘솔)
  python run.py --backtest 전략명  백테스트 (콘솔)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 윈도우 콘솔 기본 인코딩(cp949)에서 한글/기호가 깨지거나 죽는 걸 막는다.
# PyInstaller --windowed 빌드에서는 stdout이 None이라 방어가 필요하다.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def main() -> int:
    args = sys.argv[1:]

    if not args:
        from app.ui import run
        run()
        return 0

    if args[0] == "--lab":
        from app.core import setup_logging
        from app.daemon import run_daemon
        setup_logging()
        return run_daemon(args[1:] or None)

    from app.core import AppCore
    core = AppCore()

    if args[0] == "--lab-stop":
        from app.daemon import request_stop, status
        st = status(core.store)
        if not st["alive"]:
            print("돌고 있는 백그라운드 운용이 없습니다.")
            return 0
        request_stop(core.store)
        print(f"정지 요청 보냄 ({', '.join(st['profiles'])}). 최대 10초 뒤 종료됩니다.")
        return 0

    if args[0] == "--lab-status":
        from app.daemon import status
        st = status(core.store)
        if st["alive"]:
            print(f"실행 중  |  프로필: {', '.join(st['profiles'])}")
            print(f"  시작 {st['started']} / 마지막 신호 {st['heartbeat']}")
        else:
            print("실행 중 아님" + (f" (마지막 신호 {st['heartbeat']})"
                                    if st["heartbeat"] != "-" else ""))
        for perf in core.lab.all_performance():
            print(f"  [{perf['name']}] 경과 {perf['elapsed']} | "
                  f"{perf['initial']:,.0f} -> {perf['equity']:,.0f} "
                  f"({perf['return_pct']:+.2f}%) | 거래 {perf['trades']}")
        return 0

    if args[0] == "--check":
        for name, ok, msg in core.diagnostics():
            print(f"{'[OK]' if ok else '[--]'} {name:<12} {msg}")
        print()
        print(core.connection_test())
        return 0

    if args[0] == "--collect":
        if not core.collector:
            print("시세 클라이언트가 없습니다. .env를 확인하세요.")
            return 1
        core.collector.sync_daily_all(core.cfg.watchlist,
                                      core.cfg.data.daily_history_days, print)
        return 0

    if args[0] == "--backtest":
        name = args[1] if len(args) > 1 else "volatility_breakout"
        params = next((e.get("params") or {} for e in core.cfg.strategies
                       if e["name"] == name), {})
        r = core.backtester.run(name, params, core.cfg.watchlist)
        if r.error:
            print("실패:", r.error)
            return 1
        print(f"\n=== {name} | {r.start} ~ {r.end} ===")
        for k, v in r.metrics.items():
            print(f"  {k:<24} {v}")
        for w in r.warnings:
            print("  [경고]", w)
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n중단됨")
    except Exception as e:  # exe 로 돌릴 때 창이 그냥 닫히는 걸 막는다
        import traceback
        traceback.print_exc()
        input("\n오류가 발생했습니다. 엔터를 누르면 종료합니다...")
