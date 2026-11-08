"""Pre-registered prospective experiment; no runtime threshold overrides."""
import csv
import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


PACKAGE = Path(__file__).resolve().parent
PROTOCOL_PATH = PACKAGE / 'protocol.json'
DATA_BRANCH = 'data/forward-event-study-v1'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def utc(value=None):
    value = value or datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('An explicit timezone is required')
    return value.astimezone(timezone.utc)


def load_protocol():
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding='utf-8'))
    if protocol['experiment_id'] != 'forward-short-events-20261109-v1':
        raise ValueError('Unexpected prospective experiment')
    if protocol['data_branch'] != DATA_BRANCH:
        raise ValueError('Prospective data branch must not be changed')
    for relative, expected in protocol['frozen_files'].items():
        if digest((PACKAGE / relative).read_bytes()) != expected:
            raise ValueError(f'Frozen input changed: {relative}')
    dates = protocol['sessions']
    if len(dates) != 20 or len(set(dates)) != 20 or sorted(dates) != dates:
        raise ValueError('Expected the pre-registered twenty ordered sessions')
    if dates[0] != '2026-11-09' or dates[-1] != '2026-12-07' or '2026-11-23' in dates:
        raise ValueError('Incorrect TSE first evaluation period')
    if utc(protocol['registered_at']) >= utc(protocol['start_date'] + 'T09:00:00+09:00'):
        raise ValueError('This protocol was not registered before the first session')
    return protocol


def symbols():
    load_protocol()
    with (PACKAGE / 'frozen/watchlist.csv').open(encoding='utf-8-sig', newline='') as handle:
        codes = [row['コード'] for row in csv.DictReader(handle)]
    if len(codes) != 50 or len(set(codes)) != 50:
        raise ValueError('The fixed fifty-symbol universe is incomplete')
    return codes


def verify_registration():
    """Verify the prepared code lock, independent of Git's mutable branch names."""
    load_protocol()
    lock = json.loads((PACKAGE / 'registration_lock.json').read_text())
    if utc(lock['registered_at']) >= utc(load_protocol()['start_date'] + 'T09:00:00+09:00'):
        raise ValueError('Acquisition code was not frozen before the first session')
    for relative, expected in lock['files'].items():
        if digest((PACKAGE / relative).read_bytes()) != expected:
            raise ValueError(f'Registered acquisition/evaluation code changed: {relative}')
    return lock


def implementation_info():
    """Code/runtime provenance at sealing, with no environment credential values."""
    result = {'python': sys.version, 'platform': platform.platform(), 'packages': {},
              'module_sha256': {p.name: digest(p.read_bytes()) for p in sorted(PACKAGE.glob('*.py'))}}
    for name in ('numpy', 'pandas', 'yfinance', 'curl_cffi'):
        try:
            result['packages'][name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result['packages'][name] = None
    try:
        result['code_commit'] = subprocess.check_output(
            ['git', '-C', str(PACKAGE.parents[1]), 'rev-parse', 'HEAD'], stderr=subprocess.DEVNULL).decode().strip()
    except (OSError, subprocess.CalledProcessError):
        result['code_commit'] = None
    lock = PACKAGE / 'registration_lock.json'
    result['registration_lock_sha256'] = digest(lock.read_bytes()) if lock.exists() else None
    return result
