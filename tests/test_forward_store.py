"""Synthetic prospective-journal checks; no market/provider/network dependency."""
import csv
from datetime import datetime, timedelta
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from research.forward_study import protocol, store


COMMIT = 'a' * 40


def price_csv(day, full=False):
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(['Datetime', 'Open', 'High', 'Low', 'Close', 'Volume'])
    clocks = ['09:00']
    if full:
        clocks = []
        for first, count in [('09:00', 30), ('12:30', 35)]:
            start = datetime.fromisoformat(day + 'T' + first)
            clocks.extend((start + timedelta(minutes=5 * n)).strftime('%H:%M') for n in range(count))
    for clock in clocks:
        writer.writerow([day + 'T' + clock + ':00+09:00', 100, 102, 99, 101, 500])
    return output.getvalue().encode('utf-8-sig')


class ForwardStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.root = self.parent / 'archive'
        self.settings = protocol.load_protocol()
        self.symbol = protocol.symbols()[0]
        self.day = self.settings['sessions'][0]

    def capture(self, capture_id='first', *, root=None, day=None, symbol=None,
                full=False, started='2026-11-09T08:15:00+00:00',
                received='2026-11-09T08:16:00+00:00',
                completed='2026-11-09T08:17:00+00:00', csv_raw=None):
        root = self.root if root is None else root
        day = self.day if day is None else day
        symbol = self.symbol if symbol is None else symbol
        relative = 'derived/' + symbol.replace('.', '_') + '_5m.csv'
        record = dict(symbol=symbol, session_date=day, status='captured',
                      rows=65 if full else 1, csv_path=relative, received_at=received)
        payload = dict(started_at=started, completed_at=completed,
                       requested_sessions=[day], records=[record])
        files = {'raw/response.bin': b'  {"synthetic":true,"untouched":true}\n',
                 'requests/request.json': b'{"interval":"5m","synthetic":true}\n',
                 relative: price_csv(day, full) if csv_raw is None else csv_raw}
        return store.seal_capture(root, capture_id, payload, files)

    def publish(self, capture_id='first', at='2026-11-09T08:18:00+00:00'):
        return store.add_receipt(self.root, [capture_id], COMMIT, at)

    def selection(self, **kwargs):
        return store.select_primary(self.root, as_of=self.settings['final_cutoff'], **kwargs)

    def snapshot(self, root=None):
        root = self.root if root is None else root
        return {str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob('*') if path.is_file()}

    def resign_manifest(self, path, **changes):
        value = json.loads(path.read_bytes())
        value.pop('manifest_payload_sha256')
        value.update(changes)
        encode = lambda obj: (json.dumps(obj, ensure_ascii=False, sort_keys=True,
                                        indent=2, allow_nan=False) + '\n').encode()
        value['manifest_payload_sha256'] = hashlib.sha256(encode(value)).hexdigest()
        path.write_bytes(encode(value))

    def test_write_once_is_idempotent_and_refuses_replacement_and_symlinks(self):
        path = self.parent / 'bytes.bin'
        store.write_once(path, b'original\x00bytes')
        initial = path.stat()
        store.write_once(path, b'original\x00bytes')
        self.assertEqual(path.stat().st_ino, initial.st_ino)
        self.assertEqual(path.stat().st_mtime_ns, initial.st_mtime_ns)
        with self.assertRaises(ValueError):
            store.write_once(path, b'changed')
        self.assertEqual(path.read_bytes(), b'original\x00bytes')
        link = self.parent / 'leaf-link'
        link.symlink_to(path)
        with self.assertRaises(ValueError):
            store.write_once(link, b'original\x00bytes')
        target = self.parent / 'target'
        target.mkdir()
        parent_link = self.parent / 'parent-link'
        parent_link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            store.write_once(parent_link / 'new.bin', b'unsafe')
        self.assertFalse((target / 'new.bin').exists())

    def test_first_published_partial_survives_later_full_response_and_identical_retry(self):
        first = self.capture()
        self.publish()
        before = self.snapshot()
        second = self.capture('later-full', full=True,
                              started='2026-11-09T08:25:00+00:00',
                              received='2026-11-09T08:26:00+00:00',
                              completed='2026-11-09T08:27:00+00:00')
        self.publish('later-full', '2026-11-09T08:28:00+00:00')
        self.capture('same-partial', started='2026-11-09T08:35:00+00:00',
                     received='2026-11-09T08:36:00+00:00', completed='2026-11-09T08:37:00+00:00')
        self.publish('same-partial', '2026-11-09T08:38:00+00:00')
        result = self.selection()
        self.assertEqual(len(result['selected']), 1)
        chosen = result['selected'][0]
        self.assertEqual(chosen['capture_id'], 'first')
        self.assertEqual(chosen['rows'], 1)
        self.assertEqual(len(list(csv.DictReader(io.StringIO(Path(chosen['csv_path_absolute']).read_text(encoding='utf-8-sig'))))), 1)
        self.assertEqual([row['selection_status'] for row in result['revisions']],
                         ['revised_derived_bytes', 'same_derived_bytes'])
        self.assertEqual(len(result['missing']), 999)
        self.assertEqual(second['previous_capture_sha256'], first['manifest_sha256'])
        after = self.snapshot()
        self.assertTrue(all(after[name] == raw for name, raw in before.items()))
        with self.assertRaises(ValueError):
            self.capture('first', full=True)
        self.assertEqual(store.audit_store(self.root)['captures'][0]['manifest_sha256'], first['manifest_sha256'])

    def test_each_original_file_one_byte_change_or_deletion_is_detected(self):
        paths = ['raw/response.bin', 'requests/request.json',
                 'derived/' + self.symbol.replace('.', '_') + '_5m.csv', 'manifest.json']
        for number, relative in enumerate(paths):
            for action in ('mutate', 'delete'):
                with self.subTest(file=relative, action=action):
                    root = self.parent / f'case-{number}-{action}'
                    self.capture(root=root)
                    path = root / 'captures/first' / relative
                    if action == 'delete':
                        path.unlink()
                    else:
                        raw = bytearray(path.read_bytes())
                        raw[-1] ^= 1
                        path.write_bytes(raw)
                    with self.assertRaises(ValueError):
                        store.audit_store(root)

    def test_recomputed_checksums_do_not_hide_a_chain_fork_or_different_protocol(self):
        self.capture()
        self.capture('second')
        path = self.root / 'captures/second/manifest.json'
        self.resign_manifest(path, previous_capture_sha256=None)
        with self.assertRaisesRegex(ValueError, 'chain'):
            store.audit_store(self.root)
        other = self.parent / 'different-protocol'
        self.capture(root=other)
        self.resign_manifest(other / 'captures/first/manifest.json', protocol_sha256='f' * 64)
        with self.assertRaisesRegex(ValueError, 'different protocol'):
            store.audit_store(other)
        third = self.parent / 'registration-changed'
        self.capture(root=third)
        with (third / 'protocol.json').open('ab') as stream:
            stream.write(b' ')
        with self.assertRaisesRegex(ValueError, 'protocol differs'):
            store.audit_store(third)

    def test_outside_dates_symbols_and_mixed_csv_fail_before_capture_is_visible(self):
        mixed = price_csv(self.day) + b'2026-09-17T09:05:00+09:00,100,102,99,101,500\n'
        cases = [('historical', {'day': '2026-09-17'}),
                 ('holiday', {'day': '2026-11-23'}),
                 ('unknown-symbol', {'symbol': '4449.T'}),
                 ('mixed-csv', {'csv_raw': mixed}),
                 ('historical-csv', {'csv_raw': price_csv('2026-09-17')})]
        for name, kwargs in cases:
            with self.subTest(case=name):
                root = self.parent / name
                with self.assertRaises(ValueError):
                    self.capture(root=root, **kwargs)
                self.assertFalse((root / 'captures/first').exists())
                self.assertEqual(store.audit_store(root)['captures'], [])

    def test_invalid_publication_receipts_never_corrupt_a_valid_capture(self):
        self.capture()
        before = self.snapshot()
        for ids, commit, at in [(['first'], COMMIT, '2026-11-09T08:16:59+00:00'),
                                (['absent'], COMMIT, '2026-11-09T08:18:00+00:00'),
                                (['first'], 'not-a-commit', '2026-11-09T08:18:00+00:00')]:
            with self.subTest(ids=ids, commit=commit, at=at):
                with self.assertRaises(ValueError):
                    store.add_receipt(self.root, ids, commit, at)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(store.audit_store(self.root)['unreceipted_capture_ids'], ['first'])
        self.publish()
        self.assertEqual(len(self.selection()['selected']), 1)

    def test_missing_receipt_and_publication_after_asof_or_final_cutoff_are_excluded(self):
        self.capture()
        result = self.selection()
        self.assertFalse(result['selected'])
        self.assertEqual(len(result['ineligible']), 1)
        self.assertEqual(len(result['missing']), 1000)
        self.publish()
        earlier = store.select_primary(self.root, as_of='2026-11-09T08:17:59+00:00')
        self.assertFalse(earlier['selected'])
        self.assertEqual(len(self.selection()['selected']), 1)
        self.capture('published-too-late', day=self.settings['sessions'][1])
        self.publish('published-too-late', '2026-12-07T18:30:01+09:00')
        final = store.select_primary(self.root, as_of='2026-12-31T23:59:00+09:00')
        self.assertEqual([row['capture_id'] for row in final['selected']], ['first'])
        self.assertEqual(final['ineligible'][0]['capture_id'], 'published-too-late')
        self.assertEqual(protocol.utc(final['as_of']), protocol.utc(self.settings['final_cutoff']))

    def test_early_start_cannot_make_a_response_received_or_completed_after_cutoff_eligible(self):
        for name, received, completed in [
                ('late-response', '2026-12-07T18:30:01+09:00', '2026-12-07T18:31:00+09:00'),
                ('late-seal', '2026-12-07T18:29:00+09:00', '2026-12-07T18:30:01+09:00')]:
            with self.subTest(case=name):
                root = self.parent / name
                self.capture(root=root, day=self.settings['sessions'][-1],
                             started='2026-12-07T17:15:00+09:00', received=received, completed=completed)
                store.add_receipt(root, ['first'], COMMIT, '2026-12-07T18:32:00+09:00')
                for require_receipt in (True, False):
                    result = store.select_primary(root, as_of=self.settings['final_cutoff'],
                                                  require_receipt=require_receipt)
                    self.assertFalse(result['selected'])
                    self.assertEqual(len(result['missing']), 1000)
        invalid = self.parent / 'response-after-seal'
        with self.assertRaises(ValueError):
            self.capture(root=invalid, received='2026-11-09T08:18:00+00:00')
        self.assertFalse((invalid / 'captures/first').exists())

    def test_publication_on_next_japan_date_is_flagged_late_not_replaced(self):
        self.capture()
        # Still November 9 in UTC, but midnight November 10 in Tokyo.
        self.publish(at='2026-11-09T15:00:00+00:00')
        chosen = self.selection()['selected'][0]
        self.assertFalse(chosen['on_time'])
        self.assertEqual(chosen['session_date'], self.day)
        self.assertEqual(chosen['capture_id'], 'first')

    def test_empty_archive_reports_all_thousand_missing_symbol_days_without_prices(self):
        store.initialize(self.root)
        result = self.selection()
        self.assertEqual(result['selected'], [])
        self.assertEqual(result['revisions'], [])
        self.assertEqual(len(result['missing']), 1000)
        expected = {(code, day) for code in protocol.symbols() for day in self.settings['sessions']}
        self.assertEqual({(row['symbol'], row['session_date']) for row in result['missing']}, expected)
        self.assertTrue(all(row['selection_status'] == 'missing' for row in result['missing']))
        self.assertFalse(list(self.root.rglob('*.csv')))
        self.assertEqual(store.audit_store(self.root)['captures'], [])

    def test_historical_code_or_loose_csv_makes_a_store_non_dedicated(self):
        for name in ('smallcap_auto_v3.py', '3994_T_5m.csv', 'results'):
            with self.subTest(extra=name):
                root = self.parent / name.replace('.', '-')
                root.mkdir()
                extra = root / name
                extra.write_bytes(b'historical input must not be imported\n')
                before = self.snapshot(root)
                with self.assertRaisesRegex(ValueError, 'dedicated'):
                    store.initialize(root)
                self.assertEqual(self.snapshot(root), before)
                self.assertFalse((root / 'protocol.json').exists())


if __name__ == '__main__':
    unittest.main()
