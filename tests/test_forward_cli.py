import json
from datetime import datetime, timezone
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from research.forward_study import __main__ as cli
from research.forward_study import protocol


class ForwardCLITests(unittest.TestCase):
    def test_preflight_before_period_does_not_fetch_evaluate_or_create_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(cli, 'verify_registration', return_value={'test': True}), \
                 patch('research.forward_study.collector._fetch', side_effect=AssertionError('No HTTP')), \
                 patch('research.forward_study.frozen.core.run_study', side_effect=AssertionError('No outcomes')):
                result = cli.preflight(root/'archive', root/'out', datetime(2026, 11, 8, tzinfo=timezone.utc))
            self.assertEqual(result['status'], 'not_started')
            self.assertEqual(result['due_symbol_days'], 0)
            self.assertEqual(result['expected_symbol_days'], 1000)
            self.assertFalse(result['event_outcomes_computed'])
            self.assertEqual(result['network_requests'], 0)
            self.assertFalse((root/'archive').exists())
            self.assertTrue((Path(result['output_dir'])/'report.html').exists())

    def test_readiness_runs_append_new_outputs_without_replacing_previous_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(cli, 'verify_registration', return_value={}):
                a = cli.preflight(root/'archive', root/'out', datetime(2026, 11, 8, tzinfo=timezone.utc))
                original = (Path(a['output_dir'])/'preflight.json').read_bytes()
                b = cli.preflight(root/'archive', root/'out', datetime(2026, 11, 9, 10, tzinfo=timezone.utc))
            self.assertNotEqual(a['output_dir'], b['output_dir'])
            self.assertEqual((Path(a['output_dir'])/'preflight.json').read_bytes(), original)
            self.assertEqual(b['missing_due_symbol_days'], 50)

    def test_cli_full_capture_failure_is_nonzero_but_keeps_audit_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)/'out'
            with patch.object(cli, 'verify_registration', return_value={}), \
                 patch('research.forward_study.collector.collect', return_value={'status': 'failed', 'records': []}):
                code = cli.main(['collect', '--store', str(Path(tmp)/'store'), '--output', str(out)])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(next(out.rglob('collection.json')).read_text())['status'], 'failed')

    def test_programming_failure_propagates_instead_of_becoming_success(self):
        with patch.object(cli, 'verify_registration', return_value={}), \
             patch('research.forward_study.collector.collect', side_effect=RuntimeError('implementation bug')):
            with self.assertRaisesRegex(RuntimeError, 'implementation bug'):
                cli.main(['collect'])

    def test_registration_guard_detects_code_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp)/'package'
            shutil.copytree(protocol.PACKAGE, copy, ignore=shutil.ignore_patterns('__pycache__'))
            target = copy/'example.py'
            target.write_text('original')
            lock = {'registered_at': '2026-11-08T00:00:00+00:00',
                    'files': {'example.py': protocol.digest(target.read_bytes())}}
            (copy/'registration_lock.json').write_text(json.dumps(lock))
            with patch.object(protocol, 'PACKAGE', copy), patch.object(protocol, 'PROTOCOL_PATH', copy/'protocol.json'):
                self.assertEqual(protocol.verify_registration(), lock)
                target.write_text('changed')
                with self.assertRaisesRegex(ValueError, 'code changed'):
                    protocol.verify_registration()
