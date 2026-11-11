"""Post-launch acquisition policy, separate from the pre-registered study."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess

from research.forward_study import protocol as legacy


PACKAGE = Path(__file__).resolve().parent
POLICY_PATH = PACKAGE / 'acquisition_policy.json'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def load_policy():
    policy = json.loads(POLICY_PATH.read_text(encoding='utf-8'))
    if policy['implementation_id'] != 'yahoo-chart-paced-v2' or policy['version'] != 2:
        raise ValueError('Unexpected acquisition implementation')
    if policy['data_source_id'] != 'yahoo-chart-v8' or policy['endpoint'] != 'https://query1.finance.yahoo.com/v8/finance/chart/':
        raise ValueError('A provider change requires a separately reviewed acquisition version')
    if sha(legacy.PROTOCOL_PATH.read_bytes()) != policy['legacy_research_protocol_sha256']:
        raise ValueError('The pre-registered research protocol changed')
    if policy['minimum_interval_seconds'] < 5 or policy['fallback_cooldown_seconds'] < 3600:
        raise ValueError('The registered v2 traffic guard was weakened')
    return policy


def verify_acquisition_registration():
    """Retain original research registration; verify the later transport lock."""
    legacy.verify_registration()
    load_policy()
    lock = json.loads((PACKAGE / 'version_lock.json').read_text())
    for relative, expected in lock['files'].items():
        if sha((PACKAGE / relative).read_bytes()) != expected:
            raise ValueError(f'Acquisition v2 file changed: {relative}')
    return lock


def provenance():
    policy = load_policy()
    lock = PACKAGE / 'version_lock.json'
    result = {'implementation_id': policy['implementation_id'], 'version': 2,
              'data_source_id': policy['data_source_id'], 'effective_at': policy['effective_at'],
              'post_launch_acquisition_change': True, 'research_conditions_changed': False,
              'policy_sha256': sha(POLICY_PATH.read_bytes()),
              'version_lock_sha256': sha(lock.read_bytes()) if lock.exists() else None,
              'module_sha256': {p.name: sha(p.read_bytes()) for p in sorted(PACKAGE.glob('*.py'))},
              'legacy_protocol_sha256': sha(legacy.PROTOCOL_PATH.read_bytes()),
              'legacy_registration_sha256': sha((legacy.PACKAGE / 'registration_lock.json').read_bytes())}
    try:
        result['code_commit'] = subprocess.check_output(
            ['git', '-C', str(PACKAGE.parents[1]), 'rev-parse', 'HEAD'], stderr=subprocess.DEVNULL).decode().strip()
    except (OSError, subprocess.CalledProcessError):
        result['code_commit'] = None
    return result
