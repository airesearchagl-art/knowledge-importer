# Real Docling Smoke Validation

実施日: 2026-08-08

この文書は、modelをrepositoryへ含めず、既存のlocal Hugging Face cacheだけを使ったmanual release validationの結果です。実資料、外部API、model download、network fallbackは使用していません。

## 実行条件

- Knowledge Importer base: `4d0b544825537e854c8b38ee5f3b14fd4e0cddc3`
- Docling: `2.113.0`
- `do_ocr=False`
- `do_table_structure=False`（通常モード）
- `force_backend_text=True`
- `enable_remote_services=False`
- `HF_HUB_OFFLINE=1`
- `TRANSFORMERS_OFFLINE=1`
- `HF_DATASETS_OFFLINE=1`
- `HF_HUB_DISABLE_IMPLICIT_TOKEN=1`

確認したmodel snapshot:

| 用途 | Repository | Revision | Cache確認 |
|---|---|---|---|
| Layout | `docling-project/docling-layout-heron` | `1907ed0d4f5ef93ada62374230490e95c599fceb` | artifactあり、`main` refなし |
| TableFormer | `docling-project/docling-models` | `fc0f2d45e2218ea24bce5045f58a389aed16dc23`（`v2.3.0`） | artifactとtag refあり |

## 合成PDF

PDFは一時ディレクトリだけで生成し、repositoryへ追加していません。全4件にテキスト層があり、合計9ページを画像renderして欠け、重なり、文字化けがないことを確認しました。

| PDF | Pages | 構成 |
|---|---:|---|
| `basic_document.pdf` | 2 | 2階層見出し、複数段落、3項目の箇条書き |
| `table_document.pdf` | 2 | 3列4データ行の表、表の前後文 |
| `japanese_mixed.pdf` | 2 | 架空の日本語本文、ASCII、箇条書き、簡易表 |
| `multi_section.pdf` | 3 | 3階層見出し、段落、箇条書き、表、ページ境界marker |

## Production smoke結果

判定: **failed**

Docling 2.113.0はlayout modelを`revision="main"`で解決します。cacheには指定full revisionのsnapshotと必要artifactが存在しましたが、`main` refが存在しませんでした。完全offline環境では`main`からsnapshotを解決できず、`LocalEntryNotFoundError`でpipeline初期化が停止しました。

通常モードと`--table-structure`の両方が同じlayout初期化箇所で停止しました。TableFormerはlayoutより後に初期化されるため、今回の実行ではTableFormer accurateの実推論まで到達していません。

| PDF | Normal | Table mode | Heading | Body | Bullets | Table | Japanese | Quality warnings | 判定 |
|---|---|---|---|---|---|---|---|---|---|
| Basic document | 初期化失敗 | 対象外 | 未評価 | 未評価 | 未評価 | 対象外 | 対象外 | 未評価 | fail |
| Table document | 未実行 | 初期化失敗 | 未評価 | 未評価 | 対象外 | 未評価 | 対象外 | 未評価 | fail |
| Japanese mixed | batchで初期化失敗 | 未到達 | 未評価 | 未評価 | 未評価 | 未評価 | 未評価 | 未評価 | fail |
| Multi-section | batchで初期化失敗 | 対象外 | 未評価 | 未評価 | 未評価 | 未評価 | 対象外 | 未評価 | fail |

Markdownが生成されなかったため、見出し、本文、箇条書き、表、日本語、ページ順序、notable lossは評価できません。

## Report結果

4件のbatch smokeは終了コード`1`となり、全件が`converter生成・変換処理関連`へ分類されました。

- Batch JSON: `total=4`, `succeeded=0`, `failed=4`, `skipped=0`
- CSV: 4件。Batch JSONと入力順・status・分類が一致
- Quality JSON: `checked=0`, `passed=0`, `warned=0`
- Quality Warning: 変換成功Markdownがないため未評価
- report path: 入出力root相対のPOSIX pathのみ
- traceback、local absolute path: reportへ混入なし
- 同条件再実行: JSON、CSV、Quality JSONのSHA-256が一致
- `--force`: 同じlayout初期化失敗。再生成処理へ未到達

## Cache不変確認

実行前後で次のrevisionは不変でした。

- Heron: `1907ed0d4f5ef93ada62374230490e95c599fceb`
- TableFormer: `fc0f2d45e2218ea24bce5045f58a389aed16dc23`

新規download、新規snapshot、token利用、cache ref追加、`DOCLING_ARTIFACTS_PATH`変更は行っていません。

## 残るHuman Gate

- Heron full revisionをDoclingの`revision="main"`へofflineで安全に対応付ける方法の確認
- cache変更を行う場合の手順、出所、revision、license原文の人手承認
- production通常モードの再実行
- TableFormer accurateの実推論と表構造比較
- 成功MarkdownのQuality Warning / Quality JSON評価
- skip / `--force`再生成の実変換確認

GitHubソース公開とmodel配布は別判断です。この結果はmodel、wheel、sdist、PyPI、GitHub Releaseの公開・再配布を許可するものではありません。
