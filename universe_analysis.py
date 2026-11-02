"""Universe provenance and descriptive comparisons; never a shared brokerage account."""
import csv
from collections import defaultdict
from pathlib import Path
from statistics import mean, median

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent / 'data'
LEGACY20 = ('5137.T','4375.T','4419.T','5253.T','5243.T','4477.T','4475.T','4449.T',
            '4176.T','4393.T','4493.T','5574.T','5585.T','5595.T','5032.T','4051.T',
            '4385.T','3994.T','4165.T','4194.T')
ACCOUNT_CAVEAT = '各銘柄に100万円を独立配分。全銘柄共通の100万円口座・ポートフォリオを再現しない。合計損益や単一銘柄DDを口座成績と解釈しない。'
BIAS_CAVEAT = '現在選んだ母集団・存続情報・現在時価総額を使う事後的研究。先行日足だけの再判定も歴史的な母集団や時価総額を再構成せず、実際に未参照だったデータの評価ではない。'


def daily_metrics(frame, before=None, window=20):
    """Use at most the last `window` daily observations strictly before a date.

    A bad observation inside the last window reduces the count; it is neither
    filled nor replaced with a farther-back day. Qualification always needs
    at least 20 valid daily rows, 100m JPY turnover, and 2% mean daily range.
    """
    if not isinstance(window, int) or window < 1:
        raise ValueError('window must be a positive integer')
    empty = {'count':0, 'avg_value_yen':None, 'avg_range_pct':None,
             'qualified':False, 'window_start':'', 'window_end':''}
    if frame is None or frame.empty:
        return empty
    fields = ['Close','High','Low','Volume']
    if not set(fields).issubset(frame.columns):
        raise ValueError('daily metrics require Close, High, Low, Volume')
    selected = frame.sort_index().copy()
    selected = selected[~selected.index.duplicated(keep='first')]
    index = pd.DatetimeIndex(selected.index)
    if before is not None and str(before):
        cutoff = pd.Timestamp(before)
        if pd.isna(cutoff):
            raise ValueError('invalid selection cutoff')
        selected = selected.loc[index.date < cutoff.date()]
    selected = selected.tail(window)
    if selected.empty:
        return empty
    numbers = selected[fields].astype(float)
    valid = np.isfinite(numbers).all(axis=1) & (numbers.Close > 0) & (numbers.Volume >= 0) & (numbers.High >= numbers.Low)
    selected = numbers.loc[valid]
    if selected.empty:
        return empty
    count = len(selected)
    value = float((selected.Close * selected.Volume).mean())
    spread = float((100 * (selected.High-selected.Low) / selected.Close).mean())
    return {'count':count, 'avg_value_yen':value, 'avg_range_pct':spread,
            'qualified':bool(count >= 20 and value >= 100_000_000 and spread >= 2.0),
            'window_start':str(pd.Timestamp(selected.index.min()).date()),
            'window_end':str(pd.Timestamp(selected.index.max()).date())}


def _read(path):
    if not path.exists():
        return []
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def _write(path, rows):
    columns = list(dict.fromkeys(key for row in rows for key in row)) or ['注意']
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _number(value):
    if value is None or value == '':
        return None
    number = float(value)
    if not np.isfinite(number):
        raise ValueError('nonfinite comparison metric')
    return number


def _timestamp(value):
    if value is None or value == '':
        return None
    result = pd.Timestamp(value)
    if pd.isna(result):
        return None
    return result.tz_localize('Asia/Tokyo') if result.tzinfo is None else result.tz_convert('Asia/Tokyo')


def _same_time(first, second):
    left, right = _timestamp(first), _timestamp(second)
    return left is not None and right is not None and left == right


def _span(rows, start='取得開始', end='取得終了'):
    starts = [_timestamp(row.get(start)) for row in rows if row.get(start)]
    ends = [_timestamp(row.get(end)) for row in rows if row.get(end)]
    return (str(min(starts)) if starts else '', str(max(ends)) if ends else '')


def _key(row):
    return (row['コード'], row['戦略'], float(row['片道スリッページ(円/株)']), row['費用シナリオ'])


def _index(rows):
    result = {}
    for row in rows:
        key = _key(row)
        if key in result:
            raise ValueError(f'duplicate cohort condition: {key}')
        result[key] = row
    return result


def _eligible(row, statuses):
    status = statuses.get(row['コード'], {}).get('状態')
    return row.get('検証状態') == '検証済み' and (status is None or status in ('検証可能', '成功'))


