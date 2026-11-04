"""Hand-calculated, deterministic tests of the shared cash-account simulator."""
import unittest

import numpy as np
import pandas as pd

from portfolio_engine import EOD, INIT_CASH, simulate_portfolio
from smallcap_auto_v3 import Costs


def session():
    """One complete Tokyo session; no lunch/closing-auction placeholder rows."""
    return pd.date_range('2026-09-01 09:00','2026-09-01 11:25',freq='5min',tz='Asia/Tokyo').append(
        pd.date_range('2026-09-01 12:30','2026-09-01 15:20',freq='5min',tz='Asia/Tokyo'))


def prices(closes=None, opens=None):
    """Signal at 09:35, buy 09:40, sell signal 09:45, exit 09:50.

    With one yen slippage, buy=1006, sell=969, quantity=200. Gross=-7400.
    A volume pulse also makes the 09:35 signal valid for both volume strategies.
    """
    index=session()
    close=np.full(len(index),980.,dtype=float)
    close[:6]=1000.
    close[6:10]=[990.,1010.,1011.,980.]
    for position,value in (closes or {}).items():
        close[position]=value
    opening=close.copy()
    opening[8]=1005.
    opening[10]=970.
    for position,value in (opens or {}).items():
        opening[position]=value
    volume=np.full(len(index),1000.,dtype=float)
    volume[7]=10000.
    return pd.DataFrame({'Open':opening,'High':np.maximum(close,opening)+1,
        'Low':np.minimum(close,opening)-1,'Close':close,'Volume':volume},index=index)


def holding_prices():
    day=prices(closes={i:1012. for i in range(9,len(session()))})
    day.loc[day.index[9]:,'Open']=1012.
    day['High']=day[['Open','Close']].max(axis=1)+1
    day['Low']=day[['Open','Close']].min(axis=1)-1
    return day


