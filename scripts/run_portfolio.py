"""Run long-only shared cash research from the independent run's exact saved inputs."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pandas as pd
from smallcap_auto_v3 import Costs, quality_check
from portfolio_engine import simulate_portfolio,prepare,STRATEGIES
from portfolio_report import write_report


def read(path):
    with path.open(encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))


def run(source,output,fee_yen=100.,fee_bps=0.):
    started=time.perf_counter();source=Path(source);output=Path(output)
    source_run=read(source/'run_summary.csv')[0]
    statuses=read(source/'run_status.csv')
    if source_run['全体結果']=='失敗':raise ValueError('Independent source run failed its coverage or program checks')
    if source_run['重大不具合銘柄数']!='0':raise ValueError('Source contains fatal defects')
    frames={};audit=[];hashes={}
    for status in statuses:
        code=status['コード']
        path=source/'prices'/source_run['実行ID']/f'{code.replace(".","_")}_5m.csv'
        record=dict(status)
        record['portfolio_input']='取得データなし'
        if path.exists():
            frame=pd.read_csv(path,index_col=0)
            frame.index=pd.to_datetime(frame.index,utc=True).tz_convert('Asia/Tokyo')
            frames[code]=frame
            record['portfolio_input']='全日事後除外せず、時系列の欠損処理で使用'
            record['input_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
            hashes[code]=record['input_sha256']
        audit.append(record)
    if len(frames)<int(source_run['最低必要銘柄数']):raise ValueError('Insufficient price coverage')
    prepared=prepare(frames)
    results=[]
    for strategy in STRATEGIES:
        for slip in (1.,2.):
            for costs in [Costs('費用ゼロ'),Costs('現物手数料込み',fee_yen,fee_bps,0.)]:
                print(strategy,slip,costs.name,flush=True)
                result=simulate_portfolio(frames,strategy,slip,costs,prepared=prepared)
                labels={'scenario':f'{strategy}/{slip}/{costs.name}','strategy':strategy,'slippage':slip,'cost_scenario':costs.name,
                        'fee_yen':costs.fee_yen,'fee_bps':costs.fee_bps,'annual_interest_pct':0.,'source_run_id':source_run['実行ID']}
                for key in ['summary','trades','orders','equity','daily','positions']:
                    for row in [result[key]] if key=='summary' else result[key]:row.update(labels)
                results.append(result)
    comparisons=[]
    old=read(source/'backtest_summary.csv')
    for result in results:
        s=result['summary'];old_label='費用ゼロ' if s['cost_scenario']=='費用ゼロ' else '仮定費用込み'
        rows=[r for r in old if r['戦略']==s['strategy'] and float(r['片道スリッページ(円/株)'])==s['slippage'] and r['費用シナリオ']==old_label and r['検証状態']=='検証済み']
        comparisons.append({'strategy':s['strategy'],'slippage':s['slippage'],'cost_scenario':s['cost_scenario'],
            'shared_initial_cash':1000000,'shared_final_cash':s['cash'],'shared_total_pnl':s['total_pnl'],
            'shared_trades':s['trades'],'shared_max_drawdown':s['max_drawdown'],'shared_open_positions':s['open_positions'],
            'independent_eligible_symbols':len(rows),'independent_initial_cash_per_symbol':1000000,
            'independent_pnl_sum_not_portfolio':sum(float(r['損益(円)']) for r in rows),
            'independent_trades':sum(int(r['取引数']) for r in rows),
            'old_interest_pct':';'.join(sorted({r['仮定年利(%)'] for r in rows})),
            'old_fee_yen':';'.join(sorted({r['片道固定手数料(円)'] for r in rows})),
            'old_fee_bps':';'.join(sorted({r['片道手数料(bps)'] for r in rows})),
            'comparison_limit':'旧方式は各銘柄独立100万円・全日事後品質判定・最終観測足決済（11:30等も含む取得セッション）。新方式は時系列・固定15:20・現物金利0。差を資金共有だけの因果効果としない。独立条件の合算はポートフォリオではない。'})
    metadata={'source_run_id':source_run['実行ID'],'requested_symbols':len(statuses),'loaded_symbols':len(frames),
              'initial_cash':1000000,'position_limit':300000,'lot':100,'cash_only':True,'shorting_implemented':False,
              'credit_implemented':False,'priority':'signal_time then ticker ascending; all sells before buys',
              'sizing':'signal close + slippage and then-known unreserved cash; no anticipated sale proceeds',
              'missing_policy':'missing expected slot cancels order; held missing data remains unresolved, no invented sale; other exits continue',
              'eod':'15:20 scheduled open before closing auction; not retrospectively last available bar',
              'elapsed_seconds':time.perf_counter()-started,
              'selection_bias':'現在の50銘柄には先読み・生存者バイアス。将来の収益性の証明ではない。',
              'input_hashes':json.dumps(hashes,ensure_ascii=False)}
    write_report(output,results,audit,comparisons,metadata)
    if output.resolve().parent==source.resolve():
        original_report=source/'report.html'
        if original_report.is_file():
            body=original_report.read_text(encoding='utf-8')
            link=f'<p><a href="{output.name}/report.html">共有資金100万円・現物ポートフォリオのレポート</a></p>'
            if link not in body:
                original_report.write_text(body.replace('</html>',link+'</html>'),encoding='utf-8')
    (output/'manifest.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2))
    print('Portfolio scenarios:',len(results),'unresolved:',sum(r['summary']['unresolved'] for r in results),flush=True)
    return results


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,default=Path('results'))
    p.add_argument('--output',type=Path,default=Path('results/portfolio'))
    p.add_argument('--fee-yen',type=float,default=100.)
    p.add_argument('--fee-bps',type=float,default=0.)
    a=p.parse_args();run(a.source,a.output,a.fee_yen,a.fee_bps)
