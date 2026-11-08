"""Offline event-study artifacts; no trading or market-data connections."""

from __future__ import annotations

import csv
import html
import json
import math
from collections import Counter
from datetime import date, datetime
from numbers import Integral, Real
from pathlib import Path


DATASETS = {
    'observations': 'observations.csv',
    'controls': 'controls.csv',
    'summaries': 'summaries.csv',
    'quality': 'quality.csv',
    'feature_coverage': 'feature_coverage.csv',
}

OBSERVATION_COLUMNS = [
    'observation_id', 'symbol', 'event_type', 'signal_time',
    'signal_confirmed_at', 'entry_time', 'exit_time', 'time_bucket',
    'horizon_minutes', 'status', 'entry_open', 'exit_open',
    'price_return_pct', 'short_gross_return_pct', 'gross_pnl_yen',
    'net1_pnl_yen', 'net2_pnl_yen', 'net1_return_pct', 'net2_return_pct',
]

LABELS = {
    'study_id': '実行識別子', 'symbol': '銘柄', 'event_type': 'イベント',
    'observation_id': '観測ID', 'matched_observation_id': '対応するイベントID',
    'signal_time': 'シグナル足の時刻',
    'signal_confirmed_at': 'シグナル確定時刻', 'entry_time': '測定開始足',
    'exit_time': '測定終了足', 'time_bucket': '開始時刻帯',
    'horizon_minutes': '測定時間(分)', 'status': '状態',
    'control_status': '対照抽出状態', 'reason': '理由',
    'entry_open': '開始足始値', 'exit_open': '終了足始値',
    'price_return_pct': '株価騰落率(%)',
    'short_gross_return_pct': '売り方向・費用前参考騰落率(%)',
    'gross_pnl_yen': '費用前参考損益(円)',
    'net1_pnl_yen': '費用仮定1参考損益(円)',
    'net2_pnl_yen': '費用仮定2参考損益(円)',
    'net1_return_pct': '費用仮定1参考騰落率(%)',
    'net2_return_pct': '費用仮定2参考騰落率(%)',
    'cost_scenario': '費用仮定', 'event_trials': 'イベント測定対象数',
    'event_observed': 'イベント測定可能数',
    'event_coverage_pct': 'イベント測定可能率(%)',
    'event_mean_price_return_pct': 'イベント平均株価騰落率(%)',
    'event_median_price_return_pct': 'イベント中央値・株価騰落率(%)',
    'event_mean_short_reference_return_pct': 'イベント平均・売り方向参考騰落率(%)',
    'event_median_short_reference_return_pct': 'イベント中央値・売り方向参考騰落率(%)',
    'event_mean_reference_pnl_yen': 'イベント平均参考損益(円)',
    'event_median_reference_pnl_yen': 'イベント中央値参考損益(円)',
    'event_down_fraction_pct': '株価下落割合(%)',
    'random_trials': 'ランダム対照測定対象数',
    'random_observed': 'ランダム対照測定可能数',
    'random_coverage_pct': 'ランダム対照測定可能率(%)',
    'random_mean_price_return_pct': '対照平均株価騰落率(%)',
    'random_median_price_return_pct': '対照中央値・株価騰落率(%)',
    'random_mean_short_reference_return_pct': '対照平均・売り方向参考騰落率(%)',
    'paired_observed': '双方測定可能な対数',
    'paired_mean_excess_short_return_pct': '対照との差の平均(売り方向・%pt)',
    'paired_symbol_days': '対の銘柄×日数',
    'paired_dates': '対の日付数',
    'paired_day_mean_excess_short_return_pct': '日別平均差の平均(%pt)',
    'paired_comparison_status': '対応比較の解釈',
}

STATUS_LABELS = {
    'valid': '測定可能',
    'entry_session_boundary': '開始足が同一取引セッション外',
    'horizon_session_boundary': '測定終了が同一取引セッション外',
    'entry_missing': '開始足欠損', 'entry_invalid_open': '開始始値が無効',
    'entry_no_volume': '開始足の出来高なし',
    'path_missing': '測定途中の足欠損',
    'path_invalid_open': '測定途中の始値が無効',
    'path_no_volume': '測定途中の足の出来高なし',
    'exit_missing': '終了足欠損', 'exit_invalid_open': '終了始値が無効',
    'exit_no_volume': '終了足の出来高なし', 'no_control': '適格な対照候補なし',
    'not_applicable_same_universe': '同一候補集合のため対応差は対象外',
    'descriptive_matched_difference': '記述的な対応差（因果効果ではない）',
}

