"""Japanese small/mid cap RESEARCH helper. No brokerage access or orders."""
from pathlib import Path
from datetime import datetime
import csv
import argparse
from dataclasses import dataclass
import html
import time
import traceback
import sys
import hashlib
import shutil
from uuid import uuid4
from zoneinfo import ZoneInfo
from curl_cffi.requests import RequestsError
from requests.exceptions import RequestException
from run_outcome import summarize, MIN_QUALIFIED_DAYS
from symbol_notes import SYMBOL_NOTES
from universe_analysis import write_analysis
from rank_sensitivity import write_rank_sensitivity
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
MIN_DAYS = MIN_QUALIFIED_DAYS
MAX_TRADE_LOSS_YEN = 2_000  # target only, not guaranteed
DAILY_LOSS_LIMIT_YEN = 5_000 # halts new entries after realized loss threshold
MIN_BARS_DAY = 40


def write_csv(path, rows, columns):
    with path.open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


class DataRetrievalError(Exception):
    """Expected upstream transport, missing-ticker, or no-data failure."""


class InsufficientDataError(Exception):
    """Data was returned but is insufficient for the unchanged quality policy."""


def fetch(ticker, interval, period):
    # Preserve provider errors using the pinned yfinance configuration API.
    previous_hide = yf.config.debug.hide_exceptions
    try:
        yf.config.debug.hide_exceptions = False
        frame = yf.Ticker(ticker).history(interval=interval, period=period,
                    auto_adjust=False, prepost=False, actions=False)
    except (yf.exceptions.YFTickerMissingError, yf.exceptions.YFRateLimitError,
            yf.exceptions.YFDataException, RequestsError, RequestException) as exc:
        raise DataRetrievalError(f'{ticker} {interval}/{period}: {type(exc).__name__}: {exc}') from exc
    finally:
        yf.config.debug.hide_exceptions = previous_hide
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
        # Subtract only the scheduled 11:30–12:30 recess, not arbitrary noon gaps.
        recess_start = a.normalize() + pd.Timedelta(hours=11, minutes=30)
        recess_end = a.normalize() + pd.Timedelta(hours=12, minutes=30)
        overlap = max(0, (min(b, recess_end)-max(a, recess_start)).total_seconds()/60)
        if gap - overlap > 10:
            return f'足の欠損疑い({a:%H:%M}-{b:%H:%M})'
    return ''


@dataclass(frozen=True)
class Costs:
    name: str = '費用ゼロ'
    fee_yen: float = 0.0  # per execution, both entry and exit
    fee_bps: float = 0.0
    annual_interest_pct: float = 0.0

    def __post_init__(self):
        if any(not np.isfinite(x) or x < 0 for x in
               (self.fee_yen, self.fee_bps, self.annual_interest_pct)):
            raise ValueError('費用は有限の非負値で指定してください')

    def fee(self, amount):
        return self.fee_yen + amount * self.fee_bps / 10000

    def interest(self, amount, entry, exit):
        days = max(1, (exit.date() - entry.date()).days + 1)
        return amount * self.annual_interest_pct / 100 * days / 365


def max_drawdown(values):
    """Positive peak-to-trough loss, including the initial equity."""
    peak = float(values[0])
    result = 0.0
    for value in values:
        peak = max(peak, value)
        result = max(result, peak - value)
    return result


