# 精査アプリへ渡す一覧取得仕様 1.0

対象ツールv1.3.1。基準v1.2 mainコミット `f4005e8503fe164653c98cad5fd94f0519c45e30`。

## 成果物の入口

`<取得ID>.manifest.json` はUTF-8。取得IDはローカル日時＋UUID4で、全成果物のファイル名へ共通付与します。指定ルート基準の相対パスを使用し、ルート自身のCSV行は含めません。フォルダ名から県・年度・業務を推定しません。

検査側はschema_version、tool_version、acquisition_id、root、conditions、run_statusを保存してください。スキーマ版1.0以外、未知のrun_status・I列の値は「未確認」にします。JSON破損・未存在を正常と推定しません。

## CSVと状態

csv_columnsに既存A～Iの名前を列挙します。検査側は位置固定ではなく列名で読むこと。F列は整数0と空欄を区別し、空欄を0へ変換しません。G列はISO秒精度のローカル時刻で、UTCへ変換する場合は取得環境のタイムゾーンと季節によるオフセットに注意します。開始・終了・観測時刻はオフセット付きISO日時です。

旧CSVの状態列がなければ「状態未記録」。現行の「取得済み」を補いません。H列「表示分類」は機械の形式判定に使用しません。B列だけでは照合キーにしません。

`row_states` と `docs/status_spec.json` が状態値の対応表です。フォルダ「取得済み」は情報取得・列挙処理終了を示しますが、子フォルダや子ファイルの成功までは保証しません。ルートの列挙失敗はルート行を作らず範囲記録に残します。

## 終了状態

| run_status | 連携側の解釈 |
|---|---|
| completed | 設定どおり取得完了。intentional_omissionsを別に確認 |
| partial_failure | 成功した部分だけを検査可能。未走査を不存在としない |
| interrupted | 取得できた部分のみ。全対象完了としない |
| abnormal | 保存失敗等。確定した分割だけが部分検査候補 |
| not_started | 検査入力なし。正常な空対象としない |
| running | 進行中または強制停止後。完了としない |

`coverage_complete` はcompletedかつリンク先省略0のときだけtrue。対象ルートの内部品質・良品は保証しません。除外は現版では空配列です。

## 分割の確認

`csv_parts` のorderは1始まり。各要素のfile、data_rows、finalizedを読みます。現版にはfinal_fileもあり、書込途中にはfileが.csv.partになります。**finalized=trueの要素だけを確定CSVとして扱います。** 書込失敗時はprocessed_rowsとconfirmed_rowsが異なる可能性があります。

連携側は各ファイルの存在・同一出力ディレクトリ内の位置・見出し・データ行数を確認し、連番の欠落と重複を検出してください。全CSVを逐次読み、付属ファイルの照合は分割境界をまたいで実施します。管理JSONにない.csvや一時ファイルを自動追加しません。

`processed_rows = counts.files + counts.folders + counts.links + counts.unknown`

`confirmed_rows = sum(data_rows where finalized=true)`

正常・部分失敗・通常中断で最終保存が成功した場合はprocessed_rows=confirmed_rows。リンクをファイル／フォルダへ重複計上しません。counts.bytesはCSVへ出力処理した情報取得済みファイルサイズの合計で、確定分割だけの容量を求める場合は連携側で再集計します。ファイル内容を読んで算出した値ではありません。

## 未走査・省略・失敗範囲

巨大な失敗一覧を管理JSONの巨大配列にせず、`scope_file`（UTF-8 JSONL）へ逐次記録します。`unscanned_ranges`、`intentional_omissions` は同ファイルへの参照・対象kind・件数です。管理JSONとJSONLは1セットとして渡してください。JSONL欠落・未読は範囲未確認です。

| kind | 意味 |
|---|---|
| failed | 情報取得・列挙・実行失敗により完全性が未確認 |
| interrupted | 処理途中で中断。取得済み行があっても範囲全部の完了は未確認 |
| pending | 列挙待ちで未処理のフォルダ |
| link_skipped | 意図的にリンク先の再帰を省略 |

各レコードにpath、relative_path、kind、operation、reason、message、subtree、observed_atがあります。subtree=trueは当該パス配下を含む保守的な未確認範囲です。情報取得失敗では種別が不明のため、フォルダだった可能性も含めます。kindの範囲は重なり得ます。件数は範囲記録数で、欠落ファイル数の推定ではありません。

error_csvは取得ID・パス・操作・理由・errno・winerror・エラーを記録します。権限拒否、消失、通信失敗、名前・パス不正、その他をAPI例外から可能な範囲で区別します。保存先の失敗は入力データエラーと混ぜずfatal_errorへ記載します。

## 保存の確定と故障時の限界

CSVは逐次書き、flush＋fsync＋close後に改名して分割のfinalizedをtrueにします。管理JSONは同じフォルダの一時JSONに書いてfsync後にos.replaceします。更新失敗時に前回JSONを切り詰めません。

最終のerrors.csv・scopes.jsonlの保存確認後、終了概要を書き、最後に管理JSONを更新します。artifacts_finalizedは付随成果物の保存状態です。false／running／未知の状態は全成果物の完了を表しません。保存先自体が使えなくなると、最後の状態を永続化できず古いrunningのJSONが残ります。連携側は保守的に未完了扱いにします。

work.sqliteはディレクトリ列挙待ちの退避に使い、全件のパスやレコードをRAMに集めません。このファイルからの再開手順はありません。電源断に対して複数成果物の厳密なトランザクションを保証する版ではありません。

## 先行改修Aと後続Bの境界

GUIの進捗通知は最大2件、更新は最大約2Hz。確定前と確定後の行数を分け、事前に件数算出用の再走査を行いません。列挙待ちフォルダはSQLiteへ保存します。並列数1、再試行0です。

「中断」は現在のOS呼出しが戻った時点で動作します。SMB待ちの強制停止、監視プロセス、再試行、チェックポイント、異常停止後再開はBの基本設計後に実装します。画面からの通常中断は取得済み行を確定し、未処理範囲を記録しますが、再実行は新取得IDで先頭からです。
