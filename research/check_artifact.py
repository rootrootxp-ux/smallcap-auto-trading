"""Validate saved artifacts without making requests or assuming fills in data gaps."""
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path
import sys

import numpy as np
import pandas as pd


def read(path):
    with path.open(encoding='utf-8-sig',newline='') as f:
        return list(csv.DictReader(f))


def validate(root):
    manifest=json.loads((root/'manifest.json').read_text())
    for name,expected in manifest['input_hashes'].items():
        actual=hashlib.sha256((root/name).read_bytes()).hexdigest()
        assert actual==expected, name
    assert manifest['baseline_rows_reproduced']==204
    assert manifest['overlapping_bars']==0
    rows=read(root/'fixed_summary.csv')
    trades=read(root/'fixed_trades.csv')
    assert len(rows)==960
    assert len({(r['期間'],r['方式'],r['コード'],r['戦略'],r['片道スリッページ(円/株)'],r['費用シナリオ']) for r in rows})==960
    config=manifest['configuration']
    frames={}
    for period,folder in [('既存期間・再現',root/'original_40209137371/prices'),('別期間・重複なし',root/'candidate_prices')]:
        for file in folder.glob('*.csv'):
            frame=pd.read_csv(file,index_col=0)
            frame.index=pd.to_datetime(frame.index,utc=True).tz_convert('Asia/Tokyo')
            code=file.stem.removesuffix('_5m').replace('_','.')
            frames[(period,code)]=frame
            if period.startswith('別期間'):
                assert (frame.index < pd.Timestamp(config['candidate_end_exclusive'],tz='Asia/Tokyo')).all()
                assert (frame.index >= pd.Timestamp(config['candidate_start'],tz='Asia/Tokyo')).all()
                prior=frames.get(('既存期間・再現',code),pd.DataFrame())
                assert not set(frame.index).intersection(prior.index)
    grouped=defaultdict(list)
    for t in trades:
        key=tuple(t[k] for k in ['期間','方式','コード','戦略','仮定片道スリッページ(円/株)','費用シナリオ'])
        grouped[key].append(t)
        qty=float(t['数量(株)']);slip=float(t['仮定片道スリッページ(円/株)'])
        buy,sell=pd.Timestamp(t['買い日時']),pd.Timestamp(t['売り日時'])
        assert buy>pd.Timestamp(t['買いシグナル足']) and sell>buy
        assert qty>=100 and qty%100==0 and qty*float(t['買値(円)'])<=300000
        frame=frames[(t['期間'],t['コード'])]
        assert np.isclose(float(t['買値(円)']),float(frame.loc[buy,'Open'])+slip)
        assert np.isclose(float(t['売値(円)']),max(.01,float(frame.loc[sell,'Open'])-slip))
        assert np.isclose(float(t['損益(円)']),float(t['費用控除前損益(円)'])-float(t['手数料(円)'])-float(t['信用金利(円)']),atol=1e-6,rtol=0)
    for r in rows:
        key=tuple(r[k] for k in ['期間','方式','コード','戦略','片道スリッページ(円/株)','費用シナリオ'])
        history=grouped[key]
        assert len(history)==int(r['取引数'])
        if r['損益(円)']:
            assert np.isclose(float(r['損益(円)']),sum(float(t['損益(円)']) for t in history),atol=1e-6,rtol=0)
        elif r['分類']=='未決済・未確定':
            assert int(r['残存株数'])>0
        if r.get('期間最大DD(円)'):
            assert float(r['期間最大DD(円)'])+1e-6>=float(r['日内最大DD(円)'])>=0
    for total in read(root/'aggregate.csv'):
        selected=[r for r in rows if all(r[k]==total[k] for k in ['期間','方式','費用シナリオ'])]
        assert len(selected)==120
        for classification in ['黒字','赤字','取引なし','損益ゼロ','未決済・未確定','データ未取得']:
            assert sum(r['分類']==classification for r in selected)==int(total[classification])
        assert np.isclose(sum(float(r['損益(円)']) for r in selected if r['損益(円)']),float(total['確定可能条件のみ損益合計(円)']),atol=1e-6,rtol=0)
    report=(root/'report.html').read_text()
    assert '選択バイアス' in report and '未確定' in report and '最大7営業日' in report
    print(f'PASS: hashes, 960 rows, {len(trades)} trades, costs/limits/DD, aggregate counts, and zero overlap')


if __name__=='__main__':
    validate(Path(sys.argv[1]) if len(sys.argv)>1 else Path('results/fixed_validation'))
