"""Append-only Git publication for the separate forward-observation data branch.

No credentials are copied: linked worktrees inherit the original checkout's
remote configuration and Actions authentication.  Every push names one fixed
data ref explicitly and never uses force.
"""
from __future__ import annotations

import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


DATA_BRANCH = 'data/forward-event-study-v1'
DATA_REF = 'refs/heads/' + DATA_BRANCH
_LOCAL_BRANCH_PREFIX = 'forward-data-run-'


class GitStoreError(RuntimeError):
    """A safe error message that never includes remote URLs or credentials."""

    def __init__(self, message, *, phase=None, publication_commit=None):
        super().__init__(message)
        self.phase = phase
        self.publication_commit = publication_commit


def _git(root, *args, input_bytes=None, allowed=(0,)):
    result = subprocess.run(
        ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.autocrlf=false', *args], cwd=Path(root),
        input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode not in allowed:
        # Git's stderr can contain authenticated remote URLs. Do not echo it.
        raise GitStoreError(f'Git {args[0]} failed (exit {result.returncode}); '
                            'remote data was not overwritten.')
    return result


def _head(root):
    result = _git(root, 'rev-parse', '--verify', 'HEAD', allowed=(0, 128))
    return result.stdout.decode().strip() if result.returncode == 0 else None


def _allowed_path(name):
    path = PurePosixPath(name)
    return (not path.is_absolute() and '..' not in path.parts
            and (name in {'protocol.json', '.gitattributes'}
                 or len(path.parts) >= 2 and path.parts[0] in {'captures', 'receipts'})
            and '.git' not in path.parts)


def _tree(root, commit):
    if commit is None:
        return {}
    result = {}
    for entry in _git(root, 'ls-tree', '-rz', commit).stdout.split(b'\0'):
        if not entry:
            continue
        meta, raw_name = entry.split(b'\t', 1)
        mode, kind, oid = meta.decode().split()
        name = raw_name.decode('utf-8')
        if not _allowed_path(name) or kind != 'blob' or mode != '100644':
            raise GitStoreError('Data branch contains an unsupported path or file mode.')
        result[name] = (mode, oid)
    return result


def _audit_append_only(root, parent):
    """Reject edits/deletions in both the index and worktree, including staged edits."""
    root = Path(root)
    before = _tree(root, parent)
    index = {}
    for entry in _git(root, 'ls-files', '--stage', '-z').stdout.split(b'\0'):
        if not entry:
            continue
        meta, raw_name = entry.split(b'\t', 1)
        mode, oid, stage = meta.decode().split()
        name = raw_name.decode('utf-8')
        if stage != '0' or mode != '100644' or not _allowed_path(name):
            raise GitStoreError('Unsupported or unresolved staged data path.')
        index[name] = (mode, oid)
    files = {}
    for path in root.rglob('*'):
        relative = path.relative_to(root).as_posix()
        if relative == '.git' or relative.startswith('.git/'):
            continue
        if path.is_symlink():
            raise GitStoreError('Symlinks are forbidden in the immutable data store.')
        if path.is_dir():
            if relative.split('/')[0] not in {'captures', 'receipts'}:
                raise GitStoreError('Unexpected directory in the data store.')
            continue
        if not path.is_file() or not _allowed_path(relative):
            raise GitStoreError('Unexpected file in the data store.')
        files[relative] = path
    for name, (mode, oid) in before.items():
        if name not in files or index.get(name) != (mode, oid):
            raise GitStoreError('Existing data files cannot be modified or deleted, including staged changes.')
    if not set(index).issubset(files):
        raise GitStoreError('A staged data file is missing from the working tree.')
    for name, (_, oid) in index.items():
        if files[name].read_bytes() != _git(root, 'cat-file', 'blob', oid).stdout:
            raise GitStoreError('Working bytes differ from staged immutable data.')
    return sorted(set(files) - set(before))


def _assert_data_checkout(root):
    root = Path(root)
    branch = _git(root, 'symbolic-ref', '--quiet', '--short', 'HEAD',
                  allowed=(0, 1)).stdout.decode().strip()
    if branch and not branch.startswith(_LOCAL_BRANCH_PREFIX):
        raise GitStoreError('Publication requires a dedicated detached data worktree; code branches are forbidden.')
    if not (root / '.git').is_file():
        raise GitStoreError('Publication requires a linked data worktree.')


def checkout_store(code_repo: Path, destination: Path) -> dict:
    """Create a detached data worktree, or an empty unborn worktree for first use."""
    code_repo, destination = Path(code_repo).resolve(), Path(destination).resolve()
    if destination.exists():
        raise GitStoreError('Data worktree destination must not already exist.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    remote = _git(code_repo, 'ls-remote', '--exit-code', 'origin', DATA_REF,
                  allowed=(0, 2))
    if remote.returncode == 0:
        _git(code_repo, 'fetch', '--no-tags', 'origin', DATA_REF)
        parent = _git(code_repo, 'rev-parse', 'FETCH_HEAD').stdout.decode().strip()
        _git(code_repo, 'worktree', 'add', '--detach', str(destination), parent)
    else:
        _git(code_repo, 'worktree', 'add', '--detach', '--no-checkout', str(destination), 'HEAD')
        _git(destination, 'symbolic-ref', 'HEAD', 'refs/heads/' + _LOCAL_BRANCH_PREFIX + uuid.uuid4().hex)
        _git(destination, 'read-tree', '--empty')
        parent = None
    _assert_data_checkout(destination)
    _audit_append_only(destination, parent)
    return {'data_repo': str(destination), 'data_branch': DATA_BRANCH,
            'parent_commit': parent}


def _audit(root):
    from .store import audit_store
    return audit_store(Path(root))


def _add_receipt(root, capture_ids, **kwargs):
    from .store import add_receipt
    return add_receipt(Path(root), capture_ids, **kwargs)


def _receipted_ids(root):
    result = set()
    for path in sorted((Path(root) / 'receipts').glob('*.json')):
        record = json.loads(path.read_text())
        ids = record.get('capture_ids', [])
        if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
            raise GitStoreError('Invalid receipt capture IDs.')
        result.update(ids)
    return result


def _commit_new_files(root, parent, message):
    added = _audit_append_only(root, parent)
    if not added:
        return _head(root)
    _git(root, 'add', '--all', '--', '.')
    _audit_append_only(root, parent)
    _audit(root)
    _git(root, '-c', 'user.name=Forward research data', '-c',
         'user.email=forward-research@users.noreply.github.com', '-c',
         'commit.gpgsign=false', 'commit', '--no-gpg-sign', '-m', message)
    return _head(root)


def _push(root, *, phase, publication_commit=None):
    try:
        _git(root, 'push', '--porcelain', 'origin', 'HEAD:' + DATA_REF)
    except GitStoreError as error:
        raise GitStoreError('Normal data push failed or was rejected; no force push was used. '
                            'Keep the remote unchanged and rerun to repair any unreceipted capture.',
                            phase=phase, publication_commit=publication_commit) from error
    # Captured only after the remote acknowledged the publication push.
    return datetime.now(timezone.utc)


def _receipt_time(acknowledged_at, now):
    supplied = now() if callable(now) else now
    if supplied is None:
        return acknowledged_at
    if isinstance(supplied, str):
        supplied = datetime.fromisoformat(supplied.replace('Z', '+00:00'))
    if not isinstance(supplied, datetime) or supplied.tzinfo is None:
        raise GitStoreError('Publication time must be timezone-aware UTC.')
    # Tests may supply a clock; it must never backdate a receipt before the ack.
    return max(acknowledged_at, supplied.astimezone(timezone.utc))


def publish_store(data_repo: Path, capture_ids: list[str], now=None) -> dict:
    """Publish captures, then immutable receipts; both pushes are append-only."""
    root = Path(data_repo).resolve()
    _assert_data_checkout(root)
    parent = _head(root)
    _audit_append_only(root, parent)
    _audit(root)
    if not isinstance(capture_ids, list) or not all(isinstance(x, str) for x in capture_ids):
        raise GitStoreError('capture_ids must be a list of strings.')
    requested = sorted(set(capture_ids))
    for identifier in requested:
        if not identifier or PurePosixPath(identifier).name != identifier or identifier in {'.', '..'}:
            raise GitStoreError('Invalid capture ID.')
        if not (root / 'captures' / identifier / 'manifest.json').is_file():
            raise GitStoreError('Requested capture is absent from the audited store.')
    pending = sorted(set(requested) - _receipted_ids(root))
    added = _audit_append_only(root, parent)
    if any(name.startswith('receipts/') for name in added):
        raise GitStoreError('Receipts may only be appended after an acknowledged capture push.')
    if not added and not pending:
        remote = _git(root, 'ls-remote', '--exit-code', 'origin', DATA_REF, allowed=(0, 2))
        remote_head = remote.stdout.decode().split()[0] if remote.returncode == 0 else None
        if parent != remote_head:
            raise GitStoreError('Local receipt history is not the acknowledged remote history. '
                                'Use a fresh data checkout and issue a new repair receipt; never backdate it.')
        return {'status': 'no_changes', 'capture_ids': [],
                'publication_commit': parent, 'receipt_commit': parent,
                'data_branch': DATA_BRANCH}
    if not pending:
        raise GitStoreError('New immutable data requires at least one unreceipted capture.')
    publication_commit = _commit_new_files(root, parent, 'Append forward observation captures')
    acknowledged_at = _push(root, phase='capture', publication_commit=publication_commit)
    timestamp = _receipt_time(acknowledged_at, now)
    import os
    context = {key: os.environ[key] for key in (
        'GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT',
        'GITHUB_WORKFLOW', 'GITHUB_SHA', 'GITHUB_EVENT_NAME') if key in os.environ}
    receipt = _add_receipt(root, pending, publication_commit=publication_commit,
                           published_at=timestamp, workflow_context=context)
    _audit(root)
    receipt_commit = _commit_new_files(root, publication_commit,
                                       'Append acknowledged forward publication receipt')
    if receipt_commit == publication_commit:
        raise GitStoreError('Receipt creation did not append an immutable file.',
                            phase='receipt', publication_commit=publication_commit)
    _push(root, phase='receipt', publication_commit=publication_commit)
    return {'status': 'published', 'capture_ids': pending,
            'publication_commit': publication_commit, 'receipt_commit': receipt_commit,
            'data_branch': DATA_BRANCH, 'published_at': timestamp.isoformat(),
            'receipt': receipt}
