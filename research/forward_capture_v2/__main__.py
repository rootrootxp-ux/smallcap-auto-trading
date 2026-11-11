"""Bounded public-price diagnostics and paced acquisition, never orders."""
import argparse
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import uuid

from research.forward_study.protocol import load_protocol, utc
from research.forward_study.store import audit_store, canonical, write_once

from .policy import load_policy, provenance, verify_acquisition_registration
from .rate_limit import state_from_archive


def _out(path, command):
    root = Path(path) / (command + '_' + utc().strftime('%Y%m%dT%H%M%S%fZ') + '_' + uuid.uuid4().hex[:8])
    root.mkdir(parents=True, exist_ok=False)
    return root


def _guard(path):
    evidence = json.loads(Path(path).read_text())
    if evidence.get('allowed') is not True or evidence.get('provider_requests') != 0:
        raise ValueError('Prior publication safety was not verified; no Yahoo request is permitted')
    if not isinstance(evidence.get('checked_at'), str):
        raise ValueError('Publication guard has no verification timestamp')
    checked = utc(evidence['checked_at'])
    age = (utc() - checked).total_seconds()
    if not 0 <= age <= 600:
        raise ValueError('Publication guard is stale; refresh it without contacting Yahoo')
    for environment, key in [('GITHUB_REPOSITORY', 'repository'), ('GITHUB_RUN_ID', 'current_run_id'),
                             ('GITHUB_RUN_ATTEMPT', 'current_run_attempt')]:
        expected = os.environ.get(environment)
        if expected and str(evidence.get(key)) != expected:
            raise ValueError('Publication guard belongs to another workflow execution')
    return evidence


def preflight(store, output):
    verify_acquisition_registration()
    policy = load_policy()
    audit = audit_store(store)
    state = state_from_archive(audit, policy)
    now = utc()
    result = {'status': 'verified_no_requests', 'observed_at': now.isoformat(),
              'network_requests': 0, 'provider_available': 'not_tested',
              'legacy_protocol_sha256': policy['legacy_research_protocol_sha256'],
              'legacy_captures': len(audit['captures']), 'rate_limit': state,
              'cooldown_active': bool(state['blocked_until'] and now < utc(state['blocked_until'])),
              'acquisition_implementation': provenance(), 'research_conditions_changed': False,
              'orders': False, 'brokerage_connections': False,
              'automatic_collection_requires_reviewed_main_and_workflow_reenable': True}
    out = _out(output, 'preflight')
    write_once(out/'preflight.json', canonical(result))
    return dict(result, output_dir=str(out))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('preflight', 'collect', 'probe'):
        command = commands.add_parser(name)
        command.add_argument('--store', type=Path, default=Path('results/forward-data'))
        command.add_argument('--output', type=Path, default=Path('results/capture-v2'))
        if name != 'preflight':
            command.add_argument('--date', dest='dates', action='append')
            command.add_argument('--guard-report', type=Path, required=True,
                                 help='Fresh allowed GitHub publication guard; checked before any provider request')
    args = parser.parse_args(argv)
    verify_acquisition_registration()
    if args.command == 'preflight':
        result = preflight(args.store, args.output)
    else:
        guard = _guard(args.guard_report)
        from .collector import collect
        result = collect(args.store, session_dates=args.dates, probe=args.command == 'probe', guard_evidence=guard)
        out = _out(args.output, args.command)
        write_once(out/'capture.json', canonical(result))
        rate = result.get('rate_limit', {})
        result = dict(result, output_dir=str(out))
        note = ('最大1通信の診断で、研究の価格入力には採用しません。' if args.command == 'probe'
                else '取得方式v2。研究条件と既存の欠測記録は維持しています。')
        message = f"状態: {result.get('status')} / probe結果: {result.get('probe_result', '')} / 通信試行: {rate.get('actual_requests', 0)} / 再開可能下限: {rate.get('blocked_until')}。{note}"
        write_once(out/'report.html', ('<!doctype html><html lang="ja"><meta charset="utf-8"><title>Yahoo取得診断</title><p>'
                                     + html.escape(message) + '</p></html>').encode())
    print(json.dumps({key: result.get(key) for key in ('status', 'probe_result', 'capture_id', 'output_dir') if key in result}, ensure_ascii=False))
    # A completed negative probe is a diagnostic result, not a trading success.
    return 1 if result.get('status') == 'failed' else 0


if __name__ == '__main__':
    raise SystemExit(main())
