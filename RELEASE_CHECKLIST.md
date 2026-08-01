# v0.1.0 Public Release Gate

判定: **公開準備不可**

この文書はtag、GitHub Release、配布先への公開前に、人が確認する項目をまとめたものです。ライセンスや法的適合性を判断する文書ではありません。

## 確認済み

- version metadataとpackage `__version__`: `0.1.0`
- wheel / sdist build: 成功
- wheel本体のoffline install、CLI help、package import: 成功
- unit / integration / release-readiness tests: 成功
- fake converterによるBatch JSON、CSV、Quality JSON統合smoke: 成功
- tracked filesと配布物のsecret、実メール、local identity、実PDF、巨大ファイル検査: 検出0件
- wheel / sdistへのtests、scripts、生成出力、cache、`.env`混入: 検出0件
- 外部API、外部MCP、モデルdownload: 未使用

## 未完了のHuman Gate

- [ ] project licenseを決定し、LICENSEとpackage metadataへ反映する
- [ ] `THIRD_PARTY_LICENSES_REVIEW.md`のunknown・再配布注意候補を人が確認する
- [ ] Docling wheelとtransitive dependencyをoffline配布する方針を決定する
- [ ] 必要なDocling model artifactを適法な方法で事前取得し、offline実変換を再検証する
- [ ] 公開先、配布対象、Release Notes、tag作成者を決定する
- [ ] 承認後にtag、GitHub Release、必要な配布先公開を別作業として実施する

## 実Docling smoke結果

2ページの架空PDFを使用しました。タイトル、2階層見出し、複数段落、3項目の箇条書き、3列の単純表、日本語とASCII、ページ遷移語句を含みます。PDFはテキスト層を持ち、画像レンダリングでも欠け・重なり・文字化けがないことを確認しました。

`HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`、`HF_DATASETS_OFFLINE=1`および`uv --offline`でproduction CLIを実行しました。必要なDocling model snapshotがlocal cacheに存在しなかったため、変換は終了コード`1`で停止しました。Markdownは生成されず、Quality JSONは`checked=0`の安全な空レポートでした。cache件数と容量に変化はなく、downloadは発生していません。

このため、real Docling変換は公開前に再確認が必要です。ネットワークを有効化して回避した結果や、実資料による検証結果は含みません。

## Offline確認結果

| 対象 | 結果 |
|---|---|
| wheel / sdist build | offlineで成功 |
| wheel本体のclean install | `--no-deps`、offlineで成功 |
| CLI help / package import | 成功 |
| 依存込み完全offline install | Docling wheelがuv cacheになく失敗 |
| fake converter smoke | 成功 |
| real Docling smoke | model snapshot不足で失敗 |

## v0.1.0 Release Notes草案

### Added

- OCR済みテキスト層PDFをMarkdownへ変換するlocal CLI
- 単一PDFとdirectory batch conversion
- 明示指定によるrecursive探索と相対directory構造保持
- include / exclude glob filters
- 再実行時のskip / `--force`
- 安全なerror classificationと部分失敗後の継続
- Batch JSON schema version 1とCSV report
- opt-in quality warningsと独立Quality JSON schema version 1
- 決定的な合成fixture品質テスト
- wheel / sdist buildとclean-install smoke test

### Known limitations

- OCR、外部API、cloud OCR、LLM評価は実行しない
- 未OCR画像PDF、複雑な段組み、数式、複雑な表の再現を保証しない
- 完全offline installと実変換には依存wheelとDocling model artifactの事前cacheが必要
- real Docling offline smokeは必要model snapshot不足のため未完了
- project licenseと公開先は未決定