LIMITATIONS = [
    'これは保存済み5分足による記述的イベント研究です。注文・信用取引・空売りの実行、共有資金口座の運用は再現しません。',
    '株価騰落率の負値は株価下落を意味します。売り方向の参考損益は100株の独立した計算であり、実際に売買・借株できたことを意味しません。',
    '費用ゼロと仮定費用を比較します。貸株在庫、貸株料、逆日歩、空売り価格規制、証拠金、受渡制約、板・約定可能性は再現しません。',
    '同じ銘柄・日の観測、重複する測定時間、ランダム対照は独立標本ではありません。p値・信頼区間・将来収益の証明は提示しません。',
    'time_of_dayは適格な全時点を測る基準です。同じ候補集合の対照を層別に重複なしで対応させるため、集計平均は構造上一致します。対応差は欠損と任意の並べ替えによる人工的な差を避けるため未定義です。条件イベントの有効性を示す対照検証ではありません。',
    '現在の50銘柄選定には先読み・生存者バイアスがあります。今回の保存期間を未使用データでの検証とは扱いません。',
    '欠損を架空の価格で補完しません。将来の欠損で測定できない観測も分母と状態を保存し、ランダム対照を引き直しません。',
    '翌足始値は価格測定の基準であり、実際の約定保証ではありません。急騰・ストップ高では買い戻せず、空売りの理論上の損失には上限がありません。',
    '平均参考損益や下落割合は、口座損益・運用利回り・実際の売買勝率を示しません。利益に合わせた条件調整や優勝戦略の選定は行いません。',
]


