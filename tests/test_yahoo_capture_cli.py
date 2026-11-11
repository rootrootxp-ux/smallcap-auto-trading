from datetime import timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.forward_study.protocol import utc
from research.forward_capture_v2 import __main__ as cli


class YahooCaptureCLITests(unittest.TestCase):
    def test_preflight_makes_no_provider_request_and_does_not_claim_availability(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(cli, 'verify_acquisition_registration', return_value={}), \
                 patch('research.forward_capture_v2.collector._fetch', side_effect=AssertionError('No HTTP')):
                result = cli.preflight(root/'store', root/'output')
            self.assertEqual(result['network_requests'], 0)
            self.assertEqual(result['provider_available'], 'not_tested')
            self.assertFalse((root/'store').exists())

    def test_network_entry_point_requires_a_recent_allowed_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'guard.json'
            good = {'allowed': True, 'provider_requests': 0, 'checked_at': utc().isoformat(),
                    'repository': 'o/r', 'current_run_id': '1', 'current_run_attempt': 1}
            with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'o/r', 'GITHUB_RUN_ID': '1', 'GITHUB_RUN_ATTEMPT': '1'}):
                for update in ({'allowed': False}, {'checked_at': None},
                               {'checked_at': (utc()-timedelta(hours=1)).isoformat()},
                               {'current_run_id': '2'}, {'current_run_attempt': 2}):
                    with self.subTest(update=update):
                        path.write_text(json.dumps(dict(good, **update)))
                        with self.assertRaises(ValueError):
                            cli._guard(path)
                path.write_text(json.dumps(good))
                self.assertEqual(cli._guard(path)['allowed'], True)

    def test_denied_guard_prevents_collector_from_starting(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'guard.json'
            path.write_text(json.dumps({'allowed': False}))
            with patch.object(cli, 'verify_acquisition_registration', return_value={}), \
                 patch('research.forward_capture_v2.collector.collect', side_effect=AssertionError('No collection')):
                with self.assertRaisesRegex(ValueError, 'not verified'):
                    cli.main(['probe', '--guard-report', str(path)])
