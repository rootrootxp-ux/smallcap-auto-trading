"""Explicit research-universe selection; never called by backtest Actions."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pandas as pd
import yfinance as yf
from universe_analysis import daily_metrics

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data/universe50'
POLICY={'min_market_cap_yen':5e9,'max_market_cap_yen':1e12,'daily_window':20,
        'min_avg_value_yen':1e8,'min_avg_range_pct':2.,'size':50,
        'ranking':'avg_value_yen descending, ticker ascending; no PnL/intraday quality selection',
        'pool':'Manually specified broad research pool, not all Japanese equities; selection/survivorship bias remains'}


def eligible(row):
    return (row.get('listed') is True and row.get('currency')=='JPY' and
            POLICY['min_market_cap_yen']<=row.get('market_cap_yen',0)<=POLICY['max_market_cap_yen'] and
            row.get('count',0)>=POLICY['daily_window'] and
            row.get('avg_value_yen',0)>=POLICY['min_avg_value_yen'] and
            row.get('avg_range_pct',0)>=POLICY['min_avg_range_pct'])


def select(rows):
    good=[r for r in rows if eligible(r)]
    good.sort(key=lambda r:(-r['avg_value_yen'],r['コード']))
    if len(good)<POLICY['size']:
        raise ValueError(f'Only {len(good)} eligible; do not relax thresholds to force 50')
    return good[:POLICY['size']]


def write_csv(path,rows):
    columns=list(dict.fromkeys(k for row in rows for k in row))
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=columns);w.writeheader();w.writerows(rows)


def read_listing(path):
    if path.suffix != '.xlsx':
        return json.loads(path.read_text())
    ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(path) as archive:
        shared=ET.fromstring(archive.read('xl/sharedStrings.xml'))
        strings=[''.join(node.itertext()) for node in shared.findall('s:si',ns)]
        sheet=ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        rows=[]
        for row in sheet.findall('.//s:sheetData/s:row',ns):
            cells=[]
            for cell in row.findall('s:c',ns):
                value=cell.find('s:v',ns)
                raw=value.text if value is not None else ''
                cells.append(strings[int(raw)] if cell.get('t')=='s' else raw)
            rows.append(cells)
        return rows


def main(pool_path,listing_path,output,refresh=False):
    # This standalone acquisition is parallel but uses no trading or broker interface.
    yf.config.debug.hide_exceptions=False
    DATA.mkdir(parents=True,exist_ok=True)
    cache=DATA/'daily_snapshot';cache.mkdir(exist_ok=True)
    listing=read_listing(listing_path)
    fields=listing[0]
    listed={r[1]+'.T':dict(zip(fields,r)) for r in listing[1:] if r[3] in
            ['プライム（内国株式）','スタンダード（内国株式）','グロース（内国株式）']}
    delists=json.loads((DATA/'delisted_asof_20261103.json').read_text())
    excluded={r['code']+'.T' for r in delists['rows']}|{'4449.T','5585.T','5595.T'}
    with pool_path.open(encoding='utf-8-sig') as f:
        pool=list(csv.DictReader(f))
    with (ROOT/'data/legacy_watchlist20.csv').open(encoding='utf-8-sig') as f:
        legacy={r['コード'] for r in csv.DictReader(f)}
    selected_at=datetime.now(ZoneInfo('Asia/Tokyo')).isoformat()
    def acquire(item):
        code=item['コード'];meta=listed.get(code,{})
        base={'コード':code,'候補名':meta.get('銘柄名',item.get('候補名','')),
              'sector':meta.get('33業種区分',''),'listed':code in listed and code not in excluded,
              'listing_asof':meta.get('日付',''),'listing_source':'https://www.jpx.co.jp/markets/statistics-equities/misc/01.html',
              'selected_at':selected_at,'legacy20_member':code in legacy}
        if not base['listed']:
            return dict(base,selection_reason='公式国内株一覧にない、または廃止済み。選定対象外')
        file=cache/(code.replace('.','_')+'.json')
        price=cache/(code.replace('.','_')+'.csv')
        try:
            if not refresh and file.exists() and price.exists():
                observed=json.loads(file.read_text())
                if hashlib.sha256(price.read_bytes()).hexdigest()!=observed['daily_sha256']:
                    raise ValueError('Selection cache input hash mismatch')
                frame=pd.read_csv(price,index_col=0)
                frame.index=pd.to_datetime(frame.index,utc=True).tz_convert('Asia/Tokyo')
            else:
                ticker=yf.Ticker(code)
                frame=ticker.history(period='3mo',interval='1d',auto_adjust=False,actions=False)
                info=ticker.fast_info
                cap=float(info['market_cap']);currency=info['currency']
                if not math.isfinite(cap):raise ValueError('Nonfinite market cap')
                observed={'market_cap_yen':cap,'currency':currency,'market_cap_observed_at':datetime.now(ZoneInfo('Asia/Tokyo')).isoformat(),
                          'liquidity_source':'Yahoo Finance 1d/3mo, unadjusted OHLC; market cap from yfinance.fast_info'}
                frame.to_csv(price)
                observed['daily_sha256']=hashlib.sha256(price.read_bytes()).hexdigest()
                file.write_text(json.dumps(observed,ensure_ascii=False,indent=2))
                time.sleep(.3)
            metrics=daily_metrics(frame)
            base.update(observed,**metrics,selection_info_end=metrics['window_end'])
            base['selection_reason']='時価総額50億〜1兆円・20日平均売買代金1億円以上・日中値幅2%以上。平均売買代金順位。成績は選定に不使用' if eligible(base) else '事前固定した時価総額/日足本数/流動性/値幅条件のいずれか未達'
        except Exception as exc:
            base.update(selection_reason='選定データ取得失敗',selection_error=f'{type(exc).__name__}: {exc}')
        print(code,base.get('market_cap_yen',''),base.get('avg_value_yen',''),base.get('avg_range_pct',''),flush=True)
        return base
    with ThreadPoolExecutor(max_workers=3) as executor:
        rows=list(executor.map(acquire,pool))
    write_csv(DATA/'pool_audit.csv',rows)
    chosen=select(rows)
    for rank,row in enumerate(chosen,1):row['pool_rank']=rank
    write_csv(output,chosen)
    with zipfile.ZipFile(DATA/'selection_inputs.zip','w',compression=zipfile.ZIP_DEFLATED) as archive:
        for source in sorted(cache.iterdir()):
            archive.write(source,source.name)
    policy=dict(POLICY,selected_at=selected_at,pool_size=len(rows),qualified_size=sum(eligible(r) for r in rows),
                pool_sha256=hashlib.sha256(pool_path.read_bytes()).hexdigest(),
                listing_sha256=hashlib.sha256((DATA/'jpx_listing_20260930.xlsx').read_bytes()).hexdigest(),
                selected_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
                selection_inputs_sha256=hashlib.sha256((DATA/'selection_inputs.zip').read_bytes()).hexdigest(),
                manual_pool_bias='手動プールと現在の存続・時価総額による選択・生存者バイアスが残る。')
    (DATA/'selection_policy.json').write_text(json.dumps(policy,ensure_ascii=False,indent=2))
    print('Selected',len(chosen),'from',len(rows),'qualified',sum(eligible(r) for r in rows),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pool',type=Path,default=DATA/'candidate_pool.csv')
    parser.add_argument('--listing',type=Path,default=DATA/'jpx_listing_20260930.xlsx')
    parser.add_argument('--refresh',action='store_true',help='保存キャッシュを使わず公開データを再取得（再選定前にJPX資料の時点も更新すること）')
    parser.add_argument('--output',type=Path,default=ROOT/'watchlist.csv')
    args=parser.parse_args();main(args.pool,args.listing,args.output,args.refresh)
