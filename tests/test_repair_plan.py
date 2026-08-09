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
from knowledge_importer.package_validation import (
    PackageValidationResult,
    ValidationIssue,
    ValidationSeverity,
    validate_package,
)
from knowledge_importer.repair_plan import (
    RepairActionCategory,
    build_repair_plan,
    write_repair_plan,
)


def _issue(
    category: str,
    *,
    path: str = "section/a.metadata.json",
    severity: ValidationSeverity = ValidationSeverity.ERROR,
) -> ValidationIssue:
    return ValidationIssue(path, severity, category, "架空の検証理由")


def _result(*issues: ValidationIssue) -> PackageValidationResult:
    return PackageValidationResult(tuple(issue.path for issue in issues), issues)


def _write_valid_package(root: Path) -> tuple[Path, Path, Path]:
    markdown = root / "節" / "文書.md"
    markdown.parent.mkdir(parents=True)
    markdown.write_text("# 架空文書\n\n検証専用の本文です。\n", encoding="utf-8")
    source = b"%PDF-1.4\n% synthetic\n"
    item = ArtifactManifestItem(
        "節/文書.pdf",
        "節/文書.md",
        ManifestStatus.SUCCEEDED,
        ArtifactDigest(len(source), hashlib.sha256(source).hexdigest()),
        digest_file(markdown),
    )
    sidecar = markdown.with_suffix(".metadata.json")
    write_document_metadata(
        sidecar,
        build_document_metadata(item, DocumentMetadataSettings(False, None, False)),
    )
    manifest = root / "manifest.json"
    write_artifact_manifest(manifest, ArtifactManifest(ArtifactManifestSettings(), (item,)))
    return markdown, sidecar, manifest


def test_valid_validation_result_has_no_actions() -> None:
    plan = build_repair_plan(PackageValidationResult((), ()), manifest_name="manifest.json")

    assert plan.payload() == {
        "report_type": "knowledge-package-repair-plan",
        "schema_version": 1,
        "summary": {"issues": 0, "actions": 0, "manual_review": 0},
        "actions": [],
    }


@pytest.mark.parametrize(
    "category",
    [
        "artifact-digest-mismatch",
        "artifact-size-mismatch",
        "manifest-sidecar-mismatch",
        "path-mismatch",
        "settings-mismatch",
        "missing-artifact",
        "invalid-json",
        "invalid-schema",
        "unsupported-schema",
        "outside-package-root",
        "orphan-sidecar",
    ],
)
def test_ambiguous_issue_requires_manual_review(category: str) -> None:
    plan = build_repair_plan(_result(_issue(category)), manifest_name="manifest.json")

    assert len(plan.actions) == 1
    assert plan.actions[0].action is RepairActionCategory.MANUAL_REVIEW
    assert plan.actions[0].safe is False
    assert plan.manual_review == 1


@pytest.mark.parametrize(
    ("issue_category", "action_category"),
    [
        ("missing-sidecar", RepairActionCategory.REGENERATE_SIDECAR),
        ("stale-sidecar", RepairActionCategory.REMOVE_STALE_SIDECAR),
    ],
)
def test_unambiguous_manifest_issue_is_safe(
    issue_category: str,
    action_category: RepairActionCategory,
) -> None:
    plan = build_repair_plan(
        _result(_issue(issue_category)),
        manifest_name="manifest.json",
    )

    assert plan.actions[0].action is action_category
    assert plan.actions[0].safe is True


def test_missing_manifest_does_not_infer_safe_action() -> None:
    plan = build_repair_plan(_result(_issue("missing-sidecar")))

    assert plan.actions[0].action is RepairActionCategory.MANUAL_REVIEW
    assert plan.actions[0].safe is False


def test_invalid_manifest_disables_related_safe_action() -> None:
    result = _result(
        _issue("invalid-json", path="manifest.json"),
        _issue("missing-sidecar"),
    )

    plan = build_repair_plan(result, manifest_name="manifest.json")

    assert all(action.safe is False for action in plan.actions)
    assert all(action.action is RepairActionCategory.MANUAL_REVIEW for action in plan.actions)


