"""Local bare-Git tests: immutable publication, races and acknowledged receipts."""
import json
import subprocess
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from research.forward_study import git_store


def git(root, *args):
    result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', *args],
                            cwd=root, check=True, capture_output=True)
    return result.stdout.decode().strip()


class ForwardGitStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / 'remote.git'
        self.code = self.root / 'code'
        self.code.mkdir()
        git(self.root, 'init', '--bare', '--initial-branch=main', str(self.remote))
        git(self.code, 'init', '--initial-branch=main')
        git(self.code, 'config', 'user.name', 'Synthetic test')
        git(self.code, 'config', 'user.email', 'synthetic@example.invalid')
        (self.code / 'code.py').write_text('unchanged = True\n')
        git(self.code, 'add', 'code.py')
        git(self.code, 'commit', '-m', 'Code baseline')
        git(self.code, 'remote', 'add', 'origin', str(self.remote))
        git(self.code, 'push', 'origin', 'HEAD:refs/heads/main')
        self.main = git(self.code, 'rev-parse', 'HEAD')
        self.receipt_calls = []
        self.audit = patch.object(git_store, '_audit', return_value={'valid': True}).start()
        self.addCleanup(patch.stopall)
        patch.object(git_store, '_add_receipt', side_effect=self.fake_receipt).start()

    def checkout(self):
        destination = self.root / ('data-' + uuid.uuid4().hex)
        metadata = git_store.checkout_store(self.code, destination)
        return destination, metadata

    def capture(self, root, identifier):
        protocol = root / 'protocol.json'
        if not protocol.exists():
            protocol.write_text('{"synthetic_protocol": true}\n')
        capture = root / 'captures' / identifier
        capture.mkdir(parents=True)
        (capture / 'manifest.json').write_text(json.dumps({'capture_id': identifier}))
        (capture / 'raw.csv').write_text('time,open\n09:00,100\n')

    def remote_head(self):
        return git(self.remote, 'rev-parse', git_store.DATA_REF)

    def remote_paths(self):
        return set(git(self.remote, 'ls-tree', '-r', '--name-only', git_store.DATA_REF).splitlines())

    def fake_receipt(self, root, capture_ids, *, publication_commit, published_at,
                     workflow_context):
        # This is deliberately checked against the actual bare remote, rather
        # than trusting ordering of mocked calls.
        self.assertEqual(self.remote_head(), publication_commit)
        for identifier in capture_ids:
            self.assertIn('captures/' + identifier + '/manifest.json', self.remote_paths())
        self.assertLessEqual(datetime.now(timezone.utc).timestamp() - published_at.timestamp(), 5)
        record = {'capture_ids': capture_ids, 'publication_commit': publication_commit,
                  'published_at': published_at.isoformat(), 'workflow_context': workflow_context}
        path = root / 'receipts' / (uuid.uuid4().hex + '.json')
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(record))
        self.receipt_calls.append(record)
        return record

    def assert_main_unchanged(self):
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/main'), self.main)
        self.assertEqual(git(self.code, 'rev-parse', 'HEAD'), self.main)
        self.assertEqual(git(self.code, 'branch', '--show-current'), 'main')
        self.assertEqual((self.code / 'code.py').read_text(), 'unchanged = True\n')

    def seed(self):
        data, _ = self.checkout()
        self.capture(data, 'first')
        return data, git_store.publish_store(data, ['first'])

    def test_missing_remote_branch_is_empty_unborn_worktree(self):
        data, info = self.checkout()
        self.assertIsNone(info['parent_commit'])
        self.assertEqual(info['data_branch'], 'data/forward-event-study-v1')
        self.assertTrue((data / '.git').is_file())
        self.assertEqual([p.name for p in data.iterdir()], ['.git'])
        self.assertEqual(git(data, 'ls-files'), '')
        self.assert_main_unchanged()

    def test_two_phase_publication_acknowledges_capture_before_receipt(self):
        data, _ = self.checkout()
        self.capture(data, 'first')
        before = datetime.now(timezone.utc)
        with patch.object(git_store, '_git', wraps=git_store._git) as commands:
            result = git_store.publish_store(data, ['first'], now=datetime(1970, 1, 1, tzinfo=timezone.utc))
        after = datetime.now(timezone.utc)
        self.assertEqual(result['status'], 'published')
        self.assertEqual(self.remote_head(), result['receipt_commit'])
        self.assertEqual(git(data, 'rev-parse', result['receipt_commit'] + '^'), result['publication_commit'])
        self.assertEqual(git(data, 'rev-list', '--parents', '-n', '1', result['publication_commit']),
                         result['publication_commit'])
        timestamp = datetime.fromisoformat(self.receipt_calls[0]['published_at'])
        self.assertLessEqual(before, timestamp)
        self.assertLessEqual(timestamp, after)
        pushes = [call.args[1:] for call in commands.call_args_list if call.args[1:2] == ('push',)]
        self.assertEqual(pushes, [('push', '--porcelain', 'origin', 'HEAD:' + git_store.DATA_REF)] * 2)
        self.assertFalse(any('--force' in str(call) for call in commands.call_args_list))
        self.assertTrue(self.audit.called)
        self.assert_main_unchanged()

    def test_second_checkout_preserves_both_captures_and_skips_old_receipt(self):
        _, first = self.seed()
        data, info = self.checkout()
        self.assertEqual(info['parent_commit'], first['receipt_commit'])
        self.assertEqual(git(data, 'branch', '--show-current'), '')
        old = (data / 'captures/first/raw.csv').read_bytes()
        self.capture(data, 'second')
        result = git_store.publish_store(data, ['first', 'second'])
        self.assertEqual(result['capture_ids'], ['second'])
        self.assertEqual((data / 'captures/first/raw.csv').read_bytes(), old)
        self.assertIn('captures/first/raw.csv', self.remote_paths())
        self.assertIn('captures/second/raw.csv', self.remote_paths())
        self.assertEqual(len([p for p in self.remote_paths() if p.startswith('receipts/')]), 2)
        self.assert_main_unchanged()

    def test_no_changes_is_noop_without_push(self):
        self.seed()
        data, _ = self.checkout()
        with patch.object(git_store, '_push') as push:
            result = git_store.publish_store(data, ['first'])
        self.assertEqual(result['status'], 'no_changes')
        push.assert_not_called()
        self.assert_main_unchanged()

    def test_existing_modification_or_deletion_rejected_even_when_staged(self):
        self.seed()
        original = self.remote_head()
        for staged in [False, True]:
            for deleted in [False, True]:
                with self.subTest(staged=staged, deleted=deleted):
                    data, _ = self.checkout()
                    path = data / 'captures/first/raw.csv'
                    if deleted:
                        path.unlink()
                    else:
                        path.write_text('fabricated replacement\n')
                    if staged:
                        git(data, 'add', '--all')
                    with self.assertRaises(git_store.GitStoreError):
                        git_store.publish_store(data, ['first'])
                    self.assertEqual(self.remote_head(), original)
        self.assert_main_unchanged()

    def test_staged_edit_rejected_even_if_working_copy_restored(self):
        self.seed()
        data, _ = self.checkout()
        path = data / 'protocol.json'
        original = path.read_bytes()
        path.write_text('{"changed":true}')
        git(data, 'add', 'protocol.json')
        path.write_bytes(original)
        with self.assertRaises(git_store.GitStoreError):
            git_store.publish_store(data, ['first'])

    def test_code_worktree_and_named_code_branch_cannot_publish(self):
        with self.assertRaises(git_store.GitStoreError):
            git_store.publish_store(self.code, [])
        data, _ = self.checkout()
        git(data, 'symbolic-ref', 'HEAD', 'refs/heads/another-code-branch')
        with self.assertRaises(git_store.GitStoreError):
            git_store.publish_store(data, [])
        self.assert_main_unchanged()

    def test_concurrent_first_publication_rejects_loser_without_overwriting(self):
        winner, _ = self.checkout()
        loser, _ = self.checkout()
        self.capture(winner, 'winner')
        self.capture(loser, 'loser')
        successful = git_store.publish_store(winner, ['winner'])
        with self.assertRaises(git_store.GitStoreError) as caught:
            git_store.publish_store(loser, ['loser'])
        self.assertEqual(caught.exception.phase, 'capture')
        self.assertEqual(self.remote_head(), successful['receipt_commit'])
        self.assertFalse(any('/loser/' in name for name in self.remote_paths()))
        self.assertFalse((loser / 'receipts').exists())
        self.assert_main_unchanged()

    def test_failed_receipt_push_is_repaired_with_new_non_backdated_receipt(self):
        data, _ = self.checkout()
        self.capture(data, 'unreceipted')
        original_push = git_store._push
        def fail_receipt(root, *, phase, publication_commit=None):
            if phase == 'receipt':
                raise git_store.GitStoreError('Synthetic interrupted receipt push', phase=phase,
                                              publication_commit=publication_commit)
            return original_push(root, phase=phase, publication_commit=publication_commit)
        with patch.object(git_store, '_push', side_effect=fail_receipt):
            with self.assertRaises(git_store.GitStoreError):
                git_store.publish_store(data, ['unreceipted'])
        self.assertIn('captures/unreceipted/manifest.json', self.remote_paths())
        self.assertFalse(any(p.startswith('receipts/') for p in self.remote_paths()))
        # The unpublished local receipt must not make a same-worktree retry
        # silently succeed. Repair starts from acknowledged remote data.
        with self.assertRaises(git_store.GitStoreError):
            git_store.publish_store(data, ['unreceipted'])
        repair_started = datetime.now(timezone.utc)
        repaired, _ = self.checkout()
        result = git_store.publish_store(repaired, ['unreceipted'])
        self.assertGreaterEqual(datetime.fromisoformat(result['published_at']), repair_started)
        self.assertEqual(self.remote_head(), result['receipt_commit'])
        self.assert_main_unchanged()

    def test_unexpected_file_directory_or_symlink_rejected(self):
        for kind in ['file', 'directory', 'symlink']:
            with self.subTest(kind=kind):
                data, _ = self.checkout()
                self.capture(data, 'first')
                if kind == 'file':
                    (data / 'README.md').write_text('code does not belong here')
                elif kind == 'directory':
                    (data / 'unrelated').mkdir()
                else:
                    (data / 'captures/first/link').symlink_to(data / 'protocol.json')
                with self.assertRaises(git_store.GitStoreError):
                    git_store.publish_store(data, ['first'])
        self.assert_main_unchanged()

    def test_staged_new_then_deleted_file_rejected(self):
        data, _ = self.checkout()
        self.capture(data, 'first')
        git(data, 'add', '--all')
        (data / 'captures/first/raw.csv').unlink()
        with self.assertRaises(git_store.GitStoreError):
            git_store.publish_store(data, ['first'])

    def test_new_staged_bytes_must_match_the_audited_working_bytes(self):
        data, _ = self.checkout()
        self.capture(data, 'first')
        git(data, 'add', '--all')
        (data / 'captures/first/raw.csv').write_text('different after staging\n')
        with self.assertRaises(git_store.GitStoreError):
            git_store.publish_store(data, ['first'])

    def test_existing_remote_code_tree_is_not_accepted_as_data(self):
        git(self.code, 'push', 'origin', 'HEAD:' + git_store.DATA_REF)
        with self.assertRaises(git_store.GitStoreError):
            self.checkout()
        self.assert_main_unchanged()

    def test_precreated_receipt_cannot_claim_ack_before_first_push(self):
        data, _ = self.checkout()
        self.capture(data, 'first')
        receipts = data / 'receipts'
        receipts.mkdir()
        (receipts / 'premature.json').write_text('{"capture_ids":[]}')
        with self.assertRaises(git_store.GitStoreError):
            git_store.publish_store(data, ['first'])

    def test_inherited_line_ending_config_does_not_change_raw_bytes(self):
        git(self.code, 'config', 'core.autocrlf', 'true')
        data, _ = self.checkout()
        self.capture(data, 'first')
        original = b'time,open\r\n09:00,100\r\n'
        (data / 'captures/first/raw.csv').write_bytes(original)
        git_store.publish_store(data, ['first'])
        blob = subprocess.check_output(['git', 'show', git_store.DATA_REF + ':captures/first/raw.csv'],
                                       cwd=self.remote)
        self.assertEqual(blob, original)

    def test_git_errors_do_not_expose_remote_credentials(self):
        failed = subprocess.CompletedProcess([], 1, b'', b'https://secret-token@example.invalid')
        with patch.object(subprocess, 'run', return_value=failed):
            with self.assertRaises(git_store.GitStoreError) as caught:
                git_store._git(self.code, 'push', 'origin', 'HEAD:' + git_store.DATA_REF)
        self.assertNotIn('secret-token', str(caught.exception))

    def test_real_signed_capture_and_receipt_round_trip(self):
        from research.forward_study import store
        from research.forward_study.protocol import symbols
        data, _ = self.checkout()
        # A synthetic failed observation is evidence too; no prospective quote
        # is invented and this test performs no HTTP requests.
        store.seal_capture(data, 'signed-failure', {
            'records': [{'symbol': symbols()[0], 'session_date': '2026-11-09',
                         'status': 'failed', 'reason': 'synthetic HTTP failure'}],
        }, {'raw/error.txt': b'Synthetic failure; no market data requested.\n'})
        with patch.object(git_store, '_audit', side_effect=store.audit_store), \
                patch.object(git_store, '_add_receipt', side_effect=store.add_receipt):
            result = git_store.publish_store(data, ['signed-failure'])
        fresh, _ = self.checkout()
        audit = store.audit_store(fresh)
        self.assertEqual(len(audit['captures']), 1)
        self.assertEqual(len(audit['receipts']), 1)
        self.assertEqual(audit['unreceipted_capture_ids'], [])
        receipt = audit['receipts'][0]
        self.assertEqual(receipt['publication_commit'], result['publication_commit'])
        self.assertEqual(receipt['captures'][0]['manifest_sha256'],
                         audit['captures'][0]['manifest_sha256'])
        self.assert_main_unchanged()


if __name__ == '__main__':
    unittest.main()
