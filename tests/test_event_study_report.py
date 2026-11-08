"""Offline artifact and failure-path tests using synthetic observations only."""

import contextlib
import csv
import hashlib
import io
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research.event_study import __main__ as cli
from research.event_study.__main__ import main
from research.event_study.report import DATASETS, write_report


class EventStudyReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.prices = self.root / 'prices'
        self.prices.mkdir()
        self.watchlist = self.root / 'watchlist.csv'
        self.output = self.root / 'output'

    def _watchlist(self, codes):
        with self.watchlist.open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(['コード', '候補名'])
            writer.writerows((code, '<script>untrusted name</script>') for code in codes)

    def _prices(self, code, terminal_only=False):
        # Complete canonical 65-slot day, with no fabricated prices in the loader.
        slots = [(hour, minute) for hour in range(9, 16) for minute in range(0, 60, 5)
                 if (9 * 60 <= hour * 60 + minute < 11 * 60 + 30
                     or 12 * 60 + 30 <= hour * 60 + minute < 15 * 60 + 25)]
        if terminal_only:
            slots = [(15, 20)]
        with (self.prices / (code.replace('.', '_') + '_5m.csv')).open(
                'w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(['Datetime', 'Open', 'High', 'Low', 'Close', 'Volume'])
            for hour, minute in slots:
                writer.writerow([f'2026-10-01 {hour:02d}:{minute:02d}:00+09:00',
                                 100.125, 101, 99, 100.125, 1000])

    def _run(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return main(['--prices', str(self.prices), '--watchlist', str(self.watchlist),
                         '--output', str(self.output)])

    def _manifest(self):
        return json.loads((self.output / 'manifest.json').read_text(encoding='utf-8'))

    def _csv(self, name):
        with (self.output / name).open(encoding='utf-8-sig', newline='') as handle:
            return list(csv.DictReader(handle))

    def test_complete_data_success_hashes_and_escaped_input(self):
        self._watchlist(['SYNTH.T'])
        self._prices('SYNTH.T')
        self.assertEqual(self._run(), 0)
        manifest = self._manifest()
        self.assertEqual(manifest['status'], 'success')
        self.assertEqual(manifest['input_counts']['research_usable_symbols'], 1)
        self.assertGreater(manifest['input_counts']['measurable_observations'], 0)
        self.assertGreaterEqual(manifest['runtime_seconds'], manifest['calculation_seconds'])
        expected = {*DATASETS.values(), 'input_audit.csv', 'report.html'}
        self.assertEqual(set(manifest['artifacts']), expected)
        for name, evidence in manifest['artifacts'].items():
            raw = (self.output / name).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), evidence['sha256'])
            self.assertEqual(len(raw), evidence['bytes'])
        page = (self.output / 'report.html').read_text(encoding='utf-8')
        self.assertNotIn('<script>untrusted name</script>', page)
        self.assertIn('&lt;script&gt;untrusted name&lt;/script&gt;', page)
        self.assertIn('共有資金ポートフォリオ', page)
        # Planned session-boundary exclusions alone are not a partial input failure.
        self.assertTrue(any(row['status'].endswith('session_boundary')
                            for row in self._csv('observations.csv')))

    def test_partial_input_keeps_successful_symbol_and_failed_record(self):
        self._watchlist(['SYNTH.T', 'ABSENT.T'])
        self._prices('SYNTH.T')
        self.assertEqual(self._run(), 0)
        manifest = self._manifest()
        self.assertEqual(manifest['status'], 'partial_success')
        self.assertEqual(manifest['input_counts']['missing_symbols'], 1)
        audit = {row['code']: row for row in self._csv('input_audit.csv')}
        self.assertEqual(audit['SYNTH.T']['research_status'], 'usable')
        self.assertEqual(audit['ABSENT.T']['status'], 'missing')
        self.assertEqual(audit['ABSENT.T']['research_status'], 'input_unavailable')
        self.assertEqual({row['symbol'] for row in self._csv('observations.csv')}, {'SYNTH.T'})
        self.assertTrue(self._csv('summaries.csv'))
        self.assertIn('一部成功', (self.output / 'report.html').read_text())

    def test_no_loaded_input_fails_after_writing_artifacts(self):
        self._watchlist(['ABSENT.T'])
        self.assertEqual(self._run(), 1)
        manifest = self._manifest()
        self.assertEqual(manifest['status'], 'failed')
        self.assertEqual(manifest['status_reason'], 'no_loaded_inputs')
        self.assertEqual(self._csv('observations.csv'), [])
        self.assertEqual(self._csv('input_audit.csv')[0]['status'], 'missing')
        self.assertIn('失敗', (self.output / 'report.html').read_text())

    def test_loaded_but_no_measurable_outcome_fails_with_evidence(self):
        self._watchlist(['SYNTH.T'])
        self._prices('SYNTH.T', terminal_only=True)
        self.assertEqual(self._run(), 1)
        manifest = self._manifest()
        self.assertEqual(manifest['status_reason'], 'no_measurable_temporal_outcomes')
        self.assertEqual(manifest['input_counts']['usable_symbols'], 1)
        self.assertEqual(manifest['input_counts']['research_usable_symbols'], 0)
        self.assertTrue(self._csv('observations.csv'))
        self.assertEqual(self._csv('input_audit.csv')[0]['research_status'], 'insufficient_outcomes')

    def test_programming_error_and_malformed_core_result_are_not_success(self):
        self._watchlist(['SYNTH.T'])
        self._prices('SYNTH.T')
        self.output.mkdir()
        (self.output / 'manifest.json').write_text('{"status":"success"}')
        (self.output / 'report.html').write_text('previous successful report')
        with patch('research.event_study.__main__.core.run_study',
                   side_effect=RuntimeError('synthetic program defect')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic program defect'):
                self._run()
        self.assertFalse((self.output / 'manifest.json').exists())
        with patch('research.event_study.__main__.core.run_study', return_value={}):
            with self.assertRaises(KeyError):
                self._run()
        self.assertFalse((self.output / 'report.html').exists())

    def test_code_and_git_provenance_are_captured_before_computation(self):
        self._watchlist(['SYNTH.T'])
        self._prices('SYNTH.T')
        order = []
        original_sha = cli._sha256
        original_core = cli.core.run_study

        def fingerprint(path):
            if path.suffix == '.py':
                order.append('code')
            return original_sha(path)

        def compute(*args):
            order.append('core')
            return original_core(*args)

        def git():
            order.append('git')
            return {'commit': 'synthetic-pre-compute-commit', 'dirty': False}

        with patch.object(cli, '_sha256', side_effect=fingerprint), \
                patch.object(cli, '_git_info', side_effect=git), \
                patch.object(cli.core, 'run_study', side_effect=compute):
            self.assertEqual(self._run(), 0)
        self.assertLess(order.index('git'), order.index('core'))
        self.assertTrue(all(i < order.index('core') for i, item in enumerate(order) if item == 'code'))
        self.assertEqual(self._manifest()['git']['commit'], 'synthetic-pre-compute-commit')

    def test_global_invalid_watchlist_is_not_relabelled_as_partial_success(self):
        self._watchlist(['DUP.T', 'DUP.T'])
        with self.assertRaisesRegex(ValueError, 'Duplicate watchlist'):
            self._run()
        self.assertFalse((self.output / 'manifest.json').exists())

    def test_report_keeps_unknown_blank_and_numeric_precision(self):
        study = {key: [] for key in DATASETS}
        study['observations'] = [{
            'observation_id': 'synthetic', 'symbol': '<unsafe>', 'status': 'entry_missing',
            'gross_pnl_yen': None, 'net1_pnl_yen': math.nan,
            'net2_pnl_yen': math.inf, 'entry_open': 1234.567890123456,
        }]
        write_report(self.output, study, [], {
            'study_id': 'synthetic', 'status': 'failed',
            'source_info': '<script>not executed</script>',
        })
        row = self._csv('observations.csv')[0]
        for key in ('gross_pnl_yen', 'net1_pnl_yen', 'net2_pnl_yen'):
            self.assertEqual(row[key], '')
        self.assertEqual(row['entry_open'], '1234.567890123456')
        self.assertTrue((self.output / 'observations.csv').read_bytes().startswith(b'\xef\xbb\xbf'))
        page = (self.output / 'report.html').read_text()
        self.assertIn('&lt;unsafe&gt;', page)
        self.assertIn('&lt;script&gt;not executed&lt;/script&gt;', page)
        self.assertNotIn('<script>', page)
        self.assertIn('未取得／未定義', page)


if __name__ == '__main__':
    unittest.main()
