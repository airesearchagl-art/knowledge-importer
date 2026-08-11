# Knowledge Artifact Manifest v1 Output Contract

`--manifest-json PATH`が生成する、下流ツール向けの独立したartifact契約です。Local RAG、vector DB、Obsidianなどへの登録・同期処理は含みません。

## Schema

```json
{
  "report_type": "knowledge-artifact-manifest",
  "schema_version": 1,
  "engine": {
    "name": "knowledge-importer",
    "version": "0.1.0"
  },
  "settings": {
    "recursive": false,
    "include": [],
    "exclude": [],
    "force": false,
    "table_structure": false,
    "artifacts_path_configured": false,
    "normalization_profile": null
  },
  "summary": {
    "items": 1,
    "succeeded": 1,
    "skipped": 0,
    "failed": 0
  },
  "items": [
    {
      "input": {
        "path": "section/a.pdf",
        "bytes": 123,
        "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
      },
      "output": {
        "path": "section/a.md",
        "bytes": 456,
        "sha256": "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
      },
      "status": "succeeded",
      "error_category": null,
      "message": null
    }
  ]
}
```

## Root fields

| Field | Contract |
|---|---|
| `report_type` | 常に`knowledge-artifact-manifest` |
| `schema_version` | この契約では整数`1` |
| `engine.name` | 常に`knowledge-importer` |
| `engine.version` | packageのruntime version |
| `settings` | 今回の実行で有効な、machine-independent設定 |
| `summary` | item総数とstatus別件数 |
| `items` | batchの決定的な処理順。singleは1件またはvalidation error時は未生成 |

## Settings

- `recursive`: recursive探索の有無。singleでは`false`
- `include` / `exclude`: CLI指定順を保持したglob list。filter semanticsを変更するsortや重複除去はしない
- `force`: 既存Markdownの再生成を許可したか
- `table_structure`: Docling TableFormerを有効化したか
- `artifacts_path_configured`: local artifacts pathを指定したか。path値そのものは記録しない
- `normalization_profile`: 今回要求したMarkdown正規化profile。未指定時は`null`、conservative指定時は`"conservative"`

同一CLI設定を同一順序で指定した場合に同じJSONを生成します。globの順序が異なる実行は、現在のfilter結果が同じでも別の設定表現として扱います。

`normalization_profile`はglobal requested settingであり、各artifactの生成履歴を証明するfieldではありません。既存Markdownが`--force`なしでskipされた場合はartifact非変更を優先して正規化を適用しないため、設定値が`"conservative"`でも、そのskipped outputが同profileで生成済みとは限りません。生成履歴をper-itemで証明する契約はschema v1の対象外です。

## Item status

| Status | Input digest | Output digest | Error fields |
|---|---|---|---|
| `succeeded` | 必須 | 必須 | `null` |
| `skipped` | 必須 | 既存Markdownから必須 | `error_category=null`、固定skip message |
| `failed` | 読み取り可能なら記録。失敗時は`null` | 常に`null` | 既存の安全化済み分類・理由 |

failed変換が不完全なMarkdownを残した場合も、そのoutputをartifactとして採用しません。Manifest checksumの生成失敗はconversion itemのstatusを書き換えませんが、report生成失敗としてCLI終了コード`2`になります。

## Checksum

- algorithm: SHA-256
- representation: 64文字のlowercase hexadecimal
- source: file bytesを変換せずそのままhash
- memory: 1 MiB chunkによるstreaming read
- `bytes`: hash対象fileのbyte数

Manifest未指定時はchecksumを計算せず、既存のperformanceとI/O挙動を維持します。

正規化を指定したsucceeded itemでは、Docling出力を書き込んだ後に正規化をatomic適用し、その最終Markdown bytesをhashします。Quality WarningおよびQuality JSONも同じ最終bytesを評価します。

## Path semantics

- single input: PDF filenameだけ
- single output: Markdown filenameだけ
- batch input: input rootからの相対POSIX path
- batch output: output rootからの相対POSIX path
- include/exclude対象外、symlink、非PDF: itemへ含めない

絶対path、drive letter、username、cwd、temporary path、model artifact pathは記録しません。

## Deterministic guarantees

次が同一ならManifestのUTF-8 bytesは同一です。

- input bytes
- output bytes
- item statusと安全化済みerror fields
- effective settings
- item order
- Knowledge Importer version

JSONは`ensure_ascii=False`、2-space indent、固定key順、LF、末尾改行付きです。timestamp、duration、random ID、host、username、command line、mtimeは含めません。targetが0件でも空summaryと`items=[]`を出力します。

