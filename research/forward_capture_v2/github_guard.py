"""Fail closed when a previous acquisition may have lost its raw evidence.

This guard only reads GitHub Actions metadata and the local immutable archive.
It never contacts a market-data provider or downloads credentials/artifacts.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen


WORKFLOW_FILE = 'forward-event-study.yml'
CAPTURE_STEP = 'Capture raw responses without evaluating returns'
PROBE_STEP = 'Single raw-only Yahoo probe (no primary CSV)'
PUBLICATION_STEP = 'Publish new immutable files and acknowledged publication receipts'
_NETWORK_STEPS = frozenset({CAPTURE_STEP, PROBE_STEP})
_MAX_PAGES = 20


class MetadataUnavailable(RuntimeError):
    """Metadata was unavailable or insufficient to prove safe acquisition."""


def _default_api():
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not token:
        raise MetadataUnavailable('GitHub read token is not configured')

    def request(path):
        if not path.startswith('/repos/') or '://' in path:
            raise MetadataUnavailable('Unexpected GitHub metadata endpoint')
        req = Request('https://api.github.com' + path, headers={
            'Authorization': 'Bearer ' + token,
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'smallcap-forward-publication-audit',
        }, method='GET')
        with urlopen(req, timeout=20) as response:
            result = json.loads(response.read())
        if not isinstance(result, dict):
            raise MetadataUnavailable('Invalid GitHub metadata payload')
        return result
    return request


def _pages(api, path, key):
    records = []
    for page in range(1, _MAX_PAGES + 1):
        result = api(path + ('&' if '?' in path else '?') + f'per_page=100&page={page}')
        if not isinstance(result, dict) or not isinstance(result.get(key), list):
            raise MetadataUnavailable('Missing paginated GitHub metadata')
        rows = result[key]
        if not all(isinstance(row, dict) for row in rows):
            raise MetadataUnavailable('Malformed GitHub metadata record')
        records.extend(rows)
        total = result.get('total_count')
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise MetadataUnavailable('Invalid GitHub pagination count')
        if len(rows) < 100:
            if total is not None and len(records) < total:
                raise MetadataUnavailable('Truncated GitHub metadata')
            return records
        if total is not None and len(records) >= total:
            return records
    raise MetadataUnavailable('GitHub history exceeds the audited pagination bound')


def _utc(value):
    if not isinstance(value, str):
        raise MetadataUnavailable('Missing run creation timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise MetadataUnavailable('Run creation timestamp lacks timezone')
    return parsed.astimezone(timezone.utc)


def _ran(step):
    return (step.get('status') in {'in_progress', 'completed'}
            and step.get('conclusion') != 'skipped')


def _repaired(receipts, run_id, attempt):
    for receipt in receipts:
        context = receipt.get('workflow_context', {})
        if (isinstance(context, dict)
                and str(context.get('GITHUB_RUN_ID', '')) == run_id
                and str(context.get('GITHUB_RUN_ATTEMPT', '')) == str(attempt)):
            return True
    return False


def guard_previous_publications(store: Path, repository: str, current_run_id: str,
                                current_run_attempt: int = 1, api=None) -> dict:
    """Check prior attempts, including earlier attempts of the current run ID.

