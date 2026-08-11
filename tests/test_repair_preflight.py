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
from knowledge_importer.repair_approval import (
    RepairApproval,
    build_repair_approval,
    write_repair_approval,
)
from knowledge_importer.repair_plan import (
    RepairAction,
    RepairActionCategory,
    build_repair_plan,
    write_repair_plan,
)
from knowledge_importer.repair_preflight import (
    build_repair_preflight,
    write_repair_preflight,
)


def _item(root: Path, relative: str, *, status: ManifestStatus) -> ArtifactManifestItem:
    markdown = root / Path(*relative.split("/"))
    markdown.parent.mkdir(parents=True, exist_ok=True)
    markdown.write_text("# 架空文書\n\n検証専用の本文です。\n", encoding="utf-8")
    source = b"%PDF-1.4\n% synthetic preflight fixture\n"
    return ArtifactManifestItem(
        relative.removesuffix(".md") + ".pdf",
        relative,
        status,
        ArtifactDigest(len(source), hashlib.sha256(source).hexdigest()),
        digest_file(markdown)
        if status is not ManifestStatus.FAILED
        else ArtifactDigest(None, None),
    )


def _write_manifest(path: Path, items: tuple[ArtifactManifestItem, ...]) -> None:
    write_artifact_manifest(path, ArtifactManifest(ArtifactManifestSettings(), items))


def _write_sidecar(root: Path, item: ArtifactManifestItem) -> Path:
    sidecar = root / Path(*item.output_path.split("/")).with_suffix(".metadata.json")
    write_document_metadata(
        sidecar,
        build_document_metadata(item, DocumentMetadataSettings(False, None, False)),
    )
    return sidecar


def _prepare_contract(
    root: Path,
    manifest: Path,
    plan: Path,
    approval: Path,
) -> None:
    result = validate_package(root, manifest_path=manifest)
    repair_plan = build_repair_plan(result, manifest_name=manifest.name)
    write_repair_plan(plan, repair_plan)
    write_repair_approval(approval, build_repair_approval(plan))


def _args(root: Path, manifest: Path, plan: Path, approval: Path) -> list[str]:
    return [
        "repair-preflight",
        str(root),
        "--manifest",
        str(manifest),
        "--plan",
        str(plan),
        "--approval",
        str(approval),
    ]


