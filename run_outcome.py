"""Run-level coverage policy; independent of trading performance or trade counts."""
from collections import Counter
from math import ceil

MIN_VALID_FRACTION = 0.5
MIN_QUALIFIED_DAYS = 8
STATUSES = {'検証可能', 'データ不足', '取得失敗', '重大不具合'}


def summarize(status_rows):
    counts = Counter(row['状態'] for row in status_rows)
    if set(counts) - STATUSES:
        raise ValueError('Unknown symbol status')
    total = len(status_rows)
    required = max(1, ceil(total * MIN_VALID_FRACTION))
    valid = counts['検証可能']
    result = ('失敗' if counts['重大不具合'] or valid < required else
              '完全成功' if valid == total else '一部成功')
    return {'全体結果': result, '対象銘柄数': total, '検証可能銘柄数': valid,
            'データ不足銘柄数': counts['データ不足'], '取得失敗銘柄数': counts['取得失敗'],
            '重大不具合銘柄数': counts['重大不具合'], '最低必要銘柄数': required,
            '最低品質合格日数': MIN_QUALIFIED_DAYS,
            '判定基準': '重大不具合なし・各銘柄8日以上・対象の50%以上（切上げ、最低1銘柄）。取引0回だけでは失敗にしない'}
