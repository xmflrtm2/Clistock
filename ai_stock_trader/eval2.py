"""2단계 - 거래 횟수를 줄이는 방향, 그리고 리스크 설정 스윕."""
import sys, io, json, copy
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, '.')
exec(open('eval_common.py', encoding='utf-8').read())

print(f"=== 2단계: 거래 줄이기 ({START}~{END}) ===\n")
results = []
base_risk = risk_of()

print("[A] 추세 눌림목 - 오래 들고 덜 사고팔기")
variants = {
    "기본": {},
    "보유확대 40봉": {"max_hold_bars": 40},
    "보유확대+R3": {"max_hold_bars": 40, "take_profit_r": 3.0},
    "진입엄격 RSI55": {"rsi_max": 55},
    "진입엄격+보유확대": {"rsi_max": 55, "max_hold_bars": 40},
    "눌림 3봉+보유확대": {"pullback_lookback": 3, "max_hold_bars": 40},
    "목표없음 트레일만": {"max_hold_bars": 60, "take_profit_r": 0, "take_profit_pct": 0},
}
for tag, over in variants.items():
    p = default_params('trend_pullback'); p.update(over)
    o = evaluate('trend_pullback', p, base_risk, f"눌림목 {tag}")
    o['over'] = over
    results.append(o); show(o)

print("\n[B] 변동성 돌파 - 진입 문턱을 크게 올려 거래를 줄인다")
variants = {
    "기본": {},
    "k0.8+거래량1.5": {"k": 0.8, "vol_filter": 1.5},
    "k1.0+거래량2.0": {"k": 1.0, "vol_filter": 2.0},
    "k0.8+MA60+범위1.5": {"k": 0.8, "ma_filter": 60, "min_range_pct": 1.5},
    "k1.0+MA60+거래량2+범위2": {"k": 1.0, "ma_filter": 60, "vol_filter": 2.0,
                            "min_range_pct": 2.0},
}
for tag, over in variants.items():
    p = default_params('volatility_breakout'); p.update(over)
    o = evaluate('volatility_breakout', p, base_risk, f"변동성돌파 {tag}")
    o['over'] = over
    results.append(o); show(o)

print("\n[C] 장기 추세추종 - 리스크(자금관리) 설정 스윕")
for loss_pct in (0.5, 1.0, 1.5, 2.0):
    for weight in (20.0, 30.0):
        r = risk_of(max_loss_per_trade_pct=loss_pct, max_position_weight_pct=weight)
        o = evaluate('long_term_trend', default_params('long_term_trend'), r,
                     f"장투 1회손실{loss_pct}% 비중{weight:.0f}%", do_periods=False)
        o['risk_over'] = {'max_loss_per_trade_pct': loss_pct,
                          'max_position_weight_pct': weight}
        results.append(o); show(o)

json.dump(results, open('scan_out_2.json', 'w', encoding='utf-8'),
          ensure_ascii=False, indent=1)
print("\n2단계 저장 완료")