def test_help_exposes_required_preflight_inputs(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        cli.run(["repair-preflight", "--help"])

    assert error.value.code == 0
    output = capsys.readouterr().out
    for option in ("--manifest", "--plan", "--approval", "--report-json"):
        assert option in output


def test_regenerate_sidecar_is_ready_and_binds_exact_bytes(tmp_path: Path) -> None:
    root = tmp_path / "package"
    item = _item(root, "日本語/文書.md", status=ManifestStatus.SUCCEEDED)
    manifest = tmp_path / "manifest.json"
    plan = tmp_path / "plan.json"
    approval = tmp_path / "approval.json"
    _write_manifest(manifest, (item,))
    _prepare_contract(root, manifest, plan, approval)

    result = build_repair_preflight(
        root, manifest_path=manifest, plan_path=plan, approval_path=approval
    )

    assert result.exit_code == 0
    assert (result.ready, result.blocked) == (1, 0)
    assert result.plan_sha256 == hashlib.sha256(plan.read_bytes()).hexdigest()
    assert result.approval_sha256 == hashlib.sha256(approval.read_bytes()).hexdigest()
    action = result.actions[0]
    assert action.repair_action.path == "日本語/文書.metadata.json"
    assert action.target.payload() == {
        "path": "日本語/文書.metadata.json",
        "exists": False,
        "bytes": None,
        "sha256": None,
    }
    assert not action.backup_required


def test_stale_sidecar_is_ready_and_records_target_digest(tmp_path: Path) -> None:
    root = tmp_path / "package"
    succeeded = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    sidecar = _write_sidecar(root, succeeded)
    failed = ArtifactManifestItem(
        succeeded.input_path,
        succeeded.output_path,
        ManifestStatus.FAILED,
        succeeded.input_digest,
        ArtifactDigest(None, None),
    )
    manifest = tmp_path / "manifest.json"
    plan = tmp_path / "plan.json"
    approval = tmp_path / "approval.json"
    _write_manifest(manifest, (failed,))
    _prepare_contract(root, manifest, plan, approval)

    result = build_repair_preflight(
        root, manifest_path=manifest, plan_path=plan, approval_path=approval
    )

    assert result.exit_code == 0
    action = result.actions[0]
    assert action.status == "ready"
    assert action.backup_required
    assert action.target.bytes == sidecar.stat().st_size
    assert action.target.sha256 == hashlib.sha256(sidecar.read_bytes()).hexdigest()


@pytest.mark.parametrize("change", ["sidecar", "missing-markdown", "changed-markdown"])
def test_regenerate_sidecar_state_changes_are_blocked(tmp_path: Path, change: str) -> None:
    root = tmp_path / "package"
    item = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    manifest = tmp_path / "manifest.json"
    plan = tmp_path / "plan.json"
    approval = tmp_path / "approval.json"
    _write_manifest(manifest, (item,))
    _prepare_contract(root, manifest, plan, approval)
    markdown = root / "section" / "a.md"
    if change == "sidecar":
        _write_sidecar(root, item)
    elif change == "missing-markdown":
        markdown.unlink()
    else:
        markdown.write_text("changed\n", encoding="utf-8")

    result = build_repair_preflight(
        root, manifest_path=manifest, plan_path=plan, approval_path=approval
    )

    assert result.exit_code == 1
    assert result.actions[0].status == "blocked"
    assert result.actions[0].block_reason == "package-state-changed"


def test_manifest_status_change_is_blocked(tmp_path: Path) -> None:
    root = tmp_path / "package"
    item = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    manifest = tmp_path / "manifest.json"
    plan = tmp_path / "plan.json"
    approval = tmp_path / "approval.json"
    _write_manifest(manifest, (item,))
    _prepare_contract(root, manifest, plan, approval)
    failed = ArtifactManifestItem(
        item.input_path,
        item.output_path,
        ManifestStatus.FAILED,
        item.input_digest,
        ArtifactDigest(None, None),
    )
    _write_manifest(manifest, (failed,))

    result = build_repair_preflight(
        root, manifest_path=manifest, plan_path=plan, approval_path=approval
    )

    assert result.exit_code == 1
    assert result.actions[0].block_reason == "package-state-changed"


def test_stale_sidecar_disappearance_is_blocked(tmp_path: Path) -> None:
    root = tmp_path / "package"
    succeeded = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    sidecar = _write_sidecar(root, succeeded)
    failed = ArtifactManifestItem(
        succeeded.input_path,
        succeeded.output_path,
        ManifestStatus.FAILED,
        succeeded.input_digest,
        ArtifactDigest(None, None),
    )
    manifest = tmp_path / "manifest.json"
    plan = tmp_path / "plan.json"
    approval = tmp_path / "approval.json"
    _write_manifest(manifest, (failed,))
    _prepare_contract(root, manifest, plan, approval)
    sidecar.unlink()

    result = build_repair_preflight(
        root, manifest_path=manifest, plan_path=plan, approval_path=approval
    )

    assert result.exit_code == 1
    assert result.actions[0].block_reason == "package-state-changed"


def test_invalid_manifest_blocks_action_instead_of_breaking_binding(tmp_path: Path) -> None:
    root = tmp_path / "package"
    item = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    manifest = tmp_path / "manifest.json"
    plan = tmp_path / "plan.json"
    approval = tmp_path / "approval.json"
    _write_manifest(manifest, (item,))
    _prepare_contract(root, manifest, plan, approval)
    manifest.write_text("{broken", encoding="utf-8")

    result = build_repair_preflight(
        root, manifest_path=manifest, plan_path=plan, approval_path=approval
    )

    assert result.exit_code == 1
    assert result.actions[0].block_reason == "manifest-invalid"


def test_plan_digest_mismatch_returns_two_without_report(tmp_path: Path) -> None:
    root = tmp_path / "package"
    item = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    manifest = tmp_path / "manifest.json"
    plan = tmp_path / "plan.json"
    approval = tmp_path / "approval.json"
    report = tmp_path / "preflight.json"
    _write_manifest(manifest, (item,))
    _prepare_contract(root, manifest, plan, approval)
    plan.write_bytes(plan.read_bytes() + b" ")

    assert cli.run(_args(root, manifest, plan, approval) + ["--report-json", str(report)]) == 2
    assert not report.exists()


def test_approval_action_not_in_plan_returns_two(tmp_path: Path) -> None:
    root = tmp_path / "package"
    root.mkdir()
    manifest = tmp_path / "manifest.json"
    plan = tmp_path / "plan.json"
    approval = tmp_path / "approval.json"
    _write_manifest(manifest, ())
    write_repair_plan(plan, build_repair_plan(validate_package(root, manifest_path=manifest)))
    extra = RepairAction(
        "section/a.metadata.json",
        RepairActionCategory.REGENERATE_SIDECAR,
        "missing-sidecar",
        True,
    )
    write_repair_approval(
        approval,
        RepairApproval(hashlib.sha256(plan.read_bytes()).hexdigest(), (extra,)),
    )

    assert cli.run(_args(root, manifest, plan, approval)) == 2


def test_unsafe_approval_returns_two(tmp_path: Path) -> None:
    root = tmp_path / "package"
    root.mkdir()
    manifest = tmp_path / "manifest.json"
    plan = tmp_path / "plan.json"
    approval = tmp_path / "approval.json"
    _write_manifest(manifest, ())
    write_repair_plan(plan, build_repair_plan(validate_package(root, manifest_path=manifest)))
    unsafe = RepairAction(
        "section/a.metadata.json",
        RepairActionCategory.MANUAL_REVIEW,
        "path-mismatch",
        False,
    )
    write_repair_approval(
        approval,
        RepairApproval(hashlib.sha256(plan.read_bytes()).hexdigest(), (unsafe,)),
    )

    assert cli.run(_args(root, manifest, plan, approval)) == 2


def test_zero_approved_actions_returns_zero(tmp_path: Path) -> None:
    root = tmp_path / "package"
    root.mkdir()
    manifest = tmp_path / "manifest.json"
    plan = tmp_path / "plan.json"
    approval = tmp_path / "approval.json"
    _write_manifest(manifest, ())
    _prepare_contract(root, manifest, plan, approval)

    assert cli.run(_args(root, manifest, plan, approval)) == 0


def test_manifest_omission_blocks_safe_action_without_guessing(tmp_path: Path) -> None:
    root = tmp_path / "package"
    item = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    manifest = tmp_path / "manifest.json"
    plan = tmp_path / "plan.json"
    approval = tmp_path / "approval.json"
    _write_manifest(manifest, (item,))
    _prepare_contract(root, manifest, plan, approval)

    exit_code = cli.run(
        [
            "repair-preflight",
            str(root),
            "--plan",
            str(plan),
            "--approval",
            str(approval),
        ]
    )

    assert exit_code == 1


def test_mixed_ready_blocked_has_deterministic_order_and_exit_one(tmp_path: Path) -> None:
    root = tmp_path / "package"
    item_z = _item(root, "z/文書.md", status=ManifestStatus.SUCCEEDED)
    item_a = _item(root, "a/文書.md", status=ManifestStatus.SUCCEEDED)
    manifest = tmp_path / "manifest.json"
    plan = tmp_path / "plan.json"
    approval = tmp_path / "approval.json"
    _write_manifest(manifest, (item_z, item_a))
    _prepare_contract(root, manifest, plan, approval)
    _write_sidecar(root, item_z)

    result = build_repair_preflight(
        root, manifest_path=manifest, plan_path=plan, approval_path=approval
    )

    assert result.exit_code == 1
    assert [action.repair_action.path for action in result.actions] == [
        "a/文書.metadata.json",
        "z/文書.metadata.json",
    ]
    assert [action.status for action in result.actions] == ["ready", "blocked"]


def test_report_is_deterministic_and_can_atomically_replace_itself(tmp_path: Path) -> None:
    root = tmp_path / "package"
    item = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    manifest = tmp_path / "manifest.json"
    plan = tmp_path / "plan.json"
    approval = tmp_path / "approval.json"
    report = tmp_path / "preflight.json"
    _write_manifest(manifest, (item,))
    _prepare_contract(root, manifest, plan, approval)
    arguments = _args(root, manifest, plan, approval) + ["--report-json", str(report)]

    assert cli.run(arguments) == 0
    first = report.read_bytes()
    assert cli.run(arguments) == 0

    assert report.read_bytes() == first
    assert first.endswith(b"\n")


def test_other_report_is_not_overwritten_before_validation(tmp_path: Path) -> None:
    root = tmp_path / "package"
    root.mkdir()
    manifest = tmp_path / "manifest.json"
    plan = tmp_path / "plan.json"
    approval = tmp_path / "approval.json"
    report = tmp_path / "other.json"
    _write_manifest(manifest, ())
    _prepare_contract(root, manifest, plan, approval)
    report.write_text('{"report_type":"other"}\n', encoding="utf-8")
    before = report.read_bytes()

    assert cli.run(_args(root, manifest, plan, approval) + ["--report-json", str(report)]) == 2
    assert report.read_bytes() == before


def test_preflight_is_read_only_and_does_not_expose_local_details(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "package"
    item = _item(root, "日本語/文書.md", status=ManifestStatus.SUCCEEDED)
    manifest = tmp_path / "manifest.json"
    plan = tmp_path / "plan.json"
    approval = tmp_path / "approval.json"
    report = tmp_path / "report.json"
    _write_manifest(manifest, (item,))
    _prepare_contract(root, manifest, plan, approval)
    protected = (root / "日本語" / "文書.md", manifest, plan, approval)
    before = {path: path.read_bytes() for path in protected}

    assert cli.run(_args(root, manifest, plan, approval) + ["--report-json", str(report)]) == 0

    captured = capsys.readouterr()
    assert {path: path.read_bytes() for path in protected} == before
    combined = captured.out + captured.err + report.read_text(encoding="utf-8")
    assert str(tmp_path) not in combined
    assert "Traceback" not in combined
    assert "shuns" not in combined


def test_symlink_target_is_blocked_when_supported(tmp_path: Path) -> None:
    root = tmp_path / "package"
    item = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    manifest = tmp_path / "manifest.json"
    plan = tmp_path / "plan.json"
    approval = tmp_path / "approval.json"
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    _write_manifest(manifest, (item,))
    _prepare_contract(root, manifest, plan, approval)
    sidecar = root / "section" / "a.metadata.json"
    try:
        sidecar.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is not permitted")

    result = build_repair_preflight(
        root, manifest_path=manifest, plan_path=plan, approval_path=approval
    )

    assert result.exit_code == 1
    assert result.actions[0].block_reason == "path-unsafe"


def test_preflight_payload_has_no_machine_metadata(tmp_path: Path) -> None:
    root = tmp_path / "package"
    root.mkdir()
    manifest = tmp_path / "manifest.json"
    plan = tmp_path / "plan.json"
    approval = tmp_path / "approval.json"
    report = tmp_path / "preflight.json"
    _write_manifest(manifest, ())
    _prepare_contract(root, manifest, plan, approval)
    preflight = build_repair_preflight(
        root, manifest_path=manifest, plan_path=plan, approval_path=approval
    )
    write_repair_preflight(report, preflight)

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["summary"] == {"actions": 0, "ready": 0, "blocked": 0}
    for forbidden in ("timestamp", "username", "hostname", "cwd", "command_line"):
        assert forbidden not in report.read_text(encoding="utf-8")
