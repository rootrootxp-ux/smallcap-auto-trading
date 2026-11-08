"""Collect public observations or audit the pre-registered future experiment."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import uuid
from zoneinfo import ZoneInfo

from .protocol import load_protocol, symbols, utc, verify_registration
from .store import audit_store, canonical, select_primary, write_once


def _output(path, stem):
    path = Path(path) / (stem + '_' + utc().strftime('%Y%m%dT%H%M%S%fZ') + '_' + uuid.uuid4().hex[:8])
    path.mkdir(parents=True, exist_ok=False)
    return path


def preflight(store, output, now=None):
    """Validate readiness and coverage only; never fetch or calculate returns."""
    lock = verify_registration()
    protocol = load_protocol()
    now = utc(now)
    archive = audit_store(store)
    selection = select_primary(store, as_of=now)
    local = now.astimezone(ZoneInfo(protocol['timezone']))
    elapsed = [day for day in protocol['sessions']
               if now >= utc(f'{day}T{protocol["daily_audit_cutoff_local"]}:00+09:00')]
    status = ('not_started' if str(local.date()) < protocol['start_date']
              else 'evaluation_due' if now >= utc(protocol['final_cutoff']) else 'interim_audit')
    result = {'command': 'preflight', 'status': status, 'observed_at': now.isoformat(),
              'experiment_id': protocol['experiment_id'], 'start_date': protocol['start_date'],
              'end_date': protocol['end_date'], 'planned_sessions': protocol['sessions'],
              'elapsed_sessions': len(elapsed), 'expected_symbols': len(symbols()),
              'expected_symbol_days': 1000, 'due_symbol_days': len(elapsed) * 50,
              'captured_primary_symbol_days': len(selection['selected']),
              'missing_due_symbol_days': sum(r['session_date'] in elapsed for r in selection['missing']),
              'captures': len(archive['captures']), 'receipts': len(archive['receipts']),
              'protocol_sha256': archive['protocol_sha256'], 'registration_lock': lock,
              'network_requests': 0, 'event_outcomes_computed': False,
              'scheduler_requires_reviewed_default_branch_workflow': True,
              'brokerage_connections': False, 'orders': False}
    out = _output(output, 'preflight')
    write_once(out / 'preflight.json', canonical(result))
    note = (f'状態: {status}。予定20営業日、固定50銘柄。'
            f'経過営業日 {len(elapsed)}、監査済み初回データ {len(selection["selected"])}銘柄日。'
            '開始前・途中の成績や勝者は計算しません。実データを合成したものではありません。'
            '定期取得はレビュー後にworkflowがmainへ反映されるまで有効になりません。')
    import html
    write_once(out / 'report.html', ('<!doctype html><html lang="ja"><meta charset="utf-8">'
                                   '<title>将来イベント研究・取得前監査</title><h1>将来イベント研究・監査</h1><p>'
                                   + html.escape(note) + '</p></html>').encode())
    result['output_dir'] = str(out)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('preflight', 'collect', 'audit', 'evaluate'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--store', type=Path, default=Path('results/forward-data'))
        cmd.add_argument('--output', type=Path, default=Path('results/forward-study'))
        if name == 'collect':
            cmd.add_argument('--date', action='append', dest='dates', help='固定期間内の取得対象日。将来・場中は拒否')
        if name == 'evaluate':
            cmd.add_argument('--allow-interim', action='store_true', help='途中分析を明示的に参考資料として出力')
    args = parser.parse_args(argv)
    verify_registration()
    if args.command == 'preflight':
        result = preflight(args.store, args.output)
    elif args.command == 'collect':
        from .collector import collect
        result = collect(args.store, session_dates=args.dates)
        out = _output(args.output, 'collection')
        write_once(out / 'collection.json', canonical(result))
        result['output_dir'] = str(out)
    elif args.command == 'audit':
        result = audit_store(args.store)
        out = _output(args.output, 'audit')
        write_once(out / 'audit.json', canonical(result))
        result = {'command': 'audit', 'status': 'verified', 'output_dir': str(out),
                  'captures': len(result['captures']), 'receipts': len(result['receipts'])}
    else:
        from .evaluate import evaluate
        result = evaluate(args.store, args.output, allow_interim=args.allow_interim)
    summary = {key: result.get(key) for key in ('status', 'output_dir', 'capture_id') if key in result}
    summary['command'] = args.command
    print(json.dumps(summary, ensure_ascii=False))
    return 1 if result.get('status') in {'failed', 'final_incomplete'} else 0


if __name__ == '__main__':
    raise SystemExit(main())
