"""Cash-only, long-only, chronological shared-account research simulation.

No broker interface. Missing observations never produce invented fills.
"""
from dataclasses import dataclass
from datetime import time as clock
import math
import numpy as np
import pandas as pd
from smallcap_auto_v3 import (Costs, INIT_CASH, MAX_ORDER_YEN, SHARES_LOT,
    MAX_TRADE_LOSS_YEN, DAILY_LOSS_LIMIT_YEN)

STRATEGIES=('MAのみ','VWAP+出来高','高値ブレイク+出来高')
EOD=clock(15,20)  # pre-declared last continuous five-minute slot before closing auction


def next_slot(t):
    if t.time()==clock(11,25):
        return t.normalize()+pd.Timedelta(hours=12,minutes=30)
    if t.time()>=EOD:
        return None
    return t+pd.Timedelta(minutes=5)


def prepare(frames):
    data={}
    for code,source in sorted(frames.items()):
        if source.empty:
            continue
        d=source.copy()
        if not isinstance(d.index,pd.DatetimeIndex) or d.index.tz is None or d.index.has_duplicates:
            raise ValueError('Expected unique timezone-aware price timestamps')
        if not {'Open','High','Low','Close','Volume'}.issubset(d.columns):
            raise ValueError('Missing OHLCV columns')
        d.index=d.index.tz_convert('Asia/Tokyo')
        d=d.sort_index()
        for date,day in d.groupby(d.index.date):
            times=day.index.time
            day=day.loc[((times>=clock(9))&(times<=clock(11,25)))|((times>=clock(12,30))&(times<=EOD))].copy()
            if day.empty:continue
            day['ma3']=day.Close.rolling(3).mean()
            day['vwap']=(((day.High+day.Low+day.Close)/3*day.Volume).cumsum()/day.Volume.cumsum().replace(0,np.nan))
            day['volavg5']=day.Volume.shift(1).rolling(5).mean()
            day['prior6high']=day.High.shift(1).rolling(6).max()
            day['prev_close']=day.Close.shift(1);day['prev_ma3']=day.ma3.shift(1)
            day['bar_number']=range(len(day))
            for t,row in day.iterrows():
                data[(code,t)]=row.to_dict()
    return data


@dataclass
class Position:
    quantity:int
    buy:float
    entry_fee:float
    entry_time:pd.Timestamp
    signal_bar:pd.Timestamp
    signal_time:pd.Timestamp
    last_price:float
    unresolved:bool=False

    @property
    def book(self):return self.buy*self.quantity+self.entry_fee


