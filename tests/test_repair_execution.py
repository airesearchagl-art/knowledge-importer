from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import knowledge_importer.cli as cli
import knowledge_importer.repair_execution as execution
from knowledge_importer.artifact_manifest import (
    ArtifactDigest,
    ArtifactManifest,
    ArtifactManifestItem,
    ArtifactManifestSettings,
    ManifestStatus,
    digest_file,
    write_artifact_manifest,
)
from knowledge_importer.backup_inventory import (
    SESSION_MANIFEST_FILENAME,
    BackupSessionState,
    parse_backup_session_manifest_bytes,
)
from knowledge_importer.document_metadata import (
    DocumentMetadataSettings,
    build_document_metadata,
    write_document_metadata,
)
from knowledge_importer.json_writer import write_json_atomically
from knowledge_importer.package_validation import (
    PackageValidationResult,
    ValidationIssue,
    ValidationSeverity,
    validate_package,
)
from knowledge_importer.repair_approval import build_repair_approval, write_repair_approval
from knowledge_importer.repair_plan import build_repair_plan, write_repair_plan
from knowledge_importer.repair_preflight import build_repair_preflight, write_repair_preflight


def _item(root: Path, relative: str, *, status: ManifestStatus) -> ArtifactManifestItem:
    markdown = root / Path(*relative.split("/"))
    markdown.parent.mkdir(parents=True, exist_ok=True)
    markdown.write_text("# 架空文書\n\n修復実行テスト用の本文です。\n", encoding="utf-8")
    source = b"%PDF-1.4\n% synthetic repair execution fixture\n"
    return ArtifactManifestItem(
        relative.removesuffix(".md") + ".pdf",
        relative,
        status,
        ArtifactDigest(len(source), hashlib.sha256(source).hexdigest()),
        digest_file(markdown)
        if status is not ManifestStatus.FAILED
        else ArtifactDigest(None, None),
    )


def _failed(item: ArtifactManifestItem) -> ArtifactManifestItem:
    return ArtifactManifestItem(
        item.input_path,
        item.output_path,
        ManifestStatus.FAILED,
        item.input_digest,
        ArtifactDigest(None, None),
    )


def _sidecar(root: Path, item: ArtifactManifestItem) -> Path:
    path = root / Path(*item.output_path.split("/")).with_suffix(".metadata.json")
    write_document_metadata(
        path,
        build_document_metadata(item, DocumentMetadataSettings(False, None, False)),
    )
    return path


def _contract(
    tmp_path: Path,
    root: Path,
    items: tuple[ArtifactManifestItem, ...],
) -> tuple[Path, Path, Path, Path]:
    manifest = tmp_path / "manifest.json"
    plan = tmp_path / "plan.json"
    approval = tmp_path / "approval.json"
    preflight = tmp_path / "preflight.json"
    write_artifact_manifest(manifest, ArtifactManifest(ArtifactManifestSettings(), items))
    validation = validate_package(root, manifest_path=manifest)
    repair_plan = build_repair_plan(validation, manifest_name=manifest.name)
    write_repair_plan(plan, repair_plan)
    write_repair_approval(approval, build_repair_approval(plan))
    write_repair_preflight(
        preflight,
        build_repair_preflight(
            root,
            manifest_path=manifest,
            plan_path=plan,
            approval_path=approval,
        ),
    )
    return manifest, plan, approval, preflight


def _args(
    root: Path,
    manifest: Path,
    plan: Path,
    approval: Path,
    preflight: Path,
) -> list[str]:
    return [
        "repair-execute",
        str(root),
        "--manifest",
        str(manifest),
        "--plan",
        str(plan),
        "--approval",
        str(approval),
        "--preflight",
        str(preflight),
    ]


def test_execution_report_bytes_parser_accepts_canonical_report(tmp_path: Path) -> None:
    root = tmp_path / "package"
    item = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    manifest, plan, approval, preflight = _contract(tmp_path, root, (item,))
    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
    )
    content = (json.dumps(report.payload(), ensure_ascii=False, indent=2) + "\n").encode()

    parsed = execution.parse_repair_execution_report_bytes(content)

    assert parsed.payload() == report.payload()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rollback", "completed"),
        ("status", "rollback-failed"),
    ],
)
def test_execution_report_bytes_parser_rejects_invalid_status_rollback_semantics(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    root = tmp_path / "package"
    item = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    manifest, plan, approval, preflight = _contract(tmp_path, root, (item,))
    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
    ).payload()
    report["actions"][0][field] = value

    with pytest.raises(ValueError):
        execution.parse_repair_execution_report_bytes(json.dumps(report).encode())


