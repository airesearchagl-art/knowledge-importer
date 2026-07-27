# Knowledge Importer

OCR処理済みでテキストレイヤーを持つPDFを、ローカル環境だけでMarkdownへ変換する最小CLIです。外部API、クラウドOCR、LLM API、API従量課金サービスを使用せず、PDF本文をこれらのサービスへ送信しません。

## 対応範囲

- 1件のPDFを1件のUTF-8 Markdownへ変換
- Docling変換エンジン（`2.113.0`）
- 入力検証、出力先ディレクトリ作成、上書き防止
- ファイルログへの開始・終了・成否・例外種別の記録

GUI、Obsidian連携、RAG登録、要約・タグ生成は未対応です。

## セットアップ

Python 3.12と[uv](https://docs.astral.sh/uv/)を使用します。

```powershell
uv python install 3.12
uv sync --dev
```

初回セットアップまたは初回変換時、Doclingがローカル推論用パッケージやモデル成果物を取得する場合があります。変換処理自体はローカルで実行され、PDFは外部サービスへ送信されません。完全オフライン運用では、必要なモデル成果物を事前に取得したうえで、環境ごとの動作検証が別途必要です。

## CLI

```powershell
uv run knowledge-importer --help
uv run knowledge-importer convert .\input\sample.pdf --output .\output\sample.md
uv run knowledge-importer convert .\input\sample.pdf --output .\output\sample.md --force
uv run knowledge-importer convert .\input\table.pdf --output .\output\table.md --table-structure
uv run knowledge-importer convert .\input --output .\output
uv run knowledge-importer convert .\input --output .\output --force --table-structure
```

単一PDF変換では、既存出力は `--force` なしでは上書きせずエラーにします。一括変換では、既存のMarkdownファイルを `--force` なしで安全にスキップし、`--force` 指定時だけ再生成して上書きします。ログは `logs/knowledge-importer.log` に保存します。
`--table-structure` を指定した場合のみDocling TableFormerによる表構造推論を有効化します。表の行・列をMarkdown表として保持しやすくなる一方、初回は追加モデルの取得が発生する可能性があり、通常モードより処理時間とディスク使用量が増えます。

入力にディレクトリを指定すると、直下の `.pdf`（大文字・小文字を区別しない）だけをファイル名の安定順で逐次変換します。サブディレクトリは探索しません。出力は指定ディレクトリの `<入力stem>.md` です。`--table-structure` は全PDFへ適用され、`--force` は各出力の上書きを許可します。

一括変換は1件が失敗しても残りを処理し、最後に `成功=<件数> 失敗=<件数> スキップ=<件数>` を表示します。既存出力のスキップだけであれば終了コード `0`、1件以上の変換失敗または対象PDFが0件の場合は非0です。同じ出力名になるPDFが複数ある場合は、意図しない上書きを避けるため変換開始前に停止します。同じ入力と出力先で再実行した場合、未指定時は生成済みファイルを保持し、`--force` 指定時は安定したファイル名で再生成します。再帰探索、並列処理、ディレクトリ監視には対応していません。

失敗時はローカル絶対パスやtracebackを表示せず、対象ファイル名、分類、短い理由をstderrへ出します。分類は「入力・パス関連」「出力競合・書き込み関連」「converter生成・変換処理関連」「想定外エラー」の4種類です。失敗がある場合はsummaryにも分類別件数を追加します。

```text
失敗: ファイル=broken.pdf 分類=converter生成・変換処理関連 理由=RuntimeError: synthetic parse failure
一括変換完了: 成功=2 失敗=1 スキップ=1 分類別: 入力・パス関連=0 出力競合・書き込み関連=0 converter生成・変換処理関連=1 想定外エラー=0
```

終了コードは、変換対象がすべて成功またはスキップなら `0`、1件以上の変換失敗・converter生成失敗・想定外エラーがあれば `1`、入力ディレクトリが空、出力先形式が不正、同一出力名が衝突するなど一括変換開始前の検証エラーは `2` です。

## OCR設定

OCR済みPDFを前提とし、Doclingの `do_ocr=False`、`force_backend_text=True` を明示しています。通常モードは `do_table_structure=False`、`--table-structure` 指定時のみ `do_table_structure=True` です。画像だけのスキャンPDFに対する再OCRは行いません。また、`enable_remote_services=False` により外部推論サービスを無効化しています。

## 開発時の確認

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

架空PDFによる変換品質評価の生成方法、指標、既知制約は [PDF変換品質評価](docs/pdf-quality-evaluation.md) を参照してください。

Doclingの表構造推論あり・なしの比較結果は [Docling表構造モード比較](docs/converter-comparison.md) を参照してください。

### Markdown品質の回帰評価

`tests/test_markdown_quality.py` は、実資料やDocling実推論を使わず、合成PDFと合成Markdownだけで変換結果の主要構造を評価します。対象は、見出し階層、本文の主要語句、箇条書き、Markdown表、ページ境界前後の本文、空または極端に短い出力、絶対パス・traceback・制御文字の混入です。

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_markdown_quality.py
```

評価は空白・大文字小文字・一部のMarkdown装飾差を正規化し、全文完全一致ではなく情報と構造の欠落を判定します。最小文字数は合成fixtureの400文字超に対して120文字とし、軽微な整形差を許容しながら大幅な欠落を検出します。このテストは決定的な回帰検出用であり、実資料の視覚的な忠実度、複雑な段組み・数式、意味的な正確性、あらゆるPDFに対する変換品質を保証するものではありません。

## 制約とデータ管理

- PDFの複雑な段組み、表、数式ではMarkdownの再現性に差が出ます。初期版では表構造推論を無効化しています。
- `do_ocr=False` のため、OCRされていない画像PDFやテキスト層が欠落・破損したPDFからは本文を抽出できません。
- 実資料、実案件名、実会社名、実個人名をリポジトリへ追加しないでください。
- `input/`、`output/`、`logs/` の実ファイルはGit管理対象外です。

## ライセンス

プロジェクトのライセンスは未決定です。方針が決まるまで `LICENSE` は追加しません。
