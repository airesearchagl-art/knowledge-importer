# Docling表構造モード比較

7種類の架空PDFを共通入力とし、Knowledge ImporterのbaselineとDoclingの表構造推論を有効化した実験設定を比較します。MarkPDFdownなど別エンジンの統合は対象外です。

## 比較設定

| 設定 | baseline | table_structure |
|---|---:|---:|
| `do_ocr` | `False` | `False` |
| `do_table_structure` | `False` | `True` |
| `force_backend_text` | `True` | `True` |
| `enable_remote_services` | `False` | `False` |

通常CLIは引き続きbaselineを使用します。表構造推論はCLIの `--table-structure`、または比較スクリプトから明示した場合に限って有効になります。

## CLIモード

| モード | CLI指定 | `do_table_structure` | 用途・注意点 |
|---|---|---:|---|
| 通常モード | 指定なし | `False` | 従来どおりの既定値。追加の表モデルを使わず、処理時間と依存モデル量を抑える |
| 表構造モード | `--table-structure` | `True` | 表の行・列構造を優先。TableFormerモデルと追加の処理時間が必要 |

どちらのモードも `do_ocr=False`、`force_backend_text=True`、`enable_remote_services=False` を維持します。

## 実行方法

初回実測では、PDF本文を送信せず、公開Doclingモデル成果物だけを取得します。

```powershell
uv run python -m scripts.compare_docling_modes `
  --output-dir output/converter-comparison/online `
  --model-cache output/converter-comparison/model-cache `
  --render-previews
```

モデル取得後にHugging Face／Transformersのオフライン環境変数を指定する再実行：

```powershell
uv run python -m scripts.compare_docling_modes `
  --output-dir output/converter-comparison/offline `
  --model-cache output/converter-comparison/model-cache `
  --render-previews `
  --offline
```

JSON、Markdown、PDF、変換結果、PNG、モデルキャッシュはすべて `output/converter-comparison/` に生成され、Git管理対象外です。

## 実測結果

2026-07-20にPython 3.12.13、Docling 2.113.0で、取得済みモデルを使用し、`HF_HUB_OFFLINE=1` と `TRANSFORMERS_OFFLINE=1` を指定して実行しました。OSレベルのネットワーク遮断は実施していません。両モードとも7件すべて変換成功です。

| PDF | baseline秒 | 表推論秒 | baseline結果 | 表推論結果 |
|---|---:|---:|---|---|
| 単一段組み | 0.995 | 1.159 | 文字・順序保持 | 同等 |
| 見出し階層 | 0.585 | 0.577 | 全見出しがH2 | 同等 |
| 箇条書き | 0.604 | 0.586 | Markdown箇条書き保持 | 同等 |
| 2段組み | 0.621 | 0.584 | 左列から右列の順序保持 | 同等 |
| 単純な表 | 0.576 | 1.217 | 1行1列へ平坦化 | 3行2列を保持 |
| ヘッダー・フッター | 0.567 | 0.595 | ヘッダー・フッター除外 | 同等 |
| 複数ページ | 1.765 | 1.787 | 1・2ページ目の見出し欠落 | 同等 |

総処理時間はbaseline 5.713秒、表推論あり6.505秒で、表推論ありは0.792秒、約13.9%増加しました。単純な表だけでは0.576秒から1.217秒へ増加しました。これらはウォームアップや反復統計を取っていない単回測定の参考値であり、ハードウェアやキャッシュ状態で変動します。

出力文字数は表だけが97文字から79文字へ変化しました。これは欠落ではなく、平坦化された1セル表が正しい3行2列Markdown表へ置き換わった結果です。他の6件は文字数、期待文字列、読み順、箇条書き、ヘッダー・フッター除外、複数ページ見出し欠落に差がありませんでした。

## モデル取得量とオフライン動作

- baseline layoutモデル成果物: 171,764,371 bytes（約171.8 MB）
- TableFormer成果物: 358,236,323 bytes（約358.2 MB）を追加
- 初回TableFormer取得時の一時キャッシュ増分: 716,474,726 bytes（Hugging Face Xetキャッシュを含む）
- オフライン環境変数を指定した再実行: 両モード14変換すべて成功

通信不安定により初回取得は再試行を要しましたが、外部推論、外部OCR、PDF本文の送信は行っていません。モデル成果物のbytesはDocling 2.113.0と当該Hugging Faceキャッシュ構成におけるblob実体の参考値で、Docling、huggingface-hub、Xetのバージョンやキャッシュ方式により変動します。

レポートの `offline_requested` は上記2環境変数をスクリプトが設定したこと、`all_conversions_succeeded` は全変換が成功したことを別々に表します。ファイアウォールやネットワークアダプター無効化によるOSレベルの通信遮断を検証した値ではありません。

## 判断

結論は **表構造推論をオプション化** です。

baselineは依存モデル量と処理時間を抑え、表以外で同等の結果を得られるため、通常CLIの既定値として維持します。一方、表を含む文書では `do_table_structure=True` が明確に有効なため、CLIの `--table-structure` で明示的に選択できます。

今回の単純表はDocling内の設定変更だけで改善したため、MarkPDFdown統合を直ちに行う根拠はありません。複雑な結合セル、複数ページ表、図表混在などの架空fixtureを追加してもDoclingで不足する場合に、別エンジン比較へ進みます。