def test_execution_report_bytes_parser_rejects_changed_not_run_state(tmp_path: Path) -> None:
    root = tmp_path / "package"
    item = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    manifest, plan, approval, preflight = _contract(tmp_path, root, (item,))
    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
    ).payload()
    action = report["actions"][0]
    action["status"] = "not-run"
    action["rollback"] = "not-required"
    report["summary"] = {
        "planned": 1,
        "executed": 0,
        "succeeded": 0,
        "failed": 0,
        "rolled_back": 0,
        "not_run": 1,
    }

    with pytest.raises(ValueError):
        execution.parse_repair_execution_report_bytes(json.dumps(report).encode())


def test_execution_report_bytes_parser_rejects_impossible_success_state(tmp_path: Path) -> None:
    root = tmp_path / "package"
    item = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    manifest, plan, approval, preflight = _contract(tmp_path, root, (item,))
    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
    ).payload()
    report["actions"][0]["before"] = report["actions"][0]["after"]

    with pytest.raises(ValueError):
        execution.parse_repair_execution_report_bytes(json.dumps(report).encode())


@pytest.mark.parametrize(
    ("location", "value"),
    [
        ("schema_version", 1.0),
        ("planned", True),
        ("path", "C:/private/item.metadata.json"),
    ],
)
def test_execution_report_bytes_parser_enforces_field_types_and_safe_paths(
    tmp_path: Path,
    location: str,
    value: object,
) -> None:
    root = tmp_path / "package"
    item = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    manifest, plan, approval, preflight = _contract(tmp_path, root, (item,))
    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
    ).payload()
    if location == "schema_version":
        report[location] = value
    elif location == "planned":
        report["summary"][location] = value
    else:
        report["actions"][0][location] = value

    with pytest.raises(ValueError):
        execution.parse_repair_execution_report_bytes(json.dumps(report).encode())


def test_help_exposes_execution_contract(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        cli.run(["repair-execute", "--help"])

    assert error.value.code == 0
    output = capsys.readouterr().out
    for option in (
        "--manifest",
        "--plan",
        "--approval",
        "--preflight",
        "--report-json",
        "--backup-dir",
    ):
        assert option in output


def test_zero_actions_succeeds_without_package_mutation(tmp_path: Path) -> None:
    root = tmp_path / "package"
    root.mkdir()
    manifest, plan, approval, preflight = _contract(tmp_path, root, ())
    before = {path: path.read_bytes() for path in (manifest, plan, approval, preflight)}

    assert cli.run(_args(root, manifest, plan, approval, preflight)) == 0
    assert {path: path.read_bytes() for path in before} == before


def test_regenerate_sidecar_success_and_post_validation(tmp_path: Path) -> None:
    root = tmp_path / "package"
    item = _item(root, "日本語/文書.md", status=ManifestStatus.SUCCEEDED)
    manifest, plan, approval, preflight = _contract(tmp_path, root, (item,))
    report_path = tmp_path / "execution.json"

    exit_code = cli.run(
        _args(root, manifest, plan, approval, preflight) + ["--report-json", str(report_path)]
    )

    sidecar = root / "日本語" / "文書.metadata.json"
    assert exit_code == 0
    assert sidecar.is_file()
    assert validate_package(root, manifest_path=manifest).exit_code == 0
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["artifact"] == {
        "bytes": item.output_digest.bytes,
        "sha256": item.output_digest.sha256,
    }
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["summary"] == {
        "planned": 1,
        "executed": 1,
        "succeeded": 1,
        "failed": 0,
        "rolled_back": 0,
        "not_run": 0,
    }
    assert report["post_validation"] == "passed"


def test_remove_stale_sidecar_success_with_verified_backup(tmp_path: Path) -> None:
    root = tmp_path / "package"
    succeeded = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    sidecar = _sidecar(root, succeeded)
    sidecar_bytes = sidecar.read_bytes()
    manifest, plan, approval, preflight = _contract(tmp_path, root, (_failed(succeeded),))

    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
    )

    assert report.exit_code == 0
    assert not sidecar.exists()
    assert report.actions[0].before.sha256 == hashlib.sha256(sidecar_bytes).hexdigest()
    assert report.actions[0].rollback == "available"
    assert validate_package(root, manifest_path=manifest).exit_code == 0


