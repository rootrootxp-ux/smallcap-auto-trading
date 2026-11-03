"""Retrospective ranking sensitivity on saved data; no price requests or orders."""
import csv
import hashlib
import html
import io
import json
import math
from pathlib import Path
import zipfile

import pandas as pd

from universe_analysis import daily_metrics

ROOT = Path(__file__).resolve().parent


def _read(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def _yes(value):
    return value is True or str(value).lower() in ('true', '1')


def _eligible(row, metrics):
    cap = row.get('market_cap_yen')
    try:
        cap = float(cap)
    except (TypeError, ValueError):
        return False
    return (_yes(row.get('listed')) and row.get('currency') == 'JPY' and
            math.isfinite(cap) and 5e9 <= cap <= 1e12 and metrics['qualified'])


def compute_rank_rows(pool, daily_frames, current_codes, cutoff, size=None):
    """Re-rank a fixed present-day pool using only daily dates before cutoff.

    Current listing/cap remain fixed deliberately, so this cannot reconstruct
    a historical investable universe or remove survivorship/size look-ahead.
    No performance or intraday quality result enters either ranking.
    """
    cutoff_date = pd.Timestamp(cutoff)
    if pd.isna(cutoff_date):
        raise ValueError('A finite ranking cutoff is required')
    current_codes = set(current_codes)
    size = len(current_codes) if size is None else size
    if not isinstance(size, int) or size < 1:
        raise ValueError('Ranking size must be positive')
    codes = [row['コード'] for row in pool]
    if len(codes) != len(set(codes)) or not current_codes.issubset(codes):
        raise ValueError('Pool has duplicate codes or omits current members')
    records = []
    for item in pool:
        code = item['コード']
        frame = daily_frames.get(code, pd.DataFrame())
        current = daily_metrics(frame)
        past = daily_metrics(frame, before=cutoff_date)
        records.append({'コード':code, '候補名':item.get('候補名',''),
            '先行cutoff排他的':str(cutoff_date.date()), '存続情報固定':item.get('listed',''),
            '存続情報時点':item.get('listing_asof',''), '固定時価総額(円)':item.get('market_cap_yen',''),
            '時価総額観測日時':item.get('market_cap_observed_at',''),
            '現在指標日数':current['count'], '現在指標開始':current['window_start'], '現在指標終了':current['window_end'],
            '現在平均売買代金(円)':current['avg_value_yen'], '現在平均日中値幅(%)':current['avg_range_pct'],
            '現在適格':_eligible(item,current), '現在順位':None,
            '先行指標日数':past['count'], '先行指標開始':past['window_start'], '先行指標終了':past['window_end'],
            '先行平均売買代金(円)':past['avg_value_yen'], '先行平均日中値幅(%)':past['avg_range_pct'],
            '先行適格':_eligible(item,past), '先行順位':None,
            '現行watchlist採用':code in current_codes,
            '現在規則の上位採用':False, '先行規則の上位採用':False})
    current_rank = sorted((row for row in records if row['現在適格']),
                          key=lambda row:(-row['現在平均売買代金(円)'], row['コード']))
    past_rank = sorted((row for row in records if row['先行適格']),
                       key=lambda row:(-row['先行平均売買代金(円)'], row['コード']))
    for rank,row in enumerate(current_rank,1):
        row['現在順位'] = rank
        row['現在規則の上位採用'] = rank <= size
    for rank,row in enumerate(past_rank,1):
        row['先行順位'] = rank
        row['先行規則の上位採用'] = rank <= size
    for row in records:
        now, past = row['現行watchlist採用'], row['先行規則の上位採用']
        row['区分'] = 'common' if now and past else 'current_only' if now else 'past_only' if past else 'neither'
        row['先行側だけの銘柄の成績'] = '未検証（5分足成績の追加比較なし）' if row['区分']=='past_only' else 'この順位監査では損益を使用しない'
        row['注意'] = '現在の手動候補母集団・存続情報・時価総額を固定した事後的感度点検。歴史的母集団ではなく、未参照期間の検証ではない。'
    return records


def _check_hash(path, expected):
    if not expected or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError(f'Ranking input hash mismatch: {path.name}')


def write_rank_sensitivity(out, run_id, cutoff):
    out = Path(out)
    out.mkdir(parents=True,exist_ok=True)
    data = ROOT/'data/universe50'
    policy = json.loads((data/'selection_policy.json').read_text())
    expected_policy = {'min_market_cap_yen':5e9, 'max_market_cap_yen':1e12, 'daily_window':20,
                       'min_avg_value_yen':1e8, 'min_avg_range_pct':2.0}
    if any(policy.get(key) != value for key,value in expected_policy.items()):
        raise ValueError('Ranking policy differs from fixed thresholds')
    for name,key in [('candidate_pool.csv','pool_sha256'), ('selection_inputs.zip','selection_inputs_sha256')]:
        _check_hash(data/name,policy.get(key))
    _check_hash(ROOT/'watchlist.csv',policy.get('selected_sha256'))
    _check_hash(data/'jpx_listing_20260930.xlsx',policy.get('listing_sha256'))
    requested = _read(data/'candidate_pool.csv')
    pool = _read(data/'pool_audit.csv')
    watchlist = _read(ROOT/'watchlist.csv')
    if len(pool)!=policy['pool_size'] or {row['コード'] for row in requested}!={row['コード'] for row in pool}:
        raise ValueError('Ranking pool coverage mismatch')
    if len(watchlist)!=policy['size']:
        raise ValueError('Ranking watchlist size mismatch')
    frames = {}
    with zipfile.ZipFile(data/'selection_inputs.zip') as archive:
        for row in pool:
            stem = row['コード'].replace('.','_')
            csv_name, json_name = stem+'.csv', stem+'.json'
            if not row.get('daily_sha256'):
                if row.get('selection_error') or not _yes(row.get('listed')):
                    continue
                raise ValueError(f'Missing saved daily provenance: {row["コード"]}')
            raw = archive.read(csv_name)
            observed = json.loads(archive.read(json_name))
            digest = hashlib.sha256(raw).hexdigest()
            if digest != row['daily_sha256'] or digest != observed['daily_sha256']:
                raise ValueError(f'Daily hash mismatch: {row["コード"]}')
            if not math.isclose(float(row['market_cap_yen']),float(observed['market_cap_yen']),rel_tol=1e-12,abs_tol=0) or row['currency']!=observed['currency']:
                raise ValueError(f'Fixed market metadata mismatch: {row["コード"]}')
            frame = pd.read_csv(io.BytesIO(raw),index_col=0)
            frame.index = pd.to_datetime(frame.index,utc=True).tz_convert('Asia/Tokyo')
            frames[row['コード']] = frame
    rows = compute_rank_rows(pool,frames,{row['コード'] for row in watchlist},cutoff,size=policy['size'])
    calculated = [row['コード'] for row in sorted((r for r in rows if r['現在規則の上位採用']),key=lambda row:row['現在順位'])]
    if calculated != [row['コード'] for row in watchlist]:
        raise ValueError('Recomputed current rank does not match frozen watchlist')
    if sum(row['現在適格'] for row in rows)!=policy['qualified_size']:
        raise ValueError('Current eligibility count does not match selection policy')
    for row in rows:
        row['実行ID'] = run_id
        row['現在順位再現検査'] = '一致'
        row['入力ハッシュ検査'] = '一致'
    with (out/'selection_rank_audit.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer = csv.DictWriter(f,fieldnames=list(rows[0]))
        writer.writeheader();writer.writerows(rows)
    counts = {name:sum(row['区分']==name for row in rows) for name in ('common','current_only','past_only','neither')}
    past_eligible = sum(row['先行適格'] for row in rows)
    escaped_cutoff = html.escape(str(pd.Timestamp(cutoff).date()))
    return ('<section><h2>全候補プールの順位感度点検</h2><p>保存した全'+str(len(rows))+
            '候補を、'+escaped_cutoff+'より前の日足20日だけで同じ売買代金順位・閾値に再適用しました。現在の順位とwatchlistは再現一致、入力ハッシュも一致。</p>'+
            '<p>先行適格'+str(past_eligible)+'銘柄。現在上位'+str(policy['size'])+'との共通'+str(counts['common'])+
            '銘柄、現在側のみ'+str(counts['current_only'])+'銘柄、先行側のみ'+str(counts['past_only'])+
            '銘柄。全候補の順位・指標・適格性・区分は selection_rank_audit.csv に保存しています。</p>'+
            '<p>現在の手動母集団・存続情報・現在時価総額を固定した事後的感度点検です。歴史的な投資可能母集団の再構成ではなく、未参照データの評価や選択バイアス解消とは呼びません。'
            '先行側のみの銘柄の5分足成績は未検証で、入替後の利益を推定しません。</p></section>')
