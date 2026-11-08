"""Validate persisted study artifacts independently of the event calculator."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


def check(output):
    output = Path(output)
    manifest = json.loads((output / 'manifest.json').read_text())
    if manifest['status'] not in {'success', 'partial_success'}:
        raise ValueError('Study has no usable research result')
    for name, record in manifest['artifacts'].items():
        raw = (output / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != record['sha256'] or len(raw) != record['bytes']:
            raise ValueError(f'Artifact differs from manifest: {name}')
    observations = pd.read_csv(output / 'observations.csv')
    controls = pd.read_csv(output / 'controls.csv')
    summaries = pd.read_csv(output / 'summaries.csv')
    if observations.observation_id.duplicated().any():
        raise ValueError('Duplicate event observation IDs')
    if controls.matched_observation_id.duplicated().any():
        raise ValueError('Multiple controls for one event')
    if set(controls.matched_observation_id) != set(observations.observation_id):
        raise ValueError('Event/control trial sets differ')
    pairs = observations.merge(controls, left_on='observation_id',
                               right_on='matched_observation_id', suffixes=('_event', '_control'),
                               validate='one_to_one')
    comparable = pairs.status_control != 'no_control'
    for key in ('symbol', 'date', 'time_bucket', 'horizon_minutes', 'event_type'):
        if not (pairs.loc[comparable, key + '_event'].values
                == pairs.loc[comparable, key + '_control'].values).all():
            raise ValueError(f'Control matching differs: {key}')
    config = manifest['config']
    for name, rows in [('event', observations), ('control', controls)]:
        valid = rows.loc[rows.status == 'valid']
        if valid.empty:
            raise ValueError(f'No measurable {name} observations')
        start = pd.to_datetime(valid.entry_time, utc=True).dt.tz_convert(config['timezone'])
        end = pd.to_datetime(valid.exit_time, utc=True).dt.tz_convert(config['timezone'])
        signal = pd.to_datetime(valid.signal_time, utc=True).dt.tz_convert(config['timezone'])
        if not ((start - signal).dt.total_seconds() == 300).all():
            raise ValueError('Entry is not the exact next five-minute open')
        if not ((end - start).dt.total_seconds().values == valid.horizon_minutes.values * 60).all():
            raise ValueError('Wrong elapsed horizon')
        if not (start.dt.date.values == end.dt.date.values).all():
            raise ValueError('Overnight observation')
        signal_minutes = signal.dt.hour * 60 + signal.dt.minute
        end_minutes = end.dt.hour * 60 + end.dt.minute
        same_session = ((signal_minutes >= 540) & (end_minutes < 690)
                        | (signal_minutes >= 750) & (end_minutes < 925))
        if not same_session.all():
            raise ValueError('Lunch or closing-auction boundary crossed')
        entry = valid.entry_open.to_numpy()
        exit_price = valid.exit_open.to_numpy()
        qty = config['reference_quantity']
        expected = {
            'price_return_pct': (exit_price / entry - 1) * 100,
            'short_gross_return_pct': (1 - exit_price / entry) * 100,
            'gross_pnl_yen': (entry - exit_price) * qty,
        }
        for i, slip in enumerate(config['slippage_yen_per_share_per_side'], 1):
            expected[f'net{i}_pnl_yen'] = ((entry - exit_price - 2 * slip) * qty
                                          - 2 * config['assumed_fee_yen_per_side'])
            expected[f'net{i}_return_pct'] = expected[f'net{i}_pnl_yen'] / (entry * qty) * 100
        for key, values in expected.items():
            if not np.allclose(valid[key].to_numpy(), values, rtol=1e-10, atol=1e-8):
                raise ValueError(f'{name} arithmetic differs: {key}')
        invalid = rows.loc[rows.status != 'valid']
        if not invalid[list(expected)].isna().all().all():
            raise ValueError('Unobserved return was represented as a number')
    for _, summary in summaries.loc[summaries.time_bucket == 'ALL'].iterrows():
        rows = observations.loc[(observations.event_type == summary.event_type)
                                & (observations.horizon_minutes == summary.horizon_minutes)]
        valid = rows.loc[rows.status == 'valid']
        if len(rows) != summary.event_trials or len(valid) != summary.event_observed:
            raise ValueError('Summary denominator differs')
        if len(valid):
            for stat in ('mean', 'median'):
                actual = getattr(valid.price_return_pct, stat)()
                if not np.isclose(actual, summary[f'event_{stat}_price_return_pct'], atol=1e-10):
                    raise ValueError('Summary return differs')
        if summary.event_type == 'time_of_day' and not pd.isna(summary.paired_mean_excess_short_return_pct):
            raise ValueError('Unconditional time-of-day pairing presented as an effect')
    print(f'Artifacts verified: {len(observations):,} trials, '
          f'{int((observations.status == "valid").sum()):,} measurable; {manifest["status"]}')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', nargs='?', type=Path, default=Path('results/event_study'))
    check(parser.parse_args().output)


if __name__ == '__main__':
    main()