def test_extra_artifact_warning_is_omitted_but_strict_error_is_planned() -> None:
    warning = build_repair_plan(
        _result(_issue("extra-artifact", severity=ValidationSeverity.WARNING)),
        manifest_name="manifest.json",
    )
    strict = build_repair_plan(
        _result(_issue("extra-artifact")),
        manifest_name="manifest.json",
    )

    assert warning.issues == 1
    assert warning.actions == ()
    assert strict.actions[0].action is RepairActionCategory.REGENERATE_MANIFEST
    assert strict.actions[0].safe is False


def test_action_order_and_report_bytes_are_deterministic(tmp_path: Path) -> None:
    result = _result(
        _issue("artifact-digest-mismatch", path="z.metadata.json"),
        _issue("missing-sidecar", path="日本語/a.metadata.json"),
        _issue("missing-artifact", path="A.metadata.json"),
    )
    plan = build_repair_plan(result, manifest_name="manifest.json")
    report = tmp_path / "repair.json"

    write_repair_plan(report, plan)
    first = report.read_bytes()
    write_repair_plan(report, plan)

    assert report.read_bytes() == first
    payload = json.loads(first)
    assert [action["path"] for action in payload["actions"]] == [
        "A.metadata.json",
        "z.metadata.json",
        "日本語/a.metadata.json",
    ]
    assert first.endswith(b"\n")


def test_cli_reuses_validation_result_and_returns_zero_for_issues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = _result(_issue("artifact-digest-mismatch"))
    calls: list[tuple[Path, Path | None, bool]] = []

    def fake_validate(
        package_root: Path,
        *,
        manifest_path: Path | None,
        strict: bool,
    ) -> PackageValidationResult:
        calls.append((package_root, manifest_path, strict))
        return result

    monkeypatch.setattr(cli, "validate_package", fake_validate)
    report = tmp_path / "reports" / "repair.json"

    assert cli.run(["repair-plan", str(tmp_path), "--strict", "--report-json", str(report)]) == 0

    captured = capsys.readouterr()
    assert calls == [(tmp_path, None, True)]
    assert "問題=1 修復候補=1 手動確認=1" in captured.out
    assert "操作=manual-review" in captured.out
    assert captured.err == ""
    assert json.loads(report.read_text(encoding="utf-8"))["summary"]["actions"] == 1


def test_cli_rejects_invalid_inputs_and_report_conflicts(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    assert cli.run(["repair-plan", str(missing)]) == 2
    assert cli.run(["repair-plan", str(tmp_path), "--manifest", str(missing)]) == 2
    assert (
        cli.run(
            [
                "repair-plan",
                str(tmp_path),
                "--manifest",
                str(manifest),
                "--report-json",
                str(manifest),
            ]
        )
        == 2
    )


def test_report_failure_preserves_existing_and_hides_sensitive_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = tmp_path / "repair.json"
    report.write_text("existing\n", encoding="utf-8")

    def failing_writer(path: Path, plan: object) -> None:
        raise OSError(f"cannot write {tmp_path} Traceback")

    monkeypatch.setattr(cli, "write_repair_plan", failing_writer)

    assert cli.run(["repair-plan", str(tmp_path), "--report-json", str(report)]) == 2

    captured = capsys.readouterr()
    assert report.read_text(encoding="utf-8") == "existing\n"
    assert captured.err == "Repair Planを書き込めませんでした。\n"
    assert str(tmp_path) not in captured.err
    assert "Traceback" not in captured.err


def test_actual_validation_plan_is_read_only(tmp_path: Path) -> None:
    markdown, sidecar, manifest = _write_valid_package(tmp_path)
    markdown.write_text("変更された架空本文\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in (markdown, sidecar, manifest)}

    result = validate_package(tmp_path, manifest_path=manifest)
    plan = build_repair_plan(result, manifest_name=manifest.name)

    assert {path: path.read_bytes() for path in before} == before
    assert {action.reason_category for action in plan.actions} >= {
        "artifact-size-mismatch",
        "artifact-digest-mismatch",
    }
    assert all(action.action is RepairActionCategory.MANUAL_REVIEW for action in plan.actions)
