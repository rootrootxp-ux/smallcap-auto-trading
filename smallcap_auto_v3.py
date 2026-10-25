"""Japanese small/mid cap RESEARCH helper. No brokerage access or orders."""
from pathlib import Path
from datetime import datetime
import csv
import html
import time
import traceback
import numpy as np
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'results'
OUT.mkdir(exist_ok=True)
INPUT = ROOT / 'watchlist.csv'
INIT_CASH = 1_000_000
MAX_ORDER_YEN = 300_000
SHARES_LOT = 100
MIN_AVG_VALUE_OKU = 1.0
MIN_RANGE_PCT = 2.0
COST_PER_SHARE = (1.0, 2.0)
MIN_DAYS = 8
MAX_TRADE_LOSS_YEN = 2_000  # target only, not guaranteed
DAILY_LOSS_LIMIT_YEN = 5_000 # halts new entries after realized loss threshold
MIN_BARS_DAY = 40


def write_csv(path, rows, columns):
    with path.open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


def fetch(ticker, interval, period):
    frame = yf.download(ticker, interval=interval, period=period,
                        auto_adjust=False, progress=False, threads=False,
                        prepost=False)
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    if frame.empty:
        return frame
    frame = frame.rename(columns=str).sort_index()
    frame = frame[~frame.index.duplicated(keep='first')]
    frame = frame.dropna(subset=['Open','High','Low','Close','Volume'])
    if interval == '5m':
        if frame.index.tz is None:
            frame.index = frame.index.tz_localize('Asia/Tokyo')
        else:
            frame.index = frame.index.tz_convert('Asia/Tokyo')
        frame = frame.between_time('09:00', '15:30')
    return frame


def screen(symbol, name, d):
    value = d.Close * d.Volume
    intraday_range = 100 * (d.High - d.Low) / d.Close.replace(0,np.nan)
    latest = float(d.Close.iloc[-1])
    val = float(value.mean() / 1e8)
    rang = float(intraday_range.mean())
    # Important: membership is an UNVERIFIED research list, not actual point-in-time market cap.
    group = ('両条件' if val >= MIN_AVG_VALUE_OKU and rang >= MIN_RANGE_PCT
             else '値動き候補' if rang >= MIN_RANGE_PCT
             else '流動性候補' if val >= MIN_AVG_VALUE_OKU else '基準未満')
    return {'コード':symbol,'候補名':name,'最新終値(円)':round(latest,2),
            '日足本数':len(d),'平均日中値幅(%)':round(rang,2),
            '平均売買代金(億円)':round(val,2),'分類':group,
            '時価総額確認':'未確認','信用取引可否':'未確認',
            '注意':'現時点の日足による分類。過去の銘柄選択を再現しない'}


def quality_check(d):
    """Reject missing or irregular session bars; holidays/halts need manual review."""
    if len(d) < MIN_BARS_DAY:
        return f'本数不足({len(d)}本)'
    times = d.index.tz_convert('Asia/Tokyo')
    if times[0].hour > 9 or (times[0].hour == 9 and times[0].minute > 10):
        return '寄り付き付近のデータなし'
    if times[-1].hour < 15 or (times[-1].hour == 15 and times[-1].minute < 15):
        return '引け付近のデータなし'
    for a, b in zip(times[:-1], times[1:]):
        gap = (b-a).total_seconds()/60
        # Trading lunch recess crosses noon; allow this calendar gap.
        lunch = a.hour == 11 and b.hour in (12, 13)
        if gap > 10 and not lunch:
            return f'足の欠損疑い({a:%H:%M}-{b:%H:%M})'
    return ''