def test_explicit_backup_contains_identical_stale_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "package"
    succeeded = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    sidecar = _sidecar(root, succeeded)
    expected = sidecar.read_bytes()
    manifest, plan, approval, preflight = _contract(tmp_path, root, (_failed(succeeded),))
    backup_dir = tmp_path / "backup"
    monkeypatch.setattr(execution, "_repository_roots", lambda package_root: ())

    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
        backup_dir=backup_dir,
    )

    backups = tuple(backup_dir.rglob("*.bak"))
    session_manifests = tuple(backup_dir.rglob(SESSION_MANIFEST_FILENAME))
    assert report.exit_code == 0
    assert len(backups) == 1
    assert backups[0].read_bytes() == expected
    assert len(session_manifests) == 1
    session_manifest = parse_backup_session_manifest_bytes(session_manifests[0].read_bytes())
    assert session_manifest.state is BackupSessionState.COMPLETE
    assert session_manifest.items[0].source == "section/a.metadata.json"
    assert session_manifest.items[0].digest == digest_file(backups[0])


@pytest.mark.parametrize("change", ["sidecar", "markdown", "manifest"])
def test_regenerate_toctou_change_refuses_all_mutation(tmp_path: Path, change: str) -> None:
    root = tmp_path / "package"
    item = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    manifest, plan, approval, preflight = _contract(tmp_path, root, (item,))
    sidecar = root / "section" / "a.metadata.json"
    markdown = root / "section" / "a.md"
    if change == "sidecar":
        _sidecar(root, item)
    elif change == "markdown":
        markdown.write_text("changed\n", encoding="utf-8")
    else:
        write_artifact_manifest(
            manifest,
            ArtifactManifest(ArtifactManifestSettings(), (_failed(item),)),
        )
    before = markdown.read_bytes() if markdown.exists() else b""

    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
    )

    assert report.exit_code == 1
    assert report.actions[0].status == "failed-precondition"
    if change != "sidecar":
        assert not sidecar.exists()
    assert (markdown.read_bytes() if markdown.exists() else b"") == before


def test_stale_target_digest_change_is_not_deleted(tmp_path: Path) -> None:
    root = tmp_path / "package"
    succeeded = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    sidecar = _sidecar(root, succeeded)
    manifest, plan, approval, preflight = _contract(tmp_path, root, (_failed(succeeded),))
    sidecar.write_text('{"changed":true}\n', encoding="utf-8")
    changed = sidecar.read_bytes()

    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
    )

    assert report.exit_code == 1
    assert sidecar.read_bytes() == changed


def test_backup_failure_does_not_delete_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "package"
    succeeded = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    sidecar = _sidecar(root, succeeded)
    before = sidecar.read_bytes()
    manifest, plan, approval, preflight = _contract(tmp_path, root, (_failed(succeeded),))

    def fail_backup(target: Path, backup: Path, session_root: Path) -> ArtifactDigest:
        raise OSError("synthetic backup failure")

    monkeypatch.setattr(execution, "_backup_target", fail_backup)
    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
    )

    assert report.exit_code == 1
    assert sidecar.read_bytes() == before


def test_existing_backup_file_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "package"
    succeeded = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    sidecar = _sidecar(root, succeeded)
    target_before = sidecar.read_bytes()
    manifest, plan, approval, preflight = _contract(tmp_path, root, (_failed(succeeded),))
    session = tmp_path / "malicious-session"
    backup = session / "0000" / "section" / "a.metadata.json.bak"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"existing backup")
    monkeypatch.setattr(execution, "_prepare_backup_session", lambda package, root: session)

    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
    )

    assert report.exit_code == 1
    assert sidecar.read_bytes() == target_before
    assert backup.read_bytes() == b"existing backup"


def test_backup_final_symlink_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "package"
    succeeded = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    sidecar = _sidecar(root, succeeded)
    target_before = sidecar.read_bytes()
    manifest, plan, approval, preflight = _contract(tmp_path, root, (_failed(succeeded),))
    session = tmp_path / "malicious-session"
    backup = session / "0000" / "section" / "a.metadata.json.bak"
    backup.parent.mkdir(parents=True)
    outside = tmp_path / "outside-backup.bin"
    outside.write_bytes(b"outside unchanged")
    try:
        backup.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is not permitted")
    monkeypatch.setattr(execution, "_prepare_backup_session", lambda package, root: session)

    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
    )

    assert report.exit_code == 1
    assert sidecar.read_bytes() == target_before
    assert outside.read_bytes() == b"outside unchanged"


