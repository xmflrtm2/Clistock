from app.settings import load_config, RiskConfig
from app.storage import Store
from app.backtest import Backtester
from app.analysis import Analyzer
from app.strategies import default_params
from dataclasses import replace

CASH = 5_000_000          # 실제 예수금으로 검증한다. 10M 으로 하면 현실과 달라진다
START, END = '2021-07-02', '2026-08-20'

store = Store()
cfg = load_config()
SYMS = list(cfg.watchlist)
an = Analyzer(store, cfg.cost)


def risk_of(**over) -> RiskConfig:
    r = copy.deepcopy(cfg.risk)
    for k, v in over.items():
        setattr(r, k, v)
    return r


def evaluate(name, params, risk, tag, do_periods=True):
    bt = Backtester(store, cfg.cost, risk)
    r = bt.run(name, params, SYMS, CASH, START, END)
    if r.error:
        return {'tag': tag, 'error': r.error}
    m = r.metrics
    out = {
        'tag': tag, 'strategy': name,
        'net': m.get('total_return_pct', 0), 'gross': m.get('gross_return_pct', 0),
        'cost_drag': m.get('cost_drag_pct', 0), 'trades': m.get('trades', 0),
        'win': m.get('win_rate', 0), 'mdd': m.get('mdd_pct', 0),
        'pf': m.get('profit_factor', 0), 'cagr': m.get('cagr_pct', 0),
        'payoff': m.get('payoff', 0), 'warnings': r.warnings[:2],
    }
    try:
        mc = an.monte_carlo(r.trades, CASH, runs=400)
        out['loss_prob'] = mc.get('loss_prob', 100)
    except Exception as e:
        out['loss_prob'] = None
    if do_periods:
        try:
            ps = an.period_split(name, params, risk, SYMS, CASH)
            s = an.period_summary(ps)
            out['consistency'] = s.get('consistency', 0)
            out['years'] = {p['period']: round(p['return_pct'], 1) for p in ps}
        except Exception:
            out['consistency'] = None
    return out


def gate(o):
    f = []
    if o.get('error'):
        return ['오류']
    if o['trades'] < 30:
        f.append(f"거래{o['trades']}")
    if o['net'] <= 0:
        f.append(f"수익{o['net']:.1f}%")
    if o.get('loss_prob') is not None and o['loss_prob'] >= 45:
        f.append(f"손실확률{o['loss_prob']:.0f}%")
    if o.get('consistency') is not None and o['consistency'] < 50:
        f.append(f"일관성{o['consistency']:.0f}%")
    if o['mdd'] > 35:
        f.append(f"MDD{o['mdd']:.0f}%")
    return f


def show(o):
    if o.get('error'):
        print(f"  {o['tag']:38s} 오류: {o['error'][:60]}")
        return
    f = gate(o)
    print(f"  {o['tag']:38s} 순 {o['net']:+7.2f}%  총 {o['gross']:+7.2f}%  "
          f"비용 {o['cost_drag']:5.1f}%p  거래 {o['trades']:4d}  승 {o['win']:4.1f}%  "
          f"MDD {o['mdd']:5.1f}%  PF {o['pf']:.2f}  손실확률 "
          f"{o['loss_prob'] if o['loss_prob'] is None else round(o['loss_prob'])}%  "
          f"일관성 {o.get('consistency')}  {'통과' if not f else 'X ' + ','.join(f)}")
    if o.get('years'):
        print(f"      연도별: {o['years']}")