def simulate(intraday, strategy, slip, code, name):
    capital = float(INIT_CASH)
    trades, daily_equity = [], [capital]
    skipped = []
    signals = rejected = stop_count = halt_days = 0
    for date, src in intraday.groupby(intraday.index.date):
        d = src.copy()
        problem = quality_check(d)
        if problem:
            skipped.append(f'{date}: {problem}')
            continue
        d['ma3'] = d.Close.rolling(3).mean()
        typical = (d.High + d.Low + d.Close) / 3.0
        d['vwap'] = (typical * d.Volume).cumsum() / d.Volume.cumsum().replace(0,np.nan)
        d['volavg5'] = d.Volume.shift(1).rolling(5).mean()
        d['prior6high'] = d.High.shift(1).rolling(6).max()
        held = False
        buy = 0.0
        qty = 0
        entry_when = None
        day_pnl = 0.0
        halted = False

        def close_trade(bar, reason):
            nonlocal capital, held, buy, qty, day_pnl, stop_count, halted
            px = max(0.01, float(bar.Open) - slip)
            capital += px * qty
            pnl = (px - buy) * qty
            day_pnl += pnl
            trades.append({'コード':code,'候補名':name,'戦略':strategy,
                '仮定片道スリッページ(円/株)':slip,'買い日時':entry_when,
                '売り日時':bar.name,'数量(株)':qty,'買値(円)':buy,
                '売値(円)':px,'損益(円)':round(pnl,2),'決済理由':reason,
                '当日実現損益(円)':round(day_pnl,2)})
            if reason == '損切り判定':
                stop_count += 1
            held = False
            if day_pnl <= -DAILY_LOSS_LIMIT_YEN:
                halted = True

        # Signal at i close; order modeled at i+1 Open. Last bar Open reserved for EOD exit.
        for i in range(6, len(d)-2):
            prev, now, nxt = d.iloc[i-1], d.iloc[i], d.iloc[i+1]
            ma_up = pd.notna(prev.ma3) and prev.Close <= prev.ma3 and now.Close > now.ma3
            ma_down = pd.notna(prev.ma3) and prev.Close >= prev.ma3 and now.Close < now.ma3
            strong_volume = pd.notna(now.volavg5) and now.Volume > now.volavg5 * 1.5
            if strategy == 'MAのみ':
                signal = ma_up
            elif strategy == 'VWAP+出来高':
                signal = ma_up and now.Close > now.vwap and strong_volume
            else:
                signal = (pd.notna(now.prior6high) and now.Close > now.prior6high
                          and now.Close > now.vwap and strong_volume)

            if held:
                # No knowledge of intrabar fills. Close-based trigger, next-bar-open exit.
                unrealized = (float(now.Close) - buy) * qty
                if unrealized <= -MAX_TRADE_LOSS_YEN:
                    close_trade(nxt, '損切り判定')
                elif ma_down:
                    close_trade(nxt, 'MA下抜け')
            elif signal and not halted:
                signals += 1
                buy_candidate = float(nxt.Open) + slip
                if buy_candidate <= 0:
                    rejected += 1
                    continue
                q = int(min(MAX_ORDER_YEN, capital) // (buy_candidate * SHARES_LOT)) * SHARES_LOT
                if q >= SHARES_LOT:
                    capital -= buy_candidate * q
                    buy, qty, entry_when, held = buy_candidate, q, nxt.name, True
                else:
                    rejected += 1
        if held:
            # EOD fill is a modeled assumption, not guaranteed in the real market.
            close_trade(d.iloc[-1], '当日終端・最終足始値仮決済')
        if halted:
            halt_days += 1
        daily_equity.append(capital)
    pnls = [float(t['損益(円)']) for t in trades]
    quantities = [int(t['数量(株)']) for t in trades]
    amounts = [float(t['数量(株)']) * float(t['買値(円)']) for t in trades]
    if abs(sum(pnls) - (capital - INIT_CASH)) > 1.0:
        raise ValueError('売買記録と資金残高の損益が一致しません')
    equity = pd.Series(daily_equity)
    dd = float((equity-equity.cummax()).min())
    return {'コード':code,'候補名':name,'戦略':strategy,'片道スリッページ(円/株)':slip,
        '開始':str(intraday.index.min()),'終了':str(intraday.index.max()),
        'データ日数':len(set(intraday.index.date)),'スキップ日数':len(skipped),
        'スキップ理由':'; '.join(skipped[:4]),
        '取引数':len(trades),'買いシグナル数':signals,'資金不足で見送り':rejected,
        '平均株数':round(sum(quantities)/len(quantities),1) if quantities else 0,
        '最大建玉(円)':round(max(amounts)) if amounts else 0,
        '損切り回数':stop_count,'取引停止日数':halt_days,
        '取引0回の理由':('100株購入に資金または建玉上限が不足' if not trades and rejected
                     else '条件不成立またはデータ品質不足' if not trades else ''),
        '勝率(%)':round(100*sum(p>0 for p in pnls)/len(pnls),1) if pnls else 0,
        '損益(円)':round(capital-INIT_CASH),'日次最大DD(円)':round(dd),
        '注意':'損失上限は保証されない／損切りは終値で判定し次足始値約定を仮定／日次DDは終値評価ではない／実注文なし'}, trades


def html_report(stamp, screen_rows, summary_rows, status_rows):
    # Static HTML can be opened locally without network; intended for quick reading on PC.
    def table(rows, columns):
        if not rows:
            return '<p>該当データなし</p>'
        th = ''.join(f'<th>{html.escape(x)}</th>' for x in columns)
        trs = ''.join('<tr>'+''.join('<td>'+html.escape(str(r.get(c,'')))+'</td>' for c in columns)+'</tr>' for r in rows)
        return '<table><thead><tr>'+th+'</tr></thead><tbody>'+trs+'</tbody></table>'
    screen_cols = ['コード','候補名','最新終値(円)','平均日中値幅(%)','平均売買代金(億円)','分類']
    sum_cols = ['コード','戦略','片道スリッページ(円/株)','取引数','勝率(%)','損益(円)','日次最大DD(円)','平均株数','損切り回数','取引停止日数','スキップ日数','取引0回の理由']
    status_cols = ['コード','候補名','状態','日足本数','5分足本数','詳細']
    doc = f'''<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>小型株研究レポート</title><style>body{{font-family:system-ui,sans-serif;max-width:1050px;margin:24px auto;padding:0 12px;background:#f8fafc;color:#172033}}h1{{font-size:1.6rem}}h2{{margin-top:28px}}.note{{padding:12px;background:#fff4da;border-radius:10px}}.scroll{{overflow-x:auto}}table{{border-collapse:collapse;background:white;width:100%;font-size:13px}}th,td{{border-bottom:1px solid #ddd;padding:9px;text-align:right;white-space:nowrap}}th:first-child,td:first-child{{text-align:left}}th{{background:#e8eef5}}</style>
<h1>小型株 自動分析レポート</h1><p>作成: {html.escape(stamp)}</p>
<p class="note">研究用・実際の注文なし。銘柄ごとに資金100万円で独立検証、1取引の建玉上限30万円、100株単位。以前の固定100株の検証結果とは金額を直接比較できません。時価総額・信用取引可否は未確認。手数料・信用金利は含まず、片道スリッページを仮定。損切り2,000円と日次停止5,000円は保証された損失上限ではありません。最終足始値での決済は約定を保証しません。取引可能時間、データ欠損、実際の板やスプレッドは完全に再現していません。将来の利益を保証しません。</p>
<h2>候補銘柄の分類（日足）</h2><div class="scroll">{table(screen_rows,screen_cols)}</div>
<h2>バックテスト結果（1銘柄ずつ独立して資金100万円を仮定）</h2><div class="scroll">{table(summary_rows,sum_cols)}</div>
<h2>取得状況・エラー</h2><div class="scroll">{table(status_rows,status_cols)}</div>
<p>さらに詳しい情報は同じ results フォルダ内の CSV を参照してください。</p></html>'''
    (OUT / 'report.html').write_text(doc, encoding='utf-8')


def main():
    stamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with INPUT.open(encoding='utf-8-sig', newline='') as f:
        items = list(csv.DictReader(f))
    screen_rows, summary_rows, trade_rows, status_rows = [], [], [], []
    for count, item in enumerate(items,1):
        code = item['コード'].strip()
        name = item.get('候補名','').strip() or code
        if not code:
            continue
        print(f'[{count}/{len(items)}] {name} ({code}) ...',flush=True)
        row={'コード':code,'候補名':name,'状態':'','日足本数':0,'5分足本数':0,'詳細':''}
        try:
            daily = fetch(code,'1d','3mo')
            row['日足本数']=len(daily)
            if len(daily)<10:
                raise ValueError('日足が10本未満')
            screen_rows.append(screen(code,name,daily))
            five = fetch(code,'5m','30d')
            row['5分足本数']=len(five)
            if five.empty or len(set(five.index.date))<MIN_DAYS:
                raise ValueError('5分足の取引日数が不足')
            for strategy in ['MAのみ','VWAP+出来高','高値ブレイク+出来高']:
                for slip in COST_PER_SHARE:
                    s, history = simulate(five,strategy,slip,code,name)
                    summary_rows.append(s)
                    trade_rows.extend(history)
            row['状態']='成功'
            # Save intraday only if wanted for troubleshooting; 20 files can be large.
            (OUT/'prices').mkdir(exist_ok=True)
            five.to_csv(OUT/'prices'/f'{code.replace(".","_")}_5m.csv',encoding='utf-8-sig')
        except Exception as e:
            row['状態']='確認必要'
            row['詳細']=str(e)
            print('  注意:',e,flush=True)
        status_rows.append(row)
        time.sleep(1)  # Gentle throttle; no guarantee against provider rate limits.
    screening_cols = ['コード','候補名','最新終値(円)','日足本数','平均日中値幅(%)','平均売買代金(億円)','分類','時価総額確認','信用取引可否','注意']
    summary_cols = ['コード','候補名','戦略','片道スリッページ(円/株)','開始','終了','データ日数','スキップ日数','スキップ理由','取引数','買いシグナル数','資金不足で見送り','平均株数','最大建玉(円)','損切り回数','取引停止日数','取引0回の理由','勝率(%)','損益(円)','日次最大DD(円)','注意']
    trade_cols = ['コード','候補名','戦略','仮定片道スリッページ(円/株)','買い日時','売り日時','数量(株)','買値(円)','売値(円)','損益(円)','決済理由','当日実現損益(円)']
    status_cols = ['コード','候補名','状態','日足本数','5分足本数','詳細']
    screen_rows.sort(key=lambda x:x['平均売買代金(億円)'],reverse=True)
    summary_rows.sort(key=lambda x:(x['コード'],x['戦略'],x['片道スリッページ(円/株)']))
    write_csv(OUT/'screening.csv',screen_rows,screening_cols)
    write_csv(OUT/'backtest_summary.csv',summary_rows,summary_cols)
    write_csv(OUT/'trade_history.csv',trade_rows,trade_cols)
    write_csv(OUT/'run_status.csv',status_rows,status_cols)
    html_report(stamp,screen_rows,summary_rows,status_rows)
    print(f'\n完了: 成功 {sum(r["状態"]=="成功" for r in status_rows)}/{len(status_rows)}銘柄。results/report.html を開いてください。',flush=True)

if __name__=='__main__':
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
