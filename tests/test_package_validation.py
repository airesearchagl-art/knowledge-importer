from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import knowledge_importer.cli as cli
from knowledge_importer.artifact_manifest import (
    ArtifactDigest,
    ArtifactManifest,
    ArtifactManifestItem,
    ArtifactManifestSettings,
    ManifestStatus,
    digest_file,
    write_artifact_manifest,
)
from knowledge_importer.document_metadata import (
    DocumentMetadataSettings,
    build_document_metadata,
    write_document_metadata,
)
from knowledge_importer.package_validation import validate_package


def _write_valid_document(
    root: Path,
    relative_output: str = "section/a.md",
    *,
    status: ManifestStatus = ManifestStatus.SUCCEEDED,
) -> tuple[Path, ArtifactManifestItem]:
    markdown = root / Path(*relative_output.split("/"))
    markdown.parent.mkdir(parents=True, exist_ok=True)
    markdown.write_text("# 架空文書\n\n検証用の本文です。\n", encoding="utf-8")
    source_bytes = b"%PDF-1.4\n% synthetic source\n"
    source_digest = ArtifactDigest(len(source_bytes), hashlib.sha256(source_bytes).hexdigest())
    item = ArtifactManifestItem(
        input_path=relative_output.removesuffix(".md") + ".pdf",
        output_path=relative_output,
        status=status,
        input_digest=source_digest,
        output_digest=digest_file(markdown),
    )
    sidecar = build_document_metadata(
        item,
        DocumentMetadataSettings(False, None, False),
    )
    sidecar_path = markdown.with_suffix(".metadata.json")
    write_document_metadata(sidecar_path, sidecar)
    return sidecar_path, item


def _write_manifest(path: Path, items: tuple[ArtifactManifestItem, ...]) -> None:
    write_artifact_manifest(path, ArtifactManifest(ArtifactManifestSettings(), items))


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _categories(result: object) -> set[str]:
    return {issue.category for issue in result.issues}  # type: ignore[attr-defined]


def test_validate_sidecar_only_and_unknown_fields(tmp_path: Path) -> None:
    sidecar_path, _ = _write_valid_document(tmp_path)
    payload = _load(sidecar_path)
    payload["future_field"] = {"allowed": True}
    _save(sidecar_path, payload)

    result = validate_package(tmp_path)

    assert result.exit_code == 0
    assert result.payload()["summary"] == {
        "checked": 1,
        "passed": 1,
        "failed": 0,
        "warnings": 0,
    }


def test_validate_with_manifest_and_skipped_item(tmp_path: Path) -> None:
    _, item = _write_valid_document(tmp_path, "節/既存.md", status=ManifestStatus.SKIPPED)
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, (item,))

    result = validate_package(tmp_path, manifest_path=manifest)

    assert result.exit_code == 0
    assert result.issues == ()


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda payload: payload.update(schema_version=2), "unsupported-schema"),
        (lambda payload: payload.update(report_type="wrong"), "invalid-schema"),
        (lambda payload: payload["artifact"].update(bytes=-1), "invalid-schema"),
        (lambda payload: payload["artifact"].update(sha256="ABC"), "invalid-schema"),
    ],
)
def test_sidecar_schema_failures(
    tmp_path: Path,
    mutation: object,
    expected: str,
) -> None:
    sidecar_path, _ = _write_valid_document(tmp_path)
    payload = _load(sidecar_path)
    mutation(payload)  # type: ignore[operator]
    _save(sidecar_path, payload)

    assert expected in _categories(validate_package(tmp_path))


def test_invalid_sidecar_json(tmp_path: Path) -> None:
    sidecar = tmp_path / "broken.metadata.json"
    sidecar.write_text("{broken", encoding="utf-8")

    result = validate_package(tmp_path)

    assert result.exit_code == 1
    assert _categories(result) == {"invalid-json"}


def test_missing_artifact(tmp_path: Path) -> None:
    sidecar, _ = _write_valid_document(tmp_path)
    sidecar.with_name("a.md").unlink()

    assert "missing-artifact" in _categories(validate_package(tmp_path))


def test_artifact_size_and_digest_mismatch(tmp_path: Path) -> None:
    sidecar, _ = _write_valid_document(tmp_path)
    sidecar.with_name("a.md").write_text("changed", encoding="utf-8")

    categories = _categories(validate_package(tmp_path))

    assert {"artifact-size-mismatch", "artifact-digest-mismatch"} <= categories


def test_outside_root_output_path_is_rejected_without_hashing(tmp_path: Path) -> None:
    sidecar, _ = _write_valid_document(tmp_path)
    payload = _load(sidecar)
    payload["document"]["output_path"] = "../outside.md"  # type: ignore[index]
    _save(sidecar, payload)

    assert "outside-package-root" in _categories(validate_package(tmp_path))