def simulate(intraday, strategy, slip, code, name, costs=None):
    costs = costs or Costs()
    if not np.isfinite(slip) or slip < 0:
        raise ValueError('スリッページは有限の非負値で指定してください')
    capital = float(INIT_CASH)
    trades, period_equity, day_end_equity = [], [capital], [capital]
    skipped, used = [], []
    signals = cap_rejected = cash_rejected = stop_count = halt_days = 0
    max_intraday_dd = 0.0
    for date, src in intraday.groupby(intraday.index.date):
        d = src.copy()
        problem = quality_check(d)
        if problem:
            skipped.append(f'{date}: {problem}')
            continue
        used.append(d)
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
        # The last bar open is a pre-scheduled liquidation; it uses no last-bar close.
        for i in range(len(d)):
            now = d.iloc[i]
            if held and i == len(d)-1:
                close_trade(now, '当日終端・最終足始値仮決済', '事前設定')
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
            mark(float(now.Close), now.name)
            # No new signal after liquidation cutoff; no same-bar fills.
            if i < 6 or i >= len(d)-2:
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
        if halted:
            halt_days += 1
        max_intraday_dd = max(max_intraday_dd, max_drawdown(day_equity))
        day_end_equity.append(capital)
    pnls = [t['損益(円)'] for t in trades]
    if not np.isclose(sum(pnls), capital-INIT_CASH, atol=1e-6, rtol=0):
        raise ValueError('売買記録と資金残高の損益が一致しません')
    used_days = len(used)
    reason = ('データ不足（品質検査後の利用日数不足）' if used_days < MIN_DAYS else
              '建玉上限30万円・100株単位による見送り' if cap_rejected else
              '利用可能資金不足（手数料含む）' if cash_rejected else '条件不成立')
    return {'コード':code,'候補名':name,'戦略':strategy,'片道スリッページ(円/株)':slip,
        '費用シナリオ':costs.name,'片道固定手数料(円)':costs.fee_yen,
        '片道手数料(bps)':costs.fee_bps,'仮定年利(%)':costs.annual_interest_pct,
        '取得開始':str(intraday.index.min()),'取得終了':str(intraday.index.max()),
        '開始':str(used[0].index.min()) if used else '', '終了':str(used[-1].index.max()) if used else '',
        '足種':'5分足（Asia/Tokyo）','データ日数':len(set(intraday.index.date)),
        '利用日数':used_days,'スキップ日数':len(skipped),'スキップ理由':'; '.join(skipped),
        '検証状態':'利用日数不足' if used_days < MIN_DAYS else '検証済み',
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
        '損益(円)':capital-INIT_CASH,
        '日内最大DD(円)':max_intraday_dd,
        '期間最大DD(円)':max_drawdown(period_equity),
        '日末残高系列最大DD(円)':max_drawdown(day_end_equity),
        '注意':'実注文なし／日数不足の結果は参考値／全日品質判定は事後的／損失上限は保証されない'}, trades