def _cohort_rows(label, requested, rows, statuses, run_id, current_codes, note):
    selected = [row for row in rows if row['コード'] in requested]
    grouped = defaultdict(list)
    for row in selected:
        grouped[(row['戦略'], float(row['片道スリッページ(円/株)']), row['費用シナリオ'])].append(row)
    conditions = [(strategy, slip, cost) for strategy in ('MAのみ', 'VWAP+出来高', '高値ブレイク+出来高')
                  for slip in (1.0, 2.0) for cost in ('費用ゼロ', '仮定費用込み')]
    output = []
    for strategy, slip, cost in conditions:
        group = grouped[(strategy, slip, cost)]
        valid = [row for row in group if _eligible(row, statuses)]
        reference = [row for row in group if not _eligible(row, statuses)]
        pnls = [_number(row.get('損益(円)')) for row in valid]
        if any(value is None for value in pnls):
            raise ValueError('qualified cohort row lacks PnL')
        dd = [_number(row.get('期間最大DD(円)')) for row in valid]
        dd = [value for value in dd if value is not None]
        start, end = _span(group)
        evaluation_start, evaluation_end = _span(group, '開始', '終了')
        states = [statuses.get(code, {}).get('状態') for code in requested]
        absent = requested - {row['コード'] for row in group}
        zero_trade = sum(int(row.get('取引数', 0)) == 0 for row in valid)
        output.append({'record_type':'cohort_summary', '実行ID':run_id, '比較群':label,
            '戦略':strategy, '片道スリッページ(円/株)':slip, '費用シナリオ':cost,
            '対象銘柄数':len(requested), '実計算行数':len(group), '検証済み行数':len(valid),
            '参考値行数':len(reference), '結果行なし銘柄数':len(absent),
            '取得失敗銘柄数':states.count('取得失敗'), '重大不具合銘柄数':states.count('重大不具合'),
            'データ不足銘柄数':sum(state in ('データ不足','利用日数不足') for state in states),
            '今回対象外数':len(requested-current_codes),
            '黒字銘柄数':sum(value > 0 for value in pnls),
            '赤字銘柄数':sum(value < 0 for value in pnls), '取引なし銘柄数':zero_trade,
            '取引あり損益ゼロ銘柄数':sum(_number(row.get('損益(円)')) == 0 and int(row.get('取引数',0)) > 0 for row in valid),
            '検証済み平均損益(円)':mean(pnls) if pnls else None,
            '検証済み中央値損益(円)':median(pnls) if pnls else None,
            '検証済み平均収益率(%)':mean(pnls)/1_000_000*100 if pnls else None,
            '検証済み独立条件損益合算(円)':sum(pnls) if pnls else None,
            '検証済み取引数':sum(int(row.get('取引数',0)) for row in valid),
            '単一銘柄期間DD最大(円)':max(dd) if dd else None,
            '取得開始':start, '取得終了':end, '品質後検証開始':evaluation_start, '品質後検証終了':evaluation_end,
            '比較制約':note, '資金配分の注意':ACCOUNT_CAVEAT})
    return output


