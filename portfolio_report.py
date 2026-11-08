"""Static reports for the cash-only shared-capital research simulator.

This module formats the engine's ledger. It never computes fills, replaces
missing valuations, connects to a broker, or changes the simulation results.
"""
import csv
import html
import json
import math
from numbers import Integral, Real
from pathlib import Path


LABEL_COLUMNS = ['scenario', 'strategy', 'slippage', 'cost_scenario']
HTML_LABELS = {
    'scenario':'条件','strategy':'戦略','slippage':'片道スリッページ(円/株)','cost_scenario':'費用前提',
    'timestamp':'評価時刻','phase':'評価段階','cash':'現金残高(円)','reserved_cash':'予約現金(円)',
    'free_cash':'未予約現金(円)','position_cost':'保有簿価(円)','market_value':'時価評価額(円)',
    'realized_pnl':'完了取引の実現損益(円)','unrealized_pnl':'評価損益(円)','equity':'推定清算価値・未確定時は参考(円)',
    'total_pnl':'最終損益(円)','max_drawdown':'口座最大DD(円)','observed_max_drawdown':'観測点DD・参考(円)',
    'open_positions':'保有銘柄数','valuation_complete':'評価可能','daily_realized_pnl':'当日実現損益(円)',
    'daily_entry_stop':'当日損失による新規停止','data_entry_stop':'欠損による口座新規停止',
    'win_rate_pct':'完了取引の勝率(%)','trades':'完了取引数','unresolved':'未決済あり',
    'halted_days':'口座日次停止日数','data_stop_days':'欠損で新規停止した日数','data_incident_days':'欠損保有発生日数',
    'period_start':'取得開始','period_end':'取得終了','symbols':'価格入力銘柄数','status':'検証状態',
    'initial_cash':'口座初期現金(円)','fee_yen':'片道固定手数料(円)','fee_bps':'片道手数料(bps)',
    'annual_interest_pct':'信用金利・現物は0(%)','date':'営業日','day_pnl':'日次実現損益(円)',
    'day_max_drawdown':'日内最大DD(円)','code':'銘柄','quantity':'株数','buy':'買値(円)',
    'entry_fee':'買い手数料(円)','entry_time':'買い約定時刻','book_cost':'取得簿価(円)',
    'last_known_price':'最後に観測した価格(円)','max_drawdown_scope':'DDの対象範囲',
}
CSV_FILES = {
    'summary': 'portfolio_summary.csv',
    'trades': 'portfolio_trades.csv',
    'orders': 'portfolio_orders.csv',
    'equity': 'portfolio_equity.csv',
    'daily': 'portfolio_daily.csv',
    'positions': 'portfolio_positions.csv',
}


def _missing(value):
    return value is None or isinstance(value, Real) and not math.isfinite(value)


def _cell(value):
    """Retain full numeric precision in CSV; unknown is an empty cell, never 0."""
    if _missing(value):
        return ''
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


def _display(value):
    if _missing(value):
        return '未確定／未取得'
    if isinstance(value, bool):
        return 'はい' if value else 'いいえ'
    if isinstance(value, Integral):
        return f'{value:,}'
    if isinstance(value, Real):
        return f'{value:,.2f}'
    return str(_cell(value))


def _columns(rows, fallback=None):
    columns = list(dict.fromkeys(key for row in rows for key in row))
    return columns or list(fallback or LABEL_COLUMNS)


def _csv(path, rows, fallback=None):
    columns = _columns(rows, fallback)
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _cell(value) for key, value in row.items()})


def _table(rows, empty='該当データなし'):
    if not rows:
        return '<p class="empty">' + html.escape(empty) + '</p>'
    columns = _columns(rows)
    headers = ''.join('<th scope="col">' + html.escape(HTML_LABELS.get(key,str(key))) + '</th>' for key in columns)
    body = ''.join(
        '<tr>' + ''.join(
            '<td>' + html.escape(_display(row[key]) if key in row else '') + '</td>'
            for key in columns
        ) + '</tr>'
        for row in rows
    )
    return '<div class="scroll" tabindex="0"><table><thead><tr>' + headers + '</tr></thead><tbody>' + body + '</tbody></table></div>'