def test_backup_intermediate_link_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "package"
    succeeded = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    sidecar = _sidecar(root, succeeded)
    target_before = sidecar.read_bytes()
    manifest, plan, approval, preflight = _contract(tmp_path, root, (_failed(succeeded),))
    session = tmp_path / "malicious-session"
    session.mkdir()
    outside = tmp_path / "outside-directory"
    outside.mkdir()
    linked = session / "0000"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is not permitted")
    monkeypatch.setattr(execution, "_prepare_backup_session", lambda package, root: session)

    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
    )

    assert report.exit_code == 1
    assert sidecar.read_bytes() == target_before
    assert tuple(outside.iterdir()) == ()


def test_explicit_backup_root_uses_new_session_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "package"
    succeeded = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    _sidecar(root, succeeded)
    manifest, plan, approval, preflight = _contract(tmp_path, root, (_failed(succeeded),))
    backup_root = tmp_path / "backup-root"
    preexisting = backup_root / "unrelated.bin"
    preexisting.parent.mkdir()
    preexisting.write_bytes(b"preserve")
    monkeypatch.setattr(execution, "_repository_roots", lambda package_root: ())

    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
        backup_dir=backup_root,
    )

    sessions = tuple(backup_root.glob("knowledge-importer-repair-*"))
    assert report.exit_code == 0
    assert preexisting.read_bytes() == b"preserve"
    assert len(sessions) == 1
    assert sessions[0].is_dir()
    assert len(tuple(sessions[0].rglob("*.bak"))) == 1


def test_delete_failure_keeps_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "package"
    succeeded = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    sidecar = _sidecar(root, succeeded)
    before = sidecar.read_bytes()
    manifest, plan, approval, preflight = _contract(tmp_path, root, (_failed(succeeded),))
    monkeypatch.setattr(
        execution, "_delete_target", lambda target: (_ for _ in ()).throw(OSError())
    )

    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
    )

    assert report.exit_code == 1
    assert sidecar.read_bytes() == before


def test_session_manifest_failure_does_not_delete_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "package"
    succeeded = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    sidecar = _sidecar(root, succeeded)
    before = sidecar.read_bytes()
    manifest, plan, approval, preflight = _contract(tmp_path, root, (_failed(succeeded),))
    backup_root = tmp_path / "backup-root"
    monkeypatch.setattr(execution, "_repository_roots", lambda package_root: ())
    original_writer = execution.write_backup_session_manifest
    calls = 0

    def fail_item_record(path: Path, session: object, *, expected_current: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic manifest update failure")
        original_writer(path, session, expected_current=expected_current)  # type: ignore[arg-type]

    monkeypatch.setattr(execution, "write_backup_session_manifest", fail_item_record)

    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
        backup_dir=backup_root,
    )

    session_manifest_path = next(backup_root.rglob(SESSION_MANIFEST_FILENAME))
    session_manifest = parse_backup_session_manifest_bytes(session_manifest_path.read_bytes())
    assert report.exit_code == 1
    assert sidecar.read_bytes() == before
    assert session_manifest.state is BackupSessionState.OPEN
    assert session_manifest.items == ()


def test_multi_action_session_manifest_is_updated_atomically_and_deterministically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "package"
    first = _item(root, "a/first.md", status=ManifestStatus.SUCCEEDED)
    second = _item(root, "z/second.md", status=ManifestStatus.SUCCEEDED)
    first_sidecar = _sidecar(root, first)
    second_sidecar = _sidecar(root, second)
    manifest, plan, approval, preflight = _contract(
        tmp_path,
        root,
        (_failed(second), _failed(first)),
    )
    backup_root = tmp_path / "backup-root"
    monkeypatch.setattr(execution, "_repository_roots", lambda package_root: ())

    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
        backup_dir=backup_root,
    )

    session_manifest_path = next(backup_root.rglob(SESSION_MANIFEST_FILENAME))
    content = session_manifest_path.read_bytes()
    session_manifest = parse_backup_session_manifest_bytes(content)
    assert report.exit_code == 0
    assert not first_sidecar.exists()
    assert not second_sidecar.exists()
    assert session_manifest.state is BackupSessionState.COMPLETE
    assert [item.source for item in session_manifest.items] == [
        "a/first.metadata.json",
        "z/second.metadata.json",
    ]
    assert content.endswith(b"\n")


