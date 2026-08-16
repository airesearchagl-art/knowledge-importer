# Intent Status Operator Guide

`knowledge-importer intent-status`の結果をHuman operatorが読むためのガイドです。このコマンドと本ガイドはread-onlyであり、自動action、retry判断、cleanup eligibility、execution outcomeの再分類を生成しません。

## Safety contract

- Automatic retry: prohibited
- Automatic cleanup: prohibited
- `exit code 0 != safe to retry`
- `exit code 0 != no operator action required`
- `classification=paired != execution success`
- `verified != retry-safe`
- `verified != unexecuted`
- `mismatch != executed`
- `mismatch != failed`
- `stale != execution proof`
- `Receipt != execution proof`
- Statusはread-onlyな現在snapshotです。取得後のTOCTOU安全性はありません。

これらは、Statusが観測したbindingや現在状態を、実行済み・未実行・成功・失敗・再実行可能性へ読み替えないための固定規則です。

## 4つの独立した観測軸

| 軸 | 情報源 | 表すもの | 表さないもの |
| --- | --- | --- | --- |
| Final report outcome | Repair Execution Report / Backup Cleanup Audit | reportに記録された`success / partial / failed / rolled_back / not_run`等のoperation outcome | Receiptの現在freshness、現在filesystemの状態 |
| Receipt pairing classification | Intent Status | `paired / orphan / stale / conflicting`というReceiptとfinal report候補の関係 | operationの成功・失敗、retry safety |
| Lifecycle freshness | Intent Status `bindings.lifecycle_inputs` | `not-provided / verified / mismatch`というReceiptと現在提供されたlifecycle artifact実bytes・action scopeの関係 | filesystemの現在状態、実行有無 |
| Current filesystem snapshot | Intent Status `bindings.current_preconditions` | `not-provided / verified / mismatch / not-applicable`という明示rootのread-only snapshot評価 | Receipt作成時の状態、実行有無、status取得後の状態 |

4軸は独立して確認します。とくにfinal report outcomeはIntent Statusのclassificationから推測せず、対応する正式なExecution ReportまたはCleanup Auditを直接確認します。

## Human decision table

| 状態 | 観測できたこと | Humanが次に確認すること | 推測してはいけないこと | Automatic action |
| --- | --- | --- | --- | --- |
| `paired` + `lifecycle_inputs=verified` | Receipt exact bytesと唯一のfinal reportがpairingし、提供されたlifecycle入力も一致 | final report本体のoutcome、action結果、rollback、operator action項目を別途確認 | pairedだから成功、再実行可能、対応不要とは判断しない | 禁止（none） |
| `paired` + `lifecycle_inputs=mismatch` | Receiptとfinal reportのpairingは成立するが、現在のlifecycle入力はReceiptと不一致。`operator_action_required=true` | 変更されたartifact実bytesとscopeを保全し、final reportと変更経緯をHuman reviewする。Human review required | mismatchを実行済み・失敗の証拠とせず、exit code 0をretry許可としない | 禁止（none） |
| `orphan` + lifecycle verified + `current_preconditions=verified` | final report候補がなく、提供されたlifecycle入力と現在snapshotはReceipt-bound expectationに一致 | 別の保存場所にfinal reportがないか、operation logとHuman Gate記録を確認 | verifiedを未実行またはretry-safeの証拠としない | 禁止（none） |
| `orphan` + lifecycle verified + `current_preconditions=mismatch` | final report候補がなく、現在snapshotがReceipt-bound expectationと不一致 | artifactとrootを変更せず保全し、差分原因とfinal report欠落をHumanが調査 | mismatchを実行済みまたは失敗の証拠としない | 禁止（none） |
| `orphan` + `current_preconditions=not-provided` | valid Receiptにfinal report候補がなく、現在snapshotは評価されていない | 必要ならoperation typeに対応する完全なlifecycle入力とroot setを用意し、read-onlyで再確認 | filesystemが一致・不一致、operationが未実行とは判断しない | 禁止（none） |
| `orphan` + `current_preconditions=not-applicable` | final report候補がなく、action 0件などによりcurrent preconditionの検証対象がない | Receipt、Plan、Approval等でaction scopeが0件である理由を確認 | not-applicableを安全性、成功、retry許可としない | 禁止（none） |
| `stale` | orphan Receiptへ提供した完全なlifecycle入力がexact-byte bindingまたはaction scopeと不一致 | Receiptと全lifecycle artifactを保全し、不一致sourceと承認scopeをHumanが照合 | staleをexecution proof、failure proof、retry判断に使わない | 禁止（none） |
| `conflicting` | final report候補の重複・不一致などにより一意にpairingできない | 全候補を保全し、Receipt SHA-256参照、operation type、attempt label、action scopeをHumanが突合 | 任意の候補を優先したり、filesystem状態から正しいreportを推測したりしない | 禁止（none） |

### `paired + lifecycle mismatch`の扱い

この組合せではpairing自体が成立しているため、Intent Statusの終了コードは`0`になり得ます。一方で現在提供されたlifecycle artifactはReceiptと一致せず、`operator_action_required=true`です。したがってHuman review requiredであり、自動retryは禁止です。pairingの確認とfreshnessの不一致を同じ結論へ統合しません。

## Receipt identityとattempt_id

Receiptのsecurity identityはexact Receipt bytesのSHA-256です。`attempt_id`はoperator-facing correlation labelにすぎず、security identityではありません。同じ`attempt_id`を持つartifactが同一Receiptであるとは限らず、pairingへ単独使用しません。

filename、path、mtime、parse後payloadの再serialize hashもReceipt identityには使いません。Human確認では、Statusに記録されたexact-byte SHA-256と正式report内のbindingを使用します。

## Operational Auditとの役割分担

Intent StatusはReceipt pairing、lifecycle freshness、current filesystem snapshotを観測します。Operational Auditはfinal reportをsourceとして、`succeeded / partial / failed / rolled_back / not_run`等のoperation outcomeを集約します。

Intent StatusからOperational Auditのoutcomeを推測せず、Operational Auditから現在のReceipt freshnessやfilesystem snapshotも推測しません。Outcome確認が必要な場合は、exact-byte bindingを検証した正式なfinal reportとOperational Auditを別軸として確認します。

## Scope limits

- このガイドはread-onlyなHuman確認手順だけを定義します。
- retry、cleanup、retention、repair、deletionは実行しません。
- classificationやverification fieldからautomatic actionを導出しません。
- Status取得後にfilesystemやartifactが変化し得るため、後続のdestructive operationに必要な直前gateを代替しません。
- directory inventory、複数Receipt探索、Operational Audit schema統合は対象外です。