def write_analysis(out, run_id, watchlist_items, daily_frames, status_rows, summary_rows):
    """Write provenance, hindsight-selection diagnostics, and descriptive cohorts.

    The optional baseline CSVs contain the actual prior run. Every paired row
    retains its own period flags; equal envelopes do not prove identical prices.
    No past/current comparison is claimed to be an unseen-data experiment.
    """
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    status = {row['コード']:row for row in status_rows}
    current_codes = {item['コード'] for item in watchlist_items}
    legacy_items = _read(DATA_DIR/'legacy_watchlist20.csv')
    legacy_codes = {row['コード'] for row in legacy_items} if legacy_items else set(LEGACY20)
    baseline_rows = _read(DATA_DIR/'baseline20_summary.csv')
    baseline_status = {row['コード']:row for row in _read(DATA_DIR/'baseline20_status.csv')}
    common = current_codes & legacy_codes
    audit, bias, lagged_eligible = [], [], set()
    for item in watchlist_items:
        code = item['コード']
        record = status.get(code, {})
        frame = daily_frames.get(code, pd.DataFrame())
        present = daily_metrics(frame)
        acquisition_start = record.get('取得開始', '')
        acquisition_end = record.get('取得終了', '')
        prior = daily_metrics(frame, before=acquisition_start) if acquisition_start else daily_metrics(pd.DataFrame())
        if prior['qualified']:
            lagged_eligible.add(code)
        audit_row = dict(item)
        audit_row.update({'実行ID':run_id, '今回選定':True, '旧20銘柄メンバー':code in legacy_codes,
            '取得状態':record.get('状態','未記録'), '日足取得本数':record.get('日足本数',0),
            '5分足取得本数':record.get('5分足本数',0), '取得開始':acquisition_start, '取得終了':acquisition_end,
            '品質後検証開始':record.get('検証開始',''), '品質後検証終了':record.get('検証終了',''),
            '取得日数':record.get('取得日数',''), '品質合格日数':record.get('利用日数',''),
            '品質除外日数':record.get('除外日数',''), '品質除外理由':record.get('除外理由',''),
            '日足参照欠落日数':record.get('日足参照欠落日数',''), '取得段階':record.get('取得段階',''),
            '問題種別':record.get('問題種別',''), '取得詳細':record.get('詳細',''),
            '現在日足有効本数':present['count'], '現在平均売買代金(円)':present['avg_value_yen'],
            '現在平均日中値幅(%)':present['avg_range_pct'], '現在流動性条件適合':present['qualified'],
            '現在指標開始':present['window_start'], '現在指標終了':present['window_end']})
        audit.append(audit_row)
        info_end = item.get('selection_info_end') or item.get('window_end') or present['window_end']
        selected_at = item.get('selected_at','')
        start_time = _timestamp(acquisition_start)
        end_time = _timestamp(acquisition_end)
        information_time = _timestamp(info_end)
        selection_time = _timestamp(selected_at)
        future_daily = information_time.date() >= start_time.date() if information_time is not None and start_time is not None else None
        decision_after_start = selection_time >= start_time if selection_time is not None and start_time is not None else None
        if selection_time is None:
            forward = '未検証（選定日時の記録なし）'
        elif end_time is None or end_time <= selection_time:
            forward = '未検証（選定後の取得データなし）'
        else:
            forward = '未検証（選定後データを含むが独立した先行固定評価は未実施）'
        bias.append({'実行ID':run_id, 'コード':code, '候補名':item.get('候補名',''),
            'selected_at':selected_at, 'selection_info_end':info_end,
            'listing_asof':item.get('listing_asof',''), 'market_cap_observed_at':item.get('market_cap_observed_at',''),
            '検証の実取得開始':acquisition_start, '検証の実取得終了':acquisition_end,
            '選定時点が検証開始以降':decision_after_start,
            '選定日足に検証開始日以降を含む':future_daily,
            '先行日足の使用条件':'日足日付 < 5分足実取得開始日。直近20日。平均代金1億円以上かつ平均日中値幅2%以上。',
            '先行日足本数':prior['count'], '先行指標開始':prior['window_start'], '先行指標終了':prior['window_end'],
            '先行平均売買代金(円)':prior['avg_value_yen'], '先行平均日中値幅(%)':prior['avg_range_pct'],
            '先行日足だけの流動性適合':prior['qualified'], '現在日足の流動性適合':present['qualified'],
            '流動性採否差':present['qualified'] != prior['qualified'],
            '先行指標判定':('算出可' if prior['count'] >= 20 else '20日未満・適合未確認'),
            '選定後の未参照期間評価':forward, '監査制約':BIAS_CAVEAT})
    _write(out/'universe_audit.csv',audit)
    _write(out/'bias_audit.csv',bias)
    common_note = ('旧20と新50の共通銘柄。群の成績は各行に記載した実取得・品質後範囲による。'
                   '期間一致だけでは同一価格・同一品質対象日を保証しない。')
    groups = [
        ('CURRENT_UNIVERSE_CURRENT_RUN',current_codes,summary_rows,status,'今回の対象銘柄。現在情報を使った事後的研究であり、将来の未参照期間は未検証。'),
        ('LEGACY20_ON_CURRENT_DATA',legacy_codes,summary_rows,status,'旧20銘柄リストを今回の保存データで集計。新50の対象外銘柄は未計算として数え、ゼロ損益とみなさない。'),
        ('LEGACY20_BASELINE_41539585377',legacy_codes,baseline_rows,baseline_status,
         '旧runの全20銘柄。新50とは構成が異なり、各群の実期間・品質合格日数にも差があり得る。合計額や平均の差を改善とは断定しない。' if baseline_rows else '旧runの比較ファイルなし。成績比較未実施。'),
        ('COMMON_CURRENT_RUN',common,summary_rows,status,common_note),
        ('COMMON_BASELINE_41539585377',common,baseline_rows,baseline_status,common_note if baseline_rows else '旧runの比較ファイルなし。成績比較未実施。'),
        ('PAST_LIQUIDITY_ELIGIBLE_CURRENT_RUN',lagged_eligible,summary_rows,status,
         '今回対象の中で検証開始日前の日足だけが流動性閾値に適合した部分集合。現在母集団・時価総額の生存バイアスを残す感度点検で、成績差の因果効果ではない。'),
    ]
    comparison = []
    for label,codes,rows,states,note in groups:
        comparison.extend(_cohort_rows(label,codes,rows,states,run_id,current_codes,note))
    current_index = _index([row for row in summary_rows if row['コード'] in common])
    baseline_index = _index([row for row in baseline_rows if row['コード'] in common])
    for key in sorted(current_index.keys() | baseline_index.keys()):
        code,strategy,slip,cost = key
        current, prior = current_index.get(key), baseline_index.get(key)
        both = current is not None and prior is not None
        same_acquisition = both and _same_time(current.get('取得開始'),prior.get('取得開始')) and _same_time(current.get('取得終了'),prior.get('取得終了'))
        same_evaluation = both and _same_time(current.get('開始'),prior.get('開始')) and _same_time(current.get('終了'),prior.get('終了'))
        both_eligible = both and _eligible(current,status) and _eligible(prior,baseline_status)
        current_pnl = _number(current.get('損益(円)')) if current else None
        prior_pnl = _number(prior.get('損益(円)')) if prior else None
        comparison.append({'record_type':'paired_symbol', '実行ID':run_id, '比較群':'COMMON_PAIRED_41539585377_VS_CURRENT',
            'コード':code, '戦略':strategy, '片道スリッページ(円/株)':slip, '費用シナリオ':cost,
            '今回結果行あり':current is not None, '旧run結果行あり':prior is not None,
            '今回検証状態':current.get('検証状態','') if current else status.get(code,{}).get('状態',''),
            '旧run検証状態':prior.get('検証状態','') if prior else baseline_status.get(code,{}).get('状態',''),
            '今回取得開始':current.get('取得開始','') if current else '', '今回取得終了':current.get('取得終了','') if current else '',
            '旧run取得開始':prior.get('取得開始','') if prior else '', '旧run取得終了':prior.get('取得終了','') if prior else '',
            '取得期間一致':same_acquisition, '品質後期間一致':same_evaluation,
            '比較期間一致':same_acquisition and same_evaluation, '双方検証済み':both_eligible,
            '今回損益(円)':current_pnl, '旧run損益(円)':prior_pnl,
            '今回引く旧run損益差(円)':current_pnl-prior_pnl if both and current_pnl is not None and prior_pnl is not None else None,
            '比較制約':('両期間一致の記述比較。入力価格の同一性は未確認。' if same_acquisition and same_evaluation else
                     '期間差または結果行欠落を含む記述比較。成績改善と解釈しない。'),
            '資金配分の注意':ACCOUNT_CAVEAT})
    _write(out/'cohort_comparison.csv',comparison)
    return analysis_html(out)


