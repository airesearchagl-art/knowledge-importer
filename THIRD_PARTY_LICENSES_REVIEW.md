# Third-party License Metadata Review

確認日: 2026-08-09

判定: **条件付きGitHubソース公開継続可**

この一覧は、Knowledge Importerの実変換経路について、localにインストール済みのpackage metadata、同梱license / notice file、公式配布元の原文を突き合わせた事実の記録です。法的判断を行いません。SPDX、個別artifactへの適用範囲、再配布可否を推測しません。

## 判定の範囲

- GitHub repositoryはKnowledge Importer自身のMIT sourceと文書だけを公開し、third-party codeやruntime wheelを同梱していません。
- project wheel / sdistにもdependency wheel、native library、model artifactを同梱していません。
- Docling model artifactはrepository、wheel、sdistへ含めず、本projectから再配布しません。
- したがって、現在のsource-only公開は継続できます。
- GitHub Release、wheel / sdist、PyPI、依存packageを含むbinary、installer、model artifactの配布はこの判定に含めません。対象物に応じた原文license、NOTICE、attribution、再配布条件の別Human Gateが必要です。

## Projectとdirect dependency

| 対象 | Version | Installed metadata | Local license evidence | 公式参照 | Source-only判断 |
|---|---:|---|---|---|---|
| knowledge-importer | 0.1.0 | MIT | repository `LICENSE` | repository | project sourceへ反映済み |
| docling | 2.113.0 | `License-Expression: MIT` | installed metadataではlicense file未検出 | https://github.com/docling-project/docling | direct runtime dependency。codeのlicenseでありmodelには適用しない |

`pyproject.toml`のruntime direct dependencyは`docling==2.113.0`だけです。build済みwheelのmemberは`knowledge_importer` package、dist-info、project `LICENSE`で、第三者package sourceやmodel artifactの同梱は検出されません。sdistにもproject `LICENSE`を含めます。

## 実変換経路の主要runtime dependency

無制限なtransitive dependency監査は行わず、Doclingの通常PDF変換と検証済みTableFormer経路で主要なinstalled distributionへ範囲を限定しました。versionとlicense表記は今回のlocal環境で観測した値です。

| Distribution | Version | Installed metadata | Local license / notice evidence | Binary配布時の注意 |
|---|---:|---|---|---|
| docling-core | 2.87.1 | MIT | `dist-info/licenses/LICENSE` | 原文保持を確認する |
| docling-slim | 2.113.0 | MIT | `dist-info/licenses/LICENSE` | 原文保持を確認する |
| docling-ibm-models | 3.13.3 | MIT | `dist-info/licenses/LICENSE` | codeとmodel artifactの条件を混同しない |
| docling-parse | 7.8.0 | MIT | project LICENSE、CMap resources LICENSE | resourceごとの原文保持を確認する |
| torch | 2.13.0 | Apache-2.0、LLVM exception、BSD、BSL、MITを含むSPDX式 | project LICENSEと多数の`third_party` license | wheelごとの構成とNOTICEを個別監査する |
| transformers | 5.8.1 | Apache 2.0 | `dist-info/licenses/LICENSE` | 原文とNOTICE有無を対象versionで確認する |
| huggingface-hub | 1.24.0 | Apache-2.0 | `dist-info/licenses/LICENSE` | 原文とNOTICE有無を対象versionで確認する |
| safetensors | 0.8.0 | classifier: Apache Software License | `dist-info/licenses/LICENSE` | metadata fieldが空のため原文を優先する |
| pypdfium2 | 5.12.1 | BSD-3-Clause、Apache-2.0、dependency licenses | project licenses、Windows PDFium build licenses | binary buildに応じたPDFiumとdependency license一式を保持する |
| opencv-python | 5.0.0.93 | Apache 2.0 | `LICENSE.txt`、`LICENSE-3RD-PARTY.txt` | platform wheelに含まれるthird-party binaryの原文を確認する |

公式参照:

