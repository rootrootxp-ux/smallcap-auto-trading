"""Causal event labels and exact-time descriptive outcomes; entirely offline.

Returns are observations, not orders. Bar timestamps label the START of the
five-minute interval. Signals become available five minutes later. No row is
shifted over a missing timestamp, lunch, a closing auction, or an overnight gap.
"""
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import random

import numpy as np
import pandas as pd


CONFIG_PATH = Path(__file__).with_name('config.json')
EVENTS = ('ma3_cross_down', 'vwap_cross_down', 'relative_volume_gt_1_5',
          'prior6_low_break', 'time_of_day')
PRICE_COLUMNS = ('Open', 'High', 'Low', 'Close', 'Volume')
NUMERIC_OUTCOMES = ('entry_open', 'exit_open', 'price_return_pct',
                    'short_gross_return_pct', 'gross_pnl_yen', 'net1_pnl_yen',
                    'net2_pnl_yen', 'net1_return_pct', 'net2_return_pct')


def load_config(path=None):
    """Read the versioned protocol; the CLI deliberately has no threshold flags."""
    config = json.loads(Path(path or CONFIG_PATH).read_text(encoding='utf-8'))
    if config['interval_minutes'] != 5 or config['timezone'] != 'Asia/Tokyo':
        raise ValueError('Only the fixed five-minute Asia/Tokyo protocol is supported')
    if tuple(config['events']) != EVENTS:
        raise ValueError('Unexpected event definitions')
    return config


def _sessions(day, config):
    return [(pd.Timestamp(f'{day} {start}', tz=config['timezone']),
             pd.Timestamp(f'{day} {end}', tz=config['timezone']))
            for start, end in config['sessions']]


def _grid(day, config):
    parts = [pd.date_range(start, end, freq='5min', inclusive='left')
             for start, end in _sessions(day, config)]
    result = parts[0]
    for part in parts[1:]:
        result = result.append(part)
    return result


