"""Publication guard tests use fake GitHub metadata and local signed receipts."""
import json
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from research.forward_capture_v2 import github_guard as guard
from research.forward_study import store
from research.forward_study.protocol import symbols


def run(identifier, *, conclusion='failure', attempt=1, created='2026-11-09T09:26:12Z'):
    return {'id': identifier, 'run_attempt': attempt, 'created_at': created,
            'status': 'completed', 'conclusion': conclusion}


def acquisition_job(*, mode='collect', publication='failure', network='failure'):
    return {'id': 123, 'name': mode, 'status': 'completed', 'conclusion': 'failure',
            'steps': [
                {'name': guard.CAPTURE_STEP if mode == 'collect' else guard.PROBE_STEP,
                 'status': 'completed', 'conclusion': network},
                {'name': guard.PUBLICATION_STEP, 'status': 'completed',
                 'conclusion': publication},
            ]}


class FakeAPI:
    def __init__(self, history=None, jobs=None, current_attempt=1):
        self.history = history or []
        self.jobs = jobs or {}
        self.current = run(200, conclusion=None, attempt=current_attempt,
                           created='2026-11-09T10:00:00Z')
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        parsed = urlsplit(path)
        if parsed.path == '/repos/example/research/actions/runs/200':
            return self.current
        query = parse_qs(parsed.query)
        page = int(query.get('page', ['1'])[0])
        if parsed.path == '/repos/example/research/actions/workflows/forward-event-study.yml/runs':
            return {'total_count': len(self.history),
                    'workflow_runs': self.history[(page - 1) * 100:page * 100]}
        found = re.fullmatch(r'/repos/example/research/actions/runs/(\d+)/attempts/(\d+)/jobs', parsed.path)
        if found:
            key = tuple(map(int, found.groups()))
            rows = self.jobs[key]
            return {'total_count': len(rows), 'jobs': rows[(page - 1) * 100:page * 100]}
        raise AssertionError('Unexpected fake API endpoint: ' + path)


class YahooWorkflowGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.archive = Path(self.temp.name) / 'archive'

    def check(self, api, attempt=1):
        # No test may fall back to actual HTTP, including GitHub HTTP.
        with patch.object(guard, 'urlopen', side_effect=AssertionError('No live network in tests')):
            return guard.guard_previous_publications(self.archive, 'example/research', '200',
                                                       current_run_attempt=attempt, api=api)

    def receipt(self, run_id, attempt):
        identifier = 'repair-' + str(run_id) + '-' + str(attempt)
        store.seal_capture(self.archive, identifier, {
            'records': [{'symbol': symbols()[0], 'session_date': '2026-11-09',
                         'status': 'failed', 'reason': 'synthetic 429 evidence'}],
        }, {'raw/error.body': b'Too Many Requests\r\n'})
        store.add_receipt(self.archive, [identifier], publication_commit='a' * 40,
                          published_at=datetime.now(timezone.utc),
                          workflow_context={'GITHUB_RUN_ID': str(run_id),
                                            'GITHUB_RUN_ATTEMPT': str(attempt)})

    def test_empty_history_is_verified_without_any_provider_request(self):
        result = self.check(FakeAPI())
        self.assertTrue(result['allowed'])
        self.assertEqual(result['reason'], 'previous_publications_verified')
        self.assertEqual(result['provider_requests'], 0)

    def test_original_all_429_run_with_successful_publication_does_not_block(self):
        # Overall failure is expected for 50 failed symbols; durable failed
        # responses still preserve the cooldown evidence.
        api = FakeAPI([run(42593406361)], {(42593406361, 1): [acquisition_job(publication='success')]})
        result = self.check(api)
        self.assertTrue(result['allowed'])
        self.assertEqual(result['blocked_runs'], [])

    def test_capture_followed_by_failed_publication_blocks_quotes(self):
        result = self.check(FakeAPI([run(100)], {(100, 1): [acquisition_job()]}))
        self.assertFalse(result['allowed'])
        self.assertEqual(result['reason'], 'unpublished_capture_or_probe')
        self.assertEqual(result['blocked_runs'][0]['run_id'], '100')
        self.assertEqual(result['provider_requests'], 0)

    def test_probe_with_skipped_publication_blocks_quotes(self):
        result = self.check(FakeAPI([run(100)], {(100, 1): [acquisition_job(mode='probe', publication='skipped')]}))
        self.assertFalse(result['allowed'])
        self.assertEqual(result['blocked_runs'][0]['network_steps'], [guard.PROBE_STEP])

    def test_failure_before_network_step_is_not_lost_quote_evidence(self):
        result = self.check(FakeAPI([run(100)], {(100, 1): [acquisition_job(network='skipped', publication='skipped')]}))
        self.assertTrue(result['allowed'])

    def test_matching_signed_local_repair_receipt_clears_block(self):
        self.receipt(100, 1)
        result = self.check(FakeAPI([run(100)], {(100, 1): [acquisition_job()]}))
        self.assertTrue(result['allowed'])
        self.assertEqual(result['repaired_attempts'], [{'run_id': '100', 'run_attempt': 1}])

    def test_receipt_for_another_run_cannot_clear_lost_evidence(self):
        self.receipt(99, 1)
        result = self.check(FakeAPI([run(100)], {(100, 1): [acquisition_job()]}))
        self.assertFalse(result['allowed'])

    def test_receipt_for_another_attempt_cannot_clear_lost_evidence(self):
        self.receipt(100, 1)
        api = FakeAPI([run(100, attempt=2)], {
            (100, 1): [acquisition_job(publication='success')],
            (100, 2): [acquisition_job()],
        })
        result = self.check(api)
        self.assertFalse(result['allowed'])
        self.assertEqual(result['blocked_runs'][0]['run_attempt'], 2)

    def test_current_rerun_checks_its_earlier_attempt_instead_of_ignoring_same_id(self):
        api = FakeAPI([run(200, conclusion=None, attempt=2)],
                      {(200, 1): [acquisition_job()]}, current_attempt=2)
        result = self.check(api, attempt=2)
        self.assertFalse(result['allowed'])
        self.assertEqual(result['blocked_runs'][0]['run_id'], '200')
        self.assertIn('/repos/example/research/actions/runs/200/attempts/1/jobs?per_page=100&page=1', api.calls)

    def test_current_rerun_requires_its_own_matching_repair(self):
        self.receipt(200, 1)
        api = FakeAPI([], {(200, 1): [acquisition_job()]}, current_attempt=2)
        self.assertTrue(self.check(api, attempt=2)['allowed'])

    def test_successful_old_rerun_does_not_hide_its_first_lost_attempt(self):
        api = FakeAPI([run(100, conclusion='success', attempt=2)], {
            (100, 1): [acquisition_job()],
            (100, 2): [acquisition_job(publication='success')],
        })
        result = self.check(api)
        self.assertFalse(result['allowed'])
        self.assertEqual(result['blocked_runs'][0]['run_attempt'], 1)

    def test_github_api_failure_is_fail_closed_and_does_not_leak_error_body(self):
        def unavailable(_):
            raise RuntimeError('synthetic-secret-token must not be reflected')
        result = self.check(unavailable)
        self.assertFalse(result['allowed'])
        self.assertEqual(result['reason'], 'github_metadata_unavailable')
        self.assertNotIn('synthetic-secret-token', json.dumps(result))

    def test_missing_github_token_is_fail_closed_without_http(self):
        with patch.dict('os.environ', {'GH_TOKEN': '', 'GITHUB_TOKEN': ''}), \
                patch.object(guard, 'urlopen') as network:
            result = guard.guard_previous_publications(self.archive, 'example/research', '200')
        self.assertFalse(result['allowed'])
        self.assertEqual(result['github_requests'], 0)
        network.assert_not_called()

    def test_corrupt_archive_is_fail_closed_before_api(self):
        self.archive.mkdir()
        (self.archive / 'unexpected.txt').write_text('not immutable archive evidence')
        api = FakeAPI()
        result = self.check(api)
        self.assertFalse(result['allowed'])
        self.assertEqual(result['reason'], 'archive_audit_failed')
        self.assertEqual(api.calls, [])

    def test_all_history_pages_are_checked(self):
        history = [run(i, conclusion='success') for i in range(1, 102)]
        api = FakeAPI(history)
        self.assertTrue(self.check(api)['allowed'])
        self.assertTrue(any('page=2' in path for path in api.calls))

    def test_truncated_history_is_not_assumed_complete(self):
        api = FakeAPI()
        def truncated(path):
            if '/workflows/' in path:
                return {'total_count': 2, 'workflow_runs': [run(100)]}
            return api(path)
        result = self.check(truncated)
        self.assertFalse(result['allowed'])
        self.assertEqual(result['reason'], 'github_metadata_unavailable')

    def test_unknown_acquisition_step_layout_is_not_silently_accepted(self):
        job = {'id': 1, 'name': 'collect', 'conclusion': 'failure',
               'steps': [{'name': 'Unrecognized acquisition', 'status': 'completed', 'conclusion': 'failure'}]}
        result = self.check(FakeAPI([run(100)], {(100, 1): [job]}))
        self.assertFalse(result['allowed'])

    def test_future_runs_are_not_misclassified_as_previous_failed_runs(self):
        api = FakeAPI([run(300, created='2026-11-09T11:00:00Z')])
        self.assertTrue(self.check(api)['allowed'])

    def test_invalid_api_pagination_schema_blocks(self):
        api = FakeAPI()
        def malformed(path):
            if '/workflows/' in path:
                return {'workflow_runs': []}
            return api(path)
        self.assertFalse(self.check(malformed)['allowed'])

    def test_current_attempt_mismatch_is_fail_closed(self):
        self.assertFalse(self.check(FakeAPI(current_attempt=2), attempt=1)['allowed'])


if __name__ == '__main__':
    unittest.main()