## Atomic write and exit codes

既存の共通atomic JSON writerを使用し、同じ親directoryのtemporary fileを書き終えてから`Path.replace()`します。失敗時は可能な限りtemporary fileを削除し、既存Manifestを保持します。

- conversion成功またはskipのみ: `0`
- conversion itemが1件以上failed: `1`
- input/output validationまたはManifestを含むreport生成失敗: `2`

Manifest失敗時も他のBatch JSON、CSV、Quality JSONは可能な限り生成します。Manifest pathは既存reportおよび生成予定Markdownと異なる必要があります。

## Compatibility policy

Artifact ManifestはBatch JSON schema v1、CSV、Quality JSON schema v1、BatchResultから独立しています。既存schemaと列は変更しません。

- field削除、型変更、statusやfieldの意味変更などのbreaking changeでは`schema_version`を上げる
- schema v1の既存fieldへ別の意味を後付けしない
- 同名profileの意味はschema v1内で変更しない。意味を変更する場合は新しいprofile名を追加するか、breaking changeとしてschema versioningを行う
- optional fieldを追加する場合も、v1 consumerが未知fieldを無視できることと決定性を確認する
- downstream consumerは`report_type`と`schema_version`を検証してから処理する

## Per-document Metadata Sidecar v1

`--metadata-sidecar`が、成功またはskipしたMarkdownの隣へ生成するdocument単位の契約です。`section/a.md`に対して`section/a.metadata.json`を生成します。Markdown本文へfrontmatterを追加せず、Local RAGなどへの登録処理も含みません。

```json
{
  "report_type": "knowledge-document-metadata",
  "schema_version": 1,
  "engine": {
    "name": "knowledge-importer",
    "version": "0.1.0"
  },
  "document": {
    "input_path": "section/a.pdf",
    "output_path": "section/a.md",
    "status": "succeeded"
  },
  "artifact": {
    "bytes": 1234,
    "sha256": "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
  },
  "source": {
    "bytes": 5678,
    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  },
  "settings": {
    "table_structure": false,
    "normalization_profile": null,
    "artifacts_path_configured": false
  }
}
```

### Field and status semantics

- `document.input_path` / `output_path`: singleではfilename、batchでは各rootからの相対POSIX path
- `status`: `succeeded`または`skipped`。failed documentにはsidecarを生成しない
- `artifact`: 最終Markdown bytesのbyte数とSHA-256
- `source`: input PDF bytesのbyte数とSHA-256
- `settings`: artifactへ影響する今回のrequested settingsの最小集合
- `normalization_profile`: 未指定時`null`、conservative指定時`"conservative"`

skip時は既存Markdownを変更せず、その既存bytesを`artifact`としてhashします。`settings.normalization_profile`は今回要求したsettingであり、skipped outputがそのprofileで生成された履歴保証ではありません。failed itemやpartial/stale outputを新しいsidecar付きartifactとして採用しません。

### Manifestとの共有契約

同じrunでArtifact Manifestとsidecarを指定した場合、input/output path、status、source/artifactのbyte数・SHA-256、engine name/version、`table_structure`、`normalization_profile`、`artifacts_path_configured`が一致します。digestは1 documentにつき一度構築した内部artifact itemから共有します。正規化を完了して最終確定したMarkdown bytesを双方が参照し、Quality warning自体はsidecarへ含めません。

### Determinism, atomic write, and compatibility

同一input bytes、最終Markdown bytes、status、settings、engine versionからはbyte-identicalなUTF-8 JSONを生成します。timestamp、duration、random ID、hostname、username、cwd、command line、absolute path、cache path、temporary pathは含めません。各sidecarは共通atomic JSON writerで同一directoryのtemporary fileから`Path.replace()`し、失敗時は既存sidecarを保護します。

sidecar書き込み失敗はconversion itemのstatusを変更しませんが、CLI最終終了コードを`2`にします。他documentおよびBatch JSON、CSV、Quality JSON、Manifestは可能な限り処理を継続します。sidecar pathはinput PDF、Markdown、各report、他sidecarとcase-insensitive・Unicode NFC比較で競合しないことをconverter開始前に検証します。

Metadata Sidecar schema v1はBatch JSON schema v1、CSV、Quality JSON schema v1、Artifact Manifest schema v1、BatchResultから独立し、既存契約を変更しません。fieldの削除・型や意味の変更にはsidecar schema versionの更新が必要です。

## Knowledge Package Validation v1