def html_report(stamp, screen_rows, summary_rows, status_rows, outcome=None, analysis_fragment=''):
    outcome = outcome or summarize(status_rows)
    # Static HTML can be opened locally without network; intended for quick reading on PC.
    def table(rows, columns):
        if not rows:
            return '<p>該当データなし</p>'
        th = ''.join(f'<th>{html.escape(x)}</th>' for x in columns)
        trs = ''.join('<tr>'+''.join('<td>'+html.escape(str(r.get(c,'')))+'</td>' for c in columns)+'</tr>' for r in rows)
        return '<table><thead><tr>'+th+'</tr></thead><tbody>'+trs+'</tbody></table>'
    screen_cols = ['コード','候補名','最新終値(円)','平均日中値幅(%)','平均売買代金(億円)','分類']
    sum_cols = ['コード','戦略','費用シナリオ','片道スリッページ(円/株)','検証状態','足種','開始','終了','データ日数','利用日数','スキップ日数','取引数','勝率(%)','費用控除前損益(円)','手数料合計(円)','信用金利合計(円)','損益(円)','日内最大DD(円)','期間最大DD(円)','日末残高系列最大DD(円)','建玉上限で見送り','資金不足で見送り','取引0回の理由']
    status_cols = ['コード','候補名','状態','日足本数','日足開始','日足終了','5分足本数','取得開始','取得終了','足種','検証開始','検証終了','取得日数','利用日数','除外日数','日足参照欠落日数','欠落日','除外理由','取得段階','問題種別','銘柄情報','後継コード候補','参考URL','詳細','処理秒数','取得秒数','計算秒数','実行ID','全体結果']
    doc = f'''<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>小型株研究レポート</title><style>body{{font-family:system-ui,sans-serif;max-width:1050px;margin:24px auto;padding:0 12px;background:#f8fafc;color:#172033}}h1{{font-size:1.6rem}}h2{{margin-top:28px}}.note{{padding:12px;background:#fff4da;border-radius:10px}}.scroll{{overflow-x:auto}}table{{border-collapse:collapse;background:white;width:100%;font-size:13px}}th,td{{border-bottom:1px solid #ddd;padding:9px;text-align:right;white-space:nowrap}}th:first-child,td:first-child{{text-align:left}}th{{background:#e8eef5}}</style>
<h1>小型株 自動分析レポート</h1><p>作成: {html.escape(stamp)}（日本時間）</p>
<h2>全体結果: {html.escape(outcome['全体結果'])}</h2>
<div class="scroll">{table([outcome],list(outcome))}</div>
<p>部分的な取得失敗・データ不足でも検証可能銘柄の結果は保存します。重大不具合が1件でもある場合、または検証可能銘柄が最低必要数に届かない場合はActionsを失敗にします。データ不足銘柄の計算は参考値で、検証可能銘柄数には含めません。後継コードは情報のみで自動置換しません。</p>
<p class="note">研究用・実際の注文なし。銘柄ごとに資金100万円で独立検証（全銘柄共通の100万円口座を再現していません）、1取引の建玉上限30万円、100株単位。以前の固定100株の検証結果とは金額を直接比較できません。選定時の時価総額はスナップショットを参照。過去時点の時価総額・信用取引可否は未確認。費用ゼロと仮定費用込みを独立再計算（取引数も変わり得ます）。各行の片道スリッページは費用ゼロでも適用。損切り2,000円と日次停止5,000円は保証された損失上限ではありません。最終足始値での決済は約定を保証しません。取引可能時間、データ欠損、実際の板やスプレッドは完全に再現していません。将来の利益を保証しません。</p>
<h2>計算・費用の定義</h2>
<p>全時刻は日本時間。5分足の時刻は足の開始ラベルで、シグナルはその足の終値観測後、次足始値で約定。買値＝次足始値＋スリッページ、売値＝次足始値−スリッページ（下限0.01円）。株数はシグナル終値＋スリッページで事前決定し、次足価格で建玉上限・資金を超える場合は全注文を見送る価格ガードを仮定（次足価格による株数再計算なし）。最終足始値の決済は事前設定の仮定です。足内の価格順序・実際の約定可能性は未検証です。</p>
<p>費用ゼロ＝手数料・金利ゼロ。仮定費用込み＝片道固定手数料＋約定代金×bps/10000を売買両側に適用し、買い建玉代金×仮定年利/100×暦日数/365を決済時に控除（同日も最低1日）。全建玉代金に仮の信用金利を課す比較で、現物取引や実口座の再現ではありません。実在の証券会社の料金ではなく、税金・貸株料・その他費用は未反映です。損切りは費用見積り込み、日次停止は費用控除後実現損益で判定します。</p>
<div class="scroll">{table(list({r['費用シナリオ']:r for r in summary_rows}.values()), ['費用シナリオ','片道固定手数料(円)','片道手数料(bps)','仮定年利(%)'])}</div>
<p>DDは正の円額（ピークからの下落額）。日内最大DDは各日の開始資金を含め、各足始値・終値の推定清算価値で測った最大下落のうち最大値。期間最大DDは同じ系列のピークを日をまたいで保持。日末残高系列最大DDは各日決済後残高のみ（旧「日次最大DD」）。推定清算価値は現金＋保有株の評価額から売却スリッページ・売却手数料・金利を控除します。足の高値・安値を用いた連続時間DDではありません。損益とDDの一致だけでは不具合を意味しません。</p>
<p>利用日は取得5分足の品質検査を通過した日。除外日は取得済みだが検査不合格の日。欠落日は取得5分足期間内の日足に存在し5分足がない日（日足参照、取引所カレンダー照合なし）。欠落日数は除外日数に含めません。全日品質検査は事後判定で、実運用時点の銘柄・日選択を再現しません。品質判定後8日未満は「利用日数不足」の参考値です。</p>
{analysis_fragment}
<h2>候補銘柄の分類（日足）</h2><div class="scroll">{table(screen_rows,screen_cols)}</div>
<h2>バックテスト結果（1銘柄ずつ独立して資金100万円を仮定）</h2><div class="scroll">{table(summary_rows,sum_cols)}</div>
<h2>取得状況・エラー</h2><div class="scroll">{table(status_rows,status_cols)}</div>
<p>さらに詳しい情報は同じ results フォルダ内の CSV を参照してください。</p></html>'''
    (OUT / 'report.html').write_text(doc, encoding='utf-8')


