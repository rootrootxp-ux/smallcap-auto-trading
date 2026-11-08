"""Read-only evaluation of the fixed prospective capture store.

No downloader, order, broker, or adaptive strategy selection is provided here.
"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import platform
import subprocess
import uuid
from collections import defaultdict
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import protocol
from . import store as capture_store
from .frozen import core


HERE = Path(__file__).resolve().parent
DATASETS = ('observations', 'controls', 'summaries', 'quality', 'feature_coverage')
SCENARIOS = (('zero', 'short_gross_return_pct', 'gross_pnl_yen'),
             ('assumed_slip1', 'net1_return_pct', 'net1_pnl_yen'),
             ('assumed_slip2', 'net2_return_pct', 'net2_pnl_yen'))
LIMITATIONS = [
    '固定50銘柄・固定20営業日の記述的な将来観測です。欠損日を除外して20日を数え直したり、検証期間を延長したりしません。',
    '銘柄選定時の生存者・手動選択バイアスは残ります。初回保存価格は正しい約定可能価格を保証しません。',
    '100株の独立した参考損益です。共有資金ポートフォリオ、借株在庫、貸株料、逆日歩、空売り規制、証拠金、受渡・注文板・約定を再現しません。',
    '観測の時間重複と同日内の銘柄相関があります。信頼区間は日付全体を再抽出する点ごとの参考区間で、多重比較調整・因果効果・利益の証明ではありません。',
    '欠損をゼロ損益や架空価格で補完しません。区間は測定できた値に条件付きで、欠損や少数20日による偏りを解消しません。',
    'time_of_dayの対照は同じ適格集合です。任意の対応と欠損から人工的な差を作らないため、対応差とその区間は対象外です。',
    '後から保存された版は改訂監査に残し、初回の主要入力を置き換えません。締切後の公開・受領は主要評価に加えません。',
]


def _safe(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return str(value)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _git_info():
    result = {}
    for key, args in (('commit', ['rev-parse', 'HEAD']), ('branch', ['branch', '--show-current']),
                      ('dirty', ['status', '--porcelain'])):
        try:
            process = subprocess.run(['git', '-C', str(HERE.parents[1]), *args],
                                     capture_output=True, text=True, check=False, timeout=10)
            result[key] = (bool(process.stdout.strip()) if key == 'dirty' else process.stdout.strip()) if process.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            result[key] = None
    return result


def _stamp(value):
    result = pd.Timestamp(value)
    if pd.isna(result) or result.tzinfo is None:
        raise ValueError('An explicit timezone is required')
    return result


def _csv(path, rows, fallback=('status',)):
    fields = list(dict.fromkeys(k for row in rows for k in row)) or list(fallback)
    with path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            converted = {}
            for key in fields:
                value = _safe(row.get(key))
                converted[key] = (json.dumps(value, ensure_ascii=False, sort_keys=True)
                                  if isinstance(value, (dict, list)) else '' if value is None else value)
            writer.writerow(converted)


def _json(path, value):
    path.write_text(json.dumps(_safe(value), ensure_ascii=False, indent=2,
                               sort_keys=True, allow_nan=False) + '\n', encoding='utf-8')


def _frame(record, allowed_symbols, dates, now, as_of):
    symbol, day = record['symbol'], record['session_date']
    if symbol not in allowed_symbols or day not in dates:
        raise ValueError('Selected primary is outside the fixed universe/calendar')
    if day > now.date().isoformat():
        raise ValueError('Selected primary contains a future session')
    for key in ('received_at', 'published_at', 'capture_completed_at'):
        if record.get(key) is not None and _stamp(record[key]) > as_of:
            raise ValueError('Selected primary was received/published after the evaluation cutoff')
    raw = Path(record['csv_path_absolute']).read_bytes()
    if _sha(raw) != record['csv_sha256']:
        raise ValueError('Selected primary CSV hash mismatch')
    frame = pd.read_csv(io.BytesIO(raw), encoding='utf-8-sig')
    if frame.empty or not set(('Datetime', *core.PRICE_COLUMNS)) <= set(frame.columns):
        raise ValueError('Selected primary must contain nonempty OHLCV observations')
    timestamps = [_stamp(value) for value in frame.pop('Datetime')]
    frame.index = pd.DatetimeIndex(pd.to_datetime(timestamps, utc=True)).tz_convert(now.tzinfo)
    if any(str(value.date()) != day for value in frame.index):
        raise ValueError('Primary CSV contains data outside its fixed session_date')
    for name in core.PRICE_COLUMNS:
        frame[name] = pd.to_numeric(frame[name], errors='coerce')
    return frame


def _grid(day, config):
    grids = [pd.date_range(pd.Timestamp(f'{day} {start}', tz=config['timezone']),
                          pd.Timestamp(f'{day} {end}', tz=config['timezone']),
                          freq='5min', inclusive='left') for start, end in config['sessions']]
    result = grids[0]
    for grid in grids[1:]:
        result = result.append(grid)
    return result


def _quality(frame, grid):
    raw = frame.loc[:, list(core.PRICE_COLUMNS)]
    unique = raw.loc[~raw.index.duplicated(keep=False)]
    regular = unique.reindex(grid)
    present = pd.Series(grid.isin(unique.index), index=grid)
    finite = np.isfinite(regular.to_numpy(dtype=float)).all(axis=1)
    prices = regular[['Open', 'High', 'Low', 'Close']]
    consistent = ((prices > 0).all(axis=1)
                  & (regular.High >= regular[['Open', 'Close', 'Low']].max(axis=1))
                  & (regular.Low <= regular[['Open', 'Close', 'High']].min(axis=1)))
    valid = finite & consistent & (regular.Volume >= 0)
    return dict(raw_rows=len(raw), expected_regular_bars=len(grid),
                observed_unique_regular_bars=int(present.sum()),
                missing_or_ambiguous_regular_bars=int((~present).sum()),
                invalid_regular_bars=int((present & ~valid).sum()),
                zero_volume_regular_bars=int((valid & (regular.Volume == 0)).sum()),
                positive_volume_valid_bars=int((valid & (regular.Volume > 0)).sum()),
                duplicate_rows_excluded=int(raw.index.duplicated(keep=False).sum()),
                off_session_or_off_grid_rows=int((~raw.index.isin(grid)).sum()))


def _coverage(selected, frames, symbols, sessions, config, now, cutoff):
    indexed = {(r['symbol'], r['session_date']): r for r in selected}
    rows = []
    for day in sessions:
        # Publication is due at the same fixed local cutoff time on each date.
        due_at = pd.Timestamp(datetime.combine(datetime.fromisoformat(day).date(),
                                               cutoff.timetz().replace(tzinfo=None)), tz=now.tzinfo)
        for symbol in symbols:
            record = indexed.get((symbol, day))
            pending = now < due_at
            state = ('available_on_time' if record and record['on_time'] else
                     'available_late' if record else 'not_due_yet' if pending else 'missing_due')
            row = dict(symbol=symbol, session_date=day, coverage_status=state,
                       due_at=due_at.isoformat(), on_time=record['on_time'] if record else None,
                       capture_id=record.get('capture_id') if record else None,
                       expected_regular_bars=len(_grid(day, config)),
                       received_at=record.get('received_at') if record else None,
                       published_at=record.get('published_at') if record else None)
            if record:
                row.update(_quality(frames[(symbol, day)], _grid(day, config)))
            else:
                row.update(raw_rows=0 if not pending else None,
                           observed_unique_regular_bars=0 if not pending else None,
                           missing_or_ambiguous_regular_bars=len(_grid(day, config)) if not pending else None,
                           invalid_regular_bars=None, zero_volume_regular_bars=None,
                           positive_volume_valid_bars=0 if not pending else None)
            rows.append(row)
    return rows


def _coverage_group(rows, key):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row[key]].append(row)
    return [{key: name, 'cells': len(items),
             **{state: sum(r['coverage_status'] == state for r in items)
                for state in ('available_on_time', 'available_late', 'missing_due', 'not_due_yet')},
             'missing_due_bars': sum(r.get('missing_or_ambiguous_regular_bars') or 0 for r in items),
             'positive_volume_valid_bars': sum(r.get('positive_volume_valid_bars') or 0 for r in items)}
            for name, items in sorted(grouped.items())]


def cluster_intervals(date_values, dates, *, seed, replicates, min_dates):
    """Resample whole calendar dates, reusing one date draw for every metric.

    date_values maps metric IDs to {date: [observed values]}. Empty scheduled
    dates remain in the bootstrap support; they are never zero-valued returns.
    """
    dates = list(dates)
    if not dates:
        draws = np.empty((replicates, 0), dtype=int)
    else:
        draws = np.random.default_rng(seed).integers(0, len(dates), size=(replicates, len(dates)))
    result = []
    for metric, mapping in date_values.items():
        counts = np.array([len(mapping.get(day, [])) for day in dates], dtype=float)
        sums = np.array([sum(mapping.get(day, [])) for day in dates], dtype=float)
        means = np.divide(sums, counts, out=np.full(len(dates), np.nan), where=counts > 0)
        n_dates = int((counts > 0).sum())
        for weighting in ('event_weighted', 'equal_date'):
            estimate = (float(sums.sum() / counts.sum()) if weighting == 'event_weighted' and counts.sum()
                        else float(np.nanmean(means)) if n_dates else None)
            low = high = None
            valid_replicates = 0
            status = 'insufficient_observed_dates' if n_dates < min_dates else 'pointwise_date_cluster'
            if n_dates >= min_dates:
                denominator = counts[draws].sum(axis=1) if weighting == 'event_weighted' else np.isfinite(means[draws]).sum(axis=1)
                numerator = sums[draws].sum(axis=1) if weighting == 'event_weighted' else np.nansum(means[draws], axis=1)
                samples = np.divide(numerator, denominator, out=np.full(replicates, np.nan), where=denominator > 0)
                samples = samples[np.isfinite(samples)]
                valid_replicates = len(samples)
                if valid_replicates:
                    low, high = (float(v) for v in np.quantile(samples, [0.025, 0.975]))
                else:
                    status = 'no_nonempty_bootstrap_draw'
            result.append(dict(metric=metric, weighting=weighting, estimate=estimate,
                               ci95_low=low, ci95_high=high, interval_status=status,
                               observed_values=int(counts.sum()), observed_dates=n_dates,
                               calendar_dates=len(dates), bootstrap_replicates=replicates,
                               nonempty_bootstrap_replicates=valid_replicates))
    return result


def _analyses(study, symbols, sessions, config, settings):
    observations = study['observations']
    matched = {row['matched_observation_id']: row for row in study['controls']}
    daily, by_symbol, concentration, uncertainty = [], [], [], []
    for event in config['events']:
        for horizon in config['horizons_minutes']:
            trials = [r for r in observations if r['event_type'] == event and r['horizon_minutes'] == horizon]
            valid = [r for r in trials if r['status'] == 'valid']
            pairs = [(r, matched[r['observation_id']]) for r in valid
                     if matched[r['observation_id']]['status'] == 'valid']
            for scenario, return_key, pnl_key in SCENARIOS:
                base = dict(event_type=event, horizon_minutes=horizon, cost_scenario=scenario)
                scenario_index = 0 if scenario == 'assumed_slip1' else 1
                cost_per_observation = (0 if scenario == 'zero' else
                                        2 * config['assumed_fee_yen_per_side']
                                        + 2 * config['slippage_yen_per_share_per_side'][scenario_index]
                                        * config['reference_quantity'])
                values = {key: defaultdict(list) for key in
                          ('price_return_pct', 'short_reference_return_pct', 'reference_pnl_yen', 'matched_excess_short_return_pct')}
                for row in valid:
                    for key, source in (('price_return_pct', 'price_return_pct'),
                                        ('short_reference_return_pct', return_key), ('reference_pnl_yen', pnl_key)):
                        values[key][row['date']].append(row[source])
                if event != 'time_of_day':
                    for left, right in pairs:
                        values['matched_excess_short_return_pct'][left['date']].append(left[return_key] - right[return_key])
                for result in cluster_intervals(values, sessions, seed=settings['bootstrap_seed'],
                                                replicates=settings['bootstrap_replicates'], min_dates=settings['bootstrap_min_dates']):
                    if event == 'time_of_day' and result['metric'] == 'matched_excess_short_return_pct':
                        result.update(estimate=None, ci95_low=None, ci95_high=None,
                                      interval_status='not_applicable_same_universe')
                    uncertainty.append({**base, **result})
                symbol_sums = {}
                for key, names, target in (('date', sessions, daily), ('symbol', symbols, by_symbol)):
                    for name in names:
                        subset = [row for row in valid if row[key] == name]
                        pnl = [row[pnl_key] for row in subset]
                        item = {**base, key: name,
                                'event_trials': sum(row[key] == name for row in trials),
                                'event_observed': len(subset),
                                'mean_price_return_pct': float(np.mean([r['price_return_pct'] for r in subset])) if subset else None,
                                'mean_short_reference_return_pct': float(np.mean([r[return_key] for r in subset])) if subset else None,
                                'mean_reference_pnl_yen': float(np.mean(pnl)) if pnl else None,
                                'reference_cost_per_observation_yen': cost_per_observation,
                                'sum_independent_reference_cost_yen': cost_per_observation * len(subset) if subset else None,
                                'sum_independent_reference_pnl_yen': sum(pnl) if pnl else None}
                        target.append(item)
                        if key == 'symbol':
                            symbol_sums[name] = (sum(pnl) if pnl else None, len(subset))
                abs_total = sum(abs(value) for value, _ in symbol_sums.values() if value is not None)
                total_n = sum(n for _, n in symbol_sums.values())
                ranked = sorted(symbol_sums, key=lambda name: (-abs(symbol_sums[name][0] or 0), name))
                for rank, symbol in enumerate(ranked, 1):
                    pnl, count = symbol_sums[symbol]
                    concentration.append({**base, 'symbol': symbol, 'absolute_contribution_rank': rank,
                                          'event_observed': count, 'sum_independent_reference_pnl_yen': pnl,
                                          'absolute_symbol_pnl_share_pct': abs(pnl) / abs_total * 100 if pnl is not None and abs_total else None,
                                          'observations_share_pct': count / total_n * 100 if total_n else None})
    return daily, by_symbol, concentration, uncertainty


def _fixed_calendar_audits(study, symbols, sessions, config):
    """Add wholly absent scheduled dates without changing frozen event outputs.

    Features reset daily. An entirely unobserved date contributes only missing
    slots, so this augmentation neither creates an event nor changes a return.
    """
    original = {(row['symbol'], row['date']): row for row in study['quality']}
    observed_union = {row['date'] for row in study['quality']}
    extra_slots = sum(len(_grid(day, config)) for day in sessions if day not in observed_union)
    quality = []
    for symbol in symbols:
        for day in sessions:
            row = original.get((symbol, day))
            if row is None:
                slots = len(_grid(day, config))
                row = dict(symbol=symbol, date=day, raw_rows=0, expected_regular_bars=slots,
                           observed_unique_regular_bars=0, missing_or_ambiguous_regular_bars=slots,
                           invalid_regular_bars=0, positive_volume_valid_bars=0,
                           zero_volume_regular_bars=0, duplicate_rows_excluded=0,
                           off_session_or_off_grid_rows=0, opening_0900_observed=False,
                           whole_day_absent=True)
            quality.append({**row, 'calendar_scope': 'fixed protocol sessions elapsed at evaluation'})
    features = [{**row, 'scheduled_slots': row['scheduled_slots'] + extra_slots,
                 'feature_unavailable': row['feature_unavailable'] + extra_slots,
                 'union_input_dates': len(sessions),
                 'calendar_scope': 'fixed protocol sessions elapsed at evaluation'}
                for row in study['feature_coverage']]
    return quality, features


def _table(rows, limit=100):
    if not rows:
        return '<p>記録なし／結果未評価</p>'
    keys = list(dict.fromkeys(k for row in rows for k in row))
    head = ''.join('<th>' + html.escape(str(key)) + '</th>' for key in keys)
    body = []
    for row in rows[:limit]:
        cells = []
        for key in keys:
            value = _safe(row.get(key))
            text = '未定義／未評価' if value is None else str(value)
            cells.append('<td>' + html.escape(text) + '</td>')
        body.append('<tr>' + ''.join(cells) + '</tr>')
    return (f'<p>全{len(rows)}行。先頭{min(len(rows), limit)}行を表示。全件はCSV。</p>'
            '<div class="scroll"><table><tr>' + head + '</tr>' + ''.join(body) + '</table></div>')


def _report(output, metadata, coverage_days, coverage_symbols, study, intervals):
    audit = not metadata['outcomes_evaluated']
    message = ('締切前の取得・品質監査です。損益・イベント後の価格変化を評価していません。'
               if audit else '途中参考値です。確定評価・優位戦略の選択に使いません。' if metadata['reference_only']
               else '固定期間の確定集計です。欠損を残した記述的研究で、利益の証明ではありません。')
    links = ''.join(f'<li><a href="{html.escape(p.name)}">{html.escape(p.name)}</a></li>'
                    for p in sorted(output.iterdir()) if p.suffix in ('.csv', '.json'))
    limits = ''.join('<li>' + html.escape(text) + '</li>' for text in LIMITATIONS)
    outcome = ''
    if not audit:
        overall = [r for r in study['summaries'] if r['time_bucket'] == 'ALL']
        outcome = '<h2>固定条件の集計（全イベント・全費用仮定）</h2>' + _table(overall, 45)
        outcome += ('<h2>日付クラスタの95%参考区間</h2><p>primary=event_weighted、secondary=equal_date。'
                    '点ごとの区間で多重比較調整なし。観測日5日未満では区間を出しません。'
                    '欠損日は支持日付集合に残り、架空のゼロ損益は入れません。</p>' + _table(intervals, 360))
        outcome = '<p>株価騰落率の負値は下落です。売り方向の費用込み損益は100株の仮定費用計算で、約定・借株できたことを意味しません。</p>' + outcome
    page = ('<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>固定期間の将来イベント研究</title><style>body{font-family:system-ui,sans-serif;line-height:1.6;margin:24px;color:#17202a}'
            '.scroll{overflow:auto}table{border-collapse:collapse;font-size:.8rem}th,td{border:1px solid #ccd;padding:6px;max-width:350px;overflow-wrap:anywhere}'
            'th{background:#edf1f7}.notice{padding:16px;background:#fff1d9}</style>'
            '<h1>固定期間の将来イベント研究</h1><p class="notice">' + html.escape(message) + '</p>'
            + _table([{'status': metadata['status'], 'as_of': metadata['as_of'], 'final_cutoff': metadata['final_cutoff'],
                       **metadata['coverage_counts']}])
            + '<h2>日別の取得・品質</h2>' + _table(coverage_days, 20)
            + '<h2>銘柄別の取得・品質</h2>' + _table(coverage_symbols, 50) + outcome
            + '<h2>解釈上の制約</h2><ul>' + limits + '</ul><h2>成果物</h2><ul>' + links
            + '<li><a href="manifest.json">manifest.json</a></li></ul></html>')
    (output / 'report.html').write_text(page, encoding='utf-8')


def evaluate(store: Path, output: Path, now: datetime | None = None, allow_interim=False) -> dict:
    """Evaluate immutable primaries, creating a unique output directory each time."""
    settings = protocol.load_protocol()
    protocol_sha256 = _sha(protocol.PROTOCOL_PATH.read_bytes())
    symbols = list(protocol.symbols())
    sessions = list(settings['sessions'])
    config_raw = (HERE / 'frozen/config.json').read_bytes()
    config = json.loads(config_raw)
    zone = ZoneInfo(settings['timezone'])
    current = _stamp(now or datetime.now(timezone.utc)).tz_convert(zone)
    cutoff = _stamp(settings['final_cutoff']).tz_convert(zone)
    as_of = min(current, cutoff)
    code_hashes = {str(path.relative_to(HERE)): _sha(path.read_bytes())
                   for path in sorted(HERE.rglob('*.py'))}
    git_info = _git_info()
    store_audit = capture_store.audit_store(Path(store))
    selection = capture_store.select_primary(Path(store), as_of=as_of.isoformat(), require_receipt=True)
    selected = sorted(selection['selected'], key=lambda r: (r['session_date'], r['symbol'], r['published_at'], r['capture_id']))
    indexed_frames = {}
    for record in selected:
        key = (record['symbol'], record['session_date'])
        if key in indexed_frames:
            raise ValueError('Multiple primaries for one symbol/session')
        indexed_frames[key] = _frame(record, symbols, sessions, current, as_of)
    coverage = _coverage(selected, indexed_frames, symbols, sessions, config, current, cutoff)
    elapsed = [day for day in sessions if day <= current.date().isoformat()]
    final = current >= cutoff
    outcomes = bool(elapsed) and (final or bool(allow_interim))
    incomplete = any(r['coverage_status'] != 'available_on_time'
                     or r['positive_volume_valid_bars'] != r['expected_regular_bars'] for r in coverage)
    status = ('not_started' if current.date().isoformat() < settings['start_date'] else
              'final_incomplete' if final and incomplete else 'final_complete' if final else
              'interim_reference' if outcomes else 'interim_audit')
    run = Path(output) / (current.strftime('%Y%m%dT%H%M%S.%f%z') + '-' + uuid.uuid4().hex[:12])
    run.mkdir(parents=True, exist_ok=False)
    daily_coverage = _coverage_group(coverage, 'session_date')
    symbol_coverage = _coverage_group(coverage, 'symbol')
    _csv(run / 'coverage.csv', coverage)
    _csv(run / 'coverage_by_day.csv', daily_coverage)
    _csv(run / 'coverage_by_symbol.csv', symbol_coverage)
    _csv(run / 'revisions.csv', selection['revisions'])
    _csv(run / 'ineligible_inputs.csv', selection.get('ineligible', []))
    _csv(run / 'missing_inputs.csv', [r for r in coverage if r['coverage_status'] in ('missing_due', 'not_due_yet')])
    attempts = [{**record, 'capture_id': capture['capture_id'], 'completed_at': capture['completed_at']}
                for capture in store_audit.get('captures', []) for record in capture.get('records', [])]
    _csv(run / 'acquisition_attempts.csv', attempts)
    # Paths/receipts/hashes are frozen here before any outcome calculation.
    _json(run / 'captured_set.json', selected)
    identity_keys = ('symbol', 'session_date', 'received_at', 'published_at', 'capture_id',
                     'capture_manifest_sha256', 'csv_sha256', 'on_time')
    selected_identity = [{key: row.get(key) for key in identity_keys} for row in selected]
    selected_hash = _sha(json.dumps(_safe(selected_identity), ensure_ascii=False, sort_keys=True,
                                   separators=(',', ':')).encode('utf-8'))
    study = {key: [] for key in DATASETS}
    intervals = []
    if outcomes:
        frames = {}
        for symbol in symbols:
            parts = [frame for (code, day), frame in indexed_frames.items() if code == symbol and day in elapsed]
            frames[symbol] = (pd.concat(parts).sort_index() if parts else
                              pd.DataFrame(columns=core.PRICE_COLUMNS,
                                           index=pd.DatetimeIndex([], tz=zone), dtype=float))
        study = core.run_study(frames, config)
        for name in DATASETS:
            _csv(run / f'{name}.csv', study[name])
        fixed_quality, fixed_features = _fixed_calendar_audits(study, symbols, elapsed, config)
        _csv(run / 'fixed_calendar_quality.csv', fixed_quality)
        _csv(run / 'fixed_calendar_feature_coverage.csv', fixed_features)
        days, symbol_metrics, concentration, intervals = _analyses(study, symbols, elapsed, config, settings)
        for name, records in [('metrics_by_day', days), ('metrics_by_symbol', symbol_metrics),
                              ('concentration', concentration), ('uncertainty', intervals)]:
            _csv(run / f'{name}.csv', records)
    counts = {state: sum(r['coverage_status'] == state for r in coverage)
              for state in ('available_on_time', 'available_late', 'missing_due', 'not_due_yet')}
    metadata = dict(schema_version=1, status=status, as_of=as_of.isoformat(), evaluated_at=current.isoformat(),
                    final_cutoff=cutoff.isoformat(), outcomes_evaluated=outcomes,
                    reference_only=outcomes and not final, output_dir=str(run), coverage_counts=counts,
                    fixed_symbol_days=len(symbols) * len(sessions), elapsed_sessions=elapsed,
                    protocol=settings, protocol_sha256=protocol_sha256,
                    config=config, config_sha256=_sha(config_raw), code_sha256=code_hashes, git=git_info,
                    selected_capture_set_sha256=selected_hash, selected_capture_count=len(selected),
                    selection_as_of=selection['as_of'], runtime_python=platform.python_version(),
                    runtime_numpy=np.__version__, runtime_pandas=pd.__version__,
                    store_audit=dict(captures=len(store_audit.get('captures', [])),
                                     receipts=len(store_audit.get('receipts', [])),
                                     protocol_sha256=store_audit.get('protocol_sha256')),
                    bootstrap=dict(seed=settings['bootstrap_seed'], replicates=settings['bootstrap_replicates'],
                                   min_observed_dates=settings['bootstrap_min_dates'],
                                   sampling_unit='whole scheduled date across all symbols',
                                   pointwise_confidence_pct=95, multiple_comparison_adjustment=False),
                    limitations=LIMITATIONS)
    _report(run, metadata, daily_coverage, symbol_coverage, study, intervals)
    metadata['artifact_sha256'] = {path.name: _sha(path.read_bytes()) for path in sorted(run.iterdir()) if path.is_file()}
    _json(run / 'manifest.json', metadata)
    return metadata
