# PDF変換品質評価

Knowledge Importer単体の基準値を、実資料を使わず再現可能な架空PDFで取得するための評価手順です。MarkPDFdownなど他エンジンとの比較は対象外です。

## 評価パターン

- 単一段組み本文
- 見出し階層
- 箇条書き
- 2段組み
- 単純な表
- ヘッダー・フッター付き
- 複数ページ

すべてReportLabで生成し、pypdfでページ数、テキストレイヤー、期待文字列を検証します。PDF、変換Markdown、PNGプレビュー、生レポートは `output/pdf-quality-evaluation/` に生成され、Git管理対象外です。

## 実行方法

生成とPNGプレビューだけを確認する場合：

```powershell
uv run python scripts/evaluate_pdf_quality.py --generate-only --render-previews
```

実Docling変換とレポート作成：

```powershell
uv run python scripts/evaluate_pdf_quality.py --render-previews
```

出力：

- `quality-report.json`: 機械可読な全測定値
- `quality-report.md`: ケース比較表
- `pdfs/`: 架空PDF
- `markdown/`: 変換結果
- `previews/`: 目視確認用PNG

## 評価指標

- 変換成功可否、処理時間、出力文字数
- 期待文字列の有無
- 見出しレベル保持
- マーカーの読み順
- Markdown表の再現状況
- ヘッダー・フッターの出力混入

## 既知制約

初期設定では `do_ocr=False`、`do_table_structure=False`、`force_backend_text=True`、`enable_remote_services=False` です。表構造推論を無効化しているため、罫線表のセル文字列を抽出できてもMarkdown表として再構成されない可能性があります。

単体テストは変換器をfakeへ差し替え、Doclingモデルの取得、実推論、外部通信を実行しません。実評価では初回のみローカル推論モデル成果物の取得通信が発生する場合がありますが、PDF本文を外部サービスへ送信しません。

## 基準値

2026-07-19にPython 3.12.13、Docling 2.113.0、ReportLab 5.0.0、pypdf 6.14.2で測定した基準値です。Doclingモデルを取得済みのローカルキャッシュへ固定し、`HF_HUB_OFFLINE=1` と `TRANSFORMERS_OFFLINE=1` の下で実行しました。

| パターン | 成功 | 秒 | 文字数 | 主な結果 |
|---|---:|---:|---:|---|
| 単一段組み本文 | Yes | 1.033 | 188 | 全マーカーと読み順を保持 |
| 見出し階層 | Yes | 0.590 | 100 | 文字と順序は保持、全見出しがH2となり階層は不保持 |
| 箇条書き | Yes | 0.589 | 52 | Markdown箇条書きとして保持 |
| 2段組み | Yes | 0.596 | 68 | 左列の後に右列を出力し、期待順を保持 |
| 単純な表 | Yes | 0.593 | 97 | 全セル文字列は保持するが、1列のMarkdown表へ平坦化 |
| ヘッダー・フッター | Yes | 0.599 | 52 | 本文を保持し、ヘッダー・フッターは除外 |
| 複数ページ | Yes | 1.808 | 99 | 3ページを処理したが、1・2ページ目の見出しマーカーを欠落 |

7件とも変換処理自体は成功しました。単一段組み、箇条書き、単純な2段組みの読み順には強い一方、フォントサイズだけで表現した見出し階層、複数ページにまたがる同型見出しの保持には制約があります。ヘッダー・フッター除外は今回の架空PDFでは望ましい結果でした。

表は文字抽出と概ねの並びを保持しましたが、2列3行の構造を1列へ平坦化しました。これは `do_table_structure=False` の影響を示す基準ケースであり、表構造の忠実性が必要な用途では設定または変換エンジンの比較が必要です。

処理時間はハードウェア、キャッシュ状態、Doclingバージョンに依存するため、比較時は同一環境・同一バージョンを使用してください。生の測定結果は実行ごとに `quality-report.json` と `quality-report.md` へ出力されます。
