# 2026-11-09 Yahoo公開chartの429障害と収集処理v2

[Run 42593406361](https://github.com/rootrootxp-ux/smallcap-auto-trading/actions/runs/42593406361)では、対象50銘柄の取得がすべて失敗しました。50銘柄×2試行の100応答がすべてHTTP 429で、価格CSVは0、primaryは0、11月9日の不足は50銘柄日です。失敗した原応答と公開記録は保持します。価格やゼロ損益で埋めず、検証期間も延長しません。

この文書は、保存済み記録の調査と、独立した収集処理v2による運用変更を区別します。手動診断プローブの状況・結果は末尾の独立した区分へ記録します。

## 保存済み記録から確認できた事実

数値と由来は [incident_summary.json](../data/yahoo_429_evidence/incident_summary.json) に記録しました。追加のYahoo要求を行わず、保存済みの200ファイルを照合した調査です。

|項目|確認結果|
|---|---|
|固定コード|`1fea66f15d3f91b934b49151ee3315c0c38972c6`|
|実行ワークフローのmain版|`8d8bd785a51d3ca48a1fd3873db808a103c46956`|
|予定時刻|2026-11-09 17:15 JST（08:15 UTC）|
|Run作成時刻|18:26:12 JST（09:26:12 UTC）|
|予定からRun作成まで|4,272秒＝71分12秒|
|最初の要求|18:26:34.797068 JST、3905.T、1回目から429|
|最初の応答|18:26:35.190232 JST、`Too Many Requests`|
|要求開始から最終応答まで|3.964825秒で100要求|
|429後の再試行待ち|中央値0.000102秒＝0.102ミリ秒、50組|
|応答内容|100件すべて同じ本文ハッシュ、すべて429|
|保存されたRetry-After|0件|
|価格の派生CSV／primary|ともに0件|

最初の要求がすでに429なので、この100要求だけを最初の429の原因と断定できません。一方、429を受けてもほぼ待たずに再試行し、残りの銘柄にも要求を続けたことは確認でき、制限応答後の負荷を増やす動作でした。

IP単位の制限だったか、いつ制限が解除されるか、最初の429が何に由来するかは未確定です。保存時のヘッダー許可リストは`retry-after`と`expires`を含んでいましたが、値はありません。`x-ratelimit-*`は当時の保存対象外なので、提供元にも存在しなかったとは言えません。`Date`は応答日時であり、制限の有効期限ではありません。Yahooの非公式公開chart利用について、この処理に適用できる供給保証や契約SLAも確認していません。

失敗captureのmanifest SHA256は`31e97358821430827a052ae05524be9a08f0a341f962df213f6adedce03cc3bc`、公開commitは[`7627e409`](https://github.com/rootrootxp-ux/smallcap-auto-trading/commit/7627e409b515df3f064124b9803019b026f00141)、receipt公開を確認したcommitは[`dd817daf`](https://github.com/rootrootxp-ux/smallcap-auto-trading/commit/dd817dafe2c539b1f9b43cf85831182986585712)です。失敗を保存できたことと、価格データを取得できたことを区別します。

## 2026-11-11のワークフロー診断

2026-11-11 09:15:30 UTC（18:15:30 JST）時点の照会結果と出典を、[workflow_diagnostic_20261111.json](../data/yahoo_429_evidence/workflow_diagnostic_20261111.json)に保存しました。

|項目|確認結果|
|---|---|
|対象workflow|`411041094`、`.github/workflows/forward-event-study.yml`|
|状態|`disabled_manually`、更新日時は2026-11-09 09:59:57 UTC|
|停止の経緯|429の反復を防ぐため、本作業の担当エージェントが11月9日に停止した操作記録あり|
|その後の実行|定期Runは11月9日の1件のみ。11月10日・11日の定期Run履歴なし|
|リポジトリ|default branchは`main`、公開、非archive|
|mainのcron|`15 8 * * 1-5`（17:15 JST収集）、`0 10 * * 1-5`（19:00 JST評価）|
|Actions設定の確認範囲|`actions/permissions`と`actions/permissions/workflow`の照会はともにHTTP 403。実際の許可・制限設定は未検証|

本作業で修正・再開が完了せず停止を継続していたため、11月10日・11日の各50銘柄日は未取得として残します。停止状態の確認だけで、GitHub側の権限・制限全体が正常だったとは判断しません。11月9日の71分12秒の開始遅延も、個別の原因は未確認です。[GitHubのschedule仕様](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)が説明する遅延可能性を、このRunで実証した原因として扱いません。

## 旧処理を保全した収集処理v2

同じ失敗を繰り返さないため、旧 **Forward Event Study (frozen protocol)** ワークフローは当該Run完了後に停止しました。旧登録時の17ファイルのロック、protocol、50銘柄、カレンダー、イベント計算coreはそのまま保持し、収集上の対策を別パッケージ`research.forward_capture_v2`へ追加します。元の[固定検証記録](forward_study.md)を書き換えて、当初から新しい待機規則だったことにはしません。

v2の収集規則は次のとおりです。

- 要求開始の間隔を最低5秒空けます。これはYahooが許容する頻度の保証ではありません。
- 最初の429で、その実行内の全銘柄の要求を停止します。429に対する自動再試行はしません。未要求の銘柄を取得成功にしません。
- `Retry-After`があれば解釈した待機期限を保存します。応答から最低60分の保守的なローカル待機、既存のcooldown、解釈した提供元の期限のうち、最も遅い期限を適用します。値がない場合にも最低60分待機します。60分後の解除を提供元が保証した、という意味ではありません。
- cooldownを永続保存し、収集・診断プローブ・明示的な過去日再取得で共有します。新しいRunや新しいcapture IDで待機をリセットしません。既存保存先を空のディレクトリに替えて回避しません。
- 通常CIでは市場HTTP要求を行いません。cooldown後の診断は明示的な手動dispatchで、GitHubホスト上から**1要求だけ**行います。プローブの結果は診断として保存し、primary用CSVや主解析の入力にはしません。
- ホスト、IP、User-Agent、認証情報の切替えで制限を回避しません。失敗の証拠と、保存できた待機状態を残します。

運用変更はPRで確認します。通常の本番収集はmain上のワークフローに限定し、PRの自動CIは読取りと合成データ検証だけです。featureブランチで許可する明示的な手動プローブは、診断の保存だけを行います。旧ワークフローは、ユーザーが変更を確認してマージし、再開を判断するまでは停止を維持します。このPRの作成を、自動マージや本番再開の承認とは扱いません。

## 保存データ・キャッシュの扱い

評価は保存済みarchiveを読み直し、primaryは既定の初回永続公開版の選択規則で再利用します。評価のたびにYahooへ問い合わせません。cooldownもreceiptの有無にかかわらず保存済み制限応答から引き継ぎます。ローカルの新しいRunや保存先で、この状態をリセットしません。

対象日を明示した再取得は、当時の取得版・実際の取得日時を持つ別captureとして保存し、既存版を上書きしません。キャッシュ再利用を新規取得として報告せず、不足日の自動一括backfillも行いません。

## コマンドと運用上の区別

以下はv2 CLIの使用例です。`--store`には既存の前向き検証データをcheckoutした専用保存先を指定します。ローカルCLIで保存しただけでは、データブランチへの永続公開とreceipt確認を済ませたことにはなりません。

```sh
# 市場要求なしの事前確認
python -m research.forward_capture_v2 preflight --store /tmp/forward-data-worktree --output /tmp/forward-v2-preflight

# 承認・マージ後、mainの本番ワークフローから収集する場合
python -m research.forward_capture_v2 collect --guard-report path/to/fresh-guard.json --store /tmp/forward-data-worktree --output /tmp/forward-v2-collect

# cooldown後に明示的に許可した診断。primary用CSVは作らない
python -m research.forward_capture_v2 probe --guard-report path/to/fresh-guard.json --store /tmp/forward-data-worktree --output /tmp/forward-v2-probe --date 2026-11-09

# 提供元の保持範囲で許される場合だけ、対象日を明示して別版として再取得
python -m research.forward_capture_v2 collect --guard-report path/to/fresh-guard.json --store /tmp/forward-data-worktree --output /tmp/forward-v2-backfill --date 2026-11-09
```

この文書のコマンドは取得結果の報告ではありません。診断プローブ、後日再取得、v2の本番収集の実行結果・Runリンクは、実行確認後に別途記録します。

## 期間・不足・後日取得の扱い

固定窓は**2026-11-09〜12-07の20取引日**、50銘柄×20日＝1,000銘柄日、最終締切は12月7日18:30 JSTのままです。11月9日の失敗50件を削除したり、別の取引日を加えて20日へ戻したりしません。既存の失敗captureとreceiptも上書きしません。

提供元が過去日を返せる場合の再取得は、対象日、取得コード版、要求・受信・完了・公開日時を別captureとして保存します。形式が有効かつ空でない最初の永続公開版を採用する規則は、欠損のあるpartial版も含めて維持します。後日公開の`available_late`区分、primary/revisionの区別、最終締切以前の受信・完了・receipt公開という条件も変えません。過去日付の価格だからといって公開時刻を遡らせません。診断結果を価格captureとして採用しません。

不足が残れば`final_incomplete`のまま固定窓を評価します。欠損価格から架空の足やゼロ収益を作りません。貸株、実際の空売り可能性、証券口座接続、実注文は対象外です。

代替取得先の技術仕様・契約・公開権限は[データソース比較](market_data_sources.md)に整理しました。自動fallback、新規アカウント作成、有料契約は行いません。

## 2026-11-11の単発診断結果

[Run 42993814252](https://github.com/rootrootxp-ux/smallcap-auto-trading/actions/runs/42993814252)を修正ブランチから手動実行しました。Runは成功ですが、これは診断と証拠公開の完了を示すだけです。**価格取得は失敗**しました。ダウンロードした成果物も照合し、[probe_result_20261111.json](../data/yahoo_429_evidence/probe_result_20261111.json)へ要約しました。

|項目|結果|
|---|---|
|対象|固定リストの先頭3905.T、2026-11-09、5分足|
|固定収集コード|`9f4173c447f291b1a05183c322ee99b92c92c1b7`、`yahoo-chart-paced-v2`|
|要求／応答|2026-11-11 09:17:01 UTC、**1要求・HTTP 429が1件**、成功0銘柄|
|Retry-After|なし。ローカル待機下限は10:17:01.437772 UTC（19:17:01 JST）|
|原応答|19 bytes、旧失敗と同じ本文SHA256。再試行・他銘柄への要求なし|
|保存|原応答・メタデータ・manifest・待機期限・receiptの追加公開と監査が成功|
|価格CSV／primary|追加0件。診断は`probe_only`で研究価格へ不採用|
|実行後workflow|`disabled_manually`を維持。有効化は09:16:37〜47 UTCの約10秒のみ。旧main定期Runの追加なし|

原本公開commitは[`8d5f90f0`](https://github.com/rootrootxp-ux/smallcap-auto-trading/commit/8d5f90f0ca77ed06da87ed090945c2d2492cd351)、receiptは[`94f7cd2c`](https://github.com/rootrootxp-ux/smallcap-auto-trading/commit/94f7cd2ca029d7d9230fdf56d7e592f3301e6d28)です。旧失敗を削除・変更していません。

ダウンロードした公開済みarchiveを元の固定評価処理で18:17:44 JSTに監査した結果、primaryは0、締切経過分の不足は11月9日・10日の**100銘柄日**、未締切は900銘柄日でした。11月11日の50件は、その時点で18:30の当日締切前のため未締切です。後日取得がなければ締切後は不足へ移ります。研究の損益推定は行っていません。

この1回の結果は当該GitHubホストの当該要求で利用できなかった証拠です。GitHub全体で永久に利用不能、IP制限が原因、60分後に復旧する、とは断定しません。追加のYahoo要求は行わず、自動収集は停止を維持します。ソースも切り替えていないため、提供元間の価格差の実測は未実施です。認可された代替先と足仕様・保存権限を確認してから、別の取得版と重複観測で比較する必要があります。
