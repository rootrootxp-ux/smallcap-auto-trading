import contextlib
import csv
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
import test_backtest
from run_outcome import summarize
from scripts.check_results import validate

app=test_backtest.app


class RunOutcomeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.intraday=test_backtest.prices()
        cls.daily=pd.DataFrame(dict(Open=1000.,High=1030.,Low=970.,Close=1000.,Volume=200000.),
                               index=pd.bdate_range('2026-08-01',periods=60))

    def run_case(self, root, codes, broken_simulation=False):
        watch=root/'input.csv'
        watch.write_text('コード,候補名\n'+''.join(f'{code},合成\n' for code in codes),encoding='utf-8')
        def fetch(code,interval,period):
            if code=='NET':
                raise app.DataRetrievalError('upstream HTTP 503')
            if code=='EMPTY':
                return pd.DataFrame()
            if code=='SHORT':
                return self.daily.iloc[:5].copy()
            if interval=='1d':
                return self.daily.copy()
            if code=='POOR':
                return self.intraday.iloc[:5].copy()
            frame=self.intraday.copy()
            if code=='EXPENSIVE':
                frame[['Open','High','Low','Close']]*=5
            return frame
        real=app.simulate
        def simulate(frame,strategy,slip,code,name,costs):
            if broken_simulation and code=='BUG' and strategy=='VWAP+出来高':
                raise ValueError('synthetic accounting defect')
            return real(frame,strategy,slip,code,name,costs)
        with patch.object(app,'OUT',root),patch.object(app,'INPUT',watch), \
             patch.object(app,'fetch',side_effect=fetch),patch.object(app,'simulate',side_effect=simulate), \
             patch.object(app.time,'sleep'),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            return app.main()

    def read(self,root,name):
        with (root/name).open(encoding='utf-8-sig') as f:
            return list(csv.DictReader(f))

    def test_majority_policy_boundary_not_based_on_profit(self):
        for valid,expected in [(9,'失敗'),(10,'一部成功'),(20,'完全成功')]:
            rows=[{'状態':'検証可能'}]*valid+[{'状態':'取得失敗'}]*(20-valid)
            self.assertEqual(summarize(rows)['全体結果'],expected)
        self.assertEqual(summarize([])['全体結果'],'失敗')
        self.assertEqual(summarize([{'状態':'検証可能'},{'状態':'重大不具合'}])['全体結果'],'失敗')

    def test_partial_success_preserves_valid_and_reference_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            outcome=self.run_case(root,['A','B','POOR','NET'])
            self.assertEqual(outcome['全体結果'],'一部成功')
            self.assertEqual((outcome['検証可能銘柄数'],outcome['データ不足銘柄数'],outcome['取得失敗銘柄数']),(2,1,1))
            self.assertEqual(len(self.read(root,'backtest_summary.csv')),36)
            self.assertEqual({r['状態'] for r in self.read(root,'run_status.csv')},{'検証可能','データ不足','取得失敗'})
            self.assertEqual(validate(root)['全体結果'],'一部成功')
            self.assertIn('全体結果: 一部成功',(root/'report.html').read_text())

    def test_sufficient_data_with_zero_trades_is_complete_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            outcome=self.run_case(root,['EXPENSIVE'])
            self.assertEqual(outcome['全体結果'],'完全成功')
            self.assertFalse(self.read(root,'trade_history.csv'))
            self.assertEqual(len(self.read(root,'backtest_summary.csv')),12)
            validate(root)

    def test_all_unavailable_or_short_data_is_failure_with_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            outcome=self.run_case(root,['EMPTY','SHORT'])
            self.assertEqual(outcome['全体結果'],'失敗')
            self.assertEqual(outcome['取得失敗銘柄数'],1)
            self.assertEqual(outcome['データ不足銘柄数'],1)
            self.assertTrue((root/'report.html').is_file())
            with self.assertRaisesRegex(ValueError,'失敗'):
                validate(root)

    def test_too_few_valid_symbols_fails_without_losing_their_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            outcome=self.run_case(root,['A','NET','EMPTY','SHORT'])
            self.assertEqual(outcome['全体結果'],'失敗')
            self.assertEqual(len(self.read(root,'backtest_summary.csv')),12)
            with self.assertRaisesRegex(ValueError,'失敗'):
                validate(root)

    def test_unexpected_bug_is_fatal_and_symbol_results_are_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            outcome=self.run_case(root,['A','BUG'],broken_simulation=True)
            self.assertEqual(outcome['全体結果'],'失敗')
            self.assertEqual(outcome['重大不具合銘柄数'],1)
            rows=self.read(root,'backtest_summary.csv')
            self.assertEqual(len(rows),12)
            self.assertEqual({r['コード'] for r in rows},{'A'})
            statuses=self.read(root,'run_status.csv')
            self.assertEqual(statuses[1]['取得段階'],'バックテスト')
            self.assertEqual(statuses[1]['問題種別'],'ValueError')
            self.assertTrue(list(root.glob('errors_*.log')))
            with self.assertRaisesRegex(ValueError,'失敗'):
                validate(root)

    def test_fetch_only_wraps_known_upstream_errors_not_programming_errors(self):
        previous = app.yf.config.debug.hide_exceptions
        with patch.object(app.yf,'Ticker') as ticker:
            ticker.return_value.history.side_effect=app.yf.exceptions.YFRateLimitError()
            with self.assertRaises(app.DataRetrievalError):
                app.fetch('TEST','5m','30d')
            self.assertEqual(app.yf.config.debug.hide_exceptions, previous)
            ticker.return_value.history.side_effect=KeyError('bad schema')
            with self.assertRaises(KeyError):
                app.fetch('TEST','5m','30d')
            self.assertEqual(app.yf.config.debug.hide_exceptions, previous)

    def test_validator_detects_missing_scenarios_stale_ids_and_accounting_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            self.run_case(root,['A'])
            file=root/'backtest_summary.csv'
            original=file.read_bytes()
            rows=self.read(root,'backtest_summary.csv')
            app.write_csv(file,rows[:-1],list(rows[0]))
            with self.assertRaisesRegex(ValueError,'missing or duplicate'):
                validate(root)
            file.write_bytes(original)
            rows[0]['実行ID']='stale'
            app.write_csv(file,rows,list(rows[0]))
            with self.assertRaisesRegex(ValueError,'Stale'):
                validate(root)
            file.write_bytes(original)
            rows=self.read(root,'backtest_summary.csv')
            rows[0]['損益(円)']='999999'
            app.write_csv(file,rows,list(rows[0]))
            with self.assertRaisesRegex(ValueError,'Accounting'):
                validate(root)
