# Knowledge Importer

OCR処理済みでテキストレイヤーを持つPDFを、ローカル環境だけでMarkdownへ変換する最小CLIです。外部API、クラウドOCR、LLM API、API従量課金サービスを使用せず、PDF本文をこれらのサービスへ送信しません。

## 対応範囲

- 1件のPDFを1件のUTF-8 Markdownへ変換
- Docling変換エンジン（`2.113.0`）
- 入力検証、出力先ディレクトリ作成、上書き防止
- ファイルログへの開始・終了・成否・例外種別の記録

GUI、Obsidian連携、RAG登録、要約・タグ生成は未対応です。

## Install

Python 3.12と[uv](https://docs.astral.sh/uv/)を使用します。

開発環境をrepositoryから構築する場合:

```powershell
uv python install 3.12
uv sync --dev
```

ローカルwheelをbuildしてCLIとしてinstallする場合:

```powershell
uv build
uv tool install .\dist\knowledge_importer-0.1.0-py3-none-any.whl
knowledge-importer --help
```

`dist/`のwheelとsdistはGit管理対象外です。現在のversionは`0.1.0`で、repositoryはGitHubでソースコードを公開中です。GitHub Release、wheel配布、PyPI公開は行っていません。

初回セットアップまたは初回変換時、Doclingがローカル推論用パッケージやモデル成果物を取得する場合があります。変換処理自体はローカルで実行され、PDFは外部サービスへ送信されません。完全オフライン運用では、必要なモデル成果物を事前に取得したうえで、環境ごとの動作検証が別途必要です。

## Quick Start

### Single PDF

```powershell
uv run knowledge-importer --help
uv run knowledge-importer convert .\input\sample.pdf --output .\output\sample.md
uv run knowledge-importer convert .\input\sample.pdf --output .\output\sample.md --force
uv run knowledge-importer convert .\input\table.pdf --output .\output\table.md --table-structure
uv run knowledge-importer convert .\input\sample.pdf --output .\output\sample.md --artifacts-path D:\docling-artifacts
```

単一PDF変換では、既存出力は`--force`なしでは上書きせずエラーにします。`--table-structure`を指定した場合のみDocling TableFormerによる表構造推論を有効化します。表の行・列をMarkdown表として保持しやすくなる一方、初回は追加モデルの取得が発生する可能性があり、通常モードより処理時間とディスク使用量が増えます。

`--artifacts-path PATH`を指定すると、Doclingが事前取得済みmodel artifactをローカルディレクトリから読み込みます。PATHは存在するディレクトリである必要があり、単一PDF、一括変換、`--table-structure`のすべてで利用できます。未指定時は従来どおりDocling既定のmodel解決を使います。PATH自体はJSON、CSV、Quality JSON、BatchResultへ記録しません。

Docling 2.113.0で確認した推奨配置は次のとおりです。`--artifacts-path`には、この2ディレクトリを含む共通rootを指定します。

```text
docling-artifacts/
├─ docling-project--docling-layout-heron/
│  ├─ config.json
│  ├─ preprocessor_config.json
│  └─ model.safetensors
└─ docling-project--docling-models/
   └─ model_artifacts/
      └─ tableformer/
         └─ accurate/
            ├─ tm_config.json
            └─ tableformer_accurate.safetensors
```

model artifactはこのrepositoryや配布packageへコピーせず、各modelのlicense・termsを確認したうえでrepository外に保管してください。

### Batch conversion

```powershell
uv run knowledge-importer convert .\input --output .\output
uv run knowledge-importer convert .\input --output .\output --force --table-structure
```

一括変換では、既存のMarkdownファイルを`--force`なしで安全にスキップし、`--force`指定時だけ再生成して上書きします。ログは`logs/knowledge-importer.log`に保存します。

### Opt-in Markdown normalization

```powershell
uv run knowledge-importer convert .\input\sample.pdf --output .\output\sample.md --normalize-markdown conservative
uv run knowledge-importer convert .\input --output .\output --recursive --normalize-markdown conservative
```

`--normalize-markdown conservative`を明示した場合だけ、Doclingが生成したMarkdownへ決定的な後処理を適用します。未指定時は従来どおり生成bytesを変更しません。conservative profileはUTF-8 BOMを除去し、CRLF/CRをLFへ統一し、通常行の末尾タブと1個だけの末尾スペースを除去し、末尾の余分な空行を除いてファイル末尾を1改行に揃えます。Markdown hard breakになり得る2個以上の末尾スペースは保持します。

backtick/tildeのfenced code block内では、全体の改行コード統一を除き、indent、行内容、末尾空白を変更しません。表のcell、pipe配置、alignment marker、見出し、list marker、inline code、link、HTML、frontmatterも書き換えません。Unicode NFCはcode・URL・識別子を変える可能性があるため、このprofileでは適用しません。

正規化は変換後、品質検査とArtifact Manifest digest計算の前にatomic適用します。`--force`では再変換した出力へ適用します。既存出力をskipした場合はartifact非変更を優先して正規化せず、Manifestの`normalization_profile`はartifact生成履歴ではなく今回要求したglobal settingを表します。したがって、skipされた出力がそのprofileで生成済みであることは保証しません。

### Per-document metadata sidecar

```powershell
uv run knowledge-importer convert .\input\sample.pdf --output .\output\sample.md --metadata-sidecar
uv run knowledge-importer convert .\input --output .\output --recursive --metadata-sidecar --manifest-json .\reports\artifacts.json
```

`--metadata-sidecar`を明示すると、成功またはskipした各Markdownの隣へ`<stem>.metadata.json`を生成します。たとえば`section/a.md`のsidecarは`section/a.metadata.json`です。Markdown本文へfrontmatterを追加せず、Local RAG、vector DB、Obsidianへの登録・同期も行いません。未指定時はsidecar I/Oと追加checksum I/Oを行いません。

sidecar schema version 1はinput/outputの安全な相対POSIX path、status、source PDFと最終Markdownのbyte数・SHA-256、engine、`table_structure`、`normalization_profile`、`artifacts_path_configured`だけを記録します。timestamp、hostname、username、絶対path、command line、cache pathは含めません。正規化指定時は最終正規化済みMarkdownをhashし、Artifact Manifestを同時指定した場合は同じdigest結果を共有します。

batchで既存Markdownをskipした場合もsidecarを生成しますが、Markdown本体は変更しません。このとき`normalization_profile`は今回要求したsettingであり、skipped artifactの生成履歴を保証しません。変換失敗したdocumentにはsidecarを生成せず、他documentの処理を継続します。sidecarはatomic replaceされ、書き込み失敗時は既存sidecarを保護し、変換statusを変えずに最終終了コード`2`を返します。

### Knowledge Package validation

```powershell
# sidecarとMarkdownだけを検証
uv run knowledge-importer validate .\output

# Artifact Manifestとの整合も検証
uv run knowledge-importer validate .\output --manifest .\reports\artifacts.json

# extra Markdownもfailureにし、決定的なJSON reportを生成
uv run knowledge-importer validate .\output --manifest .\reports\artifacts.json `
  --strict --report-json .\reports\validation.json
```

`validate PACKAGE_ROOT`は既存Markdown、`.metadata.json`、任意のArtifact Manifest v1をread-only検証します。PDF変換、Docling、normalization、repair、sidecar生成・削除、Local RAGやvector DBへの登録は実行しません。`--report-json`を指定した場合だけ、独立したKnowledge Package Validation schema version 1をatomic出力します。

Manifestなしでは各sidecarのschemaと対応Markdownのsize・SHA-256を検証し、source PDFの存在は要求しません。Manifest指定時はmissing/stale/orphan sidecar、path・status・digest・engine・settingsの不一致も検出します。Manifestにないsidecarは常にerror、Manifestにないextra Markdownはdefaultでwarning、`--strict`ではerrorです。unknown fieldはforward compatibilityのため許容し、必須field・型・semanticだけを検証します。

終了コードは成功`0`、integrity failure`1`、package root・Manifest・validation report書き込みなどのCLI/I/O error`2`です。issueにはpackage root相対POSIX pathだけを使用し、絶対path、username、traceback、timestamp、hostname、command lineは出力しません。

### Knowledge Package repair plan / dry-run

```powershell
# validation結果から修復候補を表示（packageは変更しない）
uv run knowledge-importer repair-plan .\output --manifest .\reports\artifacts.json

# strict validationを使い、決定的なRepair Plan JSONをatomic出力
uv run knowledge-importer repair-plan .\output --manifest .\reports\artifacts.json `
  --strict --report-json .\reports\repair-plan.json
```

`repair-plan PACKAGE_ROOT`は既存のKnowledge Package validationを1回だけ実行し、issueをRepair Plan schema version 1のactionへ変換するdry-runです。Markdown、Metadata Sidecar、Artifact Manifest、PDF、既存のBatch・CSV・Quality reportを生成・削除・修正せず、`--report-json`指定時だけ独立したRepair Plan JSONを書き込みます。実修復、変換、normalization、Local RAG・vector DB登録は行いません。

`safe=true`は、validなManifestによって意味が一意に確定した`missing-sidecar`の`regenerate-sidecar`と、failed itemに残る`stale-sidecar`の`remove-stale-sidecar`だけに付与します。ただし、このコマンド自体はsafe actionも実行しません。digest・size・path・settingsなど正しい側を一意に決められない不整合は`manual-review`、strict時のextra Markdownはunsafeな`regenerate-manifest`候補です。defaultのwarningは問題数に含めますがaction化しません。

Manifestなしではsidecar単体validationだけを計画へ利用し、安全なmissing/stale判定を推測しません。invalidなManifestに関連する候補も`manual-review`へ落とします。既存の`--report-json`出力はRepair Plan schema version 1自身だけatomic更新でき、Batch JSON、Quality JSON、Artifact Manifest、Metadata Sidecar、Markdown、CSVなど他の既存fileはvalidation実行前に拒否します。計画生成成功はissueの有無にかかわらず終了コード`0`、package root・Manifest・report出力などのCLI/I/O errorは`2`です。出力は相対POSIX pathのみで、同一package・optionから同一action順・同一JSON bytesを生成します。

### Repair Execution Approval / Human Gate

```powershell
uv run knowledge-importer approve-repair .\reports\repair-plan.json `
  --all-safe --report-json .\reports\repair-approval.json
```

`approve-repair PLAN_JSON`はRepair Plan schema version 1を検証し、`safe=true`かつ`manual-review`ではないactionだけをApproval schema version 1へそのままコピーします。Plan validationでは`regenerate-sidecar`を`missing-sidecar / safe=true`、`remove-stale-sidecar`を`stale-sidecar / safe=true`へ固定し、`regenerate-manifest`、`verify-artifact`、`manual-review`の`safe=true`偽装を拒否します。`safe=false`や`manual-review`を承認するescape hatch、個別selector、identity、電子署名はありません。safe actionが0件でも空の有効Approvalを生成します。このHuman Gateは承認記録を作るだけで、repair execution、sidecar生成・削除、Manifest・digest更新、変換、normalizationは実行しません。

Approvalは入力Repair Plan fileの実bytesをstreaming SHA-256でhashし、lowercase 64桁hexとして保持します。parse後の再serialize結果、path、mtimeはhash材料にしないため、Planが1 byteでも変わるとApprovalとのbindingが失われます。同一Plan bytesと`all-safe` scopeからは同一Approval JSON bytesを生成します。

既存の`--report-json`出力は有効なApproval schema version 1自身だけatomic更新でき、Repair Plan、Artifact Manifest、Metadata Sidecar、Markdown、Batch JSON、Quality JSON、CSV、directory、symlink、読取り不能・不完全Approvalは入力Planを読む前に拒否します。Approval以外のpackage fileは変更しません。

### Repair execution preflight

```powershell
uv run knowledge-importer repair-preflight .\output `
  --manifest .\reports\artifacts.json `
  --plan .\reports\repair-plan.json `
  --approval .\reports\repair-approval.json `
  --report-json .\reports\repair-preflight.json
```

`repair-preflight PACKAGE_ROOT`はRepair Plan v1、Approval v1、明示指定したArtifact Manifest v1と現在のpackage状態を照合するread-only commandです。`--plan`と`--approval`が必須です。ManifestはPlan v1にpathを保持しないため暗黙探索せず、`--manifest`未指定時はsafe actionをreadyと推測せず`manifest-invalid`でblockedにします。Plan実bytesのSHA-256 binding、approved actionの完全一致、safe semanticを先に検証し、その後にKnowledge Package Validation v1を再実行します。

承認済み`regenerate-sidecar`はsidecarが未作成で、Manifest itemがsucceeded/skipped、Markdownのsize・SHA-256とsource digestが有効な場合だけ`ready`です。`remove-stale-sidecar`はfailed Manifest itemに対応する通常fileのstale sidecarが現在も存在する場合だけ`ready`で、削除対象のbytes・SHA-256をPreflightへ記録します。状態が変わったactionは`blocked / package-state-changed`となりますが、Markdown生成、sidecar生成・削除、Manifest更新、backup、rollback、repair executionは行いません。

終了コードは全approved actionがready（0件を含む）なら`0`、1件以上blockedなら`1`、CLI input・Plan/Approval binding・report書込みerrorなら`2`です。Preflight JSONは相対POSIX pathと決定的なdigestだけを含み、有効なPreflight schema v1自身のみatomic更新できます。将来のExecution v1は逐次・fail-fastとし、`regenerate-sidecar`はatomicな新規作成、`remove-stale-sidecar`はpackage外または専用領域へのbackupとrollbackを必須にする契約です。

### Safe Repair Execution v1

```powershell
uv run knowledge-importer repair-execute .\output `
  --manifest .\reports\artifacts.json `
  --plan .\reports\repair-plan.json `
  --approval .\reports\repair-approval.json `
  --preflight .\reports\repair-preflight.json `
  --backup-dir D:\safe-backups\knowledge-importer `
  --report-json .\reports\repair-execution.json
```

Operation Intent Receiptをmutation前gateにするopt-in modeでは、Receiptと`attempt_id`を追加指定します。receipted modeではfinal Execution Reportが必須です。

```powershell
uv run knowledge-importer repair-execute .\output `
  --manifest .\reports\artifacts.json `
  --plan .\reports\repair-plan.json `
  --approval .\reports\repair-approval.json `
  --preflight .\reports\repair-preflight.json `
  --intent-receipt .\reports\repair-intent-attempt-001.json `
  --attempt-id repair-attempt-001 `
  --report-json .\reports\repair-execution.json
```

`repair-execute`はPlan、Approval、Preflightの実bytes bindingとManifest v1をmutation前に再検証し、Preflightで`ready`となった`regenerate-sidecar`と`remove-stale-sidecar`だけを実行します。unsafe action、`manual-review`、`regenerate-manifest`、`verify-artifact`は実行できません。Preflight後のsidecar存在、Markdown digest、Manifest status、stale sidecar digest、path・symlink状態もaction直前に再検証します。

`--intent-receipt`未指定時は従来のlegacy modeです。指定時は`--attempt-id`と`--report-json`を必須とし、operatorがactionを直接入力することはできません。最初のstable readで検証したManifest／Plan／Approval／Preflightとexecution scopeからReceiptを構築し、create-only書込み、exact bytes再read、parser再検証、SHA-256確定後に全inputを再read／再bindingします。Receiptとfinal Execution Reportはどちらも新規pathだけを許可するimmutableなattempt記録です。Report既存entryはReceipt生成前に拒否し、Receipt作成・再検証失敗もmutation 0件・終了コード`2`です。Receipt後のpackage precondition変化は既存どおりaction failureの終了コード`1`となり、Receiptを保持します。

実行は決定的な順序で逐次・fail-fastです。後続actionまたはpost-validationが失敗した場合、適用済みactionを逆順rollbackします。新規sidecarは生成bytesが変わっていない場合だけ削除し、stale sidecarは削除前にpackage・Git repository外のbackup rootへExecution専用session directoryを新規作成して退避し、targetが空の場合だけ復元します。backup先の既存file・symlink・junctionは上書き・追跡せず失敗させます。他processが変更・作成したfileも上書き・削除せず`rollback-failed`にします。ManifestとMarkdownは変更しません。

終了コードは全action成功またはaction 0件が`0`、TOCTOU・mutation・post-validation・rollback failureが`1`、CLI/schema/binding/Receipt/report path/report書込みerrorが`2`です。legacy modeのExecution Reportは従来どおり既存の有効なReportをatomic更新できます。receipted modeのReportはcreate-only／no-clobberで、書込み後のactual bytesをstable readし、Receiptおよび入力とのbindingを再検証します。並行writerとの競合やpost-write検証失敗は終了コード`2`ですが、書込みはmutation後の独立処理であるため成功済みpackage変更をrollbackしません。receipted modeのReportだけが`intent_receipt`へschema version、`attempt_id`、Receipt exact bytes SHA-256を記録し、legacy Reportにはこのfieldを追加しません。Receiptはaction failure、rollback、final Report書込み失敗でも自動削除しません。retryでは新しいReceipt path、新しい`attempt_id`、新しいReport pathを使用し、過去attemptのReportを再利用しません。reportには相対POSIX pathとdigestだけを記録し、backup絶対path、timestamp、username、hostname、cwd、command line、tracebackを含めません。

### Backup Session Manifest / read-only Inventory v1

```powershell
uv run knowledge-importer backup-inventory D:\safe-backups\knowledge-importer `
  --package-root .\output `
  --report-json .\reports\backup-inventory.json
```

`remove-stale-sidecar`用の新規backup sessionは`knowledge-importer-repair-v1-*`という専用directoryを使い、直下の`session-manifest.json`へBackup Session Manifest schema version 1を保存します。ManifestはRepair Executionが検証したManifest・Plan・Approval・PreflightのSHA-256 bindingと、package相対source path、session相対backup path、bytes、SHA-256だけを保持します。timestamp、hostname、username、cwd、command line、absolute path、tracebackは含めません。

session作成直後は`open`です。各backupを作成してsourceとのdigest一致を確認した後、targetを削除する前にitemをatomic追記します。追記に失敗した場合はtargetを削除しません。全actionとpost-validationが成功した後だけ`complete`、rollback成功後は`rolled-back`、rollback失敗後は`rollback-failed`へ遷移します。process停止や記録不能により完了状態を確定できないsessionは`open`のまま残り、Inventoryでは`interrupted-open-session`となります。

managed session treeに置けるものは`session-manifest.json`、Manifestで宣言された通常backup file、その親directoryだけです。未宣言file・directory、symlink、junction／reparse point、absolute path、`..`、重複source・backup path、session root外へ解決されるpathは拒否されます。Inventoryはsession manifestを含む全宣言fileを固定順・長さ付きencodingでhashし、決定的な`tree_sha256`を出力します。

`backup-inventory BACKUP_ROOT`は`--package-root`を必須とするread-only検査です。backup root自身と全path componentでsymlink／junction／reparse pointを追跡せず、package rootまたは検出可能なGit repositoryと重なるbackup rootを拒否します。分類は`managed`、`missing-session-manifest`、`invalid-session-manifest`、`interrupted-open-session`、`unexpected-entry`、`binding-unverifiable`、`legacy-unmanaged`です。backup root直下の既知session prefixに一致しないfile、directory、symlink、junction／reparse point、その他のentryも無視せず`unexpected-entry`として報告し、その内容をcleanup候補として解釈しません。v0.1.0で作成済みの従来sessionは変更・移行せず`legacy-unmanaged`として検出します。

cleanup planning候補を示す`planning_eligible`は、構造とdigestが有効な`complete` sessionだけが`true`です。`open`、`rolled-back`、`rollback-failed`、invalid／orphan／legacy／unexpected entryは保守的に`false`です。これは単独では削除許可になりません。v1のbinding検査はsession manifest内に記録されたbinding metadataのschema妥当性を確認するもので、元のManifest・Plan・Approval・Preflight実bytesとの再照合ではありません。`--report-json`はbackup root外だけに出力でき、有効なBackup Inventory v1自身だけatomic更新できます。age／size／generationによる自動retentionや自動削除は実装していません。

終了コードは全sessionが健全な`managed`（`complete`または`rolled-back`）なら`0`、interrupted、rollback failure、invalid、orphan、legacy-unmanagedを1件以上検出した場合は`1`、CLI input・unsafe root・report path・report書込みerrorは`2`です。

### Backup Cleanup Plan / Approval / Execution v1

Backup Inventory v1から削除候補を明示選択する場合は、`backup-cleanup-plan`へsessionを1件ずつ指定します。指定順ではなくNFC正規化・casefoldによるcanonical順で出力し、同一sessionの重複指定を拒否します。`--backup-root`はInventoryが保持しないfilesystem境界を明示し、Inventory、Plan、Approvalの入力・出力がbackup rootや対象session配下へ置かれることを防ぎます。backup root自身が検出可能なGit repository内にある場合も拒否します。

```powershell
uv run knowledge-importer backup-cleanup-plan .\reports\backup-inventory.json `
  --backup-root D:\safe-backups\knowledge-importer `
  --session knowledge-importer-repair-v1-example `
  --report-json .\reports\backup-cleanup-plan.json

uv run knowledge-importer approve-backup-cleanup .\reports\backup-cleanup-plan.json `
  --backup-root D:\safe-backups\knowledge-importer `
  --all-planned `
  --report-json .\reports\backup-cleanup-approval.json

uv run knowledge-importer backup-cleanup-execute D:\safe-backups\knowledge-importer `
  --package-root .\output `
  --inventory .\reports\backup-inventory.json `
  --plan .\reports\backup-cleanup-plan.json `
  --approval .\reports\backup-cleanup-approval.json `
  --report-json .\reports\backup-cleanup-audit.json

uv run knowledge-importer backup-cleanup-execute D:\safe-backups\knowledge-importer `
  --package-root .\output `
  --inventory .\reports\backup-inventory.json `
  --plan .\reports\backup-cleanup-plan.json `
  --approval .\reports\backup-cleanup-approval.json `
  --intent-receipt .\reports\cleanup-intent-attempt-001.json `
  --attempt-id cleanup-attempt-001 `
  --report-json .\reports\backup-cleanup-audit-attempt-001.json
```

Cleanup Plan schema version 1は入力Inventory fileの実bytes SHA-256へbindingし、`policy.mode=explicit-sessions`、`action=delete-backup-session`、`reason_category=explicit-retention-release`を固定します。Inventory上で`managed / complete / planning_eligible=true`のsessionだけが`eligible=true`です。unknown、legacy、open、rollback-failed、invalid、unexpectedその他はblocked actionとしてPlanに残りますが、Approvalへは入りません。blockedを含むPlanや承認action 0件のApprovalも正常なdry-run出力で、終了コードは`0`です。input、schema、binding用metadata、path、既存report保護、書込みerrorは`2`です。

Cleanup Approval schema version 1はPlan fileの実bytes SHA-256へbindingし、`scope.mode=all-planned`でeligible actionだけをPlanのcanonical順のまま保持します。正式verifierはPlanとApprovalの両bytesを同時に検証し、Plan SHA-256に加えてsession、action、reason category、session manifest／tree digest、backup file／byte count、eligible、action順序がPlanのeligible action集合と完全一致することを必須にします。subset承認やPlan外actionは無効です。Plan/ApprovalはUTF-8、2-space indent、trailing newline、timestamp等なしで決定的です。既存fileは同じschemaのvalid reportだけatomic更新できます。

`backup-cleanup-execute`は`--package-root`を必須とし、Inventory作成時の結果だけに依存せず、実行時にもPR1と同じroot safetyを再検証します。package rootとbackup rootの同一・双方向の包含、backup rootの検出可能なGit repository内配置、存在しない／unsafe directory、全path componentのsymlink・junction／reparse pointを削除前に拒否します。package rootはAuditへ記録せず、配下のMarkdown、Manifest、Metadata Sidecar、source PDF、その他fileを変更しません。

その後に正式verifierを必ず通し、Inventory・Plan・Approvalの実bytes bindingと現在のsession manifest、tree、全backup fileのbytes／SHA-256を削除直前に再検証します。実行対象はApprovalに含まれる`delete-backup-session / explicit-retention-release`だけです。宣言済みregular backup fileを深い順、session manifest、空directory、session rootの順でno-follow削除し、backup root自体は残します。`shutil.rmtree`は使いません。symlink、junction／reparse point、未宣言entry、file／directory identity変更、digest変更を検出するとfail-fastし、後続sessionは`not-run`です。既に削除したsessionやfileは復元しません。このcleanupは明示承認後も不可逆であり、rollbackはありません。

Cleanup Audit schema version 1はInventory、Plan、Approvalの各実bytes SHA-256、`planned / deleted / failed / not_run`件数、session相対名、削除前files／bytes／tree SHA-256、削除後存在有無だけを記録します。receipted modeだけはoptional `intent_receipt` fieldでReceiptの`attempt_id`とexact bytes SHA-256を保持します。absolute path、username、hostname、cwd、command line、timestamp、tracebackは含めません。Auditはbackup rootとpackage rootの外にある新規pathだけへ出力できるimmutable execution recordです。既存のvalid Audit、foreign file、directory、symlink、junction／reparse point、読取り不能entryはcleanup開始前に拒否します。同一directoryのtemporary fileをfsyncした後、hard-link commitでcreate-only／no-clobber作成し、並行して作成されたfileを上書きしません。再実行時は別のreport pathを指定します。全削除成功は`0`、precondition／TOCTOU／部分削除を含むaction失敗は`1`、CLI・schema・binding・Audit書込み失敗は`2`です。cleanup成功後にAudit書込みが競合・失敗してもbackup sessionを復元しません。自動retentionと自動cleanupは対象外です。

### Operational Audit Summary v1

既存のRepair Execution Report v1とBackup Cleanup Audit v1を、sourceを変更せず1つの機械可読な監査summaryへ集約できます。

```powershell
uv run knowledge-importer audit `
  --repair-execution .\reports\repair-execution.json `
  --backup-cleanup-audit .\reports\backup-cleanup-audit.json `
  --report-json .\reports\operational-audit.json
```

2種類のsource optionはそれぞれ複数回指定でき、合計1件以上が必要です。sourceはschema version 1としてsemantic validationし、元file実bytesのSHA-256へbindingします。同一bytesのsourceは重複として拒否します。sourceは`source_type / sha256`、operationはcanonical source順と元reportのaction順で決定的に出力し、`source_action_index`を振り直しません。

outcomeは`succeeded / partial / failed / rolled_back / not_run`へ正規化します。Repairの`rollback-failed`は`partial`、Cleanupの`failed`はmutationを推測せず`failed`です。package変更はsourceに完全な前後digest証跡がある場合だけ`changed / unchanged`とし、それ以外は`unknown`です。Cleanupだけからpackage非変更を推測しません。失敗・rollback・not-runは変換やcleanupを再実行せず、operator確認が必要な状態として集計します。

出力はUTF-8、2-space indent、trailing newlineで、timestamp、hostname、username、cwd、command line、absolute path、traceback、Unicode format controlを含めません。`--report-json`は新規pathだけを許可し、同一directoryのtemporary fileをfsync後、hard-linkでcreate-only／no-clobber作成します。既存entryや並行writerを上書きしません。valid sourceの集約はoperation結果にかかわらず終了コード`0`、source／schema／出力境界／書込みerrorは`2`です。Auditはread-onlyで、package、backup、既存reportを変更しません。source report自体が失われた場合は再構築できません。

作成済みSummaryと現在のsource実bytesは、read-onlyの`audit-verify`で再照合できます。

```powershell
uv run knowledge-importer audit-verify .\reports\operational-audit.json `
  --repair-execution .\reports\repair-execution.json `
  --backup-cleanup-audit .\reports\backup-cleanup-audit.json
```

source optionは各々複数回指定でき、Summaryの`sources`と`source_type + exact input bytes SHA-256`の完全一致を要求します。filename、path、mtime、parse後の再serialize結果ではbindingしません。exact match後はsourceを正式parseし、source actionから再構成したcanonical operation projectionとSummaryも比較します。source不足、余分なsource、同一sourceの重複、1 byteの改変は終了コード`1`、Summary／exact-bound sourceのschema・semantic不正やoperation不一致、I/O errorは`2`、全source・operation一致は`0`です。CLI指定順は結果へ影響せず、stdoutは件数と`source_binding=verified`を決定的に表示します。path等の機微情報やtracebackは表示しません。

この確認が保証するのはSummaryと現在のsource bytesのbindingだけです。Repair内のPlan／Approval／Preflight、Cleanup内のInventory／Plan／Approvalは元artifactが入力されないため、`internal_lifecycle_binding=not-provided`と明示します。Summary、source、package、backupを変更せず、新しいreportも作成しません。

### Operation Intent Receipt v1

Operation Intent Receipt v1は、Repair Execution／Backup Cleanupで承認済みの実行scopeをmutation開始前に固定するための共通contractです。両Executionでopt-in receipted modeへ接続済みです。

```json
{
  "report_type": "knowledge-importer-operation-intent",
  "schema_version": 1,
  "attempt_id": "repair-attempt-001",
  "operation_type": "repair-execution",
  "bindings": [
    {
      "artifact_type": "artifact-manifest",
      "schema_version": 1,
      "sha256": "1111111111111111111111111111111111111111111111111111111111111111"
    },
    {
      "artifact_type": "repair-plan",
      "schema_version": 1,
      "sha256": "2222222222222222222222222222222222222222222222222222222222222222"
    },
    {
      "artifact_type": "repair-approval",
      "schema_version": 1,
      "sha256": "3333333333333333333333333333333333333333333333333333333333333333"
    },
    {
      "artifact_type": "repair-preflight",
      "schema_version": 1,
      "sha256": "4444444444444444444444444444444444444444444444444444444444444444"
    }
  ],
  "actions": [
    {
      "action_index": 0,
      "action": "regenerate-sidecar",
      "target": "section/a.metadata.json",
      "reason_category": "missing-sidecar",
      "intent": "approved-for-execution"
    }
  ]
}
```

`attempt_id`は1～64文字のASCII英数字で始まり、その後にASCII英数字、`-`、`_`、`.`だけを許すoperator向け相関labelです。security identityではなく、Receiptのidentityは有効なReceipt fileのexact bytesに対するSHA-256です。同一のsemantic inputと`attempt_id`はbyte-identicalになります。retryでは新しい`attempt_id`と新しい出力pathを使います。

Repair binding順はArtifact Manifest、Repair Plan、Repair Approval、Repair Preflight、Cleanup binding順はBackup Inventory、Backup Cleanup Plan、Backup Cleanup Approvalに固定します。各actionは相対POSIX pathを持ち、targetのcanonical順、連続した0-based `action_index`、operation固有のaction／reason対応、target重複禁止を検証します。

Receiptはexecution intentの証跡に限られ、execution、success、mutation、retry safetyの証明ではありません。timestamp、hostname、username、cwd、command line、absolute path、random UUID、Unicode format controlを含めません。既存entryはvalid Receiptでもforeign fileでも更新せず、directory、symlink、junction／reparse pointも拒否します。同一directoryのtemporary fileをflush／fsyncした後、hard-linkでcreate-only／no-clobber commitし、並行writerを上書きしません。

Repair Executionのfinal ReportはReceipt exact bytes SHA-256と`attempt_id`へbindingし、formal verifierがReceipt、Report、Manifest、Plan、Approval、Preflightのexact-byte bindingとaction scopeを再照合できます。Backup CleanupもInventory、Plan、Approvalのexact bytesと承認済みsession scopeだけからReceiptを作り、削除直前にroot、session manifest、tree、backup file digest、Receipt／Audit pathを再検証します。final Cleanup AuditはReceipt exact bytes SHA-256へbindingし、formal verifierがReceipt、Audit、3入力、action順を再照合します。

Receiptはintent proof、final Report／Auditはoutcome proofであり、Receipt単体はmutation、success、failure、retry safetyを証明しません。receipted modeではInventory／Plan／Approvalのstable read、schema／semantic、exact-byte SHA-256、承認action scopeを各不可逆actionの直前に再検証します。Receipt生成後の入力変更は終了コード`2`で検出し、当該sessionと後続sessionを削除しません。最初のaction前なら削除は0件です。途中action間で検出した場合もReceiptと変更された入力を保持し、既に削除したsessionはrollbackしません。この場合、変更後のlifecycle sourceへ正しくbindingできないためfinal Auditは生成しません。root／session precondition変更は削除前に終了コード`1`です。部分削除、Audit書込み・post-write検証失敗でもReceiptを保持し、削除済みsessionはrollbackしません。receipted modeのretryでは、新しいReceipt path、新しい`attempt_id`、新しいfinal Report／Audit pathをすべて使用します。directory inventory、Operational Audit統合、Receipt cleanupは未実装です。

### Intent Receipt pairing status

`intent-status`は1件のOperation Intent Receiptと、明示指定されたfinal Repair Execution Report／Backup Cleanup Audit候補をread-onlyで照合します。

```powershell
uv run knowledge-importer intent-status `
  --intent-receipt .\reports\repair-intent.json `
  --repair-execution .\reports\repair-execution.json `
  --manifest .\package\manifest.json `
  --plan .\reports\repair-plan.json `
  --approval .\reports\repair-approval.json `
  --preflight .\reports\repair-preflight.json `
  --package-root .\package
```

Cleanup Receiptでは`--inventory`、`--plan`、`--approval`を同時指定します。Repairは4入力、Cleanupは3入力のall-or-noneで、部分指定やoperation typeに合わない入力は終了コード`2`です。lifecycle入力はstable readした実bytesのSHA-256とcanonical action scopeをReceiptへ照合し、parse後payloadの再serialize hash、filename、path、mtimeはidentityに使いません。

Repair Receiptでは`--package-root`を指定すると、final reportがない`orphan`かつlifecycle入力が完全一致する場合だけ、Receipt-bound Preflightと現在のpackageをread-onlyで比較します。一致時は`current_preconditions=verified`、targetの出現・消失、Markdown／sidecar digest変更、unsafe target・root escape時は`mismatch`です。Receipt-bound Repair actionが0件の場合は検証対象がないためpackageを検査せず`current_preconditions=not-applicable`とし、`verified`は1件以上のactionを照合した場合だけ使用します。package root自体が不存在、symlink、junction、reparse pointの場合はStatusを生成せず終了コード`2`です。`--package-root`にはRepair lifecycle 4入力が必須で、Cleanup Receiptには使用できません。paired／conflicting／staleではfilesystemを検査せず`not-applicable`とします。Cleanup current precondition検証は未実装です。

Receipt exact bytesのSHA-256をidentityとし、`attempt_id`だけではpairingしません。Receipt SHA-256、`attempt_id`、`operation_type`、action scope、final reportが保持するlifecycle digestを比較し、結果をstdoutへ決定的なJSONで出力します。Repair final reportにはArtifact Manifest digestがないため、final reportだけではそのdigestを再検証済みとは扱いません。

- `paired`: 唯一のfinal report候補がReceiptと一致。終了コード`0`
- `orphan`: valid Receiptにfinal report候補がなく、lifecycle入力未指定または完全一致。終了コード`1`
- `stale`: orphan Receiptへ完全なlifecycle入力を指定し、exact-byte bindingまたはaction scopeが不一致。終了コード`1`
- `conflicting`: 候補が複数、legacy report、またはReceipt／scope／bindingが不一致。終了コード`1`
- CLI、I/O、Receipt／final report schema、同一final bytesの重複が不正。status JSONを出さず終了コード`2`

`stale`はorphanだけから派生し、pairedやconflictingを再分類しません。pairedで現在のlifecycle入力が不一致なら`classification=paired`、`lifecycle_inputs=mismatch`、`operator_action_required=true`としてpairingとfreshnessを分離します。`orphan / stale / conflicting`はoperator確認が必要です。`current_preconditions=verified`はretry-safe、未実行、元PDF provenanceの証明ではなく、`mismatch`も実行済みや失敗を意味しません。Statusはread-onlyな現在snapshotであり、取得後のTOCTOU安全性、digest一致による真正性、同じbytesへ復元された変更履歴を保証しません。source path、absolute path、username、hostname、timestamp、cwd、command line、tracebackは出力せず、入力artifactやpackage、backupを変更しません。v1は明示入力された単一attemptだけを扱い、元PDF実体、Cleanup current precondition、directory走査、自動retry、自動cleanup、Operational Auditへの統合は扱いません。

### Recursive conversion / include・exclude filters

```powershell
uv run knowledge-importer convert .\input --output .\output --recursive
uv run knowledge-importer convert .\input --output .\output --include "*.pdf"
uv run knowledge-importer convert .\input --output .\output --recursive --include "docs/**/*.pdf" --exclude "archive/**"
```

### Reports and quality checks

```powershell
uv run knowledge-importer convert .\input --output .\output --report-json .\reports\batch-result.json
uv run knowledge-importer convert .\input --output .\output --report-csv .\reports\batch-result.csv
uv run knowledge-importer convert .\input --output .\output --report-json .\reports\batch-result.json --report-csv .\reports\batch-result.csv
uv run knowledge-importer convert .\input --output .\output --manifest-json .\reports\artifacts.json
uv run knowledge-importer convert .\input --output .\output --quality-warnings
uv run knowledge-importer convert .\input\sample.pdf --output .\output\sample.md --quality-report-json .\reports\sample-quality.json
uv run knowledge-importer convert .\input --output .\output --quality-report-json .\reports\quality.json --quality-warnings
```

入力にディレクトリを指定すると、デフォルトでは直下の `.pdf`（大文字・小文字を区別しない）だけを相対パスの安定順で逐次変換します。`--recursive` 指定時だけサブディレクトリを探索し、入力からの相対構造を出力先でも維持します。`--table-structure` は全PDFへ適用され、`--force` は各出力の上書きを許可します。

```text
input/root.pdf             -> output/root.md
input/section/a.pdf        -> output/section/a.md
input/section/deep/b.pdf   -> output/section/deep/b.md
```

再帰探索ではsymlinkおよびjunctionのディレクトリを追跡しません。出力先が入力ディレクトリ配下にある場合、その出力サブツリーを探索対象から除外するため、生成済みMarkdownや出力先に置かれたPDFを再入力しません。

`--include GLOB` と `--exclude GLOB` は複数回指定でき、非再帰・再帰のどちらでも入力ルートからのPOSIX形式相対パス（`/` 区切り）に対して大小文字を区別せず評価します。include未指定時は全PDF、指定時はどれかのincludeに一致したPDFだけを候補とし、その後どれかのexcludeに一致したPDFを必ず除外します。`*` はディレクトリ区切りを跨がず、`**` は0階層以上を表します。

```powershell
uv run knowledge-importer convert .\input --output .\output `
  --include "manual/*.PDF" `
  --include "docs/**/*.pdf" `
  --exclude "archive/**" `
  --exclude "**/tmp/*" `
  --recursive
```

一括変換は1件が失敗しても残りを処理し、最後に `成功=<件数> 失敗=<件数> スキップ=<件数>` を表示します。既存出力のスキップだけであれば終了コード `0`、1件以上の変換失敗またはフィルタ未指定で対象PDFが0件の場合は非0です。include/exclude指定後の対象が0件の場合は正常終了し、`成功=0 失敗=0 スキップ=0` を表示します。同じ出力パスへ正規化されるPDFが複数ある場合は、意図しない上書きを避けるため変換開始前に停止します。同じ入力と出力先で再実行した場合、未指定時は生成済みファイルを保持し、`--force` 指定時は安定した相対パスで再生成します。並列処理、再帰深度指定、ディレクトリ監視には対応していません。

失敗時はローカル絶対パスやtracebackを表示せず、対象ファイル名、分類、短い理由をstderrへ出します。分類は「入力・パス関連」「出力競合・書き込み関連」「converter生成・変換処理関連」「想定外エラー」の4種類です。失敗がある場合はsummaryにも分類別件数を追加します。

```text
失敗: ファイル=broken.pdf 分類=converter生成・変換処理関連 理由=RuntimeError: synthetic parse failure
一括変換完了: 成功=2 失敗=1 スキップ=1 分類別: 入力・パス関連=0 出力競合・書き込み関連=0 converter生成・変換処理関連=1 想定外エラー=0
```

### Exit codes

終了コードは、変換対象がすべて成功またはスキップなら `0`、1件以上の変換失敗・converter生成失敗・想定外エラーがあれば `1`、入力ディレクトリが空、出力先形式が不正、同一出力名が衝突するなど一括変換開始前の検証エラーは `2` です。

### JSONレポート

ディレクトリ一括変換で `--report-json PATH` を指定すると、変換終了後にschema version `1`のJSONレポートをUTF-8で出力します。単一PDF変換では使用できません。レポートには入力・出力ルートからのPOSIX形式相対パスだけを記録し、絶対パス、ユーザー名、traceback、時刻などの環境依存情報は含めません。

```json
{
  "schema_version": 1,
  "summary": {
    "total": 2,
    "succeeded": 1,
    "failed": 0,
    "skipped": 1
  },
  "exit_code": 0,
  "items": [
    {
      "input": "section/a.pdf",
      "output": "section/a.md",
      "status": "succeeded",
      "error_category": null,
      "message": null
    },
    {
      "input": "section/b.PDF",
      "output": "section/b.md",
      "status": "skipped",
      "error_category": null,
      "message": "既存の出力を保持しました。"
    }
  ]
}
```

`status`は `succeeded`、`failed`、`skipped` のいずれかです。失敗時の `error_category` と `message` には、通常のstderrと同じ安全化済み分類・理由を記録します。include/excludeで対象外になったPDFは含まず、フィルタ適用後の対象が0件でも空の `items` を持つレポートを生成します。

JSON内の `exit_code` は実際のCLI終了コードと一致します。レポートは一時ファイルへの書き込み後に原子的に置換するため、既存レポートは安全に更新されます。親ディレクトリは必要に応じて作成します。レポート自体の書き込みに失敗した場合は、変換結果にかかわらず終了コード `2` とし、絶対パスを含まない固定メッセージをstderrへ表示します。

### CSVレポート

ディレクトリ一括変換で `--report-csv PATH` を指定すると、JSONレポートと同じ決定的な処理結果をCSVへ出力します。Excelなどで文字化けしにくいUTF-8 BOM付きで、列順は `input`、`output`、`status`、`error_category`、`message` です。ヘッダーは常に出力し、値がない項目は空文字にします。

```csv
input,output,status,error_category,message
section/a.pdf,section/a.md,succeeded,,
section/b.PDF,section/b.md,skipped,,既存の出力を保持しました。
```

`input`と`output`には各ルートからのPOSIX形式相対パスだけを記録し、絶対パス、ユーザー名、tracebackなどは含めません。include/exclude対象外は記録せず、フィルタ適用後の対象が0件または入力ディレクトリ内にPDFがない場合もヘッダーだけのCSVを生成します。単一PDF変換では利用できません。

`--report-json`と`--report-csv`は同時指定でき、変換を一度だけ実行して同じ内部結果を両形式へ出力します。ただし同じPATHを両方へ指定することはできません。各レポートは同じ親ディレクトリの一時ファイルから独立して原子的に置換されるため、一方の書き込み失敗で他方の正常なレポートは削除されません。JSONまたはCSVのどちらか一方でも書き込みに失敗した場合、最終終了コードは `2` です。

### Artifact Manifest JSON

`--manifest-json PATH`は、今回選択されたPDFとMarkdownを下流ツールへ安全に渡すため、独立したArtifact Manifest schema version 1を生成します。単一PDFと一括変換の双方で利用でき、既存Batch JSON、CSV、Quality JSONとの同時指定も可能です。Local RAGなどへの登録・同期自体は行いません。

Manifestはinput/outputのbyte数と、file bytesをそのままSHA-256で計算したlowercase digestを記録します。singleではfilename、batchでは各rootからの相対POSIX pathだけを使用します。timestamp、duration、hostname、username、cwd、command line、絶対path、model cache path、`--artifacts-path`の値は含めません。`--artifacts-path`は指定有無だけをbooleanで記録します。

`settings.normalization_profile`は未指定時に`null`、`--normalize-markdown conservative`指定時に`"conservative"`です。正規化指定時のoutput digestは、品質検査と同じ最終正規化済みMarkdown bytesを表します。

per-document sidecarとManifestの共通checksum・path契約は [Knowledge Artifact Output Contract](docs/output-contract.md) を参照してください。

`succeeded`と`skipped`ではinput/output双方のdigestが必須です。`failed`では読み取れるinputだけを記録し、output digestは`null`です。Manifestを指定しない通常実行ではchecksum I/Oを追加しません。Manifest生成または書き込みに失敗した場合、変換statusを変更せず最終終了コードを`2`とし、既存Manifestはatomic writeで保護します。

同一のinput bytes、output bytes、status、設定からはbyte-identicalなJSONを生成します。既存reportや生成Markdownと同じ出力pathは変換開始前に拒否します。field定義と互換性方針は [Knowledge Artifact Manifest v1 Output Contract](docs/output-contract.md) を参照してください。

## OCR設定

OCR済みPDFを前提とし、Doclingの `do_ocr=False`、`force_backend_text=True` を明示しています。通常モードは `do_table_structure=False`、`--table-structure` 指定時のみ `do_table_structure=True` です。画像だけのスキャンPDFに対する再OCRは行いません。また、`enable_remote_services=False` により外部推論サービスを無効化しています。

## 品質評価

架空PDFによる変換品質評価の生成方法、指標、既知制約は [PDF変換品質評価](docs/pdf-quality-evaluation.md) を参照してください。

Doclingの表構造推論あり・なしの比較結果は [Docling表構造モード比較](docs/converter-comparison.md) を参照してください。

### Markdown品質の回帰評価

通常の単一PDF・一括変換では、`--quality-warnings`を明示した場合だけ、今回生成または`--force`で再生成したMarkdownに基礎品質検査を実行します。空出力、可視文字40文字未満の極端に短い出力、Windows/POSIX絶対パス、tracebackらしい文字列、Unicode制御文字を検出すると、安全な分類と理由をstderrへ表示します。

```text
警告: ファイル=section/a.pdf 分類=short-output 理由=Markdown出力が極端に短い
```

40文字は文書固有の期待値を持たないruntime検査で大幅な欠落を拾うための保守的なwarning閾値です。短い正常文書を誤検知する可能性がある補助機能であり、warningがあっても変換成功扱い、summary、終了コードは変わりません。skipped、変換失敗、include/exclude対象外のMarkdownは検査しません。warning情報はBatchResult、JSON schema version 1、CSVへ追加しません。

このruntime検査は、見出し階層、表構造、主要語句、ページ境界、意味的正確性、視覚的忠実度など、文書固有の正解を必要とする品質を判定しません。

#### 独立Markdown品質JSONレポート

`--quality-report-json PATH`を指定すると、単一PDF・一括変換のどちらでもruntime品質検査を有効化し、今回新規生成または`--force`で再生成して検査した全Markdownを独立したJSONへ記録します。レポートだけを指定した場合はstderrへ品質warningを表示しません。`--quality-warnings`を併用すると、同じ1回の読み取り・評価結果をstderrとレポートで共有します。

```json
{
  "report_type": "markdown-quality",
  "schema_version": 1,
  "summary": {
    "checked": 2,
    "passed": 1,
    "warned": 1
  },
  "items": [
    {
      "input": "section/a.pdf",
      "output": "section/a.md",
      "status": "passed",
      "warnings": []
    },
    {
      "input": "section/b.pdf",
      "output": "section/b.md",
      "status": "warned",
      "warnings": [
        {
          "category": "short-output",
          "message": "Markdown出力が極端に短い"
        }
      ]
    }
  ]
}
```

`passed`はwarningなし、`warned`は1件以上のwarningありを示します。UTF-8読み取りに失敗した場合は、変換成功扱いを維持したまま、例外本文を含まない`quality-read-error`として記録します。batchの入出力は各ルートからの相対POSIXパス、単一PDFではファイル名だけを記録し、絶対パス、ユーザー名、traceback本文、時刻などは含めません。

skipped、変換失敗、include/exclude対象外はitemsへ含めません。検査対象が0件でもsummaryがすべて0の空レポートを生成します。品質warningは変換成否、通常summary、BatchResult、終了コードへ影響せず、既存Batch Report JSON schema version 1およびCSVへも追加されません。`--report-json`、`--report-csv`との同時指定は可能ですが、品質レポートには既存レポートおよび生成Markdownと異なる出力先が必要です。

品質レポートは同一ディレクトリの一時ファイルから原子的に置換します。書き込みに失敗した場合は既存ファイルを保持し、安全な固定メッセージをstderrへ表示して終了コード`2`とします。このレポートも見出し・表・意味・視覚的忠実度など、文書固有の正解は評価しません。

### 合成fixtureによる詳細回帰評価

`tests/test_markdown_quality.py` は、実資料やDocling実推論を使わず、合成PDFと合成Markdownだけで変換結果の主要構造を評価します。対象は、見出し階層、本文の主要語句、箇条書き、Markdown表、ページ境界前後の本文、空または極端に短い出力、絶対パス・traceback・制御文字の混入です。

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_markdown_quality.py
```

評価は空白・大文字小文字・一部のMarkdown装飾差を正規化し、全文完全一致ではなく情報と構造の欠落を判定します。最小文字数は合成fixtureの400文字超に対して120文字とし、軽微な整形差を許容しながら大幅な欠落を検出します。このテストは決定的な回帰検出用であり、実資料の視覚的な忠実度、複雑な段組み・数式、意味的な正確性、あらゆるPDFに対する変換品質を保証するものではありません。

GitHub Actionsでは `Markdown quality regression` ステップが品質評価8件を明示的に実行し、通常の `Pytest` ステップが残りのテストを実行します。品質評価にDocling実推論、モデル取得、外部通信は不要です。ローカルでは従来どおり `uv run pytest` だけで両方を実行できます。

## Development / Test

```powershell
uv sync --locked --dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build
```

`tests/test_release_readiness.py`は合成fixtureとfake converterだけを使い、wheel/sdist、clean venvへのwheel本体install、CLI help、JSON・CSV・品質JSONの統合出力を検証します。`tests/test_lifecycle_smoke.py`は同じく外部通信を行わず、`convert → validate → repair-plan → approve-repair → repair-preflight → repair-execute`を一連で検証します。missing/stale sidecar、unsafe issue、TOCTOU、rollback、Execution Report書込み失敗、決定的artifactを対象にします。実Docling推論やモデルdownloadは実行せず、実PDFの変換品質や完全オフライン変換を保証するテストではありません。

## Offline / Known limitations

- PDFの複雑な段組み、表、数式ではMarkdownの再現性に差が出ます。初期版では表構造推論を無効化しています。
- `do_ocr=False` のため、OCRされていない画像PDFやテキスト層が欠落・破損したPDFからは本文を抽出できません。
- wheel本体は依存を含まないため、完全offline installにはDoclingとtransitive dependencyのwheelを事前にcacheする必要があります。実変換には、さらにDoclingが要求するmodel artifactの事前cacheが必要です。どちらかが不足する環境ではinstallまたは変換を開始できません。
- Docling 2.113.0の既定model解決はlayout modelの`revision="main"`を参照するため、full revisionのsnapshotだけでは完全offline初期化に失敗します。`--artifacts-path`でfixed snapshot由来の正式なlocal artifacts構造を指定した2026-08-09のmanual smokeでは、通常モード4件、TableFormerモード2件、offline再実行に成功しました。詳細は [Real Docling Smoke Validation](docs/REAL_DOCLING_SMOKE_VALIDATION.md) を参照してください。
- 実資料、実案件名、実会社名、実個人名をリポジトリへ追加しないでください。
- `input/`、`output/`、`logs/` の実ファイルはGit管理対象外です。

## ライセンス

Knowledge Importer本体は [MIT License](LICENSE) です。third-party dependenciesはそれぞれのlicenseに従い、Knowledge ImporterのMIT Licenseはそれらの条件を包含・置換しません。

Doclingのmodel artifactはこのrepositoryに含まれず、同梱・再配布もしません。modelを取得・利用する方は、対象modelごとのlicenseと利用条件を確認してください。公開状態の継続に関する技術的・人手確認は [v0.1.0 Public Release Gate](RELEASE_CHECKLIST.md) と [Third-party License Metadata Review](THIRD_PARTY_LICENSES_REVIEW.md) を参照してください。

現在の公開範囲はGitHub上のproject sourceだけです。GitHub Release、wheel / sdist、PyPI、依存packageを含むbinary、model artifactの配布は、このソース公開判断には含まれず、それぞれの原文license・NOTICE・再配布条件を確認する別のHuman Gateが必要です。

v0.1.0の配布範囲別判定（Git tag、source-only GitHub Release、wheel / sdist、PyPI、binary / installer、model再配布）は [v0.1.0 Public Release Gate](RELEASE_CHECKLIST.md) に整理しています。技術的なbuild成功は公開許可を意味せず、tag・Release・uploadはいずれも公開直前のHuman Gateを必要とします。
