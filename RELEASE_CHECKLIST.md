# v0.1.0 Public Release Gate

判定: **条件付きGitHubソース公開継続可**

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
- 合成fixtureによるKnowledge Package全lifecycle smoke: 成功
- fake converterによるBatch JSON、CSV、Quality JSON統合smoke: 成功
- fixed revisionのlocal artifactsを使うproduction Docling smoke: 成功
- 通常モード4件、TableFormerモード2件、offline再実行: 成功
- tracked filesと配布物のsecret、実メール、local identity、実PDF、巨大ファイル検査: 検出0件
- wheel / sdistへのtests、scripts、生成出力、cache、`.env`混入: 検出0件
- 外部API、外部MCP、モデルdownload: 未使用

## 未完了のHuman Gate

- [x] project licenseをMITとし、`LICENSE`とpackage metadataへ反映する
- [x] 実変換経路の主要runtime dependencyについて、installed metadataと同梱license / notice fileの有無を記録する
- [x] Heronのfull revisionを正式なlocal artifacts経路で指定し、offline実変換を再検証する
- [ ] fixed model revisionに適用される原文license、NOTICE、attribution、再配布条件を人が確認する
- [ ] binary配布を行う場合、対象platformのruntime wheelに含まれるnative libraryとthird-party noticeを人が確認する
- [ ] ソース公開状態の継続前提として、各PRの最終diffを人が確認する

wheel / sdistの公開配布、GitHub Release、PyPI、tag作成、model再配布を将来行う場合は、別作業としてdependency、native library、NOTICE、model termsを再確認します。

## 実Docling smoke結果（2026-08-09）

GitHubソース公開状態の継続検証で、次の架空PDFを一時生成しました。すべてテキスト層を持ち、全9ページの画像レンダリングで欠け・重なり・文字化けがないことを確認しました。

| 架空PDF | 構成 | production Docling / Quality Report |
|---|---|---|
| Basic document | 見出し、段落、箇条書き、2ページ | 成功、Quality passed |
| Table document | 3列4データ行の表、前後文、2ページ | 通常成功、TableFormerで表構造改善 |
| Japanese mixed document | 日本語、ASCII、箇条書き、表、2ページ | 通常・TableFormer成功、Quality passed |
| Multi-section document | 3階層見出し、表、箇条書き、3ページ | 成功、Quality passed |

Heron snapshot `1907ed0d4f5ef93ada62374230490e95c599fceb`とTableFormer snapshot `fc0f2d45e2218ea24bce5045f58a389aed16dc23`（`v2.3.0`）を、repository外の一時local artifacts rootへ配置し、`--artifacts-path`で`PdfPipelineOptions.artifacts_path`へ渡しました。cacheの`main` refやmetadataは変更していません。

batch reportは`total=4`, `succeeded=4`, `failed=0`, `skipped=0`で、JSON / CSVは同じitemsを安全な相対pathだけで記録しました。Quality JSONは`checked=4`, `passed=4`, `warned=0`でした。再実行は4件skip、`--force`は4件再生成に成功し、初回とSHA-256が一致しました。詳細は [Real Docling Smoke Validation](docs/REAL_DOCLING_SMOKE_VALIDATION.md) を参照してください。

real Docling smoke、TableFormer比較、成功Markdownの基礎品質評価は完了しました。GitHubソースは第三者code、runtime wheel、model artifactを同梱・再配布しない条件で公開継続可能です。この結果はGitHub Release、wheel / sdist、PyPI、依存packageを含むbinary、model再配布を許可するものではありません。fixed model revisionの原文条件と、binary配布時のruntime dependency・native library・NOTICE確認は引き続きHuman Gateです。

## Offline確認結果

| 対象 | 結果 |
|---|---|
| wheel / sdist build | offlineで成功 |
| wheel本体のclean install | `--no-deps`、offlineで成功 |
| CLI help / package import | 成功 |
| 依存込み完全offline install | Docling wheelがuv cacheになく失敗 |
| fake converter smoke | 成功 |
| real Docling smoke | fixed revisionのlocal artifacts指定で通常4件・TableFormer 2件に成功 |

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
- real Docling offline smokeはfixed revisionのlocal artifacts指定で完了
- GitHubソース公開以外の公開先・配布形式は対象外