def test_backup_item_is_recorded_before_target_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "package"
    succeeded = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    sidecar = _sidecar(root, succeeded)
    manifest, plan, approval, preflight = _contract(tmp_path, root, (_failed(succeeded),))
    backup_root = tmp_path / "backup-root"
    monkeypatch.setattr(execution, "_repository_roots", lambda package_root: ())
    original_delete = execution._delete_target

    def assert_manifest_then_delete(target: Path) -> None:
        session_manifest = parse_backup_session_manifest_bytes(
            next(backup_root.rglob(SESSION_MANIFEST_FILENAME)).read_bytes()
        )
        assert session_manifest.state is BackupSessionState.OPEN
        assert [item.source for item in session_manifest.items] == ["section/a.metadata.json"]
        original_delete(target)

    monkeypatch.setattr(execution, "_delete_target", assert_manifest_then_delete)

    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
        backup_dir=backup_root,
    )

    assert report.exit_code == 0
    assert not sidecar.exists()


def test_complete_state_write_failure_rolls_back_deleted_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "package"
    succeeded = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    sidecar = _sidecar(root, succeeded)
    before = sidecar.read_bytes()
    manifest, plan, approval, preflight = _contract(tmp_path, root, (_failed(succeeded),))
    backup_root = tmp_path / "backup-root"
    monkeypatch.setattr(execution, "_repository_roots", lambda package_root: ())
    original_writer = execution.write_backup_session_manifest

    def reject_complete(
        path: Path,
        session: object,
        *,
        expected_current: object,
    ) -> None:
        if getattr(session, "state", None) is BackupSessionState.COMPLETE:
            raise OSError("synthetic completion failure")
        original_writer(path, session, expected_current=expected_current)  # type: ignore[arg-type]

    monkeypatch.setattr(execution, "write_backup_session_manifest", reject_complete)

    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
        backup_dir=backup_root,
    )

    session_manifest = parse_backup_session_manifest_bytes(
        next(backup_root.rglob(SESSION_MANIFEST_FILENAME)).read_bytes()
    )
    assert report.exit_code == 1
    assert report.post_validation == "failed"
    assert sidecar.read_bytes() == before
    assert report.actions[0].status == "rolled-back"
    assert session_manifest.state is BackupSessionState.ROLLED_BACK


def test_regenerated_sidecar_rolls_back_and_later_action_is_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "package"
    first = _item(root, "a/first.md", status=ManifestStatus.SUCCEEDED)
    second = _item(root, "z/second.md", status=ManifestStatus.SUCCEEDED)
    manifest, plan, approval, preflight = _contract(tmp_path, root, (second, first))
    original_writer = execution._write_new_sidecar
    calls = 0

    def fail_second(path: Path, sidecar: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic write failure")
        original_writer(path, sidecar)  # type: ignore[arg-type]

    monkeypatch.setattr(execution, "_write_new_sidecar", fail_second)
    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
    )

    assert report.exit_code == 1
    assert not (root / "a" / "first.metadata.json").exists()
    assert not (root / "z" / "second.metadata.json").exists()
    assert [action.status for action in report.actions] == ["rolled-back", "failed"]


def test_regenerate_sidecar_does_not_clobber_target_created_at_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "package"
    item = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    manifest, plan, approval, preflight = _contract(tmp_path, root, (item,))
    target = root / "section" / "a.metadata.json"
    external = b"external sidecar\n"
    original_link = execution.os.link

    def create_target_then_link(source: Path, destination: Path, **kwargs: object) -> None:
        Path(destination).write_bytes(external)
        original_link(source, destination, **kwargs)

    monkeypatch.setattr(execution.os, "link", create_target_then_link)
    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
    )

    assert report.exit_code == 1
    assert target.read_bytes() == external
    assert report.actions[0].status == "failed"


def test_fail_fast_marks_following_action_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "package"
    items = tuple(
        _item(root, f"{name}/document.md", status=ManifestStatus.SUCCEEDED)
        for name in ("a", "m", "z")
    )
    manifest, plan, approval, preflight = _contract(tmp_path, root, items)
    original_writer = execution._write_new_sidecar
    calls = 0

    def fail_second(path: Path, sidecar: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError
        original_writer(path, sidecar)  # type: ignore[arg-type]

    monkeypatch.setattr(execution, "_write_new_sidecar", fail_second)
    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
    )

    assert [action.status for action in report.actions] == [
        "rolled-back",
        "failed",
        "not-run",
    ]
    assert not (root / "z" / "document.metadata.json").exists()