``api`` is a GET-only callable taking a GitHub API path; tests inject an
offline fake. A repaired receipt must identify both the original run and its
attempt, so one successful attempt cannot conceal another attempt's lost 429.
"""
    result = {'allowed': False, 'reason': 'not_checked', 'blocked_runs': [],
              'checked_attempts': [], 'repaired_attempts': [],
              'github_requests': 0, 'provider_requests': 0,
              'evidence_version': 'capture-transport-v2',
              'checked_at': datetime.now(timezone.utc).isoformat(),
              'repository': repository, 'current_run_id': str(current_run_id),
              'current_run_attempt': current_run_attempt}
    try:
        if not isinstance(repository, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise MetadataUnavailable('Invalid repository identifier')
        current_run_id = str(current_run_id)
        if not re.fullmatch(r'[1-9][0-9]*', current_run_id):
            raise MetadataUnavailable('Invalid current run ID')
        if isinstance(current_run_attempt, bool) or not isinstance(current_run_attempt, int) or current_run_attempt < 1:
            raise MetadataUnavailable('Invalid current run attempt')
        from research.forward_study.store import audit_store
        try:
            audit = audit_store(Path(store))
        except Exception as error:
            result.update(reason='archive_audit_failed', error_type=type(error).__name__)
            return result
        receipts = audit['receipts']
        raw_api = api or _default_api()

        def counted_api(path):
            result['github_requests'] += 1
            return raw_api(path)

        base = '/repos/' + repository + '/actions'
        current = counted_api(base + '/runs/' + current_run_id)
        if str(current.get('id')) != current_run_id:
            raise MetadataUnavailable('Current run identity mismatch')
        if current.get('run_attempt', current_run_attempt) != current_run_attempt:
            raise MetadataUnavailable('Current run attempt metadata does not match the workflow')
        created = _utc(current.get('created_at'))
        history = _pages(counted_api, base + '/workflows/' + WORKFLOW_FILE + '/runs', 'workflow_runs')
        attempts = []
        # Never ignore a previous failed attempt merely because GitHub now
        # reports this same run ID as the currently executing rerun.
        attempts.extend((current_run_id, n) for n in range(1, current_run_attempt))
        for run in history:
            run_id = str(run.get('id', ''))
            if not re.fullmatch(r'[1-9][0-9]*', run_id):
                raise MetadataUnavailable('Invalid historical run ID')
            if run_id == current_run_id or _utc(run.get('created_at')) > created:
                continue
            attempt = run.get('run_attempt', 1)
            if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
                raise MetadataUnavailable('Invalid historical run attempt')
            # A first-attempt success has already passed the workflow's final
            # publication checks. Reruns still require their earlier attempts.
            if run.get('conclusion') == 'success' and attempt == 1:
                continue
            attempts.extend((run_id, n) for n in range(1, attempt + 1))
        for run_id, attempt in sorted(set(attempts), key=lambda item: (int(item[0]), item[1])):
            jobs = _pages(counted_api, base + f'/runs/{run_id}/attempts/{attempt}/jobs', 'jobs')
            result['checked_attempts'].append({'run_id': run_id, 'run_attempt': attempt})
            for job in jobs:
                steps = job.get('steps')
                if not isinstance(steps, list) or not all(isinstance(step, dict) for step in steps):
                    raise MetadataUnavailable('Missing historical step metadata')
                network = [step for step in steps if step.get('name') in _NETWORK_STEPS and _ran(step)]
                if not network:
                    # A non-skipped acquisition job whose step layout changed
                    # cannot silently bypass this guard.
                    if (job.get('name') in {'collect', 'probe'} and job.get('conclusion') != 'skipped'
                            and not any(step.get('name') in _NETWORK_STEPS for step in steps)):
                        raise MetadataUnavailable('Unknown acquisition step layout')
                    continue
                publications = [step for step in steps if step.get('name') == PUBLICATION_STEP]
                published = (len(publications) == 1 and publications[0].get('status') == 'completed'
                             and publications[0].get('conclusion') == 'success')
                if published:
                    continue
                if _repaired(receipts, run_id, attempt):
                    result['repaired_attempts'].append({'run_id': run_id, 'run_attempt': attempt})
                    continue
                result['blocked_runs'].append({
                    'run_id': run_id, 'run_attempt': attempt,
                    'job_id': job.get('id'), 'job_name': job.get('name'),
                    'network_steps': [step['name'] for step in network],
                    'publication_conclusions': [step.get('conclusion') for step in publications],
                    'reason': 'capture_or_probe_without_successful_publication_or_repair_receipt',
                })
        result['allowed'] = not result['blocked_runs']
        result['reason'] = ('previous_publications_verified' if result['allowed']
                            else 'unpublished_capture_or_probe')
        return result
    except Exception as error:
        # Do not echo API error bodies, URLs or headers, which can contain
        # credentials or operational details. Unavailability means no quotes.
        result.update(allowed=False, reason='github_metadata_unavailable',
                      error_type=type(error).__name__)
        return result
