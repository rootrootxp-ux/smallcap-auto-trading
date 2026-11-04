"""Independent validation of the exported shared-account ledger."""
import csv
from collections import defaultdict
from datetime import datetime
import math
import json
from pathlib import Path
import sys


def read(path):
    with path.open(encoding='utf-8-sig') as f:return list(csv.DictReader(f))


def equal(a,b):return math.isfinite(a) and math.isfinite(b) and math.isclose(a,b,rel_tol=0,abs_tol=1e-6)


def validate(root=Path('results/portfolio')):
    names=['portfolio_summary','portfolio_trades','portfolio_orders','portfolio_equity','portfolio_daily','portfolio_positions','portfolio_input_audit','independent_comparison','portfolio_metadata']
    for name in names:
        if not (root/f'{name}.csv').is_file():raise ValueError(f'Missing {name}')
    if not (root/'report.html').is_file():raise ValueError('Missing HTML report')
    summaries=read(root/'portfolio_summary.csv')
    if len(summaries)!=12 or len({r['scenario'] for r in summaries})!=12:raise ValueError('Expected twelve independent portfolio scenarios')
    trades=defaultdict(list);orders=defaultdict(list);equity=defaultdict(list);positions=defaultdict(list)
    for name,group in [('portfolio_trades',trades),('portfolio_orders',orders),('portfolio_equity',equity),('portfolio_positions',positions)]:
        for r in read(root/f'{name}.csv'):group[r['scenario']].append(r)
    for s in summaries:
        scenario=s['scenario'];history=trades[scenario]
        if float(s['initial_cash'])!=1000000 or float(s['annual_interest_pct'])!=0:raise ValueError('Invalid cash-only configuration')
        if len(history)!=int(s['trades']):raise ValueError('Trade count mismatch')
        if not equal(sum(float(t['net_pnl']) for t in history),float(s['realized_pnl'])):raise ValueError('Realized PnL mismatch')
        for t in history:
            if not equal(float(t['net_pnl']),float(t['gross_pnl'])-float(t['entry_fee'])-float(t['exit_fee'])):raise ValueError('Trade cost mismatch')
            if float(t['interest'])!=0:raise ValueError('Credit charge in cash-only model')
            if t['exit_signal_bar']!='事前設定' and not datetime.fromisoformat(t['exit_signal_bar'])<datetime.fromisoformat(t['exit_time']):raise ValueError('Same-bar sell fill')
            if not datetime.fromisoformat(t['entry_time'])<datetime.fromisoformat(t['exit_time']):raise ValueError('Non-chronological trade')
        previous=None
        for r in equity[scenario]:
            ts=datetime.fromisoformat(r['timestamp'])
            if previous is not None and ts<previous:raise ValueError('Equity events out of order')
            previous=ts
            cash,reserve,free=[float(r[k]) for k in ('cash','reserved_cash','free_cash')]
            if min(cash,reserve,free)<-1e-6 or not equal(cash,reserve+free):raise ValueError('Invalid cash reservation')
            holdings=json.loads(r['holdings'])
            if len(holdings)!=int(r['open_positions']) or not equal(sum(p['book_cost'] for p in holdings.values()),float(r['position_cost'])):
                raise ValueError('Holdings history/book mismatch')
            if not equal(cash+float(r['position_cost']),1000000+float(r['realized_pnl'])):raise ValueError('Cash/book mismatch')
            if not equal(float(r['equity']),1000000+float(r['realized_pnl'])+float(r['unrealized_pnl'])):raise ValueError('Account equity mismatch')
        for o in orders[scenario]:
            if o['action']=='buy' and o['status']=='filled':
                q=float(o['quantity'])
                if q<100 or q%100 or float(o['fill_price'])*q>300000:raise ValueError('Position constraint violated')
                if datetime.fromisoformat(o['fill_time'])!=datetime.fromisoformat(o['expected_fill']):raise ValueError('Fill invented across a missing slot')
                if not datetime.fromisoformat(o['signal_bar'])<datetime.fromisoformat(o['fill_time']):raise ValueError('Same-bar buy fill')
                if float(o['reference_price'])*q+float(o['fee_yen'])+float(o['reference_price'])*q*float(o['fee_bps'])/10000 > float(o['available_cash_at_signal'])+1e-6:raise ValueError('Future cash used for sizing')
        holdings=positions[scenario]
        if len(holdings)!=int(s['open_positions']):raise ValueError('Open position count mismatch')
        if (s['unresolved']=='True')!=bool(holdings):raise ValueError('Unresolved flag mismatch')
        if holdings:
            if s['total_pnl'] or s['max_drawdown']:raise ValueError('Incomplete portfolio falsely reported final PnL/DD')
        elif not equal(float(s['cash'])-1000000,float(s['total_pnl'])):raise ValueError('Final cash/PnL mismatch')
    resolved=sum(not s['unresolved']=='True' for s in summaries)
    if not resolved:raise ValueError('All scenarios are unpriced; dataset cannot validate a completed portfolio')
    print('PASS: 12 shared-account scenarios; resolved',resolved,'unresolved',12-resolved,'trades',sum(len(v) for v in trades.values()))
    return summaries


if __name__=='__main__':validate(Path(sys.argv[1]) if len(sys.argv)>1 else Path('results/portfolio'))