`knowledge-importer validate PACKAGE_ROOT`は、既存packageを変更せずMetadata Sidecar v1とMarkdownを検証します。`--manifest PATH`指定時はArtifact Manifest v1との整合も検証します。Batch JSONとQuality JSONはvalidation v1の対象外です。

```json
{
  "report_type": "knowledge-package-validation",
  "schema_version": 1,
  "summary": {
    "checked": 4,
    "passed": 3,
    "failed": 1,
    "warnings": 0
  },
  "issues": [
    {
      "path": "section/a.metadata.json",
      "severity": "error",
      "category": "artifact-digest-mismatch",
      "message": "Markdown digestがsidecarと一致しません"
    }
  ]
}
```

### Issue contract

| Category | Meaning |
|---|---|
| `invalid-json` | sidecarまたはManifestをJSONとして読めない |
| `invalid-schema` | 必須field、型、値、digest形式が不正 |
| `unsupported-schema` | `schema_version`が1ではない |
| `missing-artifact` | sidecar対応Markdownが存在しない |
| `missing-sidecar` | succeeded/skipped Manifest itemのsidecarがない |
| `stale-sidecar` | failed Manifest itemにsidecarが存在する |
| `orphan-sidecar` | Manifestに対応itemがないsidecar |
| `artifact-size-mismatch` | Markdown sizeがsidecarと一致しない |
| `artifact-digest-mismatch` | Markdown SHA-256がsidecarと一致しない |
| `manifest-sidecar-mismatch` | status、engine、source/output digestが一致しない |
| `path-mismatch` | sidecar filenameまたはManifestとのpathが一致しない |
| `settings-mismatch` | Manifestとsidecarの設定が一致しない |
| `outside-package-root` | pathまたはsymlinkがpackage root外へ解決される |
| `extra-artifact` | Manifestに含まれないMarkdown |

`orphan-sidecar`は常にerrorです。`extra-artifact`だけはdefaultでwarning、`--strict`でerrorになります。Manifest未指定時はorphan/extra判定を行わず、sidecar単体契約としてMarkdownの存在・size・digestを確認します。source PDFはpackage外にある場合を許容し、Manifestなしではsource digestのschema妥当性だけを確認します。

### Exit codes and deterministic output

- `0`: errorなし。warningのみを含む場合も成功
- `1`: integrity validation errorが1件以上
- `2`: CLI input、Manifest指定、validation report書き込みなどのI/O error

issueは相対path、category、severity、messageの固定順でsortingします。同じpackageとoptionからはstdout summaryとreport JSON bytesが同一です。validation reportはUTF-8、2-space indent、末尾改行付きで共通atomic JSON writerから出力します。absolute path、timestamp、hostname、username、cwd、command lineを含めません。

unknown fieldはforward compatibilityのため許容します。required fieldの削除、型・semantic・severity/category contractのbreaking changeにはvalidation reportのschema version更新が必要です。validationはMarkdown、sidecar、Manifest、PDFを修復・再生成・削除しません。

## Knowledge Package Repair Plan v1

`knowledge-importer repair-plan PACKAGE_ROOT`はKnowledge Package Validation v1の結果を再利用し、実ファイルを変更せず修復候補だけを生成します。`--manifest PATH`と`--strict`はvalidationと同じsemanticsを使い、`--report-json PATH`指定時だけ次の独立reportをatomic出力します。

```json
{
  "report_type": "knowledge-package-repair-plan",
  "schema_version": 1,
  "summary": {
    "issues": 2,
    "actions": 2,
    "manual_review": 1
  },
  "actions": [
    {
      "path": "section/a.metadata.json",
      "action": "regenerate-sidecar",
      "reason_category": "missing-sidecar",
      "safe": true
    }
  ]
}
```

### Action contract

| Action | Meaning |
|---|---|
| `regenerate-sidecar` | validなManifest itemに対応するmissing sidecarの再生成候補 |
| `remove-stale-sidecar` | validなManifestのfailed itemに残るsidecarの削除候補 |
| `regenerate-manifest` | strict時に検出したextra Markdownを反映するManifest再生成候補 |
| `verify-artifact` | 将来のartifact確認action用に予約。v1 plannerは現在生成しない |
| `manual-review` | 正しいartifact・sidecar・Manifestを自動決定できないため人の判断が必要 |