def simulate_portfolio(frames,strategy,slip,costs=None,prepared=None):
    if strategy not in STRATEGIES or not math.isfinite(slip) or slip<0:
        raise ValueError('Invalid strategy or slippage')
    costs=costs or Costs()
    if costs.annual_interest_pct!=0:
        raise ValueError('Cash-only portfolio does not implement credit interest')
    bars=prepare(frames) if prepared is None else prepared
    if not bars:raise ValueError('No usable portfolio observations')
    codes=sorted({code for code,t in bars})
    days=sorted({t.date() for code,t in bars})
    calendar=[]
    for day in days:
        start=pd.Timestamp(day,tz='Asia/Tokyo')
        calendar.extend(pd.date_range(start+pd.Timedelta(hours=9),start+pd.Timedelta(hours=11,minutes=30),freq='5min'))
        calendar.extend(pd.date_range(start+pd.Timedelta(hours=12,minutes=30),start+pd.Timedelta(hours=15,minutes=25),freq='5min'))
    cash=float(INIT_CASH);realized=0.;peak=float(INIT_CASH);observed_dd=0.;day_dd=0.
    positions={};pending={};processed_closes=set();halted_symbols=set();seen_days=set()
    trades=[];orders=[];equity=[];daily=[]
    day_pnl=0.;day_stopped=False;data_stopped=False;ever_incomplete=False;halt_days=set();data_days=set()
    current_day=None;day_start_realized=0.;day_peak=float(INIT_CASH)

    def reserved(exclude=None):
        return sum(o['reserved_amount'] for code,o in pending.items() if code!=exclude and o['action']=='buy')

    def reject(code,reason,t,order=None):
        if order is None:
            order={'code':code,'action':'data','quantity':0,'reserved_amount':0.}
            orders.append(order)
        order.update(status='rejected',reason=reason,resolution_time=t)

    def valuation(t,phase):
        nonlocal peak,observed_dd,day_peak,day_dd
        book=sum(p.book for p in positions.values())
        liquidation=sum(max(.01,p.last_price-slip)*p.quantity-costs.fee(max(.01,p.last_price-slip)*p.quantity) for p in positions.values())
        value=cash+liquidation
        unrealized=liquidation-book
        complete=not any(p.unresolved for p in positions.values())
        assert cash>=-1e-6 and cash-reserved()>=-1e-6,'Negative cash or double-reserved cash'
        assert math.isclose(cash+book,INIT_CASH+realized,abs_tol=1e-6,rel_tol=0),'Cash/book/PnL mismatch'
        assert math.isclose(value,INIT_CASH+realized+unrealized,abs_tol=1e-6,rel_tol=0),'Equity/PnL mismatch'
        if complete and not ever_incomplete:
            peak=max(peak,value);observed_dd=max(observed_dd,peak-value)
        if complete:
            day_peak=max(day_peak,value);day_dd=max(day_dd,day_peak-value)
        record={'timestamp':t,'phase':phase,'cash':cash,'reserved_cash':reserved(),'free_cash':cash-reserved(),
                'position_cost':book,'market_value':sum(p.last_price*p.quantity for p in positions.values()),
                'realized_pnl':realized,'unrealized_pnl':unrealized,'equity':value,
                'open_positions':len(positions),'valuation_complete':complete,
                'daily_realized_pnl':day_pnl,'daily_entry_stop':day_stopped,'data_entry_stop':data_stopped,
                'holdings':{code:{'quantity':p.quantity,'book_cost':p.book,'last_known_price':p.last_price,'unresolved':p.unresolved} for code,p in sorted(positions.items())}}
        equity.append(record)
        return record

    def close_position(code,t,price,reason,signal_bar,signal_time):
        nonlocal cash,realized,day_pnl
        p=positions.pop(code)
        sell=max(.01,price-slip);fee=costs.fee(sell*p.quantity)
        gross=(sell-p.buy)*p.quantity;pnl=gross-p.entry_fee-fee
        cash+=sell*p.quantity-fee;realized+=pnl;day_pnl+=pnl
        trades.append({'code':code,'quantity':p.quantity,'buy':p.buy,'sell':sell,'entry_time':p.entry_time,
                       'entry_signal_bar':p.signal_bar,'entry_signal_time':p.signal_time,
                       'exit_time':t,'exit_signal_bar':signal_bar,'exit_signal_time':signal_time,
                       'gross_pnl':gross,'entry_fee':p.entry_fee,'exit_fee':fee,'interest':0.,'net_pnl':pnl,
                       'exit_reason':reason,'cash_after':cash,'daily_realized_after':day_pnl})

    for t in calendar:
        if t.date()!=current_day:
            if current_day is not None:
                record=valuation(pd.Timestamp(current_day,tz='Asia/Tokyo')+pd.Timedelta(hours=15,minutes=25),'day_end')
                daily.append(dict(record,date=str(current_day),day_pnl=realized-day_start_realized,
                                  day_max_drawdown=None if not record['valuation_complete'] else day_dd))
            current_day=t.date();day_pnl=0.;day_stopped=False;halted_symbols=set();seen_days=set()
            day_start_realized=realized;day_dd=0.;day_peak=equity[-1]['equity'] if equity else float(INIT_CASH)

        # 1. Observe ALL preceding closes before any execution at this boundary.
        for code in codes:
            previous=t-pd.Timedelta(minutes=5)
            key=(code,previous)
            row=bars.get(key)
            if row is None or key in processed_closes:continue
            processed_closes.add(key)
            p=positions.get(code)
            if p and p.unresolved:continue
            if not all(math.isfinite(float(row[k])) for k in ['High','Low','Close','Volume']) or min(row['High'],row['Low'],row['Close'])<=0 or row['Volume']<0 or row['High']<row['Low']:
                halted_symbols.add(code)
                if p:
                    p.unresolved=True;data_stopped=True;ever_incomplete=True;data_days.add(current_day)
                old=pending.pop(code,None);reject(code,'missing_close',t,old)
                continue
            if p:p.last_price=float(row['Close'])
            # No signals once data has been interrupted; existing positions remain conservative unknown.
            if code in halted_symbols or row['bar_number']<6 or previous.time()>=clock(15,15):continue
            ma_up=math.isfinite(row['prev_ma3']) and row['prev_close']<=row['prev_ma3'] and row['Close']>row['ma3']
            ma_down=math.isfinite(row['prev_ma3']) and row['prev_close']>=row['prev_ma3'] and row['Close']<row['ma3']
            strong=math.isfinite(row['volavg5']) and row['Volume']>row['volavg5']*1.5
            signal=ma_up if strategy=='MAのみ' else (ma_up and row['Close']>row['vwap'] and strong) if strategy=='VWAP+出来高' else (math.isfinite(row['prior6high']) and row['Close']>row['prior6high'] and row['Close']>row['vwap'] and strong)
            expected=next_slot(previous)
            if p:
                mark=max(.01,float(row['Close'])-slip)
                loss=(mark-p.buy)*p.quantity-p.entry_fee-costs.fee(mark*p.quantity)
                reason='損切り判定' if loss<=-MAX_TRADE_LOSS_YEN else 'MA下抜け' if ma_down else None
                if reason and code not in pending:
                    o={'code':code,'action':'sell','quantity':p.quantity,'signal_bar':previous,'signal_time':t,
                       'expected_fill':expected,'reserved_amount':0.,'status':'pending','reason':reason}
                    pending[code]=o;orders.append(o)
            elif signal and code not in pending:
                reference=float(row['Close'])+slip
                free=max(0.,cash-reserved())
                cap_qty=int(MAX_ORDER_YEN//(reference*SHARES_LOT))*SHARES_LOT
                cash_qty=int(max(0,free-costs.fee_yen)//(reference*SHARES_LOT*(1+costs.fee_bps/10000)))*SHARES_LOT
                q=min(cap_qty,cash_qty)
                o={'code':code,'action':'buy','quantity':q,'signal_bar':previous,'signal_time':t,
                   'expected_fill':expected,'reference_price':reference,'available_cash_at_signal':free,
                   'reserved_amount':reference*q+costs.fee(reference*q) if q else 0.,'status':'pending'}
                orders.append(o)
                if day_stopped:reject(code,'daily_loss_stop',t,o)
                elif data_stopped:reject(code,'data_account_stop',t,o)
                elif cap_qty<SHARES_LOT:reject(code,'position_cap',t,o)
                elif cash_qty<SHARES_LOT:reject(code,'cash_shortage',t,o)
                else:pending[code]=o
        valuation(t,'close')

        # 2. Missing expected slots are known now. Never execute on a later, retrospectively observed slot.
        for code in codes:
            if t.time()>EOD or t.time()==clock(11,30):continue
            if t.time()==clock(9) and code not in seen_days:
                seen_days.add(code)
                if (code,t) not in bars:
                    halted_symbols.add(code);reject(code,'opening_missing',t)
            if code in halted_symbols:continue
            row=bars.get((code,t))
            if row is None or not math.isfinite(float(row['Open'])) or row['Open']<=0:
                halted_symbols.add(code)
                order=pending.pop(code,None)
                reject(code,'missing_bar' if row is None else 'missing_open',t,order)
                p=positions.get(code)
                if p:
                    p.unresolved=True;data_stopped=True;ever_incomplete=True;data_days.add(current_day)
        # 3. ALL exits first, then the daily account stop, then buys in fixed ticker order.
        for code in codes:
            p=positions.get(code);o=pending.get(code);row=bars.get((code,t))
            if not p or p.unresolved or row is None:continue
            if t.time()==EOD or (o and o['action']=='sell' and o['expected_fill']==t):
                if o is None:
                    o={'code':code,'action':'sell','quantity':p.quantity,'signal_bar':'事前設定','signal_time':'事前設定','expected_fill':t,'reserved_amount':0.,'status':'pending','reason':'scheduled_eod'}
                    pending[code]=o;orders.append(o)
                close_position(code,t,float(row['Open']), '事前設定15:20始値決済' if t.time()==EOD else o['reason'],
                               '事前設定' if t.time()==EOD else o['signal_bar'],
                               '事前設定' if t.time()==EOD else o['signal_time'])
                if o:
                    o.update(status='filled',fill_time=t,fill_price=max(.01,float(row['Open'])-slip),cash_after=cash)
                    pending.pop(code,None)
        if day_pnl<=-DAILY_LOSS_LIMIT_YEN:
            day_stopped=True;halt_days.add(t.date())
        for code in codes:
            o=pending.get(code)
            if not o or o['action']!='buy' or o['expected_fill']!=t:continue
            row=bars.get((code,t))
            if day_stopped or data_stopped or code in halted_symbols or row is None:
                reject(code,'daily_loss_stop' if day_stopped else 'data_account_stop' if data_stopped else 'missing_bar',t,o)
                pending.pop(code,None);continue
            price=float(row['Open'])+slip;amount=price*o['quantity'];fee=costs.fee(amount)
            if amount>MAX_ORDER_YEN:
                reject(code,'position_cap_at_open',t,o)
            elif amount+fee>cash-reserved(exclude=code)+1e-9:
                reject(code,'cash_shortage_at_open',t,o)
            else:
                cash-=amount+fee
                positions[code]=Position(o['quantity'],price,fee,t,o['signal_bar'],o['signal_time'],float(row['Open']))
                o.update(status='filled',fill_time=t,fill_price=price,cash_after=cash)
            pending.pop(code,None)
        for code,p in positions.items():
            if not p.unresolved and (code,t) in bars:p.last_price=float(bars[(code,t)]['Open'])
        valuation(t,'open')
    for code,o in list(pending.items()):
        reject(code,'end_of_input',calendar[-1],o)
        pending.pop(code)
    final=valuation(calendar[-1],'final')
    daily.append(dict(final,date=str(current_day),day_pnl=realized-day_start_realized,
                      day_max_drawdown=None if not final['valuation_complete'] else day_dd))
    assert math.isclose(sum(t['net_pnl'] for t in trades),realized,abs_tol=1e-6,rel_tol=0)
    if not positions:assert math.isclose(cash-INIT_CASH,realized,abs_tol=1e-6,rel_tol=0)
    summary=dict(final,initial_cash=INIT_CASH,total_pnl=None if positions else cash-INIT_CASH,
                 max_drawdown=None if ever_incomplete else observed_dd,observed_max_drawdown=observed_dd,
                 max_drawdown_scope='欠損前の観測点のみ' if ever_incomplete else '全銘柄の観測始値・終値清算価値',
                 win_rate_pct=100*sum(t['net_pnl']>0 for t in trades)/len(trades) if trades else 0.,
                 trades=len(trades),unresolved=bool(positions),halted_days=len(halt_days),data_stop_days=sum(row['data_entry_stop'] for row in daily),data_incident_days=len(data_days),
                 period_start=min(t for code,t in bars),period_end=max(t for code,t in bars),symbols=len(codes),
                 status='未決済・総損益未確定' if positions else '計算完了（選定・データ制約あり）')
    summary.pop('holdings',None)
    holdings=[dict(code=code,quantity=p.quantity,buy=p.buy,entry_fee=p.entry_fee,entry_time=p.entry_time,
                   book_cost=p.book,last_known_price=p.last_price,unresolved=p.unresolved) for code,p in sorted(positions.items())]
    return dict(summary=summary,trades=trades,orders=orders,equity=equity,daily=daily,positions=holdings)