- Docling family: https://github.com/docling-project
- PyTorch license / notices: https://github.com/pytorch/pytorch/blob/main/LICENSE, https://github.com/pytorch/pytorch/blob/main/NOTICE
- Transformers: https://github.com/huggingface/transformers/blob/main/LICENSE
- Hugging Face Hub: https://github.com/huggingface/huggingface_hub/blob/main/LICENSE
- Safetensors: https://github.com/huggingface/safetensors/blob/main/LICENSE
- pypdfium2 licensing: https://github.com/pypdfium2-team/pypdfium2#licensing
- opencv-python third-party notices: https://github.com/opencv/opencv-python/blob/4.x/LICENSE-3RD-PARTY.txt

この表はinstalled metadataと同梱ファイルの存在確認であり、配布物ごとの法的適合性を保証しません。特にPyTorch、pypdfium2、opencv-pythonはnative binaryや多数のthird-party componentを含み得るため、binaryをまとめて公開する場合は対象platform・対象wheelを固定して再監査します。

## Docling codeとmodel artifactの分離

Docling 2.113.0のcodeはlocal package metadata上MITです。一方、Doclingが利用するmodel artifactはDocling codeとは別の配布物です。Knowledge ImporterのMIT LicenseやDocling codeのMITからmodelごとの条件を推測しません。

### Manual smokeで確認したmodel revision

| 用途 | Repository | 固定revision | Fixed revisionのmetadata | Fixed snapshot内の原文 | Human Gate |
|---|---|---|---|---|---|
| Layout Heron | `docling-project/docling-layout-heron` | `1907ed0d4f5ef93ada62374230490e95c599fceb` | `apache-2.0` | READMEあり、独立したLICENSE / NOTICEなし | model card、Apache-2.0原文、attribution、再配布条件を人が確認する |
| TableFormer V1 accurate | `docling-project/docling-models` | `fc0f2d45e2218ea24bce5045f58a389aed16dc23`（requested `v2.3.0`） | `cdla-permissive-2.0` | READMEあり、独立したLICENSE / NOTICEなし | CDLA-Permissive-2.0原文とweight / configへの適用関係を人が確認する |

TableFormerについて、固定revisionのmetadataは`CDLA-Permissive-2.0`のみです。現在のmodel repositoryにある`CDLA-Permissive-2.0 / Apache-2.0`の併記は固定revisionより後のmetadata変更であるため、検証済みrevisionへ遡って適用されるとは扱いません。HeronもTableFormerもmetadata labelだけを原文licenseの代替にしません。

公式参照:

- Heron model card: https://huggingface.co/docling-project/docling-layout-heron
- TableFormer fixed tag: https://huggingface.co/docling-project/docling-models/tree/v2.3.0
- TableFormer license metadata変更: https://huggingface.co/docling-project/docling-models/commit/72baa83f6a61df0b1c46f627d391e98659202095
- Hugging Face model card metadata: https://huggingface.co/docs/hub/model-cards
- Apache License 2.0原文: https://www.apache.org/licenses/LICENSE-2.0
- CDLA-Permissive-2.0原文: https://cdla.dev/permissive-2-0/

2026-08-09のmanual smokeでは、両snapshotの必要artifactとREADMEをrepository外の一時local artifacts rootへ配置し、Docling 2.113.0の`PdfPipelineOptions.artifacts_path`から通常モードとTableFormerモードを完全offlineで実行しました。model download、cache ref追加、repositoryへのcopyは行っていません。詳細は [Real Docling Smoke Validation](docs/REAL_DOCLING_SMOKE_VALIDATION.md) を参照してください。

## 未解消のHuman Gate

- fixed model revisionに適用される原文license、NOTICE、attribution、利用・再配布条件
- TableFormer weight / configへCDLA-Permissive-2.0が適用される範囲
- dependency wheelを同梱するbinary配布時の、対象platform別native libraryとthird-party notice
- GitHub Release、wheel / sdist、PyPI、installer、model artifact再配布の個別判断
- 各公開操作直前の最終diffと配布物内容の確認

`unknown`やmetadata欠落は法的結論へ置き換えません。必要な原文を取得・確認できない対象は、その対象物を配布しない状態を維持します。