`safe=true`は操作の意味が一意であることだけを示し、自動実行の許可や実行済みを意味しません。v1ではvalidなManifestに基づく`missing-sidecar`と`stale-sidecar`だけが対象です。Manifestがinvalidまたは未指定ならsafe actionを推測せず、ambiguousなerrorは`manual-review`とします。artifact digest・size、Manifest/sidecar、path、settings、schema、missing artifact、outside-rootなどの不整合ではMarkdown、sidecar、Manifestのいずれも正とは決め打ちしません。

default validationのwarningは`summary.issues`に含めますがactionを生成しません。`--strict`でerrorとなる`extra-artifact`だけ、`safe=false`の`regenerate-manifest`候補にします。Manifestなしではsidecar単体validation issueだけを変換し、missing/stale/orphan判定や自動修復方向を推測しません。

### Determinism, write boundary, and exit codes

actionはNFC・case-insensitiveなpath、action、reason categoryの順で固定sortingします。JSONはUTF-8、2-space indent、末尾改行付きで、timestamp、hostname、username、絶対path、cwd、command line、cache path、random IDを含めません。同一packageとvalidation modeから同一JSON bytesを生成します。

Markdown、Metadata Sidecar、Artifact Manifest、PDF、Batch JSON、CSV、Quality JSONは一切変更しません。`--report-json`だけが共通atomic writerによる書込み境界です。既存fileへの出力は`report_type=knowledge-package-repair-plan`、`schema_version=1`および必須container型を満たすRepair Plan自身だけ許可し、その他はvalidation・planning前に拒否します。終了コードはplan生成成功（issueの有無を問わない）が`0`、CLI input・Manifest指定・report書込みerrorが`2`です。repair execution、sidecar生成・削除、Manifest・digest更新、変換、normalization、Local RAG登録は別フェーズです。

## Repair Execution Approval v1

`knowledge-importer approve-repair PLAN_JSON --all-safe --report-json PATH`は、有効なRepair Plan v1へHuman Gate承認をbindingするread-only commandです。Approval生成以外のfile変更やrepair executionは行いません。

```json
{
  "report_type": "knowledge-package-repair-approval",
  "schema_version": 1,
  "plan": {
    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "schema_version": 1
  },
  "scope": {
    "mode": "all-safe"
  },
  "approved_actions": [
    {
      "path": "section/a.metadata.json",
      "action": "regenerate-sidecar",
      "reason_category": "missing-sidecar",
      "safe": true
    }
  ]
}
```

### Plan binding and scope

`plan.sha256`はRepair Plan fileの実bytesだけをstreaming SHA-256へ入力したlowercase 64桁hexです。parse後payloadの再serialize、path、mtime、timestamp、host情報はhash材料に含めません。`plan.schema_version`はbinding対象がRepair Plan v1であることを表します。

v1のscopeは`all-safe`だけです。Repair Plan parserは`regenerate-sidecar = missing-sidecar / safe=true`、`remove-stale-sidecar = stale-sidecar / safe=true`、`regenerate-manifest = extra-artifact / safe=false`を必須とし、`verify-artifact`と`manual-review`も常に`safe=false`とします。このsemantic invariantに反するPlanはschema v1として拒否します。承認時は検証済みPlanの`safe=true` actionから`manual-review`を除外し、path、action、reason category、safeを変更・再解釈せず元の順序でコピーします。`safe=false`や`manual-review`の承認、個別action selector、approver identity、username、email、hostname、cwd、command line、timestamp、random ID、電子署名は対象外です。safe actionが0件でも`approved_actions=[]`の有効Approvalになります。

### Determinism and output protection

同一Repair Plan bytesとscopeからは同一action、順序、JSON bytesを生成します。Approval JSONはUTF-8、2-space indent、末尾改行付きで共通atomic writerを使用します。既存fileは有効なApproval v1自身だけ更新でき、Repair Plan、Artifact Manifest、Metadata Sidecar、Markdown、Batch JSON、Quality JSON、CSV、directory、symlink、読取り不能・invalid/incomplete ApprovalはPlan validation前に拒否します。成功は`0`、input・Plan schema・output protection・report書込みerrorは`2`です。

### Future Repair Execution boundary

将来Repair Executionを実装する場合も、Approvalの存在だけでは実行しません。実行直前に必ず次を満たす必要があります。

1. Repair Planを再validateする
2. Approvalの`plan.sha256`と実Plan bytesを再照合する
3. Approvalに含まれるactionだけを対象にする
4. 各actionの`safe=true`を再確認する
5. 実行対象artifactの事前digestを再検証する
6. 実行前後digestを記録する
7. partial failure policyを定義する
8. rollback / backup方針を定義する

Approval v1は上記execution、artifact変更、rollbackを実装しません。
