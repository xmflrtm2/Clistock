"""4단계 - 최종 값 좁히기.

민감도에서 넓은 구간이 다 양수로 나왔다. 그러면 '제일 높은 값'이 아니라
'구간 한가운데이면서 리스크가 낮은 값'을 골라야 한다. 가장자리는 절벽 옆이다.
"""
import sys, io, json, random, copy
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, '.')
exec(open('eval_common.py', encoding='utf-8').read())

base_risk = risk_of()
random.seed(11)

print("=== 4단계 A: 변동성 돌파 k x 거래량 격자 (MDD·손실확률까지) ===\n")
print(f"  {'k/vol':14s} {'순수익':>8s} {'거래':>5s} {'MDD':>6s} {'PF':>5s} "
      f"{'손실확률':>7s} {'일관성':>6s}")
grid = []
for k in (0.6, 0.7, 0.8, 0.9, 1.0):
    for vf in (1.5, 1.8, 2.0, 2.5):
        p = default_params('volatility_breakout')
        p.update({'k': k, 'vol_filter': vf})
        o = evaluate('volatility_breakout', p, base_risk, f"k{k}/vol{vf}")
        o['k'], o['vol'] = k, vf
        grid.append(o)
        print(f"  {'k'+str(k)+'/v'+str(vf):14s} {o['net']:+7.2f}% {o['trades']:5d} "
              f"{o['mdd']:5.1f}% {o['pf']:5.2f} "
              f"{(o['loss_prob'] or 0):6.0f}% {(o.get('consistency') or 0):5.0f}% "
              f"{'' if not gate(o) else 'X ' + ','.join(gate(o))}")

print("\n=== 4단계 B: 후보들의 종목 교체 검증 (70%, 5회) ===\n")


def subset_test(name, p, label, runs=5):
    rows = []
    for i in range(runs):
        sub = random.sample(SYMS, int(len(SYMS) * 0.7))
        bt = Backtester(store, cfg.cost, base_risk)
        r = bt.run(name, p, sub, CASH, START, END)
        rows.append(r.metrics.get('total_return_pct', 0))
    neg = sum(1 for x in rows if x <= 0)
    print(f"  {label:26s} " + " ".join(f"{x:+6.1f}%" for x in rows) +
          f"   손실 {neg}/{runs}  {'통과' if neg <= 1 else 'X'}")
    return neg, rows


cands = []
for k, vf in ((0.7, 1.8), (0.8, 1.8), (0.8, 2.0), (0.9, 2.0), (1.0, 2.0)):
    p = default_params('volatility_breakout'); p.update({'k': k, 'vol_filter': vf})
    neg, rows = subset_test('volatility_breakout', p, f"변동성돌파 k{k}/vol{vf}")
    cands.append(('volatility_breakout', {'k': k, 'vol_filter': vf}, neg, rows))

for rmax in (55, 58, 60):
    p = default_params('trend_pullback')
    p.update({'rsi_max': rmax, 'max_hold_bars': 60})
    neg, rows = subset_test('trend_pullback', p, f"눌림목 RSI{rmax}/60봉")
    cands.append(('trend_pullback', {'rsi_max': rmax, 'max_hold_bars': 60}, neg, rows))

# 장투는 3단계에서 종목교체 2/5 실패. trail_atr 를 바꾸면 살아나는지만 확인
for tr in (3.0, 4.0):
    p = default_params('long_term_trend'); p['trail_atr'] = tr
    neg, rows = subset_test('long_term_trend', p, f"장기추세 trail{tr}")
    cands.append(('long_term_trend', {'trail_atr': tr}, neg, rows))

json.dump({'grid': grid,
           'subsets': [(a, b, c, d) for a, b, c, d in cands]},
          open('scan_out_4.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print("\n4단계 저장 완료")