def test_removed_stale_sidecar_is_restored_when_later_action_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "package"
    stale_source = _item(root, "a/stale.md", status=ManifestStatus.SUCCEEDED)
    stale = _sidecar(root, stale_source)
    stale_bytes = stale.read_bytes()
    missing = _item(root, "z/missing.md", status=ManifestStatus.SUCCEEDED)
    manifest, plan, approval, preflight = _contract(
        tmp_path, root, (_failed(stale_source), missing)
    )
    backup_root = tmp_path / "backup-root"
    monkeypatch.setattr(execution, "_repository_roots", lambda package_root: ())
    monkeypatch.setattr(
        execution,
        "_write_new_sidecar",
        lambda path, sidecar: (_ for _ in ()).throw(OSError()),
    )

    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
        backup_dir=backup_root,
    )

    session_manifest = parse_backup_session_manifest_bytes(
        next(backup_root.rglob(SESSION_MANIFEST_FILENAME)).read_bytes()
    )
    assert report.exit_code == 1
    assert stale.read_bytes() == stale_bytes
    assert [action.status for action in report.actions] == ["rolled-back", "failed"]
    assert session_manifest.state is BackupSessionState.ROLLED_BACK


def test_rollback_does_not_overwrite_external_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "package"
    stale_source = _item(root, "a/stale.md", status=ManifestStatus.SUCCEEDED)
    stale = _sidecar(root, stale_source)
    missing = _item(root, "z/missing.md", status=ManifestStatus.SUCCEEDED)
    manifest, plan, approval, preflight = _contract(
        tmp_path, root, (_failed(stale_source), missing)
    )
    backup_root = tmp_path / "backup-root"
    monkeypatch.setattr(execution, "_repository_roots", lambda package_root: ())

    def conflict_then_fail(path: Path, sidecar: object) -> None:
        stale.write_text("external\n", encoding="utf-8")
        raise OSError

    monkeypatch.setattr(execution, "_write_new_sidecar", conflict_then_fail)
    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
        backup_dir=backup_root,
    )

    session_manifest = parse_backup_session_manifest_bytes(
        next(backup_root.rglob(SESSION_MANIFEST_FILENAME)).read_bytes()
    )
    assert report.exit_code == 1
    assert stale.read_text(encoding="utf-8") == "external\n"
    assert report.actions[0].status == "rollback-failed"
    assert session_manifest.state is BackupSessionState.ROLLBACK_FAILED


def test_rollback_restore_does_not_clobber_target_created_at_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "package"
    stale_source = _item(root, "a/stale.md", status=ManifestStatus.SUCCEEDED)
    stale = _sidecar(root, stale_source)
    missing = _item(root, "z/missing.md", status=ManifestStatus.SUCCEEDED)
    manifest, plan, approval, preflight = _contract(
        tmp_path, root, (_failed(stale_source), missing)
    )
    external = b"external rollback target\n"
    original_link = execution.os.link

    def create_target_then_link(source: Path, destination: Path, **kwargs: object) -> None:
        if Path(destination) == stale:
            stale.write_bytes(external)
        original_link(source, destination, **kwargs)

    monkeypatch.setattr(execution.os, "link", create_target_then_link)
    monkeypatch.setattr(
        execution,
        "_write_new_sidecar",
        lambda path, sidecar: (_ for _ in ()).throw(OSError()),
    )
    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
    )

    assert report.exit_code == 1
    assert stale.read_bytes() == external
    assert report.actions[0].status == "rollback-failed"


def test_rollback_rejects_modified_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "package"
    stale_source = _item(root, "a/stale.md", status=ManifestStatus.SUCCEEDED)
    stale = _sidecar(root, stale_source)
    missing = _item(root, "z/missing.md", status=ManifestStatus.SUCCEEDED)
    manifest, plan, approval, preflight = _contract(
        tmp_path, root, (_failed(stale_source), missing)
    )
    captured: dict[str, Path] = {}
    original_backup = execution._backup_target
    original_delete = execution._delete_target

    def capture_backup(target: Path, backup: Path, session: Path) -> ArtifactDigest:
        digest = original_backup(target, backup, session)
        captured["path"] = backup
        return digest

    def delete_then_modify_backup(target: Path) -> None:
        original_delete(target)
        captured["path"].write_bytes(b"tampered backup\n")

    monkeypatch.setattr(execution, "_backup_target", capture_backup)
    monkeypatch.setattr(execution, "_delete_target", delete_then_modify_backup)
    monkeypatch.setattr(
        execution,
        "_write_new_sidecar",
        lambda path, sidecar: (_ for _ in ()).throw(OSError()),
    )
    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
    )

    assert report.exit_code == 1
    assert not stale.exists()
    assert report.actions[0].status == "rollback-failed"


