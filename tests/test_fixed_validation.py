import unittest
import pandas as pd
from research import causal_engine as causal
from research import run_fixed as research
from research.frozen import engine_1a488e3 as frozen
import test_backtest


class FixedValidationTests(unittest.TestCase):
    def day(self):
        return test_backtest.BacktestTests().deterministic_day()

    def test_snapshot_and_frozen_limits(self):
        config=research.load_config()
        self.assertEqual(config['baseline_commit'],'1a488e3083d446baebaa07e5e700e10be66ea1fc')
        self.assertEqual((frozen.INIT_CASH,frozen.MAX_ORDER_YEN,frozen.SHARES_LOT),(1000000,300000,100))
        self.assertEqual(config['costs'][1],dict(name='仮定費用込み',fee_yen=100,fee_bps=0,annual_interest_pct=3))

    def test_outside_latest_quote_is_removed(self):
        index=pd.to_datetime(['2026-08-28T00:00:00Z','2026-09-08T00:00:00Z','2026-10-23T06:30:00Z'])
        body={'chart':{'result':[{'timestamp':[int(t.timestamp()) for t in index],
               'indicators':{'quote':[{key:[100,100,100] for key in ['open','high','low','close','volume']}]}}]}}
        frame,stats=research.bounded_frame(body,'2026-08-28','2026-09-08')
        self.assertEqual(len(frame),1)
        self.assertEqual(stats['outside_window'],2)
        self.assertEqual(str(frame.index[0].date()),'2026-08-28')

    def test_partial_observations_survive_but_empty_grid_does_not(self):
        index=pd.to_datetime(['2026-08-28T00:00:00Z','2026-08-28T00:05:00Z'])
        quote={key:[100,None] for key in ['open','high','low','close','volume']}
        quote['close'][0]=None
        body={'chart':{'result':[{'timestamp':[int(t.timestamp()) for t in index],
                                'indicators':{'quote':[quote]}}]}}
        frame,stats=research.bounded_frame(body,'2026-08-28','2026-09-08')
        self.assertEqual(len(frame),1)
        self.assertTrue(pd.isna(frame.iloc[0].Close))
        self.assertEqual(frame.iloc[0].Open,100)
        self.assertEqual(stats['empty_placeholder_rows'],1)

    def test_late_gap_keeps_completed_earlier_trade(self):
        day=self.day()
        damaged=day.drop(day.index[20:24])
        old,old_trades=frozen.simulate(damaged,'MAのみ',1.,'TEST','synthetic')
        self.assertFalse(old_trades)  # Retrospective whole-day rejection.
        full,full_trades=causal.simulate(day,'MAのみ',1.,'TEST','synthetic')
        audit=[]
        new,new_trades=causal.simulate(damaged,'MAのみ',1.,'TEST','synthetic',audit=audit)
        self.assertEqual(new_trades,full_trades)
        self.assertEqual(new['損益(円)'],-7400.)
        self.assertEqual(audit[0]['event'],'gap_halt_flat')

    def test_gap_while_held_never_invents_exit(self):
        day=self.day();day=day.drop(day.index[9])
        audit=[]
        summary,trades=causal.simulate(day,'MAのみ',1.,'TEST','synthetic',audit=audit)
        self.assertFalse(trades)
        self.assertIsNone(summary['損益(円)'])
        self.assertEqual(summary['残存株数'],200)
        self.assertEqual(audit[0]['event'],'gap_unresolved')
        self.assertEqual(research.classify(summary),'未決済・未確定')

    def test_future_missing_close_does_not_cancel_known_open_fill(self):
        day=self.day()
        day.iloc[8,day.columns.get_loc('Close')]=float('nan')
        audit=[]
        summary,trades=causal.simulate(day,'MAのみ',1.,'TEST','synthetic',audit=audit)
        self.assertIsNone(summary['損益(円)'])
        self.assertEqual(summary['残存株数'],200)
        self.assertEqual(audit[0]['event'],'invalid_completed_bar')
        self.assertFalse(trades)

    def test_missing_next_bar_cancels_entry(self):
        day=self.day();day=day.drop(day.index[8])
        summary,trades=causal.simulate(day,'MAのみ',1.,'TEST','synthetic')
        self.assertFalse(trades)
        self.assertEqual(summary['残存株数'],0)
        self.assertEqual(summary['欠損で買い注文取消'],1)
        self.assertEqual(summary['損益(円)'],0.)

    def test_missing_tail_does_not_use_last_available_open(self):
        day=self.day()
        day.iloc[8:,day.columns.get_loc('Close')]=1010.
        day.iloc[9:,day.columns.get_loc('Open')]=1010.
        early=day.between_time('09:00','15:20')
        summary,trades=causal.simulate(early,'MAのみ',1.,'TEST','synthetic')
        self.assertIsNone(summary['損益(円)'])
        self.assertFalse(trades)
        full,trades=causal.simulate(day,'MAのみ',1.,'TEST','synthetic')
        self.assertEqual(trades[0]['売り日時'].strftime('%H:%M'),'15:25')
        self.assertEqual(full['残存株数'],0)

    def test_all_original_symbols_and_conditions_include_unavailable(self):
        config=research.load_config()
        items=[{'コード':'TEST','候補名':'synthetic'},{'コード':'MISSING','候補名':'none'}]
        rows,_,_=research.evaluate(config,'fixture',{},items)
        self.assertEqual(len(rows),2*3*2*2*2)
        self.assertTrue(all(row['分類']=='データ未取得' for row in rows))
        totals=research.aggregates(rows)
        self.assertTrue(all(row['取引なし']==0 and row['データ未取得']==12 for row in totals))
