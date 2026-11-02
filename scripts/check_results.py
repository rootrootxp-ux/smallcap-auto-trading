"""Validate current-run coverage and accounting, accepting documented partial success."""
import csv
from collections import defaultdict
from datetime import datetime
import math
import os
from pathlib import Path
import sys

# Support both `python scripts/check_results.py` and module imports in tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_outcome import summarize, MIN_QUALIFIED_DAYS


def read(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def close(a, b):
    return math.isfinite(a) and math.isfinite(b) and math.isclose(a,b,abs_tol=1e-6,rel_tol=0)


def validate(root=Path('results')):
    for name in ('report.html','screening.csv','backtest_summary.csv',
                 'trade_history.csv','run_status.csv','run_summary.csv'):
        if not (root/name).is_file() or (root/name).stat().st_size == 0:
            raise ValueError(f'Missing or empty output: {name}')
    statuses = read(root/'run_status.csv')
    overview = read(root/'run_summary.csv')
    if len(overview)!=1:
        raise ValueError('Expected one current run summary')
    overview=overview[0]
    current_id=overview['実行ID']
    if not current_id:
        raise ValueError('Missing run ID')
    expected=summarize(statuses)
    if any(str(overview.get(key))!=str(value) for key,value in expected.items()):
        raise ValueError('Run summary contradicts symbol results or coverage policy')
    by_code={r['コード']:r for r in statuses}
    if set(by_code)!=set(overview['対象銘柄コード'].split(';')):
        raise ValueError('Requested symbol coverage mismatch')
    if len(by_code)!=len(statuses):
        raise ValueError('Duplicate symbol status')
    for r in statuses:
        if r['実行ID']!=current_id or r['全体結果']!=overview['全体結果']:
            raise ValueError('Stale or inconsistent symbol results')
        if r['状態']=='検証可能' and int(r['利用日数'])<MIN_QUALIFIED_DAYS:
            raise ValueError('Insufficient days counted as eligible')
    for r in statuses:
        if r['状態']=='データ不足' and int(r['5分足本数'])>0 and int(r['利用日数'])>=MIN_QUALIFIED_DAYS:
            raise ValueError('Qualified data mislabeled as insufficient')
    summaries=read(root/'backtest_summary.csv')
    trades=read(root/'trade_history.csv')
    summary_groups=defaultdict(list)
    trade_groups=defaultdict(list)
    for r in summaries:
        if r['実行ID']!=current_id or r['コード'] not in by_code:
            raise ValueError('Stale or unexpected backtest rows')
        summary_groups[r['コード']].append(r)
    for t in trades:
        if t['実行ID']!=current_id or t['コード'] not in by_code:
            raise ValueError('Stale or unexpected trade rows')
        key=(t['コード'],t['戦略'],float(t['仮定片道スリッページ(円/株)']),t['費用シナリオ'])
        trade_groups[key].append(t)
        qty=float(t['数量(株)'])
        if not (qty>=100 and qty%100==0 and 0<float(t['買値(円)'])*qty<=300000):
            raise ValueError('Trade violates position or lot limit')
        if not datetime.fromisoformat(t['買い日時'])<datetime.fromisoformat(t['売り日時']):
            raise ValueError('Invalid execution timestamps')
    expected_keys={(strategy,slip,cost) for strategy in ('MAのみ','VWAP+出来高','高値ブレイク+出来高')
                   for slip in (1.,2.) for cost in ('費用ゼロ','仮定費用込み')}
    seen_trade_keys=set()
    for code,status in by_code.items():
        group=summary_groups[code]
        should_have_results=status['状態']=='検証可能' or (
            status['状態']=='データ不足' and int(status['5分足本数'])>0)
        if not should_have_results:
            if group:
                raise ValueError('Unexpected results for uncomputed symbol')
            continue
        keys={(r['戦略'],float(r['片道スリッページ(円/株)']),r['費用シナリオ']) for r in group}
        if len(group)!=12 or keys!=expected_keys:
            raise ValueError(f'{code}: missing or duplicate strategy/slippage/cost results')
        if not (root/'prices'/current_id/f'{code.replace(".","_")}_5m.csv').is_file():
            raise ValueError(f'{code}: missing current input prices')
        for r in group:
            expected_state='検証済み' if status['状態']=='検証可能' else '利用日数不足'
            if r['検証状態']!=expected_state or int(r['利用日数'])!=int(status['利用日数']):
                raise ValueError('Backtest data qualification mismatch')
            key=(code,r['戦略'],float(r['片道スリッページ(円/株)']),r['費用シナリオ'])
            seen_trade_keys.add(key)
            history=trade_groups[key]
            if len(history)!=int(r['取引数']):
                raise ValueError('Trade count mismatch')
            for summary_col,trade_col in [('損益(円)','損益(円)'),('費用控除前損益(円)','費用控除前損益(円)'),
                                          ('手数料合計(円)','手数料(円)'),('信用金利合計(円)','信用金利(円)')]:
                if not close(float(r[summary_col]),sum(float(t[trade_col]) for t in history)):
                    raise ValueError('Accounting mismatch or nonfinite PnL')
            if not close(float(r['損益(円)']),float(r['費用控除前損益(円)'])-float(r['手数料合計(円)'])-float(r['信用金利合計(円)'])):
                raise ValueError('Cost accounting mismatch')
            dd=[float(r[c]) for c in ('日内最大DD(円)','期間最大DD(円)','日末残高系列最大DD(円)')]
            if not all(math.isfinite(v) and v>=0 for v in dd) or dd[1]+1e-6<dd[0]:
                raise ValueError('Invalid drawdown')
    if set(trade_groups)-seen_trade_keys:
        raise ValueError('Trades without a matching summary')
    if f"全体結果: {overview['全体結果']}" not in (root/'report.html').read_text(encoding='utf-8'):
        raise ValueError('Report outcome mismatch')
    print(f"{overview['全体結果']}: {overview['検証可能銘柄数']}/{overview['対象銘柄数']}銘柄、{len(summaries)}条件を整合検証")
    if overview['全体結果']=='失敗':
        raise ValueError('失敗: 重大不具合または検証可能銘柄数不足')
    if overview['全体結果']=='一部成功':
        print('::warning::一部銘柄はデータ不足・取得失敗。run_status.csvを確認してください。')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a',encoding='utf-8') as f:
            f.write(f"## バックテスト: {overview['全体結果']}\n\n検証可能 {overview['検証可能銘柄数']}/{overview['対象銘柄数']}、データ不足 {overview['データ不足銘柄数']}、取得失敗 {overview['取得失敗銘柄数']}、重大不具合 {overview['重大不具合銘柄数']}。\n")
    return overview


if __name__=='__main__':
    validate()