def test_rollback_rejects_backup_replaced_by_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "package"
    stale_source = _item(root, "a/stale.md", status=ManifestStatus.SUCCEEDED)
    stale = _sidecar(root, stale_source)
    missing = _item(root, "z/missing.md", status=ManifestStatus.SUCCEEDED)
    manifest, plan, approval, preflight = _contract(
        tmp_path, root, (_failed(stale_source), missing)
    )
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside remains unchanged\n")
    outside_before = outside.read_bytes()
    captured: dict[str, Path] = {}
    original_backup = execution._backup_target
    original_delete = execution._delete_target

    def capture_backup(target: Path, backup: Path, session: Path) -> ArtifactDigest:
        digest = original_backup(target, backup, session)
        captured["path"] = backup
        return digest

    def delete_then_replace_backup(target: Path) -> None:
        original_delete(target)
        backup = captured["path"]
        backup.unlink()
        try:
            backup.symlink_to(outside)
        except OSError:
            pytest.skip("symlink creation is not permitted")

    monkeypatch.setattr(execution, "_backup_target", capture_backup)
    monkeypatch.setattr(execution, "_delete_target", delete_then_replace_backup)
    monkeypatch.setattr(
        execution,
        "_write_new_sidecar",
        lambda path, sidecar: (_ for _ in ()).throw(OSError()),
    )
    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
    )

    assert report.exit_code == 1
    assert not stale.exists()
    assert outside.read_bytes() == outside_before
    assert report.actions[0].status == "rollback-failed"


def test_post_validation_failure_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "package"
    item = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    manifest, plan, approval, preflight = _contract(tmp_path, root, (item,))
    issue = ValidationIssue(
        "section/a.metadata.json",
        ValidationSeverity.ERROR,
        "synthetic",
        "架空の検証失敗",
    )
    monkeypatch.setattr(
        execution,
        "validate_package",
        lambda package_root, manifest_path: PackageValidationResult((issue.path,), (issue,)),
    )

    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
    )

    assert report.exit_code == 1
    assert report.post_validation == "failed"
    assert report.actions[0].status == "rolled-back"
    assert not (root / "section" / "a.metadata.json").exists()


@pytest.mark.parametrize("target_name", ["plan", "approval", "preflight"])
def test_changed_contract_bytes_return_two_without_mutation(
    tmp_path: Path, target_name: str
) -> None:
    root = tmp_path / "package"
    item = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    manifest, plan, approval, preflight = _contract(tmp_path, root, (item,))
    target = {"plan": plan, "approval": approval, "preflight": preflight}[target_name]
    target.write_bytes(target.read_bytes() + b" ")

    assert cli.run(_args(root, manifest, plan, approval, preflight)) == 2
    assert not (root / "section" / "a.metadata.json").exists()


def test_blocked_preflight_returns_two(tmp_path: Path) -> None:
    root = tmp_path / "package"
    item = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    manifest, plan, approval, preflight = _contract(tmp_path, root, (item,))
    payload = json.loads(preflight.read_text(encoding="utf-8"))
    payload["summary"].update(ready=0, blocked=1)
    payload["actions"][0].update(status="blocked", block_reason="package-state-changed")
    payload["actions"][0]["preconditions"]["package_state_matches"] = False
    write_json_atomically(preflight, payload)

    assert cli.run(_args(root, manifest, plan, approval, preflight)) == 2


@pytest.mark.parametrize(
    ("action", "reason"),
    [
        ("regenerate-manifest", "extra-artifact"),
        ("verify-artifact", "artifact-digest-mismatch"),
        ("manual-review", "path-mismatch"),
    ],
)
def test_unsafe_and_manual_approval_cannot_execute(
    tmp_path: Path, action: str, reason: str
) -> None:
    root = tmp_path / "package"
    item = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    manifest, plan, approval, preflight = _contract(tmp_path, root, (item,))
    plan_sha = hashlib.sha256(plan.read_bytes()).hexdigest()
    write_json_atomically(
        approval,
        {
            "report_type": "knowledge-package-repair-approval",
            "schema_version": 1,
            "plan": {"sha256": plan_sha, "schema_version": 1},
            "scope": {"mode": "all-safe"},
            "approved_actions": [
                {
                    "path": "section/a.metadata.json",
                    "action": action,
                    "reason_category": reason,
                    "safe": False,
                }
            ],
        },
    )

    assert cli.run(_args(root, manifest, plan, approval, preflight)) == 2
    assert not (root / "section" / "a.metadata.json").exists()


