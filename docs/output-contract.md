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
- `normalization_profile`: v1予約field。現在は常に`null`

同一CLI設定を同一順序で指定した場合に同じJSONを生成します。globの順序が異なる実行は、現在のfilter結果が同じでも別の設定表現として扱います。

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
- optional fieldを追加する場合も、v1 consumerが未知fieldを無視できることと決定性を確認する
- downstream consumerは`report_type`と`schema_version`を検証してから処理する
