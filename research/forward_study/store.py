"""Write-once capture journal and publication receipts for one fixed experiment.

Hashes detect ordinary corruption and the Git publisher rejects rewrites. This
is an auditable repository, not a claim of WORM storage against administrators.
"""
from datetime import datetime
import csv
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import uuid
from zoneinfo import ZoneInfo

from .protocol import PROTOCOL_PATH, digest, implementation_info, load_protocol, symbols, utc


ALLOWED_ROOT = {'.git', '.gitattributes', 'protocol.json', 'captures', 'receipts'}


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + '\n').encode('utf-8')


def _component(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,180}', value) or value in {'.', '..'}:
        raise ValueError('Unsafe archive identifier')
    return value


def _relative(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or '..' in path.parts or '\\' in value or str(path) != value:
        raise ValueError('Unsafe archive-relative path')
    return path


def write_once(path, data):
    """Identical retries are harmless; different bytes never replace a path."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Archive symlinks are forbidden')
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError('Archive symlink parent is forbidden')
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('xb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.read_bytes() != data:
            raise ValueError(f'Immutable archive path already has different bytes: {path.name}')


def _signed(value, key):
    result = dict(value)
    if key in result:
        raise ValueError('Checksum field supplied by caller')
    result[key] = digest(canonical(value))
    return result


def _read_signed(path, key):
    raw = path.read_bytes()
    value = json.loads(raw.decode('utf-8'))
    if raw != canonical(value):
        raise ValueError(f'Archive metadata byte encoding changed: {path.name}')
    expected = value.get(key)
    payload = {k: v for k, v in value.items() if k != key}
    if not isinstance(expected, str) or digest(canonical(payload)) != expected:
        raise ValueError(f'Archive metadata checksum mismatch: {path.name}')
    return value, digest(raw)


def _root_check(root):
    if root.is_symlink():
        raise ValueError('Archive root must not be a symlink')
    if root.exists():
        unknown = set(p.name for p in root.iterdir()) - ALLOWED_ROOT
        if unknown:
            raise ValueError('Not a dedicated prospective archive: ' + ', '.join(sorted(unknown)))


def initialize(root):
    root = Path(root)
    audit_store(root)
    root.mkdir(parents=True, exist_ok=True)
    write_once(root / 'protocol.json', PROTOCOL_PATH.read_bytes())
    write_once(root / '.gitattributes', b'*.csv -text\n*.bin -text\n')


def _check_records(manifest, capture_dir, protocol):
    universe, dates = set(symbols()), set(protocol['sessions'])
    seen = set()
    for record in manifest.get('records', []):
        code, day = record['symbol'], record['session_date']
        if code not in universe or day not in dates:
            raise ValueError('Capture record is outside the fixed symbols or prospective dates')
        if (code, day) in seen:
            raise ValueError('Duplicate symbol/day record in capture')
        seen.add((code, day))
        if record.get('received_at'):
            if utc(record['received_at']) > utc(manifest['completed_at']):
                raise ValueError('Response received after the capture was sealed')
        relative = record.get('csv_path')
        if record['status'] == 'captured':
            if not relative or relative not in manifest['files']:
                raise ValueError('Captured record has no checksummed derived CSV')
            with (capture_dir / str(_relative(relative))).open(encoding='utf-8-sig', newline='') as f:
                reader = csv.DictReader(f)
                if not {'Datetime', 'Open', 'High', 'Low', 'Close', 'Volume'}.issubset(reader.fieldnames or []):
                    raise ValueError('Derived CSV has no valid OHLCV schema')
                rows = list(reader)
            if not rows:
                raise ValueError('An empty response cannot become primary')
            if record.get('rows') is not None and record['rows'] != len(rows):
                raise ValueError('Derived row count differs from its audit record')
            for row in rows:
                stamp = datetime.fromisoformat(row['Datetime'].replace('Z', '+00:00'))
                if stamp.tzinfo is None or str(stamp.astimezone(ZoneInfo(protocol['timezone'])).date()) != day:
                    raise ValueError('Derived CSV mixes another date into the prospective record')
        elif relative:
            raise ValueError('Failed/empty record must not masquerade as a derived price input')


def audit_store(root):
    """Verify every referenced byte, chain, date and receipt; never repair data."""
    root = Path(root)
    protocol = load_protocol()
    _root_check(root)
    result = {'captures': [], 'receipts': [], 'unreceipted_capture_ids': [],
              'protocol_sha256': digest(PROTOCOL_PATH.read_bytes()), 'empty': True}
    if not (root / 'protocol.json').exists():
        if any((root / folder).exists() and any((root / folder).iterdir())
               for folder in ('captures', 'receipts')):
            raise ValueError('Archive data exists without its registration protocol')
        return result
    if (root / 'protocol.json').read_bytes() != PROTOCOL_PATH.read_bytes():
        raise ValueError('Archive protocol differs from the registered prospective experiment')
    manifests = {}
    for folder in sorted((root / 'captures').glob('*')) if (root / 'captures').exists() else []:
        if not folder.is_dir() or folder.is_symlink():
            raise ValueError('Unexpected capture path')
        _component(folder.name)
        manifest_path = folder / 'manifest.json'
        if not manifest_path.is_file():
            raise ValueError('Unsealed capture directory found')
        value, checksum = _read_signed(manifest_path, 'manifest_payload_sha256')
        if value['capture_id'] != folder.name or value['experiment_id'] != protocol['experiment_id']:
            raise ValueError('Capture identity mismatch')
        if value['protocol_sha256'] != result['protocol_sha256']:
            raise ValueError('Capture used a different protocol')
        if utc(value['completed_at']) < utc(value['started_at']):
            raise ValueError('Capture chronology is invalid')
        if set(value['requested_sessions']) - set(protocol['sessions']):
            raise ValueError('Capture requested an unregistered session')
        expected = {'manifest.json'}
        for relative, record in value['files'].items():
            path = folder / str(_relative(relative))
            if not path.is_file() or path.is_symlink():
                raise ValueError('Missing capture file or symlink')
            raw = path.read_bytes()
            if digest(raw) != record['sha256'] or len(raw) != record['bytes']:
                raise ValueError(f'Capture file checksum mismatch: {relative}')
            expected.add(relative)
        actual = {str(p.relative_to(folder)) for p in folder.rglob('*') if p.is_file()}
        if actual != expected or any(p.is_symlink() for p in folder.rglob('*')):
            raise ValueError('Unreferenced or missing capture files')
        _check_records(value, folder, protocol)
        value.update(manifest_sha256=checksum, capture_dir=str(folder.resolve()))
        manifests[checksum] = value
    previous = None
    remaining = dict(manifests)
    while remaining:
        successors = [(key, value) for key, value in remaining.items()
                      if value.get('previous_capture_sha256') == previous]
        if len(successors) != 1:
            raise ValueError('Capture chain is broken, forked or reordered')
        checksum, value = successors[0]
        result['captures'].append(value)
        del remaining[checksum]
        previous = checksum
    by_id = {r['capture_id']: r for r in result['captures']}
    receipted = set()
    receipt_root = root / 'receipts'
    for path in sorted(receipt_root.glob('*')) if receipt_root.exists() else []:
        if not path.is_file() or path.is_symlink() or path.suffix != '.json':
            raise ValueError('Unexpected receipt path')
        receipt, checksum = _read_signed(path, 'receipt_payload_sha256')
        if receipt['experiment_id'] != protocol['experiment_id'] or receipt['protocol_sha256'] != result['protocol_sha256']:
            raise ValueError('Receipt protocol mismatch')
        if not re.fullmatch(r'[0-9a-f]{40,64}', receipt['publication_commit']):
            raise ValueError('Receipt has no publication commit')
        if receipt['capture_ids'] != [r['capture_id'] for r in receipt['captures']]:
            raise ValueError('Receipt capture list differs')
        for item in receipt['captures']:
            capture = by_id.get(item['capture_id'])
            if capture is None or capture['manifest_sha256'] != item['manifest_sha256']:
                raise ValueError('Receipt references missing or changed capture')
            if utc(receipt['published_at']) < utc(capture['completed_at']):
                raise ValueError('Publication receipt predates capture completion')
            receipted.add(item['capture_id'])
        receipt.update(receipt_sha256=checksum, receipt_path=str(path.resolve()))
        result['receipts'].append(receipt)
    result['unreceipted_capture_ids'] = [key for key in by_id if key not in receipted]
    result['empty'] = not result['captures']
    return result


def seal_capture(root, capture_id, payload, files):
    """Atomically append one complete capture; all originals stay unchanged."""
    root = Path(root)
    _component(capture_id)
    before = audit_store(root)
    target = root / 'captures' / capture_id
    if target.exists():
        raise ValueError('Capture ID already exists; use a new run/attempt identifier')
    initialize(root)
    protocol = load_protocol()
    manifest = dict(payload)
    manifest['sealed_with_implementation'] = implementation_info()
    manifest.update(schema_version=1, experiment_id=protocol['experiment_id'],
                    protocol_sha256=before['protocol_sha256'], capture_id=capture_id,
                    previous_capture_sha256=(before['captures'][-1]['manifest_sha256']
                                             if before['captures'] else None))
    manifest.setdefault('started_at', utc().isoformat())
    manifest.setdefault('completed_at', utc().isoformat())
    manifest.setdefault('requested_sessions', sorted({r['session_date'] for r in manifest['records']}))
    if utc(manifest['completed_at']) < utc(manifest['started_at']):
        raise ValueError('Capture completion predates its start')
    if set(manifest['requested_sessions']) - set(protocol['sessions']):
        raise ValueError('An unregistered session cannot be sealed')
    manifest['files'] = {}
    for relative, raw in files.items():
        _relative(relative)
        if relative == 'manifest.json' or not isinstance(raw, bytes):
            raise ValueError('Capture files must be original bytes with distinct relative names')
        manifest['files'][relative] = {'sha256': digest(raw), 'bytes': len(raw)}
    manifest = _signed(manifest, 'manifest_payload_sha256')
    # Temporary files are outside the archive, so a killed build is not a
    # half-valid primary input. Normal captured failures are sealed and kept.
    with tempfile.TemporaryDirectory(prefix='forward-seal-', dir=root.parent) as tmp:
        staging = Path(tmp) / capture_id
        staging.mkdir()
        for relative, raw in files.items():
            write_once(staging / relative, raw)
        write_once(staging / 'manifest.json', canonical(manifest))
        _check_records(manifest, staging, protocol)
        (root / 'captures').mkdir(exist_ok=True)
        staging.rename(target)
    after = audit_store(root)
    return next(r for r in after['captures'] if r['capture_id'] == capture_id)


def add_receipt(root, capture_ids, publication_commit, published_at, workflow_context=None):
    """Append an acknowledgement only AFTER the Git publisher's first push."""
    root = Path(root)
    audit = audit_store(root)
    captures = {r['capture_id']: r for r in audit['captures']}
    capture_ids = list(dict.fromkeys(capture_ids))
    if not capture_ids:
        raise ValueError('A publication receipt must name a capture')
    if not re.fullmatch(r'[0-9a-f]{40,64}', publication_commit):
        raise ValueError('Receipt requires the acknowledged publication commit')
    if any(key not in captures for key in capture_ids):
        raise ValueError('Receipt references an unknown capture')
    if any(utc(published_at) < utc(captures[key]['completed_at']) for key in capture_ids):
        raise ValueError('A receipt cannot predate capture completion')
    payload = {'schema_version': 1, 'experiment_id': load_protocol()['experiment_id'],
               'protocol_sha256': audit['protocol_sha256'], 'capture_ids': capture_ids,
               'captures': [{'capture_id': key, 'manifest_sha256': captures[key]['manifest_sha256']}
                            for key in capture_ids],
               'publication_commit': publication_commit, 'published_at': utc(published_at).isoformat(),
               'workflow_context': workflow_context or {}}
    receipt = _signed(payload, 'receipt_payload_sha256')
    path = root / 'receipts' / (utc(published_at).strftime('%Y%m%dT%H%M%S%fZ') + '_' + uuid.uuid4().hex + '.json')
    write_once(path, canonical(receipt))
    audit_store(root)
    return receipt


def select_primary(root, as_of=None, require_receipt=True):
    """First published usable schema, not the best later coverage or return."""
    protocol = load_protocol()
    as_of = min(utc(as_of), utc(protocol['final_cutoff']))
    audit = audit_store(root)
    publication = {}
    for receipt in audit['receipts']:
        when = utc(receipt['published_at'])
        if when > as_of:
            continue
        for key in receipt['capture_ids']:
            if key not in publication or when < publication[key][0]:
                publication[key] = (when, receipt['publication_commit'])
    candidates, ineligible = [], []
    for sequence, capture in enumerate(audit['captures']):
        published = publication.get(capture['capture_id'])
        if not require_receipt and published is None:
            published = (utc(capture['completed_at']), None)
        for record in capture.get('records', []):
            if record['status'] != 'captured':
                continue
            row = dict(record, capture_id=capture['capture_id'],
                       capture_manifest_sha256=capture['manifest_sha256'],
                       capture_completed_at=capture['completed_at'],
                       received_at=record.get('received_at', capture['completed_at']),
                       published_at=published[0].isoformat() if published else None,
                       publication_commit=published[1] if published else None,
                       csv_path_absolute=str(Path(capture['capture_dir']) / record['csv_path']),
                       csv_sha256=capture['files'][record['csv_path']]['sha256'], sequence=sequence)
            if (published is None or published[0] > as_of
                    or utc(capture['completed_at']) > as_of or utc(row['received_at']) > as_of):
                row['eligibility_reason'] = 'missing_publication_receipt_or_after_cutoff'
                ineligible.append(row)
                continue
            local_published = published[0].astimezone(ZoneInfo(protocol['timezone']))
            row['on_time'] = str(local_published.date()) == record['session_date']
            row['eligibility_reason'] = 'primary_candidate'
            candidates.append(row)
    candidates.sort(key=lambda r: (utc(r['published_at']), r['sequence'], utc(r['received_at']),
                                   r['capture_id'], r['symbol'], r['session_date']))
    chosen = {}
    revisions = []
    for record in candidates:
        key = (record['symbol'], record['session_date'])
        if key not in chosen:
            record['selection_status'] = 'primary'
            chosen[key] = record
        else:
            primary = chosen[key]
            record['selection_status'] = 'same_derived_bytes' if record['csv_sha256'] == primary['csv_sha256'] else 'revised_derived_bytes'
            record['primary_capture_id'] = primary['capture_id']
            revisions.append(record)
    missing = [{'symbol': code, 'session_date': day, 'selection_status': 'missing'}
               for code in symbols() for day in protocol['sessions'] if (code, day) not in chosen]
    return {'selected': list(chosen.values()), 'revisions': revisions,
            'ineligible': ineligible, 'missing': missing, 'as_of': as_of.isoformat(),
            'capture_count': len(audit['captures']), 'protocol_sha256': audit['protocol_sha256']}