def main(costs=None, watchlist=None):
    started = time.perf_counter()
    input_path = Path(watchlist) if watchlist is not None else INPUT
    counters = {'attempts':0,'failures':0,'fetch_seconds':0.,'simulation_seconds':0.}
    daily_frames = {}

    def acquire(code, interval, period):
        clock = time.perf_counter()
        counters['attempts'] += 1
        try:
            data = fetch(code, interval, period)
            if data.empty:
                counters['failures'] += 1
            return data
        except DataRetrievalError:
            counters['failures'] += 1
            raise
        finally:
            counters['fetch_seconds'] += time.perf_counter()-clock

    def calculate(*args):
        clock = time.perf_counter()
        try:
            return simulate(*args)
        finally:
            counters['simulation_seconds'] += time.perf_counter()-clock
    scenarios = [Costs(), costs or Costs('仮定費用込み', 100.0, 0.0, 3.0)]
    stamp = datetime.now(ZoneInfo('Asia/Tokyo')).strftime('%Y-%m-%d %H:%M:%S')
    run_id = uuid4().hex
    OUT.mkdir(parents=True, exist_ok=True)
    with input_path.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or 'コード' not in reader.fieldnames:
            raise ValueError('watchlist.csvにコード列がありません')
        items = list(reader)
    codes = [r['コード'].strip() for r in items if r['コード'].strip()]
    if not codes or len(codes) != len(set(codes)):
        raise ValueError('対象銘柄が空、または銘柄コードが重複しています')
    screen_rows, summary_rows, trade_rows, status_rows = [], [], [], []
    for count, item in enumerate(items,1):
        code = item['コード'].strip()
        name = item.get('候補名','').strip() or code
        if not code:
            continue
        symbol_started = time.perf_counter()
        fetch_before = counters['fetch_seconds']
        calc_before = counters['simulation_seconds']
        print(f'[{count}/{len(items)}] {name} ({code}) ...',flush=True)
        row={k:'' for k in ['コード', '候補名', '状態', '日足本数', '日足開始', '日足終了', '5分足本数', '取得開始', '取得終了', '足種', '検証開始', '検証終了', '取得日数', '利用日数', '除外日数', '日足参照欠落日数', '欠落日', '除外理由', '詳細']}
        row.update({'コード':code,'候補名':name,'日足本数':0,'5分足本数':0,'足種':'5分足（Asia/Tokyo）','実行ID':run_id})
        row.update(SYMBOL_NOTES.get(code, {}))
        stage = '日足取得'
        symbol_summaries, symbol_trades = [], []
        try:
            daily = acquire(code,'1d','3mo')
            daily_frames[code] = daily.copy()
            row['日足本数']=len(daily)
            if not daily.empty:
                row['日足開始']=str(daily.index.min())
                row['日足終了']=str(daily.index.max())
            if daily.empty:
                raise DataRetrievalError('日足データなし（提供元が空データを返却）')
            if len(daily)<10:
                raise InsufficientDataError('日足が10本未満')
            stage = '日足分類'
            screened = dict(screen(code,name,daily),実行ID=run_id)
            if item.get('market_cap_yen'):
                screened['時価総額確認'] = '選定時スナップショットあり・過去時点未確認'
            screen_rows.append(screened)
            stage = '5分足取得'
            five = acquire(code,'5m','30d')
            row['5分足本数']=len(five)
            if five.empty:
                raise DataRetrievalError('5分足データなし（提供元が空データを返却）')
            stage = '品質検査'
            good, excluded = [], []
            for date, frame in five.groupby(five.index.date):
                problem = quality_check(frame)
                if problem:
                    excluded.append(f'{date}: {problem}')
                else:
                    good.append(frame)
            observed = set(five.index.date)
            missing = sorted(date for date in set(daily.index.date)
                             if min(observed) <= date <= max(observed) and date not in observed)
            row.update({'取得開始':str(five.index.min()),'取得終了':str(five.index.max()),
                        '検証開始':str(good[0].index.min()) if good else '',
                        '検証終了':str(good[-1].index.max()) if good else '',
                        '取得日数':len(observed),'利用日数':len(good),'除外日数':len(excluded),
                        '日足参照欠落日数':len(missing),'欠落日':'; '.join(map(str,missing)),
                        '除外理由':'; '.join(excluded)})
            stage = 'バックテスト'
            for strategy in ['MAのみ','VWAP+出来高','高値ブレイク+出来高']:
                for slip in COST_PER_SHARE:
                    for scenario in scenarios:
                        result, history = calculate(five,strategy,slip,code,name,scenario)
                        result['日足参照欠落日数']=len(missing)
                        symbol_summaries.append(result)
                        symbol_trades.extend(history)
            row['状態']='検証可能' if len(good) >= MIN_DAYS else 'データ不足'
            row['問題種別']='なし' if len(good) >= MIN_DAYS else '品質合格日数不足'
            if len(good) < MIN_DAYS:
                row['詳細']=f'品質検査後{len(good)}日、最低{MIN_DAYS}日未満。結果は参考値'
            stage = '価格保存'
            price_dir = OUT/'prices'/run_id
            price_dir.mkdir(parents=True, exist_ok=True)
            five.to_csv(price_dir/f'{code.replace(".","_")}_5m.csv',encoding='utf-8-sig')
            # Commit complete symbol results only; unexpected errors never masquerade as data loss.
            for result in symbol_summaries + symbol_trades:
                result['実行ID'] = run_id
            summary_rows.extend(symbol_summaries)
            trade_rows.extend(symbol_trades)
        except InsufficientDataError as exc:
            row.update(状態='データ不足',問題種別=type(exc).__name__,詳細=str(exc))
        except DataRetrievalError as exc:
            row.update(状態='取得失敗',問題種別=type(exc).__name__,詳細=str(exc))
        except Exception as exc:
            row.update(状態='重大不具合',問題種別=type(exc).__name__,詳細=str(exc))
            traceback.print_exc()
            with (OUT/f'errors_{run_id}.log').open('a',encoding='utf-8') as f:
                f.write(f'{code} {stage}\n{traceback.format_exc()}\n')
        row['処理秒数'] = time.perf_counter()-symbol_started
        row['取得秒数'] = counters['fetch_seconds']-fetch_before
        row['計算秒数'] = counters['simulation_seconds']-calc_before
        row['取得段階'] = stage
        status_rows.append(row)
        time.sleep(1)  # Gentle throttle; no guarantee against provider rate limits.
    outcome = dict(summarize(status_rows), 実行ID=run_id, 作成日時=stamp,
                   対象銘柄コード=';'.join(codes), 銘柄リストSHA256=hashlib.sha256(input_path.read_bytes()).hexdigest())
    outcome.update({'実行秒数':time.perf_counter()-started,'株価取得呼出回数':counters['attempts'],
                    '株価取得失敗回数':counters['failures'],
                    '取得呼出失敗率(%)':100*counters['failures']/counters['attempts'] if counters['attempts'] else 0.,
                    '銘柄取得失敗率(%)':100*outcome['取得失敗銘柄数']/len(status_rows),
                    '取得秒数':counters['fetch_seconds'],'計算秒数':counters['simulation_seconds'],
                    '資金モデル':'各銘柄に100万円を独立配分。全銘柄共通100万円口座・ポートフォリオは再現しない。'})
    for row in status_rows:
        row['全体結果'] = outcome['全体結果']
    screening_cols = ['コード','候補名','最新終値(円)','日足本数','平均日中値幅(%)','平均売買代金(億円)','分類','時価総額確認','信用取引可否','注意','実行ID']
    summary_cols = list(summary_rows[0]) if summary_rows else ['コード','費用シナリオ','損益(円)']
    trade_cols = list(trade_rows[0]) if trade_rows else ['コード','費用シナリオ','損益(円)']
    status_cols = ['コード','候補名','状態','日足本数','日足開始','日足終了','5分足本数','取得開始','取得終了','足種','検証開始','検証終了','取得日数','利用日数','除外日数','日足参照欠落日数','欠落日','除外理由','取得段階','問題種別','銘柄情報','後継コード候補','参考URL','詳細','処理秒数','取得秒数','計算秒数','実行ID','全体結果']
    screen_rows.sort(key=lambda x:x['平均売買代金(億円)'],reverse=True)
    summary_rows.sort(key=lambda x:(x['コード'],x['戦略'],x['片道スリッページ(円/株)'],x['費用シナリオ']))
    write_csv(OUT/'screening.csv',screen_rows,screening_cols)
    write_csv(OUT/'backtest_summary.csv',summary_rows,summary_cols)
    write_csv(OUT/'trade_history.csv',trade_rows,trade_cols)
    write_csv(OUT/'run_status.csv',status_rows,status_cols)
    # Selection metadata is immutable during a backtest, never re-ranked from returns.
    analysis_fragment = write_analysis(OUT, run_id, items, daily_frames, status_rows, summary_rows)
    if any(item.get('selected_at') for item in items):
        starts = [pd.Timestamp(row['取得開始']) for row in status_rows if row.get('取得開始')]
        if starts:
            analysis_fragment += write_rank_sensitivity(OUT, run_id, str(min(starts).date()))
        else:
            analysis_fragment += '<p>順位感度点検は未検証：5分足取得範囲がありません。</p>'
    snapshot_dir = OUT/'selection_snapshot'
    snapshot_dir.mkdir(exist_ok=True)
    shutil.copyfile(input_path, snapshot_dir/'watchlist.csv')
    if any(item.get('selected_at') for item in items):
        for name in ['selection_policy.json','pool_audit.csv','candidate_pool.csv','delisted_asof_20261103.json','selection_inputs.zip','jpx_listing_20260930.xlsx']:
            source = ROOT/'data/universe50'/name
            if source.is_file():
                shutil.copyfile(source, snapshot_dir/name)
    baseline_dir = OUT/'comparison_baseline20'
    baseline_dir.mkdir(exist_ok=True)
    for name in ['legacy_watchlist20.csv','baseline20_summary.csv','baseline20_status.csv','baseline20_manifest.json']:
        source = ROOT/'data'/name
        if source.is_file():
            shutil.copyfile(source, baseline_dir/name)
    outcome['実行秒数'] = time.perf_counter()-started
    write_csv(OUT/'run_summary.csv',[outcome],list(outcome))
    html_report(stamp,screen_rows,summary_rows,status_rows,outcome,analysis_fragment)
    print(f"\n完了: {outcome['全体結果']}、検証可能 {outcome['検証可能銘柄数']}/{outcome['対象銘柄数']}銘柄。results/report.html を開いてください。",flush=True)
    return outcome

if __name__=='__main__':
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--watchlist', choices=['watchlist.csv','data/legacy_watchlist20.csv'], default='watchlist.csv', help='現在50銘柄または比較用の旧20銘柄。履歴連結なし')
        parser.add_argument('--fee-yen', type=float, default=100.0, help='仮定費用込みの片道固定手数料（円）')
        parser.add_argument('--fee-bps', type=float, default=0.0, help='仮定費用込みの片道手数料bps')
        parser.add_argument('--annual-interest-pct', type=float, default=3.0, help='仮定年利%%、最低1暦日/365')
        args = parser.parse_args()
        outcome = main(Costs('仮定費用込み', args.fee_yen, args.fee_bps, args.annual_interest_pct), ROOT/args.watchlist)
        sys.exit(1 if outcome['全体結果'] == '失敗' else 0)
    except Exception:
        traceback.print_exc()
        raise
