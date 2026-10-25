"""Reject empty or incomplete backtests, even when the application exits zero."""
import csv
from pathlib import Path


def validate(root=Path('results')):
    for name in ('report.html', 'screening.csv', 'backtest_summary.csv',
                 'trade_history.csv', 'run_status.csv'):
        if not (root / name).is_file() or (root / name).stat().st_size == 0:
            raise ValueError(f'Missing or empty output: {name}')
    with (root / 'run_status.csv').open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    failed = [row for row in rows if row['状態'] != '成功']
    if not rows or failed:
        details = '; '.join(f"{row['コード']}: {row.get('詳細', row['状態'])}"
                            for row in failed)
        raise ValueError(f'Data retrieval incomplete ({len(rows) - len(failed)}/{len(rows)} successful); '
                         f'{details or "no status rows"}; inspect run_status.csv')
    with (root / 'backtest_summary.csv').open(encoding='utf-8-sig', newline='') as f:
        summaries = list(csv.DictReader(f))
    if len(summaries) != 12 * len(rows):
        raise ValueError('Expected twelve strategy/slippage/cost summaries per symbol')
    print(f'Validated {len(rows)} symbols and {len(summaries)} summaries')


if __name__ == '__main__':
    validate()
