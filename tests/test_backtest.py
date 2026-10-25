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
        noon_gap=day.drop(day.between_time('11:05','12:55').index)
        self.assertIn('欠損',app.quality_check(noon_gap))

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

    def deterministic_day(self):
        day = prices().iloc[:68].copy()
        day.loc[:, 'Close'] = 980.0
        day.iloc[:6, day.columns.get_loc('Close')] = 1000.0
        day.iloc[6:10, day.columns.get_loc('Close')] = [990., 1010., 1011., 980.]
        day['Open'] = day.Close
        day.iloc[8, day.columns.get_loc('Open')] = 1005.
        day.iloc[10, day.columns.get_loc('Open')] = 970.
        day['High'] = day[['Open','Close']].max(axis=1)+1
        day['Low'] = day[['Open','Close']].min(axis=1)-1
        return day

    def test_exact_next_bar_fills_slippage_and_costs(self):
        day = self.deterministic_day()
        cost = app.Costs('仮定', 100., 0., 3.)
        summary, trades = app.simulate(day, 'MAのみ', 1., 'TEST', '合成', cost)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t['買いシグナル足'], day.index[7])
        self.assertEqual(t['買い日時'], day.index[8])
        self.assertEqual(t['売りシグナル足'], day.index[9])
        self.assertEqual(t['売り日時'], day.index[10])
        self.assertEqual((t['買値(円)'],t['売値(円)'],t['数量(株)']), (1006.,969.,200))
        self.assertEqual(t['費用控除前損益(円)'], -7400.)
        self.assertEqual(t['手数料(円)'], 200.)
        expected_interest = 201200 * .03 / 365
        self.assertAlmostEqual(t['信用金利(円)'], expected_interest)
        self.assertAlmostEqual(summary['損益(円)'], -7400-200-expected_interest)
        self.assertGreaterEqual(summary['期間最大DD(円)'], summary['日内最大DD(円)'])
        # Later bars cannot alter already executed trades.
        changed = day.copy()
        changed.iloc[20:, changed.columns.get_loc('Close')] = 5000.
        _, future_trades = app.simulate(changed, 'MAのみ', 1., 'TEST', '合成', cost)
        self.assertEqual(t, future_trades[0])

    def test_dd_definitions_and_cost_validation(self):
        self.assertEqual(app.max_drawdown([100,120,110,130,90,100]), 40)
        self.assertEqual(app.max_drawdown([100,110,120]), 0)
        # A cross-day peak makes period DD exceed each day's independent DD.
        self.assertEqual(app.max_drawdown([100,120,110,110,115,95]), 25)
        self.assertEqual(max(app.max_drawdown([100,120,110]), app.max_drawdown([110,115,95])), 20)
        self.assertAlmostEqual(app.Costs('仮定',100,5,3).fee(200000),200)
        with self.assertRaises(ValueError):
            app.Costs('bad', -1)
        with self.assertRaises(ValueError):
            app.Costs('bad', annual_interest_pct=float('nan'))

    def test_gap_rejects_order_without_future_price_resizing(self):
        day=self.deterministic_day()
        day.iloc[8,day.columns.get_loc('Open')]=2000.
        summary,trades=app.simulate(day,'MAのみ',1.,'TEST','合成')
        # Signal requests 200 shares; filling 100 instead using next-open price is forbidden.
        self.assertFalse(trades)
        self.assertEqual(summary['建玉上限で見送り'],1)

    def test_final_liquidation_ignores_final_close(self):
        day = self.deterministic_day()
        day.iloc[8:, day.columns.get_loc('Close')] = 1010.
        day.iloc[9:, day.columns.get_loc('Open')] = 1010.
        _, trades = app.simulate(day,'MAのみ',1.,'TEST','合成')
        self.assertEqual(len(trades),1)
        self.assertEqual(trades[0]['売り日時'],day.index[-1])
        self.assertIn('最終足始値',trades[0]['決済理由'])
        changed=day.copy()
        changed.iloc[-1, changed.columns.get_loc('Close')]=1.
        _, changed_trades=app.simulate(changed,'MAのみ',1.,'TEST','合成')
        self.assertEqual(trades,changed_trades)

    def test_intraday_vs_period_dd_in_actual_simulation(self):
        day = self.deterministic_day()
        next_day = day.copy()
        next_day.index = next_day.index + pd.Timedelta(days=1)
        summary, trades = app.simulate(pd.concat([day,next_day]), 'MAのみ', 1., 'TEST', '合成')
        self.assertEqual(len(trades),2)
        self.assertAlmostEqual(summary['損益(円)'],-14800.)
        # Each day peaks +800 above its opening cash, then loses 7400.
        self.assertAlmostEqual(summary['日内最大DD(円)'],8200.)
        self.assertAlmostEqual(summary['期間最大DD(円)'],15600.)
        self.assertAlmostEqual(summary['日末残高系列最大DD(円)'],14800.)

    def test_rejection_and_data_shortage_are_distinct(self):
        day = self.deterministic_day()
        expensive = day.copy()
        expensive[['Open','High','Low','Close']] *= 4
        summary, trades = app.simulate(expensive,'MAのみ',1.,'TEST','合成')
        self.assertFalse(trades)
        self.assertGreater(summary['建玉上限で見送り'],0)
        self.assertEqual(summary['資金不足で見送り'],0)
        with patch.object(app,'INIT_CASH',50000):
            summary, trades = app.simulate(day,'MAのみ',1.,'TEST','合成')
        self.assertGreater(summary['資金不足で見送り'],0)
        self.assertEqual(summary['建玉上限で見送り'],0)
        summary, trades = app.simulate(day.iloc[:5],'MAのみ',1.,'TEST','合成')
        self.assertEqual(summary['利用日数'],0)
        self.assertEqual(summary['スキップ日数'],1)
        self.assertIn('データ不足',summary['取引0回の理由'])
        self.assertEqual(summary['建玉上限で見送り'],0)

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
            with (out/'backtest_summary.csv').open(encoding='utf-8-sig') as f:
                summary_rows=list(csv.DictReader(f))
            self.assertEqual(len(summary_rows),12)
            self.assertEqual({r['費用シナリオ'] for r in summary_rows},{'費用ゼロ','仮定費用込み'})
            # Whole missing day and an observed but incomplete day remain separate.
            dates=sorted(set(intraday.index.date))
            partial=intraday[intraday.index.date != dates[2]].copy()
            bad_index=partial[partial.index.date == dates[1]].index[5:]
            partial=partial.drop(bad_index)
            def partial_fetch(code,interval,period):
                return daily.copy() if interval=='1d' else partial.copy()
            with patch.object(app, 'OUT', out), patch.object(app, 'INPUT', watch), \
                 patch.object(app.time, 'sleep'), patch.object(app, 'fetch', side_effect=partial_fetch):
                app.main()
            with (out/'run_status.csv').open(encoding='utf-8-sig') as f:
                status=list(csv.DictReader(f))[0]
            self.assertEqual(int(status['取得日数']),9)
            self.assertEqual(int(status['利用日数']),8)
            self.assertEqual(int(status['除外日数']),1)
            # Interior missing day is detected from daily reference.
            self.assertEqual(int(status['日足参照欠落日数']),1)
            with patch.object(app, 'OUT', out), patch.object(app, 'INPUT', watch), \
                 patch.object(app.time, 'sleep'), patch.object(app, 'fetch', return_value=pd.DataFrame()):
                app.main()
                with self.assertRaisesRegex(ValueError, 'incomplete'):
                    validate(out)


if __name__ == '__main__':
    unittest.main()
