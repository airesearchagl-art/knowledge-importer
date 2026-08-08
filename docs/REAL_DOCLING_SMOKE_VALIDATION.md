# Real Docling Smoke Validation

実施日: 2026-08-09

この文書は、modelをrepositoryへ含めず、cache済みfixed revisionをDoclingの正式なlocal artifacts経路から利用したmanual validationの結果です。実資料、外部API、model download、network fallbackは使用していません。

## 判定

判定: **pass with limitations**

- 通常モード: 架空PDF 4件すべて成功
- `--table-structure`: 表を含む架空PDF 2件すべて成功
- Batch JSON / CSV: 4件すべて`succeeded`
- Quality JSON: `checked=4`, `passed=4`, `warned=0`
- 再実行: 4件すべてskip
- `--force`: 4件すべて再生成し、初回出力とSHA-256が一致
- 完全offline再実行: 成功

この結果は合成PDFと確認済みrevisionに限定され、複雑な実資料の変換品質、modelの再配布可否、wheel / PyPI公開を保証しません。

## 実行条件

- Knowledge Importer base: `f508a8147bb3936ba6d74ba4eaa11d6c1c30db8b`
- Docling: `2.113.0`
- `do_ocr=False`
- `force_backend_text=True`
- `enable_remote_services=False`
- 通常モード: `do_table_structure=False`
- 表構造モード: `do_table_structure=True`
- `HF_HUB_OFFLINE=1`
- `TRANSFORMERS_OFFLINE=1`
- `HF_DATASETS_OFFLINE=1`
- `HF_HUB_DISABLE_IMPLICIT_TOKEN=1`

## Local artifacts

`PdfPipelineOptions.artifacts_path`へ、次の共通rootを`--artifacts-path`で渡しました。

```text
artifacts/
├─ docling-project--docling-layout-heron/
│  ├─ config.json
│  ├─ preprocessor_config.json
│  ├─ model.safetensors
│  └─ README.md
└─ docling-project--docling-models/
   ├─ README.md
   └─ model_artifacts/
      └─ tableformer/
         └─ accurate/
            ├─ tm_config.json
            └─ tableformer_accurate.safetensors
```

| 用途 | Repository | Revision | 結果 |
|---|---|---|---|
| Layout Heron | `docling-project/docling-layout-heron` | `1907ed0d4f5ef93ada62374230490e95c599fceb` | 通常・表構造モードで使用成功 |
| TableFormer V1 accurate | `docling-project/docling-models` | `fc0f2d45e2218ea24bce5045f58a389aed16dc23`（`v2.3.0`） | 表構造モードで使用成功 |

cacheの`refs/main`やmetadataは変更していません。必要ファイルは検証専用のrepository外一時ディレクトリへ通常copyし、READMEも保持しました。一時copyは検証後に削除します。

## 合成PDF

4件すべてを一時ディレクトリへ生成し、テキスト層と全9ページの画像renderを確認しました。repositoryへ追加していません。

| PDF | Pages | 構造 |
|---|---:|---|
| `basic_document.pdf` | 2 | title、見出し、段落、箇条書き |
| `table_document.pdf` | 2 | 3列・4データ行の表、前後本文 |
| `japanese_mixed.pdf` | 2 | 日本語、ASCII、見出し、箇条書き、簡易表 |
| `multi_section.pdf` | 3 | 3階層見出し、段落、箇条書き、表 |

実会社名、実案件名、実個人名、実住所、実メールは使用していません。

## 通常モード結果

| PDF | Exit | Chars | 見出し | 箇条書き | 主要語句・ページ順 | 判定 |
|---|---:|---:|---:|---:|---|---|
| Basic document | 0 | 438 | 4 | 3 | 保持 | pass |
| Table document | 0 | 445 | 2 | 0 | 保持 | pass with limitation |
| Japanese mixed | 0 | 291 | 3 | 2 | 日本語を含め保持 | pass with limitation |
| Multi-section | 0 | 652 | 5 | 2 | 3ページ終端まで保持 | pass with limitation |

全MarkdownはUTF-8、非空で、Windows/POSIX絶対path、traceback、Unicode category Cfを含みませんでした。通常モードの表はセル文字列を保持しましたが、行・列が1行へ平坦化されました。

## TableFormer比較

| PDF | 通常モード | `--table-structure` | 変化 |
|---|---|---|---|
| Table document | セル保持、行・列は平坦化 | 3列、header＋4データ行 | 改善 |
| Japanese mixed | 日本語セル保持、行・列は平坦化 | 3列、header＋2データ行 | 改善 |

両ケースとも周辺本文、見出し順、セル文字列を保持し、欠落や誤結合は確認されませんでした。TableFormerは追加modelと推論時間を必要とするため、既定値は引き続き`False`です。

## Quality・batch・再実行

- 初回batch: `total=4`, `succeeded=4`, `failed=0`, `skipped=0`, exit code `0`
- Batch JSON schema version 1とCSV: 同じ4 items、相対POSIX path、決定的順序
- Quality JSON schema version 1: `checked=4`, `passed=4`, `warned=0`
- Quality Warning: 0件。`quality-read-error`、`absolute-path`、`traceback`、`control-character`、`short-output`なし
- 通常再実行: `succeeded=0`, `failed=0`, `skipped=4`, exit code `0`
- skip時Quality JSON: `checked=0`, `passed=0`, `warned=0`
- `--force`再実行: `succeeded=4`, `failed=0`, `skipped=0`, exit code `0`
- 初回と`--force`後のMarkdown 4件、Batch JSON、CSV、Quality JSONはSHA-256一致

warningの有無は変換成否、BatchResult、Batch JSON、CSV、終了コードを変更していません。

## Offline・cache不変確認

同じlocal artifacts setupでoffline再実行し、4件すべて成功しました。検証前後で次のrevisionは不変で、新規snapshotはありませんでした。

- Heron: `1907ed0d4f5ef93ada62374230490e95c599fceb`
- TableFormer: `fc0f2d45e2218ea24bce5045f58a389aed16dc23`

model download、snapshot download、token利用、cache ref追加、cache metadata変更、external API、external MCPは行っていません。

## 公開判断と残るHuman Gate

判定: **条件付きGitHubソース公開継続可**

Knowledge Importerは第三者code、runtime wheel、model artifactをrepositoryへ同梱・再配布せず、`--artifacts-path`は利用者が適法に取得したlocal artifactの参照だけを提供します。この条件下でGitHubソース公開を継続できます。

引き続き人が確認する項目:

- Heron model cardとApache-2.0原文
- TableFormer V1 accurateの固定revisionでmetadataに記録されたCDLA-Permissive-2.0原文と、weight / configへの適用関係
- Doclingおよび主要runtime dependencyのlicense原文とNOTICE要件
- 実資料を使う組織内検証は、repository外かつ適切な権限・取扱規則のもとで別途実施
- GitHub Release、wheel / sdist公開、PyPI、依存packageを含むbinary、model再配布は別Human Gate