def analysis_html(out):
    """A fragment for the caller's report; never replaces the main report."""
    import html
    out = Path(out)
    rows = [row for row in _read(out/'cohort_comparison.csv') if row.get('record_type') == 'cohort_summary']
    columns = ['比較群','戦略','片道スリッページ(円/株)','費用シナリオ','対象銘柄数','検証済み行数',
               '参考値行数','結果行なし銘柄数','取引なし銘柄数','検証済み平均損益(円)',
               '検証済み中央値損益(円)','単一銘柄期間DD最大(円)','取得開始','取得終了']
    heads = ''.join('<th>'+html.escape(column)+'</th>' for column in columns)
    body = ''.join('<tr>'+''.join('<td>'+html.escape(str(row.get(column,'')))+'</td>' for column in columns)+'</tr>' for row in rows)
    return ('<section><h2>候補銘柄の選定監査・旧20銘柄との記述比較</h2><p>'+html.escape(ACCOUNT_CAVEAT)+
            '</p><p>'+html.escape(BIAS_CAVEAT)+
            '</p><p>選定理由・取得状況・品質は universe_audit.csv、将来情報の利用と先行日足判定は bias_audit.csv、'
            '旧runとの共通銘柄・同一条件の期間一致フラグは cohort_comparison.csv に記録します。'
            '先行日足のみの部分集合も母集団・期間が異なり、合計損益を比較して改善とは判断できません。</p>'+
            '<div class="scroll"><table><thead><tr>'+heads+'</tr></thead><tbody>'+body+'</tbody></table></div></section>')
