# Knowledge Importer

OCR処理済みでテキストレイヤーを持つPDFを、ローカル環境だけでMarkdownへ変換する最小CLIです。外部API、クラウドOCR、LLM API、API従量課金サービスを使用せず、PDF本文をこれらのサービスへ送信しません。

## 対応範囲

- 1件のPDFを1件のUTF-8 Markdownへ変換
- Docling変換エンジン（`2.113.0`）
- 入力検証、出力先ディレクトリ作成、上書き防止
- ファイルログへの開始・終了・成否・例外種別の記録

GUI、バッチ変換、Obsidian連携、RAG登録、要約・タグ生成は未対応です。

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
```

既存出力は `--force` なしでは上書きしません。ログは `logs/knowledge-importer.log` に保存します。

## OCR設定

OCR済みPDFを前提とし、Doclingの `do_ocr=False`、`do_table_structure=False`、`force_backend_text=True` を明示しています。画像だけのスキャンPDFに対する再OCRや表構造モデルによる推論は行いません。また、`enable_remote_services=False` により外部推論サービスを無効化しています。

## 開発時の確認

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

架空PDFによる変換品質評価の生成方法、指標、既知制約は [PDF変換品質評価](docs/pdf-quality-evaluation.md) を参照してください。

Doclingの表構造推論あり・なしの比較結果は [Docling表構造モード比較](docs/converter-comparison.md) を参照してください。

## 制約とデータ管理

- PDFの複雑な段組み、表、数式ではMarkdownの再現性に差が出ます。初期版では表構造推論を無効化しています。
- `do_ocr=False` のため、OCRされていない画像PDFやテキスト層が欠落・破損したPDFからは本文を抽出できません。
- 実資料、実案件名、実会社名、実個人名をリポジトリへ追加しないでください。
- `input/`、`output/`、`logs/` の実ファイルはGit管理対象外です。

## ライセンス

プロジェクトのライセンスは未決定です。方針が決まるまで `LICENSE` は追加しません。