def json_safe(value):
    """Convert timestamps/scalars and unknown values without inventing zeros."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Real):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, str):
        return value
    # numpy/pandas scalars may not inherit Python's scalar classes.
    if hasattr(value, 'item'):
        return json_safe(value.item())
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return str(value)


def _cell(value):
    value = json_safe(value)
    if value is None:
        return ''
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _display(value):
    value = json_safe(value)
    if value is None or value == '':
        return '未取得／未定義'
    if isinstance(value, bool):
        return 'はい' if value else 'いいえ'
    if isinstance(value, Integral):
        return f'{value:,}'
    if isinstance(value, Real):
        return f'{value:,.6f}'.rstrip('0').rstrip('.')
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _columns(rows, fallback):
    return list(dict.fromkeys(key for row in rows for key in row)) or fallback


def _write_csv(path, rows, fallback):
    columns = _columns(rows, fallback)
    with path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows({key: _cell(row.get(key)) for key in columns} for row in rows)


def _table(rows, *, fallback=None, limit=None, empty='該当する記録はありません。'):
    if not rows:
        return f'<p>{html.escape(empty)}</p>'
    columns = _columns(rows, fallback or [])
    shown = rows if limit is None else rows[:limit]
    header = ''.join(
        f'<th title="{html.escape(str(key), quote=True)}">'
        f'{html.escape(LABELS.get(key, str(key)))}</th>' for key in columns
    )
    body = []
    for row in shown:
        cells = []
        for key in columns:
            value = row.get(key)
            label = STATUS_LABELS.get(value, value) if key in {
                'status', 'control_status', 'paired_comparison_status'} else value
            cells.append(f'<td>{html.escape(_display(label))}</td>')
        body.append('<tr>' + ''.join(cells) + '</tr>')
    note = ''
    if len(shown) < len(rows):
        note = f'<p>全{len(rows):,}件のうち先頭{len(shown):,}件を表示。全件はCSVに保存しています。</p>'
    return note + '<div class="scroll"><table><thead><tr>' + header + '</tr></thead><tbody>' + ''.join(body) + '</tbody></table></div>'


def _status_counts(study):
    rows = []
    for dataset in ('observations', 'controls'):
        counts = Counter((row.get('event_type'), row.get('status')) for row in study.get(dataset, []))
        for (event_type, status), count in sorted(counts.items(), key=lambda item: str(item[0])):
            rows.append({'データ区分': dataset, 'event_type': event_type,
                         'status': status, '測定対象数': count})
    return rows


def _project(rows, columns):
    return [{key: row.get(key) for key in columns} for row in rows]


def write_report(out: Path, study: dict, input_records: list[dict], manifest: dict) -> None:
    """Write every core row to CSV and a bounded, escaped HTML presentation."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    study_id = manifest.get('study_id', '')
    for key, filename in DATASETS.items():
        rows = [{'study_id': study_id, **row} for row in study.get(key, [])]
        fallback = ['study_id'] + (OBSERVATION_COLUMNS if key in {'observations', 'controls'} else ['status'])
        _write_csv(out / filename, rows, fallback)
    _write_csv(out / 'input_audit.csv',
               [{'study_id': study_id, **row} for row in input_records],
               ['study_id', 'symbol', 'status', 'reason'])

    run_status = {'success': '完全成功', 'partial_success': '一部成功', 'failed': '失敗'}.get(
        manifest.get('status'), str(manifest.get('status', '未取得')))
    overview = [{
        '全体状態': run_status, '実行識別子': study_id,
        '対象銘柄数': manifest.get('input_counts', {}).get('requested_symbols'),
        '読込可能銘柄数': manifest.get('input_counts', {}).get('usable_symbols'),
        '価格読込・計算秒数': manifest.get('calculation_seconds'),
        '開始時刻(UTC)': manifest.get('started_at'),
        '入力の最初の足': manifest.get('input_period', {}).get('first_bar'),
        '入力の最後の足': manifest.get('input_period', {}).get('last_bar'),
    }]
    metadata_rows = [{'項目': key, '内容': value} for key, value in {
        'コード版': manifest.get('git'), 'コードSHA-256': manifest.get('code_sha256'),
        '設定SHA-256': manifest.get('config_sha256'),
        'watchlist SHA-256': manifest.get('watchlist_sha256'),
        '価格入力SHA-256': manifest.get('prices_sha256'),
        '保存元の来歴': manifest.get('source_info'),
    }.items()]
    csv_links = ''.join(f'<li><a href="{html.escape(name, quote=True)}">{html.escape(name)}</a></li>'
                        for name in [*DATASETS.values(), 'input_audit.csv', 'manifest.json'])
    limitations = ''.join(f'<li>{html.escape(note)}</li>' for note in manifest.get('limitations', LIMITATIONS))
    config_rows = [{'設定': key, '値': value} for key, value in manifest.get('config', {}).items()]
    summaries = study.get('summaries', [])
    overall = [row for row in summaries if row.get('time_bucket') == 'ALL']
    if not overall:
        overall = summaries
    overall_metrics = _project(overall, [
        'event_type', 'horizon_minutes', 'cost_scenario', 'event_trials', 'event_observed',
        'event_coverage_pct', 'event_mean_price_return_pct', 'event_median_price_return_pct',
        'event_mean_short_reference_return_pct', 'event_mean_reference_pnl_yen',
        'event_median_reference_pnl_yen', 'event_down_fraction_pct',
    ])
    control_metrics = _project(overall, [
        'event_type', 'horizon_minutes', 'cost_scenario', 'random_trials', 'random_observed',
        'random_coverage_pct', 'random_mean_short_reference_return_pct',
        'paired_comparison_status', 'paired_observed',
        'paired_mean_excess_short_return_pct', 'paired_symbol_days', 'paired_dates',
        'paired_day_mean_excess_short_return_pct',
    ])
    bucket_metrics = _project([row for row in summaries if row.get('time_bucket') != 'ALL'], [
        'event_type', 'horizon_minutes', 'time_bucket', 'cost_scenario', 'event_trials',
        'event_observed', 'event_coverage_pct', 'event_mean_price_return_pct',
        'event_mean_reference_pnl_yen', 'paired_observed', 'paired_mean_excess_short_return_pct',
    ])
    content = f'''<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>小型株・下落イベント研究</title>
<style>
body{{font-family:system-ui,sans-serif;line-height:1.65;color:#17202a;background:#f6f8fb;margin:0}}
main{{max-width:1440px;margin:0 auto;padding:24px}}h1{{font-size:1.7rem}}h2{{font-size:1.25rem;margin-top:2rem}}
.notice{{background:#fff4dd;border-left:5px solid #a66b00;padding:12px 20px}}.scroll{{overflow-x:auto;background:white;border:1px solid #d7dfe8}}
table{{border-collapse:collapse;min-width:100%;font-size:.85rem}}th,td{{padding:8px 10px;border:1px solid #d7dfe8;vertical-align:top}}
th{{background:#e8eef6;position:sticky;top:0;text-align:left;min-width:80px;max-width:180px}}td{{max-width:700px;overflow-wrap:anywhere}}
details{{margin:1rem 0}}summary{{cursor:pointer;font-weight:600}}
a{{color:#1155a3}}li{{margin:.4rem 0}}code{{overflow-wrap:anywhere}}footer{{font-size:.85rem;color:#455}}
</style></head><body><main>
<h1>小型株・下落イベント研究</h1>
<p class="notice">保存済み5分足から条件後の株価変化を測定する記述的研究です。売買注文や信用・空売り取引、共有資金ポートフォリオの実行結果ではありません。</p>
{_table(overview)}
<h2>集計</h2>
<p>株価騰落率は負値が下落、売り方向参考騰落率は正値が下落方向を表します。費用シナリオごとに平均・中央値・測定可能率を示します。ALLと時刻帯別は同じ観測を含み、合算できません。</p>
<p>ランダム対照は同銘柄・同日・同じ開始30分帯・同じ測定時間・同じ特徴量準備条件から固定seedで、層内の重複なしで選びます。イベント自身も候補に含みます。将来の欠損を見て引き直しません。対照との差は双方測定可能な対だけを使います。time_of_dayでは全適格時点と同じ候補集合を対応させるため、集計平均は構造上一致します。</p>
{_table(overall_metrics, empty='測定対象がないため集計値はありません。利益の有無を判定できません。')}
<h2>ランダム対照との比較（全時刻帯）</h2>
<p>対照との差は各イベントと対応する対照の売り方向参考騰落率の差です。日別平均差は同日内で平均した後、日を等しい重みで平均します。測定可能な対の減少による偏りは残ります。time_of_dayの対応差は、欠損と任意の並べ替えから人工的な差を生むため未定義とします。</p>
{_table(control_metrics)}
<details><summary>時刻帯別の主要指標を表示</summary>
{_table(bucket_metrics)}
<p>中央値を含む全指標はsummaries.csvに保存しています。</p></details>
<h2>欠損・測定不能を含む対象数</h2>
<p>状態別の対象数はイベント×測定時間の行数です。同一イベントを複数の測定時間で数えます。欠損値はゼロとして集計しません。</p>
{_table(_status_counts(study))}
<h2>特徴量の準備状況</h2>
{_table(study.get('feature_coverage', []), limit=300)}
<h2>価格データ品質</h2>
{_table(study.get('quality', []), limit=300)}
<h2>銘柄別の入力状態</h2>
<p>読込失敗や不足がある銘柄もここに残します。正常な銘柄の記録と区別して確認してください。</p>
{_table(input_records, limit=300)}
<details><summary>イベント観測の明細</summary>
{_table(study.get('observations', []), limit=100)}
</details><details><summary>ランダム対照の明細</summary>
{_table(study.get('controls', []), limit=100)}
</details>
<h2>固定した研究条件</h2>
{_table(config_rows)}
<h2>解釈上の制約</h2><ul>{limitations}</ul>
<h2>再現情報</h2>
{_table(metadata_rows)}
<h2>成果物</h2><ul>{csv_links}</ul>
<footer><p>CSVは全件・元の数値精度を保存し、未取得・未定義は空欄です。HTMLの数値表示は最大小数6桁に丸めています。manifest.jsonに設定・入力ハッシュ・コード版・実行時間を保存しています。</p></footer>
</main></body></html>'''
    (out / 'report.html').write_text(content, encoding='utf-8')
