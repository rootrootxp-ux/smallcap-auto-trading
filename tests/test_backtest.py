import csv
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from scripts.check_results import validate

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('backtest', ROOT / 'smallcap_auto_v3.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


def prices():
    frames = []
    for day in pd.bdate_range('2026-09-01', periods=10):
        date = str(day.date())
        index = pd.date_range(date+' 09:00', date+' 11:30', freq='5min', tz='Asia/Tokyo').append(
            pd.date_range(date+' 12:30', date+' 15:30', freq='5min', tz='Asia/Tokyo'))
        x = np.arange(len(index))
        close = 1000 + 10*np.sin(x*1.3) + x*.3
        frames.append(pd.DataFrame(dict(Open=close-1, High=close+3, Low=close-3,
                                       Close=close, Volume=np.where(x % 4 == 0, 10000, 1000)), index=index))
    return pd.concat(frames)


class BacktestTests(unittest.TestCase):
    def test_quality_checks(self):
        day = prices().iloc[:68]
        self.assertEqual(app.quality_check(day), '')
        self.assertIn('本数不足', app.quality_check(day.iloc[:5]))
        self.assertIn('欠損', app.quality_check(day.drop(day.index[10:13])))

    def test_strategies_and_accounting(self):
        count = 0
        for strategy in ('MAのみ', 'VWAP+出来高', '高値ブレイク+出来高'):
            for slip in app.COST_PER_SHARE:
                with self.subTest(strategy=strategy, slip=slip):
                    summary, trades = app.simulate(prices(), strategy, slip, 'TEST', '合成')
                    self.assertEqual(summary['スキップ日数'], 0)
                    self.assertAlmostEqual(summary['損益(円)'], sum(t['損益(円)'] for t in trades), delta=1)
                    for trade in trades:
                        self.assertEqual(trade['数量(株)'] % 100, 0)
                        self.assertLessEqual(trade['数量(株)'] * trade['買値(円)'], app.MAX_ORDER_YEN)
                        self.assertLess(trade['買い日時'], trade['売り日時'])
                    count += len(trades)
        self.assertGreater(count, 0)

    def test_complete_report_and_failed_download(self):
        intraday = prices()
        daily = pd.DataFrame(dict(Open=1000., High=1030., Low=970., Close=1000., Volume=200000.),
                             index=pd.bdate_range('2026-08-01', periods=60))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            watch = out / 'watchlist.csv'
            watch.write_text('コード,候補名\nTEST,合成データ\n', encoding='utf-8')
            def fetch(code, interval, period):
                return daily.copy() if interval == '1d' else intraday.copy()
            with patch.object(app, 'OUT', out), patch.object(app, 'INPUT', watch), \
                 patch.object(app.time, 'sleep'), patch.object(app, 'fetch', side_effect=fetch):
                app.main()
                validate(out)
                self.assertIn('小型株 自動分析レポート', (out/'report.html').read_text())
                with (out/'trade_history.csv').open(encoding='utf-8-sig') as f:
                    self.assertGreater(len(list(csv.DictReader(f))), 0)
            with patch.object(app, 'OUT', out), patch.object(app, 'INPUT', watch), \
                 patch.object(app.time, 'sleep'), patch.object(app, 'fetch', return_value=pd.DataFrame()):
                app.main()
                with self.assertRaisesRegex(ValueError, 'incomplete'):
                    validate(out)


if __name__ == '__main__':
    unittest.main()
