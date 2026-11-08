"""Fixed-window evaluation and whole-date uncertainty, with synthetic data."""
import csv
import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import numpy as np

from research.forward_study import evaluate as module
from research.forward_study import protocol


class ForwardEvaluateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = protocol.load_protocol()
        self.symbols = protocol.symbols()
        self.sessions = self.settings['sessions']
        self.records = []
        self.revisions = []

    def _record(self, day=None, *, symbol=None, received=None, published=None, price=100):
        day = day or self.sessions[0]
        symbol = symbol or self.symbols[0]
        path = self.root / f'{len(self.records)}-{symbol}-{day}.csv'
        config = json.loads((module.HERE / 'frozen/config.json').read_text())
        with path.open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(['Datetime', 'Open', 'High', 'Low', 'Close', 'Volume'])
            for stamp in module._grid(day, config):
                writer.writerow([stamp.isoformat(), price, price + 1, price - 1, price, 100])
        record = dict(symbol=symbol, session_date=day, csv_path_absolute=str(path),
                      received_at=received or day + 'T17:15:00+09:00',
                      published_at=published or day + 'T17:20:00+09:00',
                      capture_id=f'synthetic-{len(self.records)}',
                      capture_manifest_sha256='a' * 64,
                      csv_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), on_time=True)
        self.records.append(record)
        return record

    def _evaluate(self, now, allow_interim=False):
        def selection(*args, **kwargs):
            return dict(selected=list(reversed(self.records)), revisions=self.revisions,
                        missing=[], as_of=kwargs['as_of'])
        with patch.object(module.capture_store, 'audit_store', return_value=dict(captures=[], receipts=[])), \
                patch.object(module.capture_store, 'select_primary', side_effect=selection) as selector:
            result = module.evaluate(self.root / 'store', self.root / 'output',
                                     datetime.fromisoformat(now), allow_interim)
        return result, selector

    def _read(self, result, filename):
        with (Path(result['output_dir']) / filename).open(encoding='utf-8-sig', newline='') as handle:
            return list(csv.DictReader(handle))

    def test_preperiod_no_outcomes_and_unique_outputs(self):
        with patch.object(module.core, 'run_study', side_effect=AssertionError('profit peek')):
            result, _ = self._evaluate('2026-11-08T19:00:00+09:00')
            repeated, _ = self._evaluate('2026-11-08T19:00:00+09:00', allow_interim=True)
        self.assertEqual(result['status'], 'not_started')
        self.assertFalse(result['outcomes_evaluated'])
        self.assertEqual(result['coverage_counts']['not_due_yet'], 1000)
        self.assertNotEqual(result['output_dir'], repeated['output_dir'])
        coverage = self._read(result, 'coverage.csv')
        self.assertEqual(len(coverage), 1000)
        self.assertTrue(all(row['missing_or_ambiguous_regular_bars'] == '' for row in coverage))
        self.assertFalse((Path(result['output_dir']) / 'observations.csv').exists())
        missing = self._read(result, 'missing_inputs.csv')
        self.assertTrue(all(row['coverage_status'] == 'not_due_yet' for row in missing))

    def test_real_empty_store_is_read_only_and_preperiod_has_no_outcomes(self):
        store_path = self.root / 'nonexistent-store'
        result = module.evaluate(store_path, self.root / 'real-output',
                                 datetime.fromisoformat('2026-11-08T19:00:00+09:00'))
        self.assertFalse(store_path.exists())
        self.assertEqual(result['status'], 'not_started')
        self.assertEqual(result['coverage_counts']['not_due_yet'], 1000)

    def test_interim_defaults_to_coverage_and_entire_missing_days_remain_visible(self):
        self._record()
        with patch.object(module.core, 'run_study', side_effect=AssertionError('profit peek')):
            result, _ = self._evaluate('2026-11-10T19:00:00+09:00')
        self.assertEqual(result['status'], 'interim_audit')
        coverage = self._read(result, 'coverage.csv')
        second_day = [row for row in coverage if row['session_date'] == self.sessions[1]]
        self.assertEqual(len(second_day), 50)
        self.assertTrue(all(row['coverage_status'] == 'missing_due' for row in second_day))
        self.assertTrue(all(row['missing_or_ambiguous_regular_bars'] == '65' for row in second_day))
        self.assertEqual(result['coverage_counts']['not_due_yet'], 900)

    def test_explicit_interim_is_reference_only_and_revisions_do_not_replace_primary(self):
        record = self._record(price=100)
        self.revisions = [{**record, 'csv_path_absolute': '/never/read/a/revision.csv', 'csv_sha256': 'f' * 64}]
        result, _ = self._evaluate('2026-11-09T19:00:00+09:00', allow_interim=True)
        self.assertEqual(result['status'], 'interim_reference')
        self.assertTrue(result['reference_only'])
        observations = self._read(result, 'observations.csv')
        observed = [row for row in observations if row['status'] == 'valid']
        self.assertTrue(observed)
        self.assertTrue(all(float(row['entry_open']) == 100 for row in observed))
        self.assertTrue(all(row['interval_status'] == 'insufficient_observed_dates'
                            for row in self._read(result, 'uncertainty.csv')
                            if row['metric'] != 'matched_excess_short_return_pct'))

    def test_final_missing_window_is_not_extended_and_all_event_cost_groups_exist(self):
        result, selector = self._evaluate('2026-12-08T19:00:00+09:00')
        self.assertEqual(result['status'], 'final_incomplete')
        self.assertEqual(result['coverage_counts']['missing_due'], 1000)
        self.assertEqual(len(self._read(result, 'coverage.csv')), 1000)
        self.assertEqual(len(self._read(result, 'summaries.csv')), 45)
        self.assertEqual(len(self._read(result, 'metrics_by_day.csv')), 20 * 45)
        self.assertEqual(len(self._read(result, 'fixed_calendar_quality.csv')), 1000)
        self.assertTrue(all(row['whole_day_absent'] == 'True'
                            for row in self._read(result, 'fixed_calendar_quality.csv')))
        self.assertTrue(all(row['scheduled_slots'] == '1300' and row['feature_unavailable'] == '1300'
                            for row in self._read(result, 'fixed_calendar_feature_coverage.csv')))
        self.assertEqual(result['elapsed_sessions'], self.sessions)
        self.assertEqual(selector.call_args.kwargs['as_of'], self.settings['final_cutoff'])
        self.assertIs(selector.call_args.kwargs['require_receipt'], True)
        for filename, expected in result['artifact_sha256'].items():
            self.assertEqual(hashlib.sha256((Path(result['output_dir']) / filename).read_bytes()).hexdigest(), expected)

    def test_selected_after_cutoff_is_refused_and_source_order_does_not_select_winners(self):
        record = self._record(self.sessions[-1], published='2026-12-07T18:30:01+09:00')
        with self.assertRaisesRegex(ValueError, 'after the evaluation cutoff'):
            self._evaluate('2026-12-08T19:00:00+09:00')
        self.records.clear()
        record = self._record(self.sessions[0], published=self.sessions[1] + 'T17:20:00+09:00')
        record['on_time'] = False
        result, _ = self._evaluate('2026-12-08T19:00:00+09:00')
        self.assertEqual(result['coverage_counts']['available_late'], 1)
        self.assertEqual(result['status'], 'final_incomplete')

    def test_outside_fixed_window_and_mixed_csv_are_refused(self):
        record = self._record('2026-09-17')
        with self.assertRaisesRegex(ValueError, 'outside the fixed universe/calendar'):
            self._evaluate('2026-12-08T19:00:00+09:00')
        self.records.clear()
        record = self._record()
        path = Path(record['csv_path_absolute'])
        text = path.read_text(encoding='utf-8-sig').replace('2026-11-09T09:00', '2026-09-17T09:00')
        path.write_text(text, encoding='utf-8-sig')
        record['csv_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, 'outside its fixed session_date'):
            self._evaluate('2026-12-08T19:00:00+09:00')

    def test_primary_csv_tampering_and_duplicate_primary_are_refused(self):
        record = self._record()
        path = Path(record['csv_path_absolute'])
        path.write_bytes(path.read_bytes() + b'\n')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            self._evaluate('2026-12-08T19:00:00+09:00')
        record['csv_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.records.append(dict(record))
        with self.assertRaisesRegex(ValueError, 'Multiple primaries'):
            self._evaluate('2026-12-08T19:00:00+09:00')


class DateClusterTests(unittest.TestCase):
    def test_reproducible_whole_dates_preserve_shared_correlations(self):
        dates = [f'2026-11-{day:02d}' for day in range(9, 15)]
        mapping = {dates[0]: [0] * 9, dates[1]: [10], dates[2]: [20], dates[3]: [30], dates[4]: [40]}
        values = {'x': mapping, 'twice_x': {day: [2 * x for x in xs] for day, xs in mapping.items()}}
        kwargs = dict(seed=20261108, replicates=2000, min_dates=5)
        first = module.cluster_intervals(values, dates, **kwargs)
        self.assertEqual(first, module.cluster_intervals(values, dates, **kwargs))
        indexed = {(row['metric'], row['weighting']): row for row in first}
        event = indexed['x', 'event_weighted']
        self.assertAlmostEqual(event['estimate'], 100 / 13)
        self.assertEqual(indexed['x', 'equal_date']['estimate'], 20)
        self.assertEqual(event['observed_dates'], 5)
        self.assertEqual(event['calendar_dates'], 6)  # wholly absent date stays in support
        for weighting in ('event_weighted', 'equal_date'):
            for field in ('estimate', 'ci95_low', 'ci95_high'):
                self.assertAlmostEqual(indexed['twice_x', weighting][field], 2 * indexed['x', weighting][field])
        # Direct whole-date ratio bootstrap, rather than resampling individual events.
        draw = np.random.default_rng(20261108).integers(0, 6, size=(2000, 6))
        counts = np.array([9, 1, 1, 1, 1, 0])
        sums = np.array([0, 10, 20, 30, 40, 0])
        denominator = counts[draw].sum(axis=1)
        samples = sums[draw].sum(axis=1)[denominator > 0] / denominator[denominator > 0]
        self.assertAlmostEqual(event['ci95_low'], np.quantile(samples, .025))
        self.assertAlmostEqual(event['ci95_high'], np.quantile(samples, .975))

    def test_no_interval_when_fewer_than_five_observed_dates(self):
        dates = [f'2026-11-{day:02d}' for day in range(9, 15)]
        rows = module.cluster_intervals({'x': {day: [1] * 100 for day in dates[:4]}}, dates,
                                        seed=20261108, replicates=2000, min_dates=5)
        self.assertTrue(all(row['estimate'] == 1 for row in rows))
        self.assertTrue(all(row['ci95_low'] is None and row['ci95_high'] is None for row in rows))
        self.assertTrue(all(row['interval_status'] == 'insufficient_observed_dates' for row in rows))

    def test_time_of_day_matched_difference_stays_undefined_with_five_dates(self):
        settings = protocol.load_protocol()
        dates = settings['sessions'][:5]
        config = json.loads((module.HERE / 'frozen/config.json').read_text())
        config = {**config, 'events': ['time_of_day'], 'horizons_minutes': [5]}
        observations, controls = [], []
        for i, day in enumerate(dates):
            row = dict(event_type='time_of_day', horizon_minutes=5, status='valid',
                       observation_id=str(i), date=day, symbol='synthetic', price_return_pct=-1,
                       short_gross_return_pct=1, gross_pnl_yen=100,
                       net1_return_pct=.6, net1_pnl_yen=60,
                       net2_return_pct=.4, net2_pnl_yen=40)
            observations.append(row)
            controls.append({**row, 'matched_observation_id': str(i)})
        _, _, _, intervals = module._analyses(dict(observations=observations, controls=controls),
                                               ['synthetic'], dates, config, settings)
        paired = [row for row in intervals if row['metric'] == 'matched_excess_short_return_pct']
        self.assertEqual(len(paired), 6)
        self.assertTrue(all(row['estimate'] is None and row['ci95_low'] is None and row['ci95_high'] is None for row in paired))
        self.assertTrue(all(row['interval_status'] == 'not_applicable_same_universe' for row in paired))


if __name__ == '__main__':
    unittest.main()
