"""3단계 - 과최적화 검증.

거래를 줄였더니 좋아졌다. 그런데 그게 '이 값이라서' 좋은 거라면 과최적화다.
① 이웃 파라미터에서도 유지되나 ② 종목을 갈아도 유지되나
"""
import sys, io, json, random, copy
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, '.')
exec(open('eval_common.py', encoding='utf-8').read())

base_risk = risk_of()
random.seed(7)

FINAL = {
    'volatility_breakout': {"k": 1.0, "vol_filter": 2.0},
    'trend_pullback': {"rsi_max": 55, "max_hold_bars": 40},
    'long_term_trend': {},
}

print("=== 3단계 A: 파라미터 민감도 (이웃 값에서도 버티나) ===\n")
sweeps = {
    'volatility_breakout': [('k', [0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2]),
                            ('vol_filter', [1.2, 1.5, 1.8, 2.0, 2.2, 2.5, 3.0])],
    'trend_pullback': [('rsi_max', [45, 50, 55, 60, 65]),
                       ('max_hold_bars', [15, 25, 40, 60, 90])],
    'long_term_trend': [('trend_ma', [150, 180, 200, 220, 250]),
                        ('trail_atr', [1.5, 2.0, 2.5, 3.0, 3.5, 4.0])],
}
sens_out = {}
for name, plist in sweeps.items():
    for pname, vals in plist:
        line = []
        for v in vals:
            p = default_params(name); p.update(FINAL[name]); p[pname] = v
            bt = Backtester(store, cfg.cost, base_risk)
            r = bt.run(name, p, SYMS, CASH, START, END)
            m = r.metrics
            line.append((v, m.get('total_return_pct', 0), m.get('trades', 0),
                         m.get('mdd_pct', 0)))
        sens_out[f"{name}.{pname}"] = line
        rets = [x[1] for x in line]
        pos = sum(1 for x in rets if x > 0)
        print(f"{name}.{pname}:  " + "  ".join(
            f"{v}={ret:+.1f}%({tr})" for v, ret, tr, _ in line))
        print(f"    → 폭 {max(rets) - min(rets):.1f}%p, "
              f"양수 {pos}/{len(rets)}  "
              f"{'평탄 - 특정값 의존 아님' if pos >= len(rets) * 0.7 else '주의 - 값에 휘둘림'}\n")

print("\n=== 3단계 B: 종목 교체 검증 (70%만 뽑아 5회) ===\n")
sub_out = {}
for name, over in FINAL.items():
    p = default_params(name); p.update(over)
    rows = []
    for i in range(5):
        sub = random.sample(SYMS, int(len(SYMS) * 0.7))
        bt = Backtester(store, cfg.cost, base_risk)
        r = bt.run(name, p, sub, CASH, START, END)
        rows.append((r.metrics.get('total_return_pct', 0),
                     r.metrics.get('trades', 0), r.metrics.get('mdd_pct', 0)))
    neg = sum(1 for x in rows if x[0] <= 0)
    sub_out[name] = rows
    print(f"{name:22s} " + "  ".join(f"{a:+.1f}%({b})" for a, b, _ in rows))
    print(f"    → 손실 {neg}/5  {'통과' if neg <= 1 else 'X 종목 구성에 취약'}\n")

json.dump({'sens': sens_out, 'subsets': sub_out},
          open('scan_out_3.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print("3단계 저장 완료")