def _flatten(scenario_results, key):
    if key == 'summary':
        return [dict(result['summary']) for result in scenario_results]
    return [dict(row) for result in scenario_results for row in result.get(key, [])]


def write_report(out: Path, scenario_results: list[dict], input_audit: list[dict],
                 comparisons: list[dict], metadata: dict) -> None:
    """Write CSV ledgers and an escaped, standalone Japanese HTML report.

    Scenario labels are supplied by the engine/runner and preserved as given.
    An empty positions or trades list is meaningful and still produces a CSV
    header. An unknown total remains blank in CSV and explicit in HTML.
    """
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    records = {key: _flatten(scenario_results, key) for key in CSV_FILES}
    for key, filename in CSV_FILES.items():
        _csv(out / filename, records[key])
    _csv(out / 'portfolio_input_audit.csv', input_audit, ['コード', '状態', '詳細'])
    _csv(out / 'independent_comparison.csv', comparisons, ['戦略', '比較対象', '注意'])
    _csv(out / 'portfolio_metadata.csv', [metadata], ['実行ID'])

    downloads = [
        ('portfolio_summary.csv', '口座集計・勝率・取引数・最大DD'),
        ('portfolio_trades.csv', '完了した売買・手数料・損益'),
        ('portfolio_orders.csv', '注文・順位・約定・見送り理由'),
        ('portfolio_equity.csv', '時間順の現金・予約資金・建玉・資産評価'),
        ('portfolio_daily.csv', '日次資産推移・損失停止'),
        ('portfolio_positions.csv', '保有建玉・未決済状態'),
        ('portfolio_input_audit.csv', '50銘柄の入力・データ品質'),
        ('independent_comparison.csv', '旧・銘柄ごと独立資金方式との比較'),
        ('portfolio_metadata.csv', '実行ID・入力元・設定の記録'),
    ]
    download_list = '<ul>' + ''.join(
        '<li><a href="' + html.escape(filename, quote=True) + '">' +
        html.escape(filename) + '</a> — ' + html.escape(label) + '</li>'
        for filename, label in downloads
    ) + '</ul>'
    metadata_rows = [{'項目': key, '値': value} for key, value in metadata.items()]
    doc = '''<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>共有資金100万円・買い専用ポートフォリオ検証</title>
<style>
body{font-family:system-ui,sans-serif;color:#172033;background:#f8fafc;max-width:1280px;margin:24px auto;padding:0 16px;line-height:1.65}
h1{font-size:1.65rem}h2{font-size:1.2rem;margin-top:30px}h3{font-size:1rem}
.note{padding:16px;border-left:5px solid #b77900;background:#fff4da;border-radius:6px}
.rule{padding:16px;background:#edf4ff;border-radius:6px}.scroll{overflow:auto;max-height:65vh;background:white;margin:12px 0}
table{border-collapse:collapse;width:100%;font-size:12px}th,td{padding:8px;border:1px solid #d8dee8;text-align:right;white-space:nowrap}
th{position:sticky;top:0;background:#e8eef5;z-index:1}th:first-child,td:first-child{text-align:left}
a{color:#1454a0}.empty{color:#536174}summary{cursor:pointer;font-weight:600}code{overflow-wrap:anywhere}
</style></head><body>
<h1>共有資金100万円・買い専用ポートフォリオ検証</h1>
<p class="note"><strong>全銘柄で初期資金100万円を共有します。</strong>1銘柄の建玉上限は30万円、100株単位です。
現物の買い専用で、信用取引・空売り・レバレッジ運用は未実装です。証券会社への接続・実注文はありません。
MA、VWAP＋出来高、高値ブレイク＋出来高は混ぜず、戦略・スリッページ・費用条件ごとに独立した100万円口座を計算します。</p>
<section class="rule"><h2>約定・資金・損失停止の前提</h2>
<p>シグナルは足の終値で確定し、次の取引可能な足の始値で約定すると仮定します。
株数はシグナル時点の既知の価格と利用可能資金で固定し、注文資金を予約します。未来の始値で株数を決め直しません。
同じ時刻の注文順位は銘柄コード順で再現可能にし、決済を先に処理します。未来の値動き・最終損益を優先順位に使いません。</p>
<p>既定の手数料は片道固定100円・従量0bps、または費用ゼロです。片道スリッページ1円／2円をそれぞれ検証します。
具体的な設定は各集計行と実行情報を参照してください。現物共有資金計算では信用金利を計上しません。
1取引の損切り判定2,000円と、銘柄横断の口座全体で費用控除後の日次実現損益が−5,000円以下となった場合の新規停止を適用します。
停止後も既存建玉の決済は継続します。同じ時点では全銘柄の終値を処理し、決済、口座の損失停止判定、新規買いの順に進めます。判定後の次足約定なので、これらの金額は保証された損失上限ではありません。</p>
<p>資金不足・建玉上限・データ欠損の見送りは注文CSVに分けて記録します。欠損足に架空の価格を与えず、架空決済もしません。
保有中の欠損などで総損益が確定できない場合は空欄／未確定とし、残存建玉と観測範囲を記録します。
保有銘柄で欠損が生じた場合はその建玉を未確定とし、口座全体の新規注文を停止します。他の観測可能な既存建玉の決済は続けます。最後に観測した価格による参考評価額と未確定フラグを区別し、総損益・評価DDは確定値として表示しません。終端決済はクロージング・オークション前の15:20始値を事前指定した仮定で、必要な足がなければ約定を作りません。
売却代金は帳簿上すぐに再利用します。受渡日・差金決済制約、実際の板、売買停止、執行遅延、約定可能性を完全には再現していません。</p></section>
<section><h2>資金モデルと選定バイアス</h2>
<p>比較用の旧方式は、銘柄ごとに100万円を独立配分しており、全銘柄共通の現金や同時注文の資金競合を再現していません。
旧方式の独立損益の単純合計と、今回の共有100万円口座を同じ資金の成績として解釈しないでください。
比較に使った費用・金利・期間・銘柄の違いも比較CSVを参照してください。</p>
<p>現在の50銘柄選定には、現在の上場情報・時価総額・過去検証後の日足を用いた先読み・生存者・手動選定バイアスが残ります。
今回の結果を将来の収益性の証明や、真に未参照だったデータでの検証とは扱いません。</p></section>
'''
    doc += '<section><h2>実行情報</h2>' + _table(metadata_rows) + '</section>'
    doc += '<section><h2>共有口座の集計</h2>' + _table(records['summary'], '計算できたシナリオがありません。実行ログと入力監査を確認してください。') + '</section>'
    doc += '<section><h2>日次資産推移</h2>' + _table(records['daily']) + '</section>'
    doc += '<section><h2>旧・銘柄ごと独立資金方式との比較</h2>' + _table(comparisons) + '</section>'
    doc += ('<section><h2>最終残存建玉・未決済状態</h2><p>各シナリオの終端で残った建玉です。最後の観測価格による参考値は、約定済みの売却代金や確定損益とは区別してください。</p>' +
            _table(records['positions'], '最終残存建玉はありません。損益が確定しているかは口座集計でも確認してください。') + '</section>')
    doc += '<section><h2>入力・データ品質</h2>' + _table(input_audit) + '</section>'
    doc += '<section><h2>CSV一覧</h2>' + download_list + '<p>CSVはBOM付きUTF-8です。空欄の未確定値を0円と読み替えないでください。数値はCSVに計算精度のまま保存し、HTMLの小数表示だけを丸めています。</p></section>'
    doc += '</body></html>'
    (out / 'report.html').write_text(doc, encoding='utf-8')
