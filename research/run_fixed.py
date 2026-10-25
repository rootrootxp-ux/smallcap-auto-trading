"""Reproducible fixed-condition comparison. Public data only; no orders."""
import csv
import argparse
import hashlib
import html
import json
import platform
import subprocess
import sys
import time
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from curl_cffi import requests

from .frozen import engine_1a488e3 as baseline
from . import causal_engine

ROOT = Path(__file__).resolve().parents[1]
FROZEN = ROOT / 'research/frozen'
OUT = ROOT / 'results/fixed_validation'
SAVED = None


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_config():
    config = json.loads((FROZEN/'config.json').read_text())
    for name, expected in config['hashes'].items():
        if sha(FROZEN/name) != expected:
            raise ValueError(f'Frozen input changed: {name}')
    return config


def read_csv(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def write_csv(path, rows):
    columns = list(dict.fromkeys(k for row in rows for k in row)) or ['状態']
    baseline.write_csv(path, rows, columns)


def bounded_frame(body, start, end):
    """The endpoint may append a latest quote OUTSIDE the requested window."""
    result = body.get('chart', {}).get('result') or []
    if not result or not result[0].get('timestamp'):
        return pd.DataFrame(), {'raw_rows':0, 'outside_window':0, 'invalid_rows':0}
    data = result[0]
    quote = data['indicators']['quote'][0]
    frame = pd.DataFrame({key.title():quote[key] for key in ['open','high','low','close','volume']},
                         index=pd.to_datetime(data['timestamp'], unit='s', utc=True).tz_convert('Asia/Tokyo'))
    lower = pd.Timestamp(start, tz='Asia/Tokyo')
    upper = pd.Timestamp(end, tz='Asia/Tokyo')
    inside = (frame.index >= lower) & (frame.index < upper)
    stats = {'raw_rows':len(frame), 'outside_window':int((~inside).sum())}
    frame = frame.loc[inside].sort_index()
    invalid = ~np.isfinite(frame.astype(float)).all(axis=1) | (frame[['Open','High','Low','Close']] <= 0).any(axis=1)
    stats['invalid_rows'] = int(invalid.sum())
    # A null grid timestamp is not an observed quote (including lunch placeholders).
    empty=frame.isna().all(axis=1)
    stats['empty_placeholder_rows']=int(empty.sum())
    frame=frame.loc[~empty]
    # Keep PARTIAL observations for causal open-vs-close handling.
    frame = frame[~frame.index.duplicated(keep='first')].between_time('09:00','15:30')
    assert not ((frame.index >= upper) | (frame.index < lower)).any()
    return frame, stats


def fetch_window(symbol, start, end, folder):
    folder.mkdir(parents=True, exist_ok=True)
    request = {'symbol':symbol,'interval':'5m','start_inclusive':start,'end_exclusive':end,
               'source':'https://query1.finance.yahoo.com/v8/finance/chart/','retrieved_at':datetime.now(ZoneInfo('UTC')).isoformat()}
    params = {'period1':int(pd.Timestamp(start,tz='Asia/Tokyo').timestamp()),
              'period2':int(pd.Timestamp(end,tz='Asia/Tokyo').timestamp()),'interval':'5m'}
    if SAVED is not None:
        with zipfile.ZipFile(SAVED) as archive:
            evidence=json.loads(archive.read('acquisition_evidence.json'))
            saved=next(r for r in evidence if r['symbol']==symbol and
                       r['start_inclusive']==start and r['end_exclusive']==end)
            relative=folder.relative_to(OUT)/(symbol.replace('.','_')+'.json')
            if str(relative) not in archive.namelist():
                return pd.DataFrame(), dict(saved,replayed_from_snapshot=True)
            raw=archive.read(str(relative))
            if hashlib.sha256(raw).hexdigest()!=saved['raw_sha256']:
                raise ValueError('Candidate raw snapshot hash mismatch')
            (OUT/relative).write_bytes(raw)
            frame,stats=bounded_frame(json.loads(raw),start,end)
            return frame,dict(saved,**stats,rows=len(frame),
                              actual_start=str(frame.index.min()) if len(frame) else '',
                              actual_end=str(frame.index.max()) if len(frame) else '',
                              replayed_from_snapshot=True)
    try:
        response = requests.get(request['source']+symbol,params=params,impersonate='chrome',timeout=30)
        request['http_status'] = response.status_code
        body = response.json()
        raw = folder/(symbol.replace('.','_')+'.json')
        raw.write_text(json.dumps(body, ensure_ascii=False))
        request['raw_sha256'] = sha(raw)
        request['provider_error'] = body.get('chart',{}).get('error')
        frame, stats = bounded_frame(body,start,end)
        request.update(stats)
        request.update(rows=len(frame), actual_start=str(frame.index.min()) if len(frame) else '',
                       actual_end=str(frame.index.max()) if len(frame) else '')
        return frame, request
    except (requests.RequestsError, ValueError, KeyError) as exc:
        request.update(rows=0,error=str(exc))
        return pd.DataFrame(), request


def classify(row):
    if row.get('損益(円)') is None:
        return '未決済・未確定' if row.get('残存株数',0) else 'データ未取得'
    if int(row['取引数']) == 0:
        return '取引なし'
    return '黒字' if row['損益(円)'] > 1e-7 else '赤字' if row['損益(円)'] < -1e-7 else '損益ゼロ'


def evaluate(config, period, frames, items):
    rows, trades, events = [], [], []
    for item in items:
        code, name = item['コード'], item['候補名']
        frame = frames.get(code,pd.DataFrame())
        for strategy in config['strategies']:
            for slip in config['slippage_yen']:
                for c in config['costs']:
                    costs=baseline.Costs(**c)
                    for method, engine in [('従来・全日事後判定',baseline),('時系列・欠損時停止',causal_engine)]:
                        audit=[]
                        if frame.empty:
                            result={'コード':code,'候補名':name,'戦略':strategy,'片道スリッページ(円/株)':slip,
                                    '費用シナリオ':costs.name,'検証状態':'データ未取得','取引数':0,'損益(円)':None}
                            history=[]
                        else:
                            kwargs={'audit':audit} if method.startswith('時系列') else {}
                            engine_frame=frame
                            if method.startswith('従来'):
                                valid=np.isfinite(frame.astype(float)).all(axis=1) & (frame[['Open','High','Low','Close']]>0).all(axis=1)
                                engine_frame=frame.loc[valid]
                            if engine_frame.empty:
                                result={'コード':code,'候補名':name,'戦略':strategy,'片道スリッページ(円/株)':slip,
                                        '費用シナリオ':costs.name,'検証状態':'データ未取得','取引数':0,'損益(円)':None}
                                history=[]
                            else:
                                result,history=engine.simulate(engine_frame,strategy,slip,code,name,costs,**kwargs)
                        result.update(期間=period,方式=method,分類=classify(result))
                        rows.append(result)
                        for record in history:
                            record.update(期間=period,方式=method)
                            trades.append(record)
                        for event in audit:
                            event.update(期間=period,方式=method,コード=code,戦略=strategy,
                                         スリッページ=slip,費用シナリオ=costs.name)
                            events.append(event)
    return rows,trades,events


def aggregates(rows):
    grouped=defaultdict(list)
    for row in rows:
        grouped[(row['期間'],row['方式'],row['費用シナリオ'])].append(row)
    output=[]
    for (period,method,scenario),group in grouped.items():
        counts=Counter(row['分類'] for row in group)
        known=[r for r in group if r.get('損益(円)') is not None]
        output.append({'期間':period,'方式':method,'費用シナリオ':scenario,'条件数':len(group),
                       **{key:counts[key] for key in ['黒字','赤字','取引なし','損益ゼロ','未決済・未確定','データ未取得']},
                       '確定可能条件のみ損益合計(円)':sum(r['損益(円)'] for r in known),
                       '合計の制約':'独立条件の機械的合算。ポートフォリオ損益ではなく未確定条件を含まない。',
                       '参考・完了取引損益合計(円)':sum(float(r.get('完了取引損益(円)',r.get('損益(円)') or 0)) for r in group)})
    return output


def compare(rows):
    groups=defaultdict(dict)
    for row in rows:
        groups[(row['期間'],row['コード'],row['戦略'],row['片道スリッページ(円/株)'],row['費用シナリオ'])][row['方式']]=row
    comparisons=[]
    for (period,code,strategy,slip,cost),pair in groups.items():
        old,new=pair['従来・全日事後判定'],pair['時系列・欠損時停止']
        comparisons.append({'期間':period,'コード':code,'戦略':strategy,'スリッページ':slip,'費用シナリオ':cost,
                            '従来損益(円)':old.get('損益(円)'), '時系列損益(円)':new.get('損益(円)'),
                            '差額(円)':new['損益(円)']-old['損益(円)'] if new.get('損益(円)') is not None and old.get('損益(円)') is not None else None,
                            '従来取引数':old['取引数'],'時系列完了取引数':new['取引数'],
                            '時系列分類':new['分類'],'時系列未決済情報':new.get('未決済情報','')})
    return comparisons


def table(rows):
    if not rows:
        return '<p>データなし</p>'
    cols=list(dict.fromkeys(k for row in rows for k in row))
    def display(value):
        if value is None:
            return '未確定／未取得'
        if isinstance(value,float):
            return f'{value:,.2f}'
        return str(value)
    return '<div style="overflow:auto"><table><tr>'+''.join('<th>'+html.escape(c)+'</th>' for c in cols)+'</tr>'+''.join('<tr>'+''.join('<td>'+html.escape(display(r.get(c,'')))+'</td>' for c in cols)+'</tr>' for r in rows)+'</table></div>'


def main(refresh=False):
    global SAVED
    config=load_config()
    SAVED=None if refresh else FROZEN/'candidate_inputs.zip'
    OUT.mkdir(parents=True,exist_ok=True)
    original=OUT/'original_40209137371'
    with zipfile.ZipFile(FROZEN/'baseline_inputs.zip') as z:
        z.extractall(original)
    items=read_csv(FROZEN/'watchlist.csv')
    existing={}
    for item in items:
        file=original/'prices'/(item['コード'].replace('.','_')+'_5m.csv')
        if file.exists():
            frame=pd.read_csv(file,index_col=0)
            frame.index=pd.to_datetime(frame.index,utc=True).tz_convert('Asia/Tokyo')
            existing[item['コード']]=frame
    evidence=[]
    # Verify source range limits independently of profits; no paid or account-based sources.
    for start in ['2026-08-01','2026-08-27']:
        _, request=fetch_window('5243.T',start,'2026-09-08',OUT/'probes'/start)
        evidence.append(request)
    separate={}
    coverage=[]
    for item in items:
        code=item['コード']
        frame,request=fetch_window(code,config['candidate_start'],config['candidate_end_exclusive'],OUT/'raw_candidate')
        evidence.append(request)
        if not frame.empty:
            # Both date-window and actual timestamp overlap must be zero.
            assert not ((frame.index >= pd.Timestamp(config['baseline_start'],tz='Asia/Tokyo')) &
                        (frame.index < pd.Timestamp(config['baseline_end_exclusive'],tz='Asia/Tokyo'))).any()
            assert not set(frame.index).intersection(existing.get(code,pd.DataFrame()).index)
            separate[code]=frame
            folder=OUT/'candidate_prices';folder.mkdir(exist_ok=True)
            frame.to_csv(folder/(code.replace('.','_')+'_5m.csv'))
        days=[]
        for date,day in frame.groupby(frame.index.date) if not frame.empty else []:
            valid=np.isfinite(day.astype(float)).all(axis=1) & (day[['Open','High','Low','Close']]>0).all(axis=1)
            legacy_day=day.loc[valid]
            days.append({'コード':code,'日付':str(date),'本数':len(day),
                         '不完全行':int((~valid).sum()),'従来除外理由':baseline.quality_check(legacy_day)})
        coverage.append({'コード':code,'要求開始':config['candidate_start'],'要求終了・排他的':config['candidate_end_exclusive'],
                         '実取得開始':request.get('actual_start',''),'実取得終了':request.get('actual_end',''),
                         '取得本数':len(frame),'取得日数':len(days),
                         '従来品質合格日数':sum(not d['従来除外理由'] for d in days),
                         '期間重複本数':0,'http_status':request.get('http_status',''),
                         'エラー':str(request.get('provider_error') or request.get('error') or ''),
                         '範囲外除外本数':request.get('outside_window',0),'全項目空の非観測行':request.get('empty_placeholder_rows',0),'不完全OHLC行数':sum(d['不完全行'] for d in days)})
        if days:
            write_csv(OUT/(code.replace('.','_')+'_candidate_daily_quality.csv'),days)
        print(code,'candidate',len(frame),'rows',flush=True)
        if SAVED is None:
            time.sleep(1)
    all_rows,all_trades,all_events=[],[],[]
    for period,frames in [('既存期間・再現',existing),('別期間・重複なし',separate)]:
        print('Evaluating',period,flush=True)
        rows,trades,events=evaluate(config,period,frames,items)
        all_rows.extend(rows);all_trades.extend(trades);all_events.extend(events)
    # Check every original summary against the immutable original, not just note.
    prior=read_csv(original/'backtest_summary.csv')
    index={(r['コード'],r['戦略'],float(r['片道スリッページ(円/株)']),r['費用シナリオ']):r
           for r in all_rows if r['期間']=='既存期間・再現' and r['方式']=='従来・全日事後判定'}
    for row in prior:
        result=index[(row['コード'],row['戦略'],float(row['片道スリッページ(円/株)']),row['費用シナリオ'])]
        for key in ['損益(円)','取引数','日内最大DD(円)','期間最大DD(円)','日末残高系列最大DD(円)']:
            assert np.isclose(float(row[key]),result[key],atol=1e-6,rtol=0),(key,row['コード'])
    totals=aggregates(all_rows)
    comparison=compare(all_rows)
    write_csv(OUT/'fixed_summary.csv',all_rows)
    write_csv(OUT/'fixed_trades.csv',all_trades)
    write_csv(OUT/'causal_events.csv',all_events)
    excluded_day_trades=[]
    for period,frames in [('既存期間・再現',existing),('別期間・重複なし',separate)]:
        bad={(code,str(date)) for code,frame in frames.items()
             for date,day in frame.groupby(frame.index.date) if baseline.quality_check(day)}
        for record in all_trades:
            if record['期間']==period and record['方式']=='時系列・欠損時停止' and (record['コード'],str(record['買い日時'].date())) in bad:
                excluded_day_trades.append(record)
    write_csv(OUT/'completed_trades_on_retrospectively_excluded_days.csv',excluded_day_trades)
    write_csv(OUT/'method_comparison.csv',comparison)
    write_csv(OUT/'aggregate.csv',totals)
    write_csv(OUT/'candidate_coverage.csv',coverage)
    (OUT/'acquisition_evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
    manifest={'configuration':config,'current_code_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
              'python':platform.python_version(),'acquisition_mode':'保存済み生応答の再現' if SAVED else '公開API再取得','source_files':{str(p.relative_to(ROOT)):sha(p) for p in [Path(__file__),ROOT/'research/causal_engine.py']},
              'baseline_rows_reproduced':len(prior),'overlapping_bars':0,
              'input_hashes':{str(f.relative_to(OUT)):sha(f) for f in sorted(OUT.rglob('*')) if f.is_file() and f.suffix in ('.csv','.json') and f.name not in ('manifest.json',)},
              'limitations':['最大7営業日で最低8日に満たない参考検証','noteには既存結果による選択バイアス',
                             '保存入力は全て元の期間。別期間は独立取得し厳密な範囲フィルターを適用',
                             '未決済は損益未確定。欠損足や欠損区間で架空の売買を作成しない',
                             '時系列方式は09:00初足、5分間隔（昼休み控除）、15:25事前決済を仮定。実際の約定可能性は未検証']}
    (OUT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    info='''<p>戦略・閾値・銘柄リスト・資金100万円・上限30万円・100株単位・費用シナリオは固定。費用込みは片道100円、0bps、仮定年利3%、同日も最低1日。証券会社の実料金ではありません。</p>
<p>既存期間2026-09-08〜10-23。別期間2026-08-28〜09-07、重複ゼロ。ただし最大7営業日で最低8日に届かない参考値。既存期間の分割を未使用データとは呼びません。noteは既存の成績を見て注目されたため選択バイアスがあります。</p>
<p>従来方式は全日の欠損を事後判定して日全体を除外します。時系列方式は09:00の初足がなければ当日停止し、その後は昼休み以外の5分超の間隔を観測した時点で新規注文停止・未約定買いを取消。既に終わった取引は消しません。保有中に欠損や15:25決済足不足があれば、勝手な価格で決済せず総損益を未確定とし、その条件の後続取引も停止します。</p>
<p>損益差には全日除外の撤廃以外に初足必須・5分超欠損停止・15:25事前決済の変更も含むため、差の全てを事後判定だけの効果と断定しません。時系列方式の利用日数は一部でも処理できた日で、従来方式の全日品質合格日数とは異なります。時系列方式のDDは実際に観測できた足の始値・終値による推定清算価値のみ。未決済条件は観測範囲の部分DDです。取引なしと、未決済・未取得は別分類。黒字・赤字件数には短期間・少数日数の参考値を含みます。独立条件を機械的に合算した損益はポートフォリオ損益ではありません。</p>'''
    (OUT/'report.html').write_text('<!doctype html><html lang="ja"><meta charset="utf-8"><title>固定条件・別期間と品質方式の比較</title><style>body{font-family:system-ui;margin:24px}table{border-collapse:collapse;font-size:12px}th,td{padding:6px;border:1px solid #ccc;white-space:nowrap}</style><h1>固定条件・別期間と品質方式の比較</h1>'+info+'<h2>集計</h2>'+table(totals)+'<h2>別期間の取得と品質</h2>'+table(coverage)+'<h2>全銘柄・全条件</h2>'+table(all_rows)+'<h2>新旧比較（未確定条件の差額は算出しない）</h2>'+table(comparison)+'</html>')
    print(json.dumps(totals,ensure_ascii=False,indent=2),flush=True)
    print('PASS: 204 original rows reproduced; no overlap; all 20 symbols retained',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh-market-data',action='store_true',help='同じ固定期間をAPIで再取得。既定は取得証跡付き保存応答を再現')
    main(refresh=parser.parse_args().refresh_market_data)
