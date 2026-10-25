"""Conservative causal data handling; never invent a fill across missing observations."""
import numpy as np
import pandas as pd
from .frozen.engine_1a488e3 import (Costs, INIT_CASH, MAX_ORDER_YEN, SHARES_LOT,
    MIN_DAYS, MAX_TRADE_LOSS_YEN, DAILY_LOSS_LIMIT_YEN, max_drawdown)


def trading_gap_minutes(a, b):
    recess_start = a.normalize() + pd.Timedelta(hours=11, minutes=30)
    recess_end = a.normalize() + pd.Timedelta(hours=12, minutes=30)
    overlap = max(0, (min(b,recess_end)-max(a,recess_start)).total_seconds()/60)
    return (b-a).total_seconds()/60-overlap


def simulate(intraday, strategy, slip, code, name, costs=None, audit=None):
    costs = costs or Costs()
    if not np.isfinite(slip) or slip < 0:
        raise ValueError('スリッページは有限の非負値で指定してください')
    capital = float(INIT_CASH)
    trades, period_equity, day_end_equity = [], [capital], [capital]
    skipped, used = [], []
    audit = audit if audit is not None else []
    unresolved = None
    cancelled = 0
    last_equity = float(INIT_CASH)
    signals = cap_rejected = cash_rejected = stop_count = halt_days = 0
    max_intraday_dd = 0.0
    for date, src in intraday.groupby(intraday.index.date):
        d = src.copy()
        # Opening availability is known at the first observation, not from the day's tail.
        if d.index[0].strftime('%H:%M') != '09:00':
            skipped.append(f'{date}: 初足09:00欠落、当日は新規停止')
            audit.append({'date':str(date),'observed_at':str(d.index[0]),'event':'opening_missing','held':False})
            continue
        processed_until = None
        d['ma3'] = d.Close.rolling(3).mean()
        typical = (d.High + d.Low + d.Close) / 3.0
        d['vwap'] = (typical * d.Volume).cumsum() / d.Volume.cumsum().replace(0,np.nan)
        d['volavg5'] = d.Volume.shift(1).rolling(5).mean()
        d['prior6high'] = d.High.shift(1).rolling(6).max()
        held = False
        buy = qty = entry_fee = 0
        entry_when = entry_signal = None
        day_pnl = 0.0
        halted = False
        pending = None
        day_equity = [capital]

        def mark(price, when):
            # Net liquidation estimate: sell slippage, exit fee and accrued financing.
            value = capital
            if held:
                sell = max(0.01, price - slip)
                value += sell * qty - costs.fee(sell * qty) - costs.interest(buy * qty, entry_when, when)
            day_equity.append(value)
            period_equity.append(value)

        def close_trade(bar, reason, signal_time):
            nonlocal capital, held, day_pnl, stop_count, halted
            px = max(0.01, float(bar.Open) - slip)
            exit_fee = costs.fee(px * qty)
            interest = costs.interest(buy * qty, entry_when, bar.name)
            gross = (px - buy) * qty
            pnl = gross - entry_fee - exit_fee - interest
            capital += px * qty - exit_fee - interest
            day_pnl += pnl
            trades.append({'コード':code, '候補名':name, '戦略':strategy,
                '費用シナリオ':costs.name, '仮定片道スリッページ(円/株)':slip,
                '買いシグナル足':entry_signal, '買い日時':entry_when,
                '売りシグナル足':signal_time, '売り日時':bar.name,
                '数量(株)':qty, '買値(円)':buy, '売値(円)':px,
                '費用控除前損益(円)':gross, '手数料(円)':entry_fee+exit_fee,
                '信用金利(円)':interest, '損益(円)':pnl, '決済理由':reason,
                '当日実現損益(円)':day_pnl})
            if reason == '損切り判定':
                stop_count += 1
            held = False
            if day_pnl <= -DAILY_LOSS_LIMIT_YEN:
                halted = True

        # Sequential execution: previous close signal -> current open fill -> close observation.
        # 15:25 liquidation is pre-scheduled; it never uses the eventual last available row.
        for i in range(len(d)):
            now = d.iloc[i]
            if i and trading_gap_minutes(d.index[i-1], now.name) > 5:
                cancelled += int(pending is not None and pending[0] == 'buy')
                audit.append({'date':str(date),'observed_at':str(now.name),
                              'event':'gap_unresolved' if held else 'gap_halt_flat',
                              'held':bool(held),'last_observed':str(d.index[i-1]),
                              'cancelled_buy':bool(pending is not None and pending[0]=='buy')})
                skipped.append(f'{date}: {d.index[i-1]:%H:%M}以降欠損、新規停止')
                if held:
                    unresolved = {'entry':str(entry_when),'quantity':qty,'buy':buy,
                                  'entry_fee':entry_fee,'last_observed':str(d.index[i-1])}
                pending = None
                break
            if not np.isfinite(float(now.Open)) or now.Open <= 0:
                skipped.append(f'{date}: 始値未観測、新規停止')
                audit.append({'date':str(date),'observed_at':str(now.name),'event':'invalid_open','held':bool(held)})
                if held:
                    unresolved={'entry':str(entry_when),'quantity':qty,'buy':buy,'entry_fee':entry_fee,'last_observed':str(d.index[i-1])}
                pending=None
                break
            processed_until = now.name
            # Clock-based liquidation at 15:25, never at the retrospectively last available bar.
            if held and now.name.strftime('%H:%M') == '15:25':
                close_trade(now, '事前設定15:25始値仮決済', '事前設定')
                pending = None
            elif pending:
                action, reason, signal_time, requested_qty = pending
                pending = None
                if action == 'sell':
                    close_trade(now, reason, signal_time)
                else:
                    buy_candidate = float(now.Open) + slip
                    q = requested_qty
                    if not np.isfinite(buy_candidate) or buy_candidate <= 0:
                        raise ValueError('不正な約定候補価格')
                    if buy_candidate*q > MAX_ORDER_YEN:
                        cap_rejected += 1
                    elif buy_candidate*q+costs.fee(buy_candidate*q) > capital:
                        cash_rejected += 1
                    else:
                        entry_fee = costs.fee(buy_candidate*q)
                        capital -= buy_candidate*q + entry_fee
                        buy, qty, entry_when, entry_signal, held = buy_candidate, q, now.name, signal_time, True
            mark(float(now.Open), now.name)
            if now.name.strftime('%H:%M') >= '15:25':
                break  # The scheduled open liquidation does not depend on this bar's close.
            if not np.isfinite([now.High,now.Low,now.Close,now.Volume]).all() or min(now.High,now.Low,now.Close) <= 0:
                skipped.append(f'{date}: 完成足データ不足、新規停止')
                audit.append({'date':str(date),'observed_at':str(now.name+pd.Timedelta(minutes=5)),
                              'event':'invalid_completed_bar','held':bool(held)})
                if held:
                    unresolved={'entry':str(entry_when),'quantity':qty,'buy':buy,'entry_fee':entry_fee,
                                'last_observed':str(now.name),'valuation':'当該足始値まで。終値未取得'}
                pending=None
                break
            mark(float(now.Close), now.name)
            # No new signal after liquidation cutoff; no same-bar fills.
            if i < 6 or now.name.strftime('%H:%M') >= '15:20':
                continue
            prev = d.iloc[i-1]
            ma_up = pd.notna(prev.ma3) and prev.Close <= prev.ma3 and now.Close > now.ma3
            ma_down = pd.notna(prev.ma3) and prev.Close >= prev.ma3 and now.Close < now.ma3
            strong_volume = pd.notna(now.volavg5) and now.Volume > now.volavg5 * 1.5
            if strategy == 'MAのみ':
                signal = ma_up
            elif strategy == 'VWAP+出来高':
                signal = ma_up and now.Close > now.vwap and strong_volume
            else:
                signal = pd.notna(now.prior6high) and now.Close > now.prior6high and now.Close > now.vwap and strong_volume
            if held:
                unrealized = (max(0.01,float(now.Close)-slip)-buy)*qty-entry_fee
                unrealized -= costs.fee(max(0.01,float(now.Close)-slip)*qty)
                unrealized -= costs.interest(buy*qty, entry_when, now.name)
                if unrealized <= -MAX_TRADE_LOSS_YEN:
                    pending = ('sell', '損切り判定', now.name, 0)
                elif ma_down:
                    pending = ('sell', 'MA下抜け', now.name, 0)
            elif signal and not halted:
                signals += 1
                # Quantity is fixed using only signal-close information.
                reference = float(now.Close)+slip
                if not np.isfinite(reference) or reference <= 0:
                    raise ValueError('不正なシグナル価格')
                cap_qty = int(MAX_ORDER_YEN // (reference*SHARES_LOT))*SHARES_LOT
                cash_qty = int(max(0,capital-costs.fee_yen) //
                               (reference*SHARES_LOT*(1+costs.fee_bps/10000)))*SHARES_LOT
                requested_qty = min(cap_qty,cash_qty)
                if cap_qty < SHARES_LOT:
                    cap_rejected += 1
                elif requested_qty < SHARES_LOT:
                    cash_rejected += 1
                else:
                    pending = ('buy', '', now.name, requested_qty)
        if processed_until is not None:
            used.append(d.loc[:processed_until])
        if held and unresolved is None:
            skipped.append(f'{date}: 15:25決済足不足・未決済')
            unresolved={'entry':str(entry_when),'quantity':qty,'buy':buy,'entry_fee':entry_fee,
                        'last_observed':str(processed_until)}
            audit.append({'date':str(date),'observed_at':str(processed_until),
                          'event':'session_tail_unresolved','held':True})
        if halted:
            halt_days += 1
        max_intraday_dd = max(max_intraday_dd, max_drawdown(day_equity))
        last_equity = period_equity[-1]
        if unresolved:
            break  # Exposure cannot be reset into cash at the next day without a known fill.
        day_end_equity.append(capital)
    pnls = [t['損益(円)'] for t in trades]
    if unresolved is None and not np.isclose(sum(pnls), capital-INIT_CASH, atol=1e-6, rtol=0):
        raise ValueError('売買記録と資金残高の損益が一致しません')
    used_days = len(used)
    reason = ('データ不足（観測できた時系列処理日数不足）' if used_days < MIN_DAYS else
              '建玉上限30万円・100株単位による見送り' if cap_rejected else
              '利用可能資金不足（手数料含む）' if cash_rejected else '条件不成立')
    return {'コード':code,'候補名':name,'戦略':strategy,'片道スリッページ(円/株)':slip,
        '費用シナリオ':costs.name,'片道固定手数料(円)':costs.fee_yen,
        '片道手数料(bps)':costs.fee_bps,'仮定年利(%)':costs.annual_interest_pct,
        '取得開始':str(intraday.index.min()),'取得終了':str(intraday.index.max()),
        '開始':str(used[0].index.min()) if used else '', '終了':str(used[-1].index.max()) if used else '',
        '足種':'5分足（Asia/Tokyo）','データ日数':len(set(intraday.index.date)),
        '利用日数':used_days,'利用日数定義':'一部でも時系列処理できた日（全日品質合格ではない）',
        'データ停止日数':len(skipped),'データ停止理由':'; '.join(skipped),
        '検証状態':'未決済・総損益未確定' if unresolved else '利用日数不足' if used_days < MIN_DAYS else '検証済み',
        '取引数':len(trades),'買いシグナル数':signals,
        '建玉上限で見送り':cap_rejected,'資金不足で見送り':cash_rejected,
        '平均株数':round(np.mean([t['数量(株)'] for t in trades]),1) if trades else 0,
        '最大建玉(円)':max((t['数量(株)']*t['買値(円)'] for t in trades),default=0),
        '損切り回数':stop_count,'取引停止日数':halt_days,
        '取引0回の理由':reason if not trades else '',
        '勝率(%)':round(100*sum(p>0 for p in pnls)/len(pnls),1) if pnls else 0,
        '費用控除前損益(円)':sum(t['費用控除前損益(円)'] for t in trades),
        '手数料合計(円)':sum(t['手数料(円)'] for t in trades),
        '信用金利合計(円)':sum(t['信用金利(円)'] for t in trades),
        '損益(円)':None if unresolved else capital-INIT_CASH,
        '完了取引損益(円)':sum(pnls),'未決済情報':str(unresolved) if unresolved else '',
        '残存株数':unresolved['quantity'] if unresolved else 0,
        '最終観測推定清算価値(円)':last_equity,'欠損で買い注文取消':cancelled,
        'DD範囲':'未決済前の観測範囲のみ' if unresolved else '観測済み評価点',
        '日内最大DD(円)':max_intraday_dd,
        '期間最大DD(円)':max_drawdown(period_equity),
        '日末残高系列最大DD(円)':max_drawdown(day_end_equity),
        '注意':'時系列品質処理／欠損時保有は架空決済せず総損益未確定／実注文なし'}, trades

