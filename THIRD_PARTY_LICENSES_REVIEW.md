# Third-party License Metadata Review

この一覧は、localにインストール済みのpackage metadata、同梱license file名、`pyproject.toml`、`uv.lock`から得た事実の記録です。SPDXや再配布条件を推測せず、法的判断を行いません。公開前に人による確認が必要です。

## Projectと主要dependency

| 対象 | Version / 範囲 | Metadata上のlicense | License file | Metadata URL | 用途・確認事項 |
|---|---:|---|---:|---|---|
| knowledge-importer | 0.1.0 | unknown | なし | repository | LICENSEとpackage license metadataが未決定。公開blocker |
| docling | 2.113.0 | MIT | metadataから未検出 | https://github.com/docling-project/docling | 唯一のruntime direct dependency。原文と再配布条件の確認が必要 |
| argparse | Python 3.12標準library | package metadata対象外 | package外 | metadata対象外 | CLI framework。Python自体を同梱配布する場合は別途確認 |
| reportlab | 5.0.0 | BSD licenseとの記載 | 4件検出 | https://www.reportlab.com/ | dev dependency、合成PDF生成用。license.txt原文確認が必要 |
| pypdf | 6.14.2 | BSD-3-Clause | 1件検出 | https://github.com/py-pdf/pypdf | dev dependency、PDFテキスト層検査用 |
| pytest | 8.4.2 | MIT | 1件検出 | https://github.com/pytest-dev/pytest | dev / test tooling |
| ruff | 0.12.12 | unknown | 1件検出 | https://github.com/astral-sh/ruff | dev tooling。metadata fieldが空のため原文確認が必要 |
| uv_build | >=0.11.0,<0.12.0 | unknown | local metadata未取得 | local metadata未取得 | build backend。公開前に使用versionと原文確認が必要 |

wheelのmemberは`knowledge_importer` packageとdist-infoだけで、第三者packageのsource code同梱は検出されませんでした。sdistもREADME、pyproject、`src/knowledge_importer`だけです。ただし、利用者がinstallするDoclingのtransitive dependencyは別途確認対象です。

## Metadataがunknownだったinstalled distribution

local環境の110 distributions中、次の11件はlicense metadata fieldが空または判別不能でした。license fileが存在する場合でも、metadataだけでは断定していません。

- annotated-types 0.7.0
- colorama 0.4.6
- Jinja2 3.1.6
- knowledge-importer 0.1.0
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

## 公開前に人が確認する事項

- project licenseとcopyright holder
- Docling本体およびruntime transitive dependencyのlicense原文
- binary wheelに含まれるnative libraryとnotice要件
- model artifactのlicense、取得条件、再配布可否
- GitHub Releaseのみか、wheel / sdist / installerを配布するか
- 必要なNOTICEまたはthird-party attributionの形式
