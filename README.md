# smallcap-auto-trading

日本株の小型・中型株の研究用バックテスト v3。証券会社への接続・実注文機能はありません。

## 構成

- `smallcap_auto_v3.py`: 株価取得、銘柄分類、バックテスト、HTML/CSV出力。
- `watchlist.csv`: 対象銘柄のコードと候補名。時価総額・信用取引可否は未確認です。
- `requirements.txt`: NumPy、pandas、yfinance の検証済みバージョン。
- `tests/test_backtest.py`: 外部通信不要の合成データによる自動テスト。
- `scripts/check_results.py`: 出力漏れ・取得失敗を検出する検証コマンド。
- `.github/workflows/backtest.yml`: 手動実行用 GitHub Actions。

日足3か月と5分足30日を取得し、MA、VWAP＋出来高、高値ブレイク＋出来高の3戦略を片道スリッページ1円・2円で検証します。銘柄ごとに資金100万円、建玉上限30万円、100株単位で独立計算します。

## ローカル実行

Python 3.12 を使用します。Linux/macOS:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python smallcap_auto_v3.py
python scripts/check_results.py
```

Windows は Python を導入して `setup.bat`、続いて `run.bat` を実行します。
生成物は `results/` 内に保存されます。`report.html` はブラウザで開けます。

## GitHub Actions での手動実行

1. 変更を GitHub のデフォルトブランチへ反映します。
2. リポジトリの **Actions → Backtest v3 → Run workflow** を選びます。
3. 実行終了後、実行詳細の **Artifacts** から `backtest-results-…` をダウンロードします。
4. ZIP を展開して `report.html` を開きます。CSVと取得できた5分足データも同梱されます。

成果物は14日間保持されます。自動スケジュールはなく、手動でのみ実行します。
GitHub Actions の利用が許可されている必要があります。公開株価取得に API キーや証券口座情報は不要です。

出力CSVは `screening.csv`（分類）、`backtest_summary.csv`（検証結果）、
`trade_history.csv`（仮想売買記録）、`run_status.csv`（銘柄別取得状況）です。
取得失敗時も存在する成果物をアップロードします。元プログラムは取得失敗でも正常終了し得るため、
Actions は別途出力を検証し、不完全な結果なら実行を失敗と表示します。
取引0回は必ずしもエラーではありません。

Yahoo Finance のレート制限・銘柄廃止・データ欠損などで失敗する場合は `run_status.csv` を確認してください。
合成データテストの成功は実データ取得の成功を保証しません。
手数料・信用金利等は含まず、約定と損失上限は仮定です。
