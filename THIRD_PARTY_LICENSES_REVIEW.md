# Third-party License Metadata Review

この一覧は、localにインストール済みのpackage metadata、同梱license file名、`pyproject.toml`、`uv.lock`から得た事実の記録です。SPDXや再配布条件を推測せず、法的判断を行いません。public repositoryでのソース公開を継続する間も、人による確認が必要です。

## Projectと主要dependency

| 対象 | Version / 範囲 | Metadata上のlicense | License file | Metadata URL | 用途・確認事項 |
|---|---:|---|---:|---|---|
| knowledge-importer | 0.1.0 | MIT | `LICENSE` | repository | `Copyright (c) 2026 airesearchagl-art`とpackage metadataに反映済み |
| docling | 2.113.0 | MIT | metadataから未検出 | https://github.com/docling-project/docling | 唯一のruntime direct dependency。Docling codeのmetadata上の記録であり、model artifactには適用しない |
| argparse | Python 3.12標準library | package metadata対象外 | package外 | metadata対象外 | CLI framework。Python自体を同梱配布する場合は別途確認 |
| reportlab | 5.0.0 | BSD licenseとの記載 | 4件検出 | https://www.reportlab.com/ | dev dependency、合成PDF生成用。license.txt原文確認が必要 |
| pypdf | 6.14.2 | BSD-3-Clause | 1件検出 | https://github.com/py-pdf/pypdf | dev dependency、PDFテキスト層検査用 |
| pytest | 8.4.2 | MIT | 1件検出 | https://github.com/pytest-dev/pytest | dev / test tooling |
| ruff | 0.12.12 | unknown | 1件検出 | https://github.com/astral-sh/ruff | dev tooling。metadata fieldが空のため原文確認が必要 |
| uv_build | >=0.11.0,<0.12.0 | unknown | local metadata未取得 | local metadata未取得 | build backend。公開前に使用versionと原文確認が必要 |

wheelのmemberは`knowledge_importer` package、dist-info、project `LICENSE`だけで、第三者packageのsource code同梱は検出されません。sdistにもproject `LICENSE`を含めます。利用者がinstallするDoclingのtransitive dependencyは別途確認対象です。

## Docling codeとmodel artifactの分離

Docling 2.113.0のcodeはlocal package metadata上MITです。一方、Doclingが利用するmodel artifactはDocling codeとは別の配布物であり、Knowledge ImporterのMIT LicenseやDocling codeのlicenseからmodelごとの条件を推測しません。

Knowledge Importerのrepository、wheel、sdistにmodel artifactは含めず、本projectから再配布しません。利用者によるmodel取得・利用前に、対象modelのlicense、terms、取得条件のmanual verificationが必要です。

### Manual smokeで確認したmodel revision

| 用途 | Repository | 固定revision | Metadata上のlicense | Human Gate |
|---|---|---|---|---|
| Layout Heron | `docling-project/docling-layout-heron` | `1907ed0d4f5ef93ada62374230490e95c599fceb` | Apache-2.0 | model cardと原文license、Doclingの`main`指定との対応付けを人が確認する |
| TableFormer V1 accurate | `docling-project/docling-models` | `fc0f2d45e2218ea24bce5045f58a389aed16dc23`（`v2.3.0`） | CDLA-Permissive-2.0 / Apache-2.0 | `model_artifacts/tableformer/accurate`へ適用される条件をmodel card・原文から人が確認する |

上記licenseはmodel repository metadataの記録であり、個別artifactへの適用関係や再配布可否を判断するものではありません。特に`docling-models`は複数license表記があるため、TableFormer weightとconfigに適用される原文条件を確認する必要があります。

2026-08-08のmanual smokeでは両snapshotがrepository外のlocal cacheに存在することを確認しましたが、Heronに`main` refがなく完全offline初期化は失敗しました。model download、cache ref追加、repositoryへのcopyは行っていません。詳細は [Real Docling Smoke Validation](docs/REAL_DOCLING_SMOKE_VALIDATION.md) を参照してください。

## Metadataがunknownだったinstalled distribution

local環境の110 distributions中、次の10件はlicense metadata fieldが空または判別不能でした。license fileが存在する場合でも、metadataだけでは断定していません。

- annotated-types 0.7.0
- colorama 0.4.6
- Jinja2 3.1.6
- markdown-it-py 4.2.0
- mdurl 0.1.2
- omegaconf 2.3.1
- ruff 0.12.12
- safetensors 0.8.0
- tokenizers 0.22.2
- tree-sitter 0.26.0

`tokenizers`はlicense fileもmetadataから検出できませんでした。package配布元の原文確認が必要です。

## 再配布上の注意候補

文字列ベースのmetadata確認では、以下が追加確認候補です。これはcopyleft適用や公開可否を判断するものではありません。

- certifi 2026.6.17: `MPL-2.0`
- tqdm 4.69.0: `MPL-2.0 AND MIT`
- scipy 1.18.0: metadata内にbundled libraryとGCC Runtime Library Exceptionの記載

## 公開状態の継続と追加配布前に人が確認する事項

- project MIT Licenseとcopyright表記の最終確認
- Docling本体およびruntime transitive dependencyのlicense原文
- binary wheelに含まれるnative libraryとnotice要件
- model artifactのlicense、取得条件、再配布可否
- 将来wheel / sdist / installerを公開する場合の配布形式（現在はpublic GitHub repositoryでのソース公開のみ）
- 必要なNOTICEまたはthird-party attributionの形式