def test_sidecar_symlink_escape_is_rejected_when_supported(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.json"
    outside.write_text("{}", encoding="utf-8")
    link = tmp_path / "escape.metadata.json"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is not permitted")
    try:
        assert "outside-package-root" in _categories(validate_package(tmp_path))
    finally:
        outside.unlink(missing_ok=True)


def test_missing_stale_and_orphan_sidecars(tmp_path: Path) -> None:
    sidecar, item = _write_valid_document(tmp_path, "present.md")
    missing_item = ArtifactManifestItem(
        "missing.pdf",
        "missing.md",
        ManifestStatus.SUCCEEDED,
        item.input_digest,
        item.output_digest,
    )
    failed_item = ArtifactManifestItem(
        item.input_path,
        item.output_path,
        ManifestStatus.FAILED,
        item.input_digest,
        ArtifactDigest(None, None),
    )
    manifest = tmp_path / "manifest.json"

    _write_manifest(manifest, (missing_item, failed_item))
    categories = _categories(validate_package(tmp_path, manifest_path=manifest))

    assert {"missing-sidecar", "stale-sidecar"} <= categories
    assert "orphan-sidecar" not in categories

    _write_manifest(manifest, ())
    assert "orphan-sidecar" in _categories(validate_package(tmp_path, manifest_path=manifest))
    assert sidecar.is_file()


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda payload: payload["document"].update(input_path="other.pdf"), "path-mismatch"),
        (lambda payload: payload["document"].update(output_path="other.md"), "path-mismatch"),
        (
            lambda payload: payload["document"].update(status="skipped"),
            "manifest-sidecar-mismatch",
        ),
        (
            lambda payload: payload["source"].update(sha256="0" * 64),
            "manifest-sidecar-mismatch",
        ),
        (
            lambda payload: payload["artifact"].update(sha256="0" * 64),
            "manifest-sidecar-mismatch",
        ),
        (
            lambda payload: payload["engine"].update(version="9.9.9"),
            "manifest-sidecar-mismatch",
        ),
        (
            lambda payload: payload["settings"].update(table_structure=True),
            "settings-mismatch",
        ),
        (
            lambda payload: payload["settings"].update(normalization_profile="conservative"),
            "settings-mismatch",
        ),
        (
            lambda payload: payload["settings"].update(artifacts_path_configured=True),
            "settings-mismatch",
        ),
    ],
)
def test_manifest_sidecar_mismatches(tmp_path: Path, mutate: object, expected: str) -> None:
    sidecar, item = _write_valid_document(tmp_path)
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, (item,))
    payload = _load(sidecar)
    mutate(payload)  # type: ignore[operator]
    _save(sidecar, payload)

    assert expected in _categories(validate_package(tmp_path, manifest_path=manifest))


def test_extra_markdown_warning_and_strict_failure(tmp_path: Path) -> None:
    _, item = _write_valid_document(tmp_path)
    extra = tmp_path / "extra.md"
    extra.write_text("extra\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, (item,))

    default = validate_package(tmp_path, manifest_path=manifest)
    strict = validate_package(tmp_path, manifest_path=manifest, strict=True)

    assert default.exit_code == 0
    assert default.warnings == 1
    assert strict.exit_code == 1
    assert strict.failed == 1


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("{broken", "invalid-json"),
        ('{"report_type":"knowledge-artifact-manifest","schema_version":2}', "unsupported-schema"),
        ('{"report_type":"wrong","schema_version":1}', "invalid-schema"),
    ],
)
def test_manifest_parse_failures(tmp_path: Path, content: str, expected: str) -> None:
    _write_valid_document(tmp_path)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(content, encoding="utf-8")

    assert expected in _categories(validate_package(tmp_path, manifest_path=manifest))


def test_deterministic_issue_order_and_report_bytes(tmp_path: Path) -> None:
    for name in ("z", "a"):
        (tmp_path / f"{name}.metadata.json").write_text("broken", encoding="utf-8")
    report = tmp_path / "validation.json"
    arguments = ["validate", str(tmp_path), "--report-json", str(report)]

    assert cli.run(arguments) == 1
    first = report.read_bytes()
    assert cli.run(arguments) == 1

    assert report.read_bytes() == first
    assert [issue["path"] for issue in _load(report)["issues"]] == [
        "a.metadata.json",
        "z.metadata.json",
    ]


def test_cli_summary_and_safe_relative_stderr(tmp_path: Path, capsys: object) -> None:
    (tmp_path / "broken.metadata.json").write_text("broken", encoding="utf-8")

    exit_code = cli.run(["validate", str(tmp_path)])
    captured = capsys.readouterr()  # type: ignore[attr-defined]

    assert exit_code == 1
    assert "対象=1 成功=0 失敗=1 警告=0" in captured.out
    assert "ファイル=broken.metadata.json 分類=invalid-json" in captured.err
    assert str(tmp_path) not in captured.err
    assert "Traceback" not in captured.err


def test_invalid_package_root_and_manifest_return_two(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    assert cli.run(["validate", str(missing)]) == 2
    assert cli.run(["validate", str(tmp_path), "--manifest", str(missing)]) == 2


def test_validation_report_cannot_overwrite_package_contract_files(tmp_path: Path) -> None:
    sidecar, _ = _write_valid_document(tmp_path)

    assert cli.run(["validate", str(tmp_path), "--report-json", str(sidecar)]) == 2


def test_report_write_failure_returns_two_and_preserves_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: object
) -> None:
    _write_valid_document(tmp_path)
    report = tmp_path / "validation.json"
    report.write_text("existing\n", encoding="utf-8")

    def failing_writer(path: Path, result: object) -> None:
        raise OSError(f"cannot write {tmp_path} Traceback")

    monkeypatch.setattr(cli, "write_validation_report", failing_writer)
    assert cli.run(["validate", str(tmp_path), "--report-json", str(report)]) == 2
    stderr = capsys.readouterr().err  # type: ignore[attr-defined]

    assert report.read_text(encoding="utf-8") == "existing\n"
    assert stderr == "Validation reportを書き込めませんでした。\n"
    assert str(tmp_path) not in stderr
    assert "Traceback" not in stderr


def test_validation_does_not_modify_package_artifacts(tmp_path: Path) -> None:
    sidecar, item = _write_valid_document(tmp_path)
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, (item,))
    markdown = sidecar.with_name("a.md")
    before = {path: path.read_bytes() for path in (markdown, sidecar, manifest)}

    assert cli.run(["validate", str(tmp_path), "--manifest", str(manifest)]) == 0

    assert {path: path.read_bytes() for path in before} == before