def _validate_frame(frame):
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
        raise ValueError('Input timestamps must be an explicit timezone-aware DatetimeIndex')
    if frame.index.hasnans:
        raise ValueError('Input timestamps contain NaT')
    missing = set(PRICE_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(f'Missing OHLCV columns: {sorted(missing)}')


def _prepare(frame, config, dates=None):
    _validate_frame(frame)
    raw = frame.loc[:, list(PRICE_COLUMNS)].copy()
    raw.index = raw.index.tz_convert(config['timezone'])
    raw = raw.apply(pd.to_numeric, errors='coerce').sort_index()
    all_dates = sorted(set(dates or [str(t.date()) for t in raw.index]))
    # Keep neither copy of an ambiguous timestamp; do not silently choose a price.
    ambiguous = raw.index[raw.index.duplicated(keep=False)]
    unique = raw.loc[~raw.index.duplicated(keep=False)]
    planned = [_grid(day, config) for day in all_dates]
    index = planned[0] if planned else pd.DatetimeIndex([], tz=config['timezone'])
    for part in planned[1:]:
        index = index.append(part)
    regular = unique.reindex(index)  # NaN is evidence of absence, never a fill.
    present = pd.Series(index.isin(unique.index), index=index)
    finite = pd.Series(np.isfinite(regular.to_numpy(dtype=float)).all(axis=1), index=index)
    prices = regular.loc[:, ['Open', 'High', 'Low', 'Close']]
    consistent = ((prices > 0).all(axis=1)
                  & (regular.High >= regular[['Open', 'Close', 'Low']].max(axis=1))
                  & (regular.Low <= regular[['Open', 'Close', 'High']].min(axis=1)))
    data_valid = finite & consistent & (regular.Volume >= 0)
    signal_valid = data_valid & (regular.Volume > 0)
    quality = []
    for day, grid in zip(all_dates, planned):
        d = raw.loc[raw.index.date == pd.Timestamp(day).date()]
        p = present.loc[grid]
        valid = data_valid.loc[grid]
        quality.append({
            'date': day, 'raw_rows': len(d), 'expected_regular_bars': len(grid),
            'observed_unique_regular_bars': int(p.sum()),
            'missing_or_ambiguous_regular_bars': int((~p).sum()),
            'invalid_regular_bars': int((p & ~valid).sum()),
            'positive_volume_valid_bars': int(signal_valid.loc[grid].sum()),
            'zero_volume_regular_bars': int((valid & (regular.loc[grid].Volume == 0)).sum()),
            'duplicate_rows_excluded': int((ambiguous.date == pd.Timestamp(day).date()).sum()),
            'off_session_or_off_grid_rows': int((~d.index.isin(grid)).sum()),
            'opening_0900_observed': bool(p.iloc[0] and valid.iloc[0]),
            'whole_day_absent': len(d) == 0,
            'calendar_scope': 'union of observed input dates, not a verified exchange holiday calendar',
        })
    return regular, present, data_valid, signal_valid, quality


def _features(regular, data_valid, signal_valid, config):
    records = []
    for date, day in regular.groupby(regular.index.date, sort=True):
        valid = signal_valid.loc[day.index]
        close = day.Close.where(valid)
        volume = day.Volume.where(valid)
        ma = close.rolling(config['ma_window'], min_periods=config['ma_window']).mean()
        vol = volume.shift(1).rolling(config['volume_window'], min_periods=config['volume_window']).mean()
        low = day.Low.where(valid).shift(1).rolling(config['prior_low_window'], min_periods=config['prior_low_window']).min()
        # A zero-volume interval contributes zero weight, but a missing/invalid
        # interval makes the true day-to-date VWAP unknown for the rest of the day.
        prefix = data_valid.loc[day.index].cummin()
        tp = (day.High + day.Low + day.Close) / 3
        cumulative_volume = day.Volume.where(data_valid.loc[day.index], 0).cumsum()
        vwap = ((tp * day.Volume).where(data_valid.loc[day.index], 0).cumsum()
                / cumulative_volume.replace(0, np.nan)).where(prefix)
        eligible = {
            'ma3_cross_down': valid & valid.shift(1, fill_value=False) & ma.notna() & ma.shift(1).notna(),
            'vwap_cross_down': valid & valid.shift(1, fill_value=False) & vwap.notna() & vwap.shift(1).notna(),
            'relative_volume_gt_1_5': valid & vol.notna() & (vol > 0),
            'prior6_low_break': valid & low.notna(),
            'time_of_day': valid,
        }
        flags = {
            'ma3_cross_down': (close.shift(1) >= ma.shift(1)) & (close < ma),
            'vwap_cross_down': (close.shift(1) >= vwap.shift(1)) & (close < vwap),
            'relative_volume_gt_1_5': day.Volume > vol * config['relative_volume_threshold'],
            'prior6_low_break': close < low,
            'time_of_day': valid,
        }
        for i, timestamp in enumerate(day.index):
            row = {'signal_time': str(timestamp),
                   'signal_confirmed_at': str(timestamp + pd.Timedelta(minutes=5)),
                   'date': str(date), 'signal_valid': bool(valid.iloc[i]),
                   'vwap_prefix_complete': bool(prefix.iloc[i]),
                   'ma3': _number(ma.iloc[i]), 'vwap': _number(vwap.iloc[i]),
                   'prior5_volume_mean': _number(vol.iloc[i]),
                   'prior6_low': _number(low.iloc[i])}
            for event in EVENTS:
                row[event + '_eligible'] = bool(eligible[event].iloc[i])
                row[event] = bool(eligible[event].iloc[i] and flags[event].iloc[i])
            records.append(row)
    return records


def _number(value):
    return float(value) if np.isfinite(value) else None


def feature_rows(frame, config=None, dates=None):
    """Return causal labels for every scheduled slot and descriptive day audits.

    Prior windows reset each day and follow scheduled bars (lunch is not a
    missing bar). They can recover after a gap. Daily VWAP cannot recover until
    the next day. These audits NEVER retroactively remove an earlier event.
    """
    config = config or load_config()
    regular, _, data_valid, signal_valid, quality = _prepare(frame, config, dates)
    return _features(regular, data_valid, signal_valid, config), quality


def _schedule(signal, horizon, config):
    entry = signal + pd.Timedelta(minutes=config['interval_minutes'])
    exit_time = entry + pd.Timedelta(minutes=horizon)
    session = next(((start, end) for start, end in _sessions(str(signal.date()), config)
                    if start <= signal < end), None)
    if session is None or not (session[0] <= entry < session[1]):
        status = 'entry_session_boundary'
    elif exit_time >= session[1]:
        status = 'horizon_session_boundary'
    else:
        status = 'valid'
    return entry, exit_time, status


def _bucket(timestamp, config):
    minutes = timestamp.hour * 60 + timestamp.minute
    start = minutes // config['time_bucket_minutes'] * config['time_bucket_minutes']
    return f'{start // 60:02d}:{start % 60:02d}'


def _outcome(signal, horizon, regular, present, config):
    entry, exit_time, status = _schedule(signal, horizon, config)
    row = {'signal_time': str(signal),
           'signal_confirmed_at': str(signal + pd.Timedelta(minutes=5)),
           'entry_time': str(entry), 'exit_time': str(exit_time),
           'date': str(signal.date()), 'time_bucket': _bucket(entry, config),
           'horizon_minutes': horizon, 'status': status,
           **dict.fromkeys(NUMERIC_OUTCOMES)}
    if status != 'valid':
        return row
    path = pd.date_range(entry, exit_time, freq='5min')
    for i, timestamp in enumerate(path):
        part = 'entry' if i == 0 else 'exit' if i == len(path) - 1 else 'path'
        if timestamp not in present.index or not present.loc[timestamp]:
            row['status'] = part + '_missing'
            return row
        observed = regular.loc[timestamp]
        if not np.isfinite(observed.Open) or observed.Open <= 0:
            row['status'] = part + '_invalid_open'
            return row
        if not np.isfinite(observed.Volume) or observed.Volume <= 0:
            row['status'] = part + '_no_volume'
            return row
    entry_price, exit_price = float(regular.loc[entry].Open), float(regular.loc[exit_time].Open)
    qty = config['reference_quantity']
    gross = (entry_price - exit_price) * qty
    row.update(entry_open=entry_price, exit_open=exit_price,
               price_return_pct=(exit_price / entry_price - 1) * 100,
               short_gross_return_pct=(1 - exit_price / entry_price) * 100,
               gross_pnl_yen=gross)
    for i, slip in enumerate(config['slippage_yen_per_share_per_side'], 1):
        # Hypothetical short: sell at O-slip, cover at O+slip. Denominator is
        # unadjusted entry notional, not credit margin or portfolio equity.
        net = gross - 2 * slip * qty - 2 * config['assumed_fee_yen_per_side']
        row[f'net{i}_pnl_yen'] = net
        row[f'net{i}_return_pct'] = net / (entry_price * qty) * 100
    return row


def _identity(*parts):
    return hashlib.sha256('|'.join(map(str, parts)).encode()).hexdigest()[:24]


def _summary(observations, controls, config):
    groups = defaultdict(list)
    for event in EVENTS:
        for horizon in config['horizons_minutes']:
            groups[(event, horizon, 'ALL')] = []
    matched = {row['matched_observation_id']: row for row in controls}
    for row in observations:
        for bucket in ('ALL', row['time_bucket']):
            groups[(row['event_type'], row['horizon_minutes'], bucket)].append(row)
    summaries = []
    def mean(rows, key):
        return float(np.mean([row[key] for row in rows])) if rows else None
    def median(rows, key):
        return float(np.median([row[key] for row in rows])) if rows else None
    for (event, horizon, bucket), trials in sorted(groups.items()):
        observed = [row for row in trials if row['status'] == 'valid']
        random_trials = [matched[row['observation_id']] for row in trials]
        random_observed = [row for row in random_trials if row['status'] == 'valid']
        pairs = [(row, matched[row['observation_id']]) for row in observed
                 if matched[row['observation_id']]['status'] == 'valid']
        symbol_days = {(a['symbol'], a['date']) for a, _ in pairs}
        for scenario, return_key, pnl_key in (
                ('zero', 'short_gross_return_pct', 'gross_pnl_yen'),
                ('assumed_slip1', 'net1_return_pct', 'net1_pnl_yen'),
                ('assumed_slip2', 'net2_return_pct', 'net2_pnl_yen')):
            differences = [a[return_key] - b[return_key] for a, b in pairs]
            daily = defaultdict(list)
            for (a, b), difference in zip(pairs, differences):
                daily[a['date']].append(difference)
            daily_means = [float(np.mean(values)) for values in daily.values()]
            summaries.append({
                'event_type': event, 'horizon_minutes': horizon, 'time_bucket': bucket,
                'cost_scenario': scenario, 'event_trials': len(trials),
                'event_observed': len(observed),
                'event_coverage_pct': len(observed) / len(trials) * 100 if trials else None,
                'event_mean_price_return_pct': mean(observed, 'price_return_pct'),
                'event_median_price_return_pct': median(observed, 'price_return_pct'),
                'event_mean_short_reference_return_pct': mean(observed, return_key),
                'event_median_short_reference_return_pct': median(observed, return_key),
                'event_mean_reference_pnl_yen': mean(observed, pnl_key),
                'event_median_reference_pnl_yen': median(observed, pnl_key),
                'event_down_fraction_pct': (sum(r['price_return_pct'] < 0 for r in observed)
                                            / len(observed) * 100 if observed else None),
                'random_trials': len(random_trials), 'random_observed': len(random_observed),
                'random_coverage_pct': len(random_observed) / len(random_trials) * 100 if random_trials else None,
                'random_mean_price_return_pct': mean(random_observed, 'price_return_pct'),
                'random_median_price_return_pct': median(random_observed, 'price_return_pct'),
                'random_mean_short_reference_return_pct': mean(random_observed, return_key),
                'random_median_short_reference_return_pct': median(random_observed, return_key),
                'random_mean_reference_pnl_yen': mean(random_observed, pnl_key),
                'paired_observed': len(pairs),
                # The unconditional time-of-day event is the entire pool.
                # Complete-pair filtering can otherwise manufacture a nonzero
                # difference solely from missingness and an arbitrary pairing.
                'paired_comparison_status': ('not_applicable_same_universe' if event == 'time_of_day'
                                             else 'descriptive_matched_difference'),
                'paired_mean_excess_short_return_pct': (float(np.mean(differences))
                    if differences and event != 'time_of_day' else None),
                'paired_symbol_days': len(symbol_days), 'paired_dates': len(daily),
                'paired_day_mean_excess_short_return_pct': (float(np.mean(daily_means))
                    if daily_means and event != 'time_of_day' else None),
            })
    return summaries


def run_study(frames, config=None):
    """Compute event trials and one seeded control per trial, preserving failures.

    Controls match symbol, date, entry half-hour, horizon and event-specific
    feature readiness. Selection uses ONLY signal-side history and the known
    session clock. The pool includes event times. We do not redraw or filter
    candidates by future missing data. Each stratum uses its own hash-derived
    RNG, sorted IDs and sampling without replacement, making input order inert.
    """
    config = config or load_config()
    dates = set()
    for frame in frames.values():
        _validate_frame(frame)
        dates.update(str(t.date()) for t in frame.index.tz_convert(config['timezone']))
    dates = sorted(dates)
    observations, controls, quality, feature_coverage = [], [], [], []
    for symbol, frame in sorted(frames.items()):
        regular, present, data_valid, signal_valid, audit = _prepare(frame, config, dates)
        features = _features(regular, data_valid, signal_valid, config)
        for record in audit:
            quality.append(dict(symbol=symbol, **record))
        for event in EVENTS:
            feature_coverage.append({
                'symbol': symbol, 'event_type': event, 'scheduled_slots': len(features),
                'signal_side_eligible': sum(row[event + '_eligible'] for row in features),
                'feature_unavailable': sum(not row[event + '_eligible'] for row in features),
                'events_detected': sum(row[event] for row in features),
                'observed_dates': sum(not r['whole_day_absent'] for r in audit),
                'union_input_dates': len(dates),
            })
        pools = defaultdict(list)
        event_groups = defaultdict(list)
        outcomes = {}
        def outcome(signal, horizon):
            key = (signal, horizon)
            if key not in outcomes:
                outcomes[key] = _outcome(signal, horizon, regular, present, config)
            return outcomes[key]
        for feature in features:
            timestamp = pd.Timestamp(feature['signal_time'])
            for horizon in config['horizons_minutes']:
                entry, _, schedule_status = _schedule(timestamp, horizon, config)
                for event in EVENTS:
                    if not feature[event + '_eligible']:
                        continue
                    stratum = (event, feature['date'], _bucket(entry, config), horizon)
                    if schedule_status == 'valid':
                        pools[stratum].append(timestamp)
                    if not feature[event]:
                        continue
                    row = dict(outcome(timestamp, horizon), symbol=symbol, event_type=event,
                               observation_id=_identity(symbol, event, timestamp, horizon))
                    observations.append(row)
                    if schedule_status == 'valid':
                        event_groups[stratum].append(row)
                    else:
                        controls.append({
                            'symbol': symbol, 'event_type': event,
                            'matched_observation_id': row['observation_id'],
                            'observation_id': _identity('control', row['observation_id'], 'boundary'),
                            'date': row['date'], 'time_bucket': row['time_bucket'],
                            'horizon_minutes': horizon, 'status': 'no_control',
                            'reason': schedule_status, 'candidate_count': 0,
                            'signal_time': None, 'signal_confirmed_at': None,
                            'entry_time': None, 'exit_time': None,
                            **dict.fromkeys(NUMERIC_OUTCOMES),
                        })
        for stratum, trials in sorted(event_groups.items()):
            candidates = sorted(pools[stratum])
            seed = int(_identity(config['random_seed'], symbol, *stratum), 16)
            rng = random.Random(seed)
            trials = sorted(trials, key=lambda row: row['signal_time'])
            # Every event is itself eligible, so enough candidates exist whenever
            # the requested interval fits the known session schedule.
            selected = rng.sample(candidates, len(trials)) if len(candidates) >= len(trials) else []
            for i, event_row in enumerate(trials):
                if selected:
                    control = dict(outcome(selected[i], event_row['horizon_minutes']))
                    control_signal = str(selected[i])
                else:
                    control = {key: event_row[key] for key in
                               ('date', 'time_bucket', 'horizon_minutes')}
                    control.update(signal_time=None, signal_confirmed_at=None,
                                   entry_time=None, exit_time=None, status='no_control',
                                   **dict.fromkeys(NUMERIC_OUTCOMES))
                    control_signal = None
                control.update(symbol=symbol, event_type=event_row['event_type'],
                               matched_observation_id=event_row['observation_id'],
                               observation_id=_identity('control', event_row['observation_id'], control_signal),
                               candidate_count=len(candidates),
                               selection_seed=str(seed),
                               matching_policy='symbol/date/entry-half-hour/horizon/event-readiness; no redraw')
                controls.append(control)
    observations.sort(key=lambda r: (r['symbol'], r['signal_time'], r['event_type'], r['horizon_minutes']))
    controls.sort(key=lambda r: r['matched_observation_id'])
    return {'observations': observations, 'controls': controls,
            'summaries': _summary(observations, controls, config),
            'quality': quality, 'feature_coverage': feature_coverage}