def test_unapproved_preflight_action_returns_two(tmp_path: Path) -> None:
    root = tmp_path / "package"
    item = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    manifest, plan, approval, preflight = _contract(tmp_path, root, (item,))
    payload = json.loads(preflight.read_text(encoding="utf-8"))
    extra = json.loads(json.dumps(payload["actions"][0]))
    extra["path"] = "other/b.metadata.json"
    extra["target"]["path"] = "other/b.metadata.json"
    payload["actions"].append(extra)
    payload["summary"].update(actions=2, ready=2)
    write_json_atomically(preflight, payload)

    assert cli.run(_args(root, manifest, plan, approval, preflight)) == 2


def test_symlink_target_is_rejected_before_mutation(tmp_path: Path) -> None:
    root = tmp_path / "package"
    item = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    manifest, plan, approval, preflight = _contract(tmp_path, root, (item,))
    outside = tmp_path / "outside.json"
    outside.write_text("outside\n", encoding="utf-8")
    sidecar = root / "section" / "a.metadata.json"
    try:
        sidecar.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is not permitted")

    report = execution.execute_repair(
        root,
        manifest_path=manifest,
        plan_path=plan,
        approval_path=approval,
        preflight_path=preflight,
    )

    assert report.exit_code == 1
    assert outside.read_text(encoding="utf-8") == "outside\n"


def test_report_overwrite_and_other_report_protection(tmp_path: Path) -> None:
    root = tmp_path / "package"
    root.mkdir()
    manifest, plan, approval, preflight = _contract(tmp_path, root, ())
    report = tmp_path / "execution.json"
    arguments = _args(root, manifest, plan, approval, preflight) + [
        "--report-json",
        str(report),
    ]

    assert cli.run(arguments) == 0
    first = report.read_bytes()
    assert cli.run(arguments) == 0
    assert report.read_bytes() == first

    report.write_text('{"report_type":"other"}\n', encoding="utf-8")
    before = report.read_bytes()
    assert cli.run(arguments) == 2
    assert report.read_bytes() == before


def test_report_write_failure_does_not_rollback_successful_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "package"
    item = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    manifest, plan, approval, preflight = _contract(tmp_path, root, (item,))
    report = tmp_path / "execution.json"
    monkeypatch.setattr(
        cli,
        "write_execution_report",
        lambda path, result: (_ for _ in ()).throw(OSError("hidden local path")),
    )

    assert (
        cli.run(_args(root, manifest, plan, approval, preflight) + ["--report-json", str(report)])
        == 2
    )
    assert (root / "section" / "a.metadata.json").is_file()


def test_execution_keeps_contract_and_unrelated_files_byte_identical(tmp_path: Path) -> None:
    root = tmp_path / "package"
    item = _item(root, "section/a.md", status=ManifestStatus.SUCCEEDED)
    unrelated = root / "notes.txt"
    unrelated.write_text("unrelated synthetic content\n", encoding="utf-8")
    manifest, plan, approval, preflight = _contract(tmp_path, root, (item,))
    protected = (root / "section" / "a.md", unrelated, manifest, plan, approval, preflight)
    before = {path: path.read_bytes() for path in protected}

    assert cli.run(_args(root, manifest, plan, approval, preflight)) == 0

    assert {path: path.read_bytes() for path in protected} == before


def test_stderr_and_report_do_not_expose_machine_details(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "package"
    item = _item(root, "日本語/文書.md", status=ManifestStatus.SUCCEEDED)
    manifest, plan, approval, preflight = _contract(tmp_path, root, (item,))
    report = tmp_path / "execution.json"

    assert (
        cli.run(_args(root, manifest, plan, approval, preflight) + ["--report-json", str(report)])
        == 0
    )
    captured = capsys.readouterr()
    combined = captured.out + captured.err + report.read_text(encoding="utf-8")
    assert str(tmp_path) not in combined
    assert "Traceback" not in combined
    assert "shuns" not in combined
