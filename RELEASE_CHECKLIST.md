# v0.1.0 Public Release Gate

判定: **GitHubソース公開継続可（Human Gate継続）**

この文書は、既にpublicであるGitHub repositoryのソース公開状態を安全に継続するため、人が確認する項目をまとめたものです。repositoryをこれからpublicへ切り替えることを前提としません。ライセンスや法的適合性を判断する文書ではありません。GitHub Release、wheel / sdist配布、PyPI公開、model artifact再配布は別のHuman Gateが必要で、今回の対象外です。

## 公開範囲

- GitHub repositoryのソースコードのみ
- project本体はMIT License
- dependencyとmodel artifactは各配布元のlicense / termsに従う
- Docling model artifactはrepository、wheel、sdistに含めず、再配布しない

## 確認済み

- version metadataとpackage `__version__`: `0.1.0`
- project MIT Licenseとpackage metadata: 反映済み
- wheel / sdist build: 成功
- wheel本体のoffline install、CLI help、package import: 成功
- unit / integration / release-readiness tests: 成功
- fake converterによるBatch JSON、CSV、Quality JSON統合smoke: 成功
- tracked filesと配布物のsecret、実メール、local identity、実PDF、巨大ファイル検査: 検出0件
- wheel / sdistへのtests、scripts、生成出力、cache、`.env`混入: 検出0件
- 外部API、外部MCP、モデルdownload: 未使用

## 未完了のHuman Gate

- [x] project licenseをMITとし、`LICENSE`とpackage metadataへ反映する
- [ ] `THIRD_PARTY_LICENSES_REVIEW.md`のunknown・再配布注意候補を人が確認する
- [ ] 必要なDocling model artifactを適法な方法で事前取得し、offline実変換を再検証する
- [ ] Docling codeとruntime dependencyのlicense原文について最終的な人手確認を行う
- [ ] ソース公開状態の継続前提として、各PRの最終diffを人が確認する

wheel / sdistの公開配布、GitHub Release、PyPI、tag作成、model再配布を将来行う場合は、別作業としてdependency、native library、NOTICE、model termsを再確認します。

## 実Docling smoke結果

GitHubソース公開状態の継続検証で、次の架空PDFを一時生成しました。すべてテキスト層を持ち、全9ページの画像レンダリングで欠け・重なり・文字化けがないことを確認しました。

| 架空PDF | 構成 | production Docling / Quality Report |
|---|---|---|
| Basic document | 見出し、段落、箇条書き、2ページ | model cache不足のため未実行 |
| Table document | 3列4行の表、前後文、2ページ | model cache不足のため未実行 |
| Japanese mixed document | 日本語、ASCII、箇条書き、表、2ページ | model cache不足のため未実行 |
| Multi-section document | 3階層見出し、表、箇条書き、3ページ | model cache不足のため未実行 |

production Doclingが参照するmodel snapshot / artifactはlocal cacheから確認できませんでした。確認できたHugging Face cacheは本変換と無関係の音声認識modelのみです。指示どおりmodel downloadや取得requestを開始せず、production CLIによる変換とQuality Report生成は実行していません。

このためreal Docling smokeは未完了のHuman Gateとして維持します。この未完了項目は、wheel / PyPI / model再配布を許可する結果ではありません。

## Offline確認結果

| 対象 | 結果 |
|---|---|
| wheel / sdist build | offlineで成功 |
| wheel本体のclean install | `--no-deps`、offlineで成功 |
| CLI help / package import | 成功 |
| 依存込み完全offline install | Docling wheelがuv cacheになく失敗 |
| fake converter smoke | 成功 |
| real Docling smoke | model snapshot不足のため未実行（過去のoffline検証でも同不足により失敗） |

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
- GitHubソース公開以外の公開先・配布形式は対象外