class PortfolioEngineTests(unittest.TestCase):
    def assert_ledger(self,result):
        self.assertTrue(result['equity'])
        for point in result['equity']:
            with self.subTest(timestamp=point['timestamp'],phase=point['phase']):
                self.assertGreaterEqual(point['cash'],-1e-6)
                self.assertGreaterEqual(point['reserved_cash'],-1e-6)
                self.assertLessEqual(point['reserved_cash'],point['cash']+1e-6)
                self.assertAlmostEqual(point['free_cash'],point['cash']-point['reserved_cash'],places=6)
                self.assertAlmostEqual(point['cash']+point['position_cost'],INIT_CASH+point['realized_pnl'],places=6)
                self.assertAlmostEqual(point['equity'],INIT_CASH+point['realized_pnl']+point['unrealized_pnl'],places=6)
        self.assertAlmostEqual(sum(trade['net_pnl'] for trade in result['trades']),result['summary']['realized_pnl'],places=6)

    def test_simultaneous_signals_are_ticker_ordered_and_dictionary_order_invariant(self):
        frames={code:prices() for code in 'ABCDEF'}
        forward=simulate_portfolio(frames,'MAのみ',1.)
        reverse=simulate_portfolio(dict(reversed(list(frames.items()))),'MAのみ',1.)
        self.assertEqual(forward,reverse)
        buys=[order for order in forward['orders'] if order['action']=='buy']
        self.assertEqual([order['code'] for order in buys],list('ABCDEF'))
        filled=[order for order in buys if order['status']=='filled']
        self.assertEqual([(order['code'],order['quantity']) for order in filled],
                         [('A',200),('B',200),('C',200),('D',200),('E',100)])
        self.assertEqual((buys[-1]['status'],buys[-1]['reason']),('rejected','cash_shortage'))
        self.assertEqual(len(forward['trades']),5)
        self.assertAlmostEqual(forward['summary']['total_pnl'],-33300.)
        self.assertTrue(any(point['reserved_cash']>0 for point in forward['equity']))
        self.assert_ledger(forward)

    def test_next_bar_fills_slippage_and_fees_match_hand_calculation(self):
        costs=Costs('cash-fee-example',100.,5.,0.)
        for strategy in ('MAのみ','VWAP+出来高','高値ブレイク+出来高'):
            with self.subTest(strategy=strategy):
                result=simulate_portfolio({'A':prices()},strategy,1.,costs)
                self.assertEqual(len(result['trades']),1)
                trade=result['trades'][0]
                self.assertEqual(trade['entry_signal_bar'],session()[7])
                self.assertEqual(trade['entry_time'],session()[8])
                self.assertEqual(trade['exit_signal_bar'],session()[9])
                self.assertEqual(trade['exit_time'],session()[10])
                self.assertGreater(trade['entry_time'],trade['entry_signal_bar'])
                self.assertGreater(trade['exit_time'],trade['exit_signal_bar'])
                self.assertEqual(trade['entry_signal_time'],trade['entry_time'])
                self.assertEqual((trade['buy'],trade['sell'],trade['quantity']),(1006.,969.,200))
                self.assertEqual(trade['gross_pnl'],-7400.)
                self.assertAlmostEqual(trade['entry_fee'],200.6)
                self.assertAlmostEqual(trade['exit_fee'],196.9)
                self.assertEqual(trade['interest'],0.)
                self.assertAlmostEqual(trade['net_pnl'],-7797.5)
                self.assertAlmostEqual(result['summary']['total_pnl'],-7797.5)
                self.assertFalse(result['positions'])
                self.assert_ledger(result)

    def test_opening_gap_cannot_resize_signal_quantity_to_fit_position_cap(self):
        result=simulate_portfolio({'A':prices(opens={8:2000.})},'MAのみ',1.)
        buy=next(order for order in result['orders'] if order['action']=='buy')
        self.assertEqual(buy['reference_price'],1011.)
        self.assertEqual(buy['quantity'],200)
        self.assertEqual((buy['status'],buy['reason']),('rejected','position_cap_at_open'))
        self.assertFalse(result['trades'])
        self.assertFalse(result['positions'])
        self.assertEqual(result['summary']['cash'],INIT_CASH)
        self.assert_ledger(result)

    def test_opening_gap_cannot_spend_cash_reserved_for_other_orders(self):
        frames={code:prices() for code in 'ABCDE'}
        frames['A']=prices(opens={8:1490.})
        result=simulate_portfolio(frames,'MAのみ',1.)
        buy=next(order for order in result['orders'] if order['action']=='buy' and order['code']=='A')
        # Requested 200 shares cost 298200, below the position cap, but leave
        # insufficient cash for the other four already-reserved orders.
        self.assertEqual(buy['quantity'],200)
        self.assertEqual((buy['status'],buy['reason']),('rejected','cash_shortage_at_open'))
        self.assertNotIn('A',{trade['code'] for trade in result['trades']})
        self.assertEqual({trade['code'] for trade in result['trades']},set('BCDE'))
        self.assert_ledger(result)

    def test_account_daily_loss_stop_cancels_buy_while_other_exit_continues(self):
        a=prices()
        b=prices(closes={i:1000. for i in range(len(session()))})
        b.loc[b.index[8],'Close']=990.
        b.loc[b.index[9],'Close']=1010.
        b['Open']=b.Close
        b['High']=b[['Open','Close']].max(axis=1)+1
        b['Low']=b[['Open','Close']].min(axis=1)-1
        # C stays open one bar longer than A, so its exit must still execute
        # after A's realized loss has stopped new entries for the whole account.
        c=prices(closes={9:1012.,10:980.},opens={9:1012.,10:1012.,11:1000.})
        result=simulate_portfolio({'A':a,'B':b,'C':c},'MAのみ',1.)
        rejected=next(order for order in result['orders'] if order['code']=='B' and order['action']=='buy')
        self.assertEqual(rejected['expected_fill'],session()[10])
        self.assertEqual((rejected['status'],rejected['reason']),('rejected','daily_loss_stop'))
        by_code={trade['code']:trade for trade in result['trades']}
        self.assertEqual(set(by_code),{'A','C'})
        self.assertEqual(by_code['A']['net_pnl'],-7400.)
        self.assertEqual(by_code['A']['exit_time'],session()[10])
        self.assertEqual(by_code['C']['exit_time'],session()[11])
        stop=[point for point in result['equity'] if point['timestamp']==session()[10] and point['phase']=='open'][0]
        self.assertTrue(stop['daily_entry_stop'])
        self.assertLessEqual(stop['daily_realized_pnl'],-5000.)
        self.assertEqual(result['summary']['halted_days'],1)
        self.assertFalse(result['positions'])
        self.assert_ledger(result)

    def test_daily_entry_stop_resets_next_day_without_resetting_account_cash(self):
        first=prices()
        second=first.copy()
        second.index=second.index+pd.Timedelta(days=1)
        result=simulate_portfolio({'A':pd.concat([first,second])},'MAのみ',1.)
        self.assertEqual(len(result['trades']),2)
        self.assertEqual([trade['net_pnl'] for trade in result['trades']],[-7400.,-7400.])
        self.assertAlmostEqual(result['summary']['total_pnl'],-14800.)
        self.assertEqual([day['day_pnl'] for day in result['daily']],[-7400.,-7400.])
        self.assertEqual(result['summary']['halted_days'],2)
        self.assert_ledger(result)

    def test_missing_expected_entry_open_cancels_buy_instead_of_filling_later(self):
        day=prices().drop(session()[8])
        result=simulate_portfolio({'A':day},'MAのみ',1.)
        buy=next(order for order in result['orders'] if order['action']=='buy')
        self.assertEqual(buy['expected_fill'],session()[8])
        self.assertEqual((buy['status'],buy['reason']),('rejected','missing_bar'))
        self.assertNotIn('fill_time',buy)
        self.assertFalse(result['trades'])
        self.assertFalse(result['positions'])
        self.assertEqual(result['summary']['total_pnl'],0.)
        self.assert_ledger(result)

    def test_held_gap_is_unresolved_but_other_symbols_exit_is_not_suppressed(self):
        a=prices().drop(session()[9])
        result=simulate_portfolio({'A':a,'B':prices()},'MAのみ',1.)
        self.assertEqual([trade['code'] for trade in result['trades']],['B'])
        self.assertEqual(result['trades'][0]['exit_time'],session()[10])
        self.assertIsNone(result['summary']['total_pnl'])
        self.assertIsNone(result['summary']['max_drawdown'])
        self.assertEqual(result['summary']['realized_pnl'],-7400.)
        self.assertEqual(len(result['positions']),1)
        self.assertEqual((result['positions'][0]['code'],result['positions'][0]['quantity']),('A',200))
        self.assertTrue(result['positions'][0]['unresolved'])
        self.assertFalse(result['summary']['valuation_complete'])
        self.assertTrue(result['summary']['data_entry_stop'])
        self.assert_ledger(result)

    def test_missing_future_close_does_not_erase_known_open_fill(self):
        day=prices()
        day.loc[session()[8],'Close']=float('nan')
        result=simulate_portfolio({'A':day},'MAのみ',1.)
        buy=next(order for order in result['orders'] if order['action']=='buy')
        self.assertEqual(buy['status'],'filled')
        self.assertEqual(buy['fill_time'],session()[8])
        self.assertEqual(buy['quantity'],200)
        self.assertIsNone(result['summary']['total_pnl'])
        self.assertTrue(result['positions'][0]['unresolved'])
        self.assertFalse(result['trades'])
        self.assert_ledger(result)

    def test_clock_based_eod_uses_1520_open_not_its_close(self):
        day=holding_prices()
        day.loc[day.index[-1],'Open']=1100.
        result=simulate_portfolio({'A':day},'MAのみ',1.)
        trade=result['trades'][0]
        self.assertEqual(trade['exit_time'].time(),EOD)
        self.assertEqual(trade['sell'],1099.)
        self.assertAlmostEqual(result['summary']['total_pnl'],18600.)
        changed=day.copy()
        changed.loc[changed.index[-1],'Close']=1.
        repeated=simulate_portfolio({'A':changed},'MAのみ',1.)
        self.assertEqual(result['trades'],repeated['trades'])
        self.assertEqual(result['summary']['total_pnl'],repeated['summary']['total_pnl'])
        self.assert_ledger(result)

    def test_missing_eod_bar_never_liquidates_at_last_retrospectively_available_open(self):
        day=holding_prices().iloc[:-1]
        result=simulate_portfolio({'A':day},'MAのみ',1.)
        self.assertFalse(result['trades'])
        self.assertIsNone(result['summary']['total_pnl'])
        self.assertTrue(result['positions'][0]['unresolved'])
        self.assert_ledger(result)

    def test_simultaneous_offsetting_exits_do_not_stop_new_buys_by_ticker_order(self):
        loss=prices(opens={8:1000.,10:970.})
        profit=prices(closes={8:1100.,9:1040.},opens={8:1000.,10:1030.})
        delayed=prices(closes={i:1000. for i in range(len(session()))},opens={8:1000.,10:1000.})
        delayed.loc[session()[8],'Close']=990.
        delayed.loc[session()[9],'Close']=1010.
        delayed['High']=delayed[['Open','Close']].max(axis=1)+1
        delayed['Low']=delayed[['Open','Close']].min(axis=1)-1
        results=[]
        # Switch which ticker carries the loss. A per-sale latch would stop
        # the first account but not the second despite equal simultaneous PnL.
        for a,b in [(loss,profit),(profit,loss)]:
            frames={'A':a,'B':b,'C':delayed}
            result=simulate_portfolio(frames,'MAのみ',0.)
            reverse=simulate_portfolio(dict(reversed(list(frames.items()))),'MAのみ',0.)
            self.assertEqual(result,reverse)
            results.append(result)
            exits=[trade for trade in result['trades'] if trade['code'] in ('A','B')]
            self.assertEqual(len(exits),2)
            self.assertEqual({trade['exit_time'] for trade in exits},{session()[10]})
            self.assertEqual(sorted(trade['net_pnl'] for trade in exits),[-6000.,6000.])
            buy=next(order for order in result['orders'] if order['action']=='buy' and order['code']=='C')
            self.assertEqual(buy['status'],'filled')
            self.assertEqual(buy['fill_time'],session()[10])
            boundary=next(point for point in result['equity'] if point['timestamp']==session()[10] and point['phase']=='open')
            self.assertEqual(boundary['daily_realized_pnl'],0.)
            self.assertFalse(boundary['daily_entry_stop'])
            self.assertEqual(result['summary']['halted_days'],0)
            self.assertAlmostEqual(result['summary']['total_pnl'],0.)
            self.assert_ledger(result)
        self.assertEqual(results[0]['summary'],results[1]['summary'])

    def test_prior_daily_stop_stays_latched_after_later_profit_recovers_loss(self):
        loss=prices(opens={8:1000.,10:970.})
        later_profit=prices(closes={8:1050.,9:1100.,10:1040.},
                            opens={8:1000.,10:1100.,11:1030.})
        delayed=prices(closes={i:1000. for i in range(len(session()))},opens={8:1000.,10:1000.})
        delayed.loc[session()[9],'Close']=990.
        delayed.loc[session()[10],'Close']=1010.
        delayed['High']=delayed[['Open','Close']].max(axis=1)+1
        delayed['Low']=delayed[['Open','Close']].min(axis=1)-1
        result=simulate_portfolio({'A':loss,'B':later_profit,'C':delayed},'MAのみ',0.)
        by_code={trade['code']:trade for trade in result['trades']}
        self.assertEqual(set(by_code),{'A','B'})
        self.assertEqual((by_code['A']['exit_time'],by_code['A']['net_pnl']),(session()[10],-6000.))
        self.assertEqual((by_code['B']['exit_time'],by_code['B']['net_pnl']),(session()[11],6000.))
        buy=next(order for order in result['orders'] if order['action']=='buy' and order['code']=='C')
        self.assertEqual(buy['expected_fill'],session()[11])
        self.assertEqual((buy['status'],buy['reason']),('rejected','daily_loss_stop'))
        first_stop=next(point for point in result['equity'] if point['timestamp']==session()[10] and point['phase']=='open')
        recovery=next(point for point in result['equity'] if point['timestamp']==session()[11] and point['phase']=='open')
        self.assertEqual(first_stop['daily_realized_pnl'],-6000.)
        self.assertTrue(first_stop['daily_entry_stop'])
        self.assertEqual(recovery['daily_realized_pnl'],0.)
        self.assertTrue(recovery['daily_entry_stop'])
        self.assertTrue(result['summary']['daily_entry_stop'])
        self.assertEqual(result['summary']['halted_days'],1)
        self.assertAlmostEqual(result['summary']['total_pnl'],0.)
        self.assertFalse(result['positions'])
        self.assert_ledger(result)

    def test_credit_interest_is_rejected_for_cash_only_account(self):
        with self.assertRaisesRegex(ValueError,'credit interest'):
            simulate_portfolio({'A':prices()},'MAのみ',1.,Costs('unsupported',100.,0.,3.))
