from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

import knowledge_importer.backup_cleanup_execution as execution
import knowledge_importer.backup_inventory as inventory_module
import knowledge_importer.cli as cli
import knowledge_importer.operation_intent as intent_module
from knowledge_importer.artifact_manifest import ArtifactDigest
from knowledge_importer.backup_cleanup_approval import (
    build_backup_cleanup_approval,
    write_backup_cleanup_approval,
)
from knowledge_importer.backup_cleanup_execution import (
    BackupCleanupAudit,
    BackupCleanupAuditStatus,
    parse_backup_cleanup_audit_bytes,
    verify_backup_cleanup_intent,
    write_backup_cleanup_audit,
)
from knowledge_importer.backup_cleanup_plan import (
    build_backup_cleanup_plan,
    write_backup_cleanup_plan,
)
from knowledge_importer.backup_inventory import (
    MANAGED_SESSION_PREFIX,
    SESSION_MANIFEST_FILENAME,
    BackupSessionBindings,
    BackupSessionItem,
    BackupSessionManifest,
    BackupSessionState,
    build_backup_inventory,
    write_backup_inventory,
    write_backup_session_manifest,
)
from knowledge_importer.operation_intent import (
    BACKUP_CLEANUP,
    operation_intent_sha256,
    parse_operation_intent_bytes,
)


@pytest.fixture(autouse=True)
def _ignore_workspace_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inventory_module, "repository_roots", lambda path: ())
    monkeypatch.setattr(execution, "repository_roots", lambda path: ())
    monkeypatch.setattr(cli, "repository_roots", lambda path: ())


def _session_name(suffix: str) -> str:
    return f"{MANAGED_SESSION_PREFIX}{suffix}"


def _managed_session(
    backup_root: Path,
    suffix: str,
    *,
    contents: tuple[bytes, ...] = (b"synthetic backup\n",),
    state: BackupSessionState = BackupSessionState.COMPLETE,
) -> Path:
    session = backup_root / _session_name(suffix)
    session.mkdir(parents=True)
    items: list[BackupSessionItem] = []
    for index, content in enumerate(contents):
        source = f"documents/item-{index}.metadata.json"
        backup = f"{index:04d}/{source}.bak"
        path = session / Path(*backup.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        items.append(
            BackupSessionItem(
                source,
                backup,
                ArtifactDigest(len(content), hashlib.sha256(content).hexdigest()),
            )
        )
    manifest = BackupSessionManifest(
        state,
        BackupSessionBindings("a" * 64, "b" * 64, "c" * 64, "d" * 64),
        tuple(items),
    )
    write_backup_session_manifest(
        session / SESSION_MANIFEST_FILENAME,
        manifest,
        expected_current=None,
    )
    return session


def _lifecycle(
    tmp_path: Path,
    suffixes: tuple[str, ...] = ("alpha",),
    *,
    contents: tuple[bytes, ...] = (b"synthetic backup\n",),
    states: dict[str, BackupSessionState] | None = None,
) -> tuple[Path, Path, Path, Path, Path, Path]:
    package_root = tmp_path / "package"
    backup_root = tmp_path / "backups"
    reports = tmp_path / "reports"
    package_root.mkdir()
    backup_root.mkdir()
    reports.mkdir()
    for suffix in suffixes:
        _managed_session(
            backup_root,
            suffix,
            contents=contents,
            state=(states or {}).get(suffix, BackupSessionState.COMPLETE),
        )

    inventory_path = reports / "inventory.json"
    plan_path = reports / "plan.json"
    approval_path = reports / "approval.json"
    audit_path = reports / "audit.json"
    inventory = build_backup_inventory(package_root, backup_root)
    write_backup_inventory(inventory_path, inventory)
    plan = build_backup_cleanup_plan(
        inventory_path,
        tuple(_session_name(suffix) for suffix in suffixes),
    )
    write_backup_cleanup_plan(plan_path, plan)
    approval = build_backup_cleanup_approval(plan_path)
    write_backup_cleanup_approval(approval_path, approval)
    return backup_root, inventory_path, plan_path, approval_path, audit_path, package_root


def _args(
    backup_root: Path,
    inventory_path: Path,
    plan_path: Path,
    approval_path: Path,
    audit_path: Path,
    package_root: Path,
) -> list[str]:
    return [
        "backup-cleanup-execute",
        str(backup_root),
        "--package-root",
        str(package_root),
        "--inventory",
        str(inventory_path),
        "--plan",
        str(plan_path),
        "--approval",
        str(approval_path),
        "--report-json",
        str(audit_path),
    ]


def _run_lifecycle(paths: tuple[Path, Path, Path, Path, Path, Path]) -> int:
    return cli.run(_args(*paths))


def _receipted_args(
    paths: tuple[Path, Path, Path, Path, Path, Path],
    receipt_path: Path,
    *,
    attempt_id: str = "cleanup-attempt-001",
) -> list[str]:
    return [
        *_args(*paths),
        "--intent-receipt",
        str(receipt_path),
        "--attempt-id",
        attempt_id,
    ]


def _run_receipted(
    paths: tuple[Path, Path, Path, Path, Path, Path],
    receipt_path: Path,
    *,
    attempt_id: str = "cleanup-attempt-001",
) -> int:
    return cli.run(_receipted_args(paths, receipt_path, attempt_id=attempt_id))


def _audit(path: Path) -> BackupCleanupAudit:
    return parse_backup_cleanup_audit_bytes(path.read_bytes())


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def test_single_approved_session_is_deleted_with_deterministic_audit(
    tmp_path: Path,
) -> None:
    paths = _lifecycle(tmp_path)
    backup_root, _, _, _, audit_path, package_root = paths
    package_file = package_root / "document.md"
    package_file.write_bytes(b"package bytes")

    assert _run_lifecycle(paths) == 0

    assert backup_root.is_dir()
    assert not (backup_root / _session_name("alpha")).exists()
    assert package_file.read_bytes() == b"package bytes"
    audit = _audit(audit_path)
    assert audit.actions[0].status is BackupCleanupAuditStatus.DELETED
    assert audit.actions[0].after_exists is False
    assert audit.payload()["summary"] == {
        "planned": 1,
        "deleted": 1,
        "failed": 0,
        "not_run": 0,
    }


def test_multiple_sessions_are_deleted_in_canonical_order(tmp_path: Path) -> None:
    paths = _lifecycle(tmp_path, ("zulu", "alpha"))

    assert _run_lifecycle(paths) == 0

    audit = _audit(paths[4])
    assert [action.session for action in audit.actions] == [
        _session_name("alpha"),
        _session_name("zulu"),
    ]
    assert all(action.status is BackupCleanupAuditStatus.DELETED for action in audit.actions)


@pytest.mark.parametrize("bound_input", ["inventory", "plan", "approval"])
def test_exact_input_tamper_is_rejected_before_deletion(
    tmp_path: Path,
    bound_input: str,
) -> None:
    paths = _lifecycle(tmp_path)
    indexes = {"inventory": 1, "plan": 2, "approval": 3}
    target = paths[indexes[bound_input]]
    if bound_input == "approval":
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["approved_actions"][0]["backup_bytes"] += 1
        target.write_text(json.dumps(payload), encoding="utf-8")
    else:
        target.write_bytes(target.read_bytes().replace(b"\n", b"\r\n"))

    assert _run_lifecycle(paths) == 2
    assert (paths[0] / _session_name("alpha")).is_dir()
    assert not paths[4].exists()


@pytest.mark.parametrize("mutation", ["manifest", "backup", "extra-file", "extra-directory"])
def test_session_change_is_failed_without_deleting_session(
    tmp_path: Path,
    mutation: str,
) -> None:
    paths = _lifecycle(tmp_path)
    session = paths[0] / _session_name("alpha")
    if mutation == "manifest":
        (session / SESSION_MANIFEST_FILENAME).write_bytes(b"tampered")
    elif mutation == "backup":
        next(session.rglob("*.bak")).write_bytes(b"tampered")
    elif mutation == "extra-file":
        (session / "unexpected.bin").write_bytes(b"unexpected")
    else:
        (session / "unexpected").mkdir()

    assert _run_lifecycle(paths) == 1

    audit = _audit(paths[4])
    assert audit.actions[0].status is BackupCleanupAuditStatus.FAILED
    assert audit.actions[0].after_exists is True
    assert session.exists()


def test_blocked_session_is_not_in_approval_scope_or_deleted(tmp_path: Path) -> None:
    paths = _lifecycle(
        tmp_path,
        ("alpha", "rolled-back"),
        states={"rolled-back": BackupSessionState.ROLLED_BACK},
    )

    assert _run_lifecycle(paths) == 0

    audit = _audit(paths[4])
    assert [action.session for action in audit.actions] == [_session_name("alpha")]
    assert (paths[0] / _session_name("rolled-back")).is_dir()


def test_failure_stops_later_sessions_without_restoring_deleted_session(
    tmp_path: Path,
) -> None:
    paths = _lifecycle(tmp_path, ("alpha", "middle", "zulu"))
    failed_session = paths[0] / _session_name("middle")
    (failed_session / "unexpected.bin").write_bytes(b"race")

    assert _run_lifecycle(paths) == 1

    audit = _audit(paths[4])
    assert [action.status for action in audit.actions] == [
        BackupCleanupAuditStatus.DELETED,
        BackupCleanupAuditStatus.FAILED,
        BackupCleanupAuditStatus.NOT_RUN,
    ]
    assert not (paths[0] / _session_name("alpha")).exists()
    assert failed_session.exists()
    assert (paths[0] / _session_name("zulu")).exists()


def test_partial_file_deletion_failure_has_no_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _lifecycle(tmp_path, contents=(b"first", b"second"))
    session = paths[0] / _session_name("alpha")
    first = session / "0000/documents/item-0.metadata.json.bak"
    second = session / "0001/documents/item-1.metadata.json.bak"
    original_unlink = Path.unlink

    def fail_second(path: Path, *args: object, **kwargs: object) -> None:
        if path == second:
            raise PermissionError("synthetic denial")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_second)

    assert _run_lifecycle(paths) == 1
    assert not first.exists()
    assert second.exists()
    assert _audit(paths[4]).actions[0].status is BackupCleanupAuditStatus.FAILED


def test_file_identity_swap_immediately_before_delete_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _lifecycle(tmp_path)
    original = execution._digest_regular_file
    swapped = False

    def swap_after_digest(path: Path):  # type: ignore[no-untyped-def]
        nonlocal swapped
        result = original(path)
        if path.suffix == ".bak" and not swapped:
            swapped = True
            content = path.read_bytes()
            path.unlink()
            path.write_bytes(content)
        return result

    monkeypatch.setattr(execution, "_digest_regular_file", swap_after_digest)

    assert _run_lifecycle(paths) == 1
    assert swapped
    assert next((paths[0] / _session_name("alpha")).rglob("*.bak")).exists()


def test_extra_entry_injected_after_action_validation_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _lifecycle(tmp_path)
    session = paths[0] / _session_name("alpha")
    original = execution._validate_actual_session

    def inject_extra(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        result = original(*args, **kwargs)  # type: ignore[arg-type]
        (session / "injected.bin").write_bytes(b"race")
        return result

    monkeypatch.setattr(execution, "_validate_actual_session", inject_extra)

    assert _run_lifecycle(paths) == 1
    assert (session / "injected.bin").read_bytes() == b"race"
    assert next(session.rglob("*.bak")).exists()


def test_session_directory_identity_swap_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _lifecycle(tmp_path)
    session = paths[0] / _session_name("alpha")
    holding = tmp_path / "original-session"
    original = execution._capture_session_directory_identities
    calls = 0

    def swap_after_capture(path: Path, items: tuple[BackupSessionItem, ...]):
        nonlocal calls
        result = original(path, items)
        calls += 1
        if calls == 1:
            path.rename(holding)
            shutil.copytree(holding, path)
        return result

    monkeypatch.setattr(execution, "_capture_session_directory_identities", swap_after_capture)

    assert _run_lifecycle(paths) == 1
    assert session.exists()
    assert holding.exists()


def test_linked_backup_file_is_rejected_without_following_target(tmp_path: Path) -> None:
    paths = _lifecycle(tmp_path)
    session = paths[0] / _session_name("alpha")
    backup = next(session.rglob("*.bak"))
    external = tmp_path / "external.bin"
    external.write_bytes(b"external")
    backup.unlink()
    try:
        backup.symlink_to(external)
    except OSError:
        pytest.skip("symlink creation is not permitted")

    assert _run_lifecycle(paths) == 1
    assert external.read_bytes() == b"external"
    assert backup.is_symlink()


def test_intermediate_reparse_detection_stops_before_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _lifecycle(tmp_path)
    session = paths[0] / _session_name("alpha")
    intermediate = session / "0000"
    original = execution.is_link_or_reparse

    monkeypatch.setattr(
        execution,
        "is_link_or_reparse",
        lambda path: path == intermediate or original(path),
    )

    assert _run_lifecycle(paths) == 1
    assert next(session.rglob("*.bak")).exists()


def test_declared_files_manifest_and_directories_use_safe_deletion_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _lifecycle(tmp_path, contents=(b"first", b"second"))
    session = paths[0] / _session_name("alpha")
    deleted_files: list[str] = []
    deleted_directories: list[str] = []
    original_file_delete = execution._delete_verified_file
    original_directory_delete = execution._remove_verified_empty_directory

    def record_file(path: Path, *args: object, **kwargs: object) -> None:
        deleted_files.append(path.relative_to(session).as_posix())
        original_file_delete(path, *args, **kwargs)  # type: ignore[arg-type]

    def record_directory(path: Path, *args: object, **kwargs: object) -> None:
        deleted_directories.append("." if path == session else path.relative_to(session).as_posix())
        original_directory_delete(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(execution, "_delete_verified_file", record_file)
    monkeypatch.setattr(execution, "_remove_verified_empty_directory", record_directory)

    assert _run_lifecycle(paths) == 0
    assert deleted_files[-1] == SESSION_MANIFEST_FILENAME
    assert all(name.endswith(".bak") for name in deleted_files[:-1])
    assert deleted_directories[-1] == "."
    assert paths[0].is_dir()


def test_audit_write_failure_does_not_restore_deleted_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _lifecycle(tmp_path)

    def fail_write(path: Path, audit: object) -> None:
        raise OSError("synthetic report failure")

    monkeypatch.setattr(cli, "write_backup_cleanup_audit", fail_write)

    assert _run_lifecycle(paths) == 2
    assert not (paths[0] / _session_name("alpha")).exists()
    assert not paths[4].exists()


def test_foreign_audit_is_preserved_and_execution_does_not_start(tmp_path: Path) -> None:
    paths = _lifecycle(tmp_path)
    paths[4].write_bytes(b"foreign report")

    assert _run_lifecycle(paths) == 2
    assert paths[4].read_bytes() == b"foreign report"
    assert (paths[0] / _session_name("alpha")).exists()


def test_existing_valid_audit_is_rejected_before_execution(tmp_path: Path) -> None:
    paths = _lifecycle(tmp_path)
    existing = BackupCleanupAudit("1" * 64, "2" * 64, "3" * 64, ())
    write_backup_cleanup_audit(paths[4], existing)
    before = paths[4].read_bytes()

    assert _run_lifecycle(paths) == 2
    assert paths[4].read_bytes() == before
    assert (paths[0] / _session_name("alpha")).exists()


def test_existing_audit_directory_is_rejected_before_execution(tmp_path: Path) -> None:
    paths = _lifecycle(tmp_path)
    paths[4].mkdir()

    assert _run_lifecycle(paths) == 2
    assert paths[4].is_dir()
    assert (paths[0] / _session_name("alpha")).exists()


def test_existing_audit_symlink_is_rejected_without_following_target(tmp_path: Path) -> None:
    paths = _lifecycle(tmp_path)
    target = tmp_path / "foreign-audit.json"
    target.write_bytes(b"foreign report")
    try:
        paths[4].symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is not permitted")

    assert _run_lifecycle(paths) == 2
    assert target.read_bytes() == b"foreign report"
    assert paths[4].is_symlink()
    assert (paths[0] / _session_name("alpha")).exists()


def test_existing_audit_reparse_is_rejected_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _lifecycle(tmp_path)
    paths[4].write_bytes(b"foreign report")
    original = cli.path_uses_link_or_reparse
    monkeypatch.setattr(
        cli,
        "path_uses_link_or_reparse",
        lambda path: path == paths[4] or original(path),
    )

    assert _run_lifecycle(paths) == 2
    assert paths[4].read_bytes() == b"foreign report"
    assert (paths[0] / _session_name("alpha")).exists()


def test_inputs_and_audit_must_be_outside_backup_root(tmp_path: Path) -> None:
    paths = _lifecycle(tmp_path)
    inside = paths[0] / "audit.json"

    assert cli.run(_args(paths[0], paths[1], paths[2], paths[3], inside, paths[5])) == 2
    assert (paths[0] / _session_name("alpha")).exists()


def test_audit_must_be_outside_package_root(tmp_path: Path) -> None:
    paths = _lifecycle(tmp_path)
    inside = paths[5] / "cleanup-audit.json"
    before = _snapshot(paths[5])

    assert cli.run(_args(paths[0], paths[1], paths[2], paths[3], inside, paths[5])) == 2
    assert _snapshot(paths[5]) == before
    assert not inside.exists()
    assert (paths[0] / _session_name("alpha")).exists()


def test_backup_root_inside_repository_is_rejected_before_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _lifecycle(tmp_path)
    monkeypatch.setattr(cli, "repository_roots", lambda path: (tmp_path.resolve(),))

    assert _run_lifecycle(paths) == 2
    assert (paths[0] / _session_name("alpha")).exists()
    assert not paths[4].exists()


@pytest.mark.parametrize("layout", ["equal", "backup-inside-package", "package-inside-backup"])
def test_package_and_backup_overlap_is_rejected_without_mutation(
    tmp_path: Path,
    layout: str,
) -> None:
    paths = _lifecycle(tmp_path)
    backup_root, inventory_path, plan_path, approval_path, audit_path, package_root = paths
    if layout == "equal":
        package_root = backup_root
    elif layout == "backup-inside-package":
        moved = package_root / "backups"
        backup_root.rename(moved)
        backup_root = moved
    else:
        moved = backup_root / "package"
        package_root.rename(moved)
        package_root = moved
    before_backup = _snapshot(backup_root)
    before_package = _snapshot(package_root)

    assert (
        cli.run(
            _args(
                backup_root,
                inventory_path,
                plan_path,
                approval_path,
                audit_path,
                package_root,
            )
        )
        == 2
    )
    assert _snapshot(backup_root) == before_backup
    assert _snapshot(package_root) == before_package
    assert not audit_path.exists()


def test_backup_root_inside_detected_git_repository_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _lifecycle(tmp_path)
    before = _snapshot(paths[0])
    repository = tmp_path.resolve()
    monkeypatch.setattr(inventory_module, "repository_roots", lambda path: (repository,))
    monkeypatch.setattr(execution, "repository_roots", lambda path: (repository,))
    monkeypatch.setattr(cli, "repository_roots", lambda path: (repository,))

    assert _run_lifecycle(paths) == 2
    assert _snapshot(paths[0]) == before
    assert not paths[4].exists()


@pytest.mark.parametrize("unsafe_root", ["package", "backup"])
def test_root_reparse_is_rejected_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_root: str,
) -> None:
    paths = _lifecycle(tmp_path)
    target = paths[5] if unsafe_root == "package" else paths[0]
    before = _snapshot(paths[0])
    original = inventory_module.is_link_or_reparse
    monkeypatch.setattr(
        inventory_module,
        "is_link_or_reparse",
        lambda path: path.absolute() == target.absolute() or original(path),
    )

    assert _run_lifecycle(paths) == 2
    assert _snapshot(paths[0]) == before
    assert not paths[4].exists()


@pytest.mark.parametrize("linked_root", ["package", "backup"])
def test_root_symlink_is_rejected_without_following_target(
    tmp_path: Path,
    linked_root: str,
) -> None:
    paths = _lifecycle(tmp_path)
    backup_root = paths[0]
    package_root = paths[5]
    target = package_root if linked_root == "package" else backup_root
    alias = tmp_path / f"{linked_root}-link"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is not permitted")
    if linked_root == "package":
        package_root = alias
    else:
        backup_root = alias
    before_backup = _snapshot(paths[0])
    before_package = _snapshot(paths[5])

    assert (
        cli.run(
            _args(
                backup_root,
                paths[1],
                paths[2],
                paths[3],
                paths[4],
                package_root,
            )
        )
        == 2
    )
    assert _snapshot(paths[0]) == before_backup
    assert _snapshot(paths[5]) == before_package
    assert not paths[4].exists()


def test_valid_external_roots_cleanup_and_keep_package_byte_identical(tmp_path: Path) -> None:
    paths = _lifecycle(tmp_path)
    package_file = paths[5] / "knowledge.md"
    package_file.write_bytes(b"immutable package content")
    before = _snapshot(paths[5])

    assert _run_lifecycle(paths) == 0
    assert _snapshot(paths[5]) == before
    assert paths[0].is_dir()
    assert not (paths[0] / _session_name("alpha")).exists()


def test_concurrent_foreign_audit_creation_is_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _lifecycle(tmp_path)
    original_link = execution.os.link
    concurrent = b"concurrent foreign report"

    def create_foreign_before_commit(
        source: Path,
        destination: Path,
        *,
        follow_symlinks: bool,
    ) -> None:
        Path(destination).write_bytes(concurrent)
        original_link(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(execution.os, "link", create_foreign_before_commit)

    assert _run_lifecycle(paths) == 2
    assert paths[4].read_bytes() == concurrent
    assert not list(paths[4].parent.glob(f".{paths[4].name}.*.tmp"))
    assert not (paths[0] / _session_name("alpha")).exists()


def test_console_and_audit_do_not_expose_absolute_paths_or_tracebacks(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _lifecycle(tmp_path)
    (paths[0] / _session_name("alpha") / "unexpected.bin").write_bytes(b"failure")

    assert _run_lifecycle(paths) == 1

    output = capsys.readouterr()
    combined = output.out + output.err + paths[4].read_text(encoding="utf-8")
    assert str(tmp_path) not in combined
    assert "Traceback" not in combined
    assert _session_name("alpha") in combined


def test_empty_approval_produces_empty_audit_without_deleting_blocked_session(
    tmp_path: Path,
) -> None:
    paths = _lifecycle(
        tmp_path,
        ("rolled-back",),
        states={"rolled-back": BackupSessionState.ROLLED_BACK},
    )

    assert _run_lifecycle(paths) == 0
    first = paths[4].read_bytes()
    retry_audit = paths[4].with_name("audit-retry.json")
    retry_paths = (*paths[:4], retry_audit, paths[5])
    assert _run_lifecycle(retry_paths) == 0
    assert retry_audit.read_bytes() == first
    assert _audit(paths[4]).actions == ()
    assert (paths[0] / _session_name("rolled-back")).exists()


def test_receipted_cleanup_binds_exact_receipt_and_inputs(tmp_path: Path) -> None:
    paths = _lifecycle(tmp_path)
    receipt_path = tmp_path / "reports" / "intent.json"

    assert _run_receipted(paths, receipt_path) == 0

    receipt_content = receipt_path.read_bytes()
    receipt = parse_operation_intent_bytes(receipt_content)
    audit_content = paths[4].read_bytes()
    audit = parse_backup_cleanup_audit_bytes(audit_content)
    assert receipt.operation_type == BACKUP_CLEANUP
    assert [action.target for action in receipt.actions] == [_session_name("alpha")]
    assert audit.intent_receipt is not None
    assert audit.intent_receipt.attempt_id == "cleanup-attempt-001"
    assert audit.intent_receipt.sha256 == operation_intent_sha256(receipt_content)
    verify_backup_cleanup_intent(
        receipt_content,
        audit_content,
        inventory_content=paths[1].read_bytes(),
        plan_content=paths[2].read_bytes(),
        approval_content=paths[3].read_bytes(),
    )


def test_receipted_cleanup_receipt_is_deterministic_for_same_scope(tmp_path: Path) -> None:
    (tmp_path / "first").mkdir()
    (tmp_path / "second").mkdir()
    first = _lifecycle(tmp_path / "first")
    second = _lifecycle(tmp_path / "second")
    first_receipt = tmp_path / "first" / "reports" / "intent.json"
    second_receipt = tmp_path / "second" / "reports" / "intent.json"

    assert _run_receipted(first, first_receipt) == 0
    assert _run_receipted(second, second_receipt) == 0
    assert first_receipt.read_bytes() == second_receipt.read_bytes()


def test_receipted_cleanup_deletes_multiple_sessions_in_canonical_order(
    tmp_path: Path,
) -> None:
    paths = _lifecycle(tmp_path, ("zulu", "alpha"))
    receipt_path = tmp_path / "reports" / "intent.json"

    assert _run_receipted(paths, receipt_path) == 0

    assert [
        action.target for action in parse_operation_intent_bytes(receipt_path.read_bytes()).actions
    ] == [
        _session_name("alpha"),
        _session_name("zulu"),
    ]
    assert all(
        action.status is BackupCleanupAuditStatus.DELETED for action in _audit(paths[4]).actions
    )


@pytest.mark.parametrize("option", ["attempt-only", "receipt-only"])
def test_receipted_cleanup_options_must_be_paired(tmp_path: Path, option: str) -> None:
    paths = _lifecycle(tmp_path)
    receipt_path = tmp_path / "reports" / "intent.json"
    args = _args(*paths)
    args.extend(
        ["--attempt-id", "cleanup-attempt-001"]
        if option == "attempt-only"
        else ["--intent-receipt", str(receipt_path)]
    )

    assert cli.run(args) == 2
    assert (paths[0] / _session_name("alpha")).exists()
    assert not receipt_path.exists()
    assert not paths[4].exists()


@pytest.mark.parametrize("conflict", ["audit", "inventory", "plan", "approval"])
def test_receipt_path_cannot_conflict_with_lifecycle_artifact(
    tmp_path: Path,
    conflict: str,
) -> None:
    paths = _lifecycle(tmp_path)
    indexes = {"inventory": 1, "plan": 2, "approval": 3, "audit": 4}
    receipt_path = paths[indexes[conflict]]

    assert _run_receipted(paths, receipt_path) == 2
    assert (paths[0] / _session_name("alpha")).exists()
    assert not paths[4].exists()


@pytest.mark.parametrize("root_index", [0, 5])
def test_receipt_path_cannot_be_inside_backup_or_package_root(
    tmp_path: Path,
    root_index: int,
) -> None:
    paths = _lifecycle(tmp_path)
    receipt_path = paths[root_index] / "intent.json"

    assert _run_receipted(paths, receipt_path) == 2
    assert (paths[0] / _session_name("alpha")).exists()
    assert not receipt_path.exists()
    assert not paths[4].exists()


def test_existing_receipt_is_preserved_before_deletion(tmp_path: Path) -> None:
    paths = _lifecycle(tmp_path)
    receipt_path = tmp_path / "reports" / "intent.json"
    receipt_path.write_bytes(b"foreign receipt")

    assert _run_receipted(paths, receipt_path) == 2
    assert receipt_path.read_bytes() == b"foreign receipt"
    assert (paths[0] / _session_name("alpha")).exists()
    assert not paths[4].exists()


def test_concurrent_receipt_writer_is_preserved_before_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _lifecycle(tmp_path)
    receipt_path = tmp_path / "reports" / "intent.json"
    original_link = intent_module.os.link
    concurrent = b"concurrent receipt"

    def create_foreign_before_commit(
        source: Path,
        destination: Path,
        *,
        follow_symlinks: bool,
    ) -> None:
        Path(destination).write_bytes(concurrent)
        original_link(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(intent_module.os, "link", create_foreign_before_commit)

    assert _run_receipted(paths, receipt_path) == 2
    assert receipt_path.read_bytes() == concurrent
    assert (paths[0] / _session_name("alpha")).exists()
    assert not paths[4].exists()


@pytest.mark.parametrize("input_index", [1, 2, 3])
def test_input_change_after_receipt_stops_before_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    input_index: int,
) -> None:
    paths = _lifecycle(tmp_path)
    receipt_path = tmp_path / "reports" / "intent.json"
    original_write = execution.write_operation_intent

    def write_then_change(path: Path, receipt: object) -> None:
        original_write(path, receipt)  # type: ignore[arg-type]
        target = paths[input_index]
        target.write_bytes(target.read_bytes().replace(b"{", b"{ ", 1))

    monkeypatch.setattr(execution, "write_operation_intent", write_then_change)

    assert _run_receipted(paths, receipt_path) == 2
    assert receipt_path.exists()
    assert (paths[0] / _session_name("alpha")).exists()
    assert not paths[4].exists()


@pytest.mark.parametrize("input_index", [1, 2, 3])
def test_input_change_after_post_receipt_rebind_stops_before_first_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    input_index: int,
) -> None:
    paths = _lifecycle(tmp_path)
    receipt_path = tmp_path / "reports" / "intent.json"
    original_verify = execution._verify_receipted_output_state
    calls = 0
    changed_bytes = b""

    def verify_then_change(*args: object, **kwargs: object) -> None:
        nonlocal calls, changed_bytes
        calls += 1
        original_verify(*args, **kwargs)  # type: ignore[arg-type]
        if calls == 2:
            target = paths[input_index]
            changed_bytes = target.read_bytes().replace(b"{", b"{ ", 1)
            target.write_bytes(changed_bytes)

    monkeypatch.setattr(execution, "_verify_receipted_output_state", verify_then_change)

    assert _run_receipted(paths, receipt_path) == 2
    assert calls == 2
    assert receipt_path.exists()
    assert paths[input_index].read_bytes() == changed_bytes
    assert (paths[0] / _session_name("alpha")).exists()
    assert not paths[4].exists()


def test_session_change_after_receipt_is_failed_without_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _lifecycle(tmp_path)
    receipt_path = tmp_path / "reports" / "intent.json"
    backup = next((paths[0] / _session_name("alpha")).rglob("*.bak"))
    original_write = execution.write_operation_intent

    def write_then_change(path: Path, receipt: object) -> None:
        original_write(path, receipt)  # type: ignore[arg-type]
        backup.write_bytes(b"tampered after receipt")

    monkeypatch.setattr(execution, "write_operation_intent", write_then_change)

    assert _run_receipted(paths, receipt_path) == 1
    assert receipt_path.exists()
    assert backup.exists()
    audit = _audit(paths[4])
    assert audit.intent_receipt is not None
    assert audit.actions[0].status is BackupCleanupAuditStatus.FAILED


def test_root_boundary_change_after_receipt_stops_before_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _lifecycle(tmp_path)
    receipt_path = tmp_path / "reports" / "intent.json"
    calls = 0

    def roots(path: Path) -> tuple[Path, ...]:
        nonlocal calls
        calls += 1
        return () if calls == 1 else (tmp_path.resolve(),)

    monkeypatch.setattr(execution, "repository_roots", roots)

    assert _run_receipted(paths, receipt_path) == 1
    assert receipt_path.exists()
    assert (paths[0] / _session_name("alpha")).exists()
    assert _audit(paths[4]).actions[0].status is BackupCleanupAuditStatus.FAILED


def test_audit_path_change_after_receipt_stops_before_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _lifecycle(tmp_path)
    receipt_path = tmp_path / "reports" / "intent.json"
    original_write = execution.write_operation_intent
    foreign = b"concurrent audit"

    def write_then_claim_audit(path: Path, receipt: object) -> None:
        original_write(path, receipt)  # type: ignore[arg-type]
        paths[4].write_bytes(foreign)

    monkeypatch.setattr(execution, "write_operation_intent", write_then_claim_audit)

    assert _run_receipted(paths, receipt_path) == 2
    assert receipt_path.exists()
    assert paths[4].read_bytes() == foreign
    assert (paths[0] / _session_name("alpha")).exists()


def test_receipted_partial_failure_keeps_receipt_and_does_not_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _lifecycle(tmp_path, contents=(b"first", b"second"))
    receipt_path = tmp_path / "reports" / "intent.json"
    session = paths[0] / _session_name("alpha")
    first = session / "0000/documents/item-0.metadata.json.bak"
    second = session / "0001/documents/item-1.metadata.json.bak"
    original_unlink = Path.unlink

    def fail_second(path: Path, *args: object, **kwargs: object) -> None:
        if path == second:
            raise PermissionError("synthetic denial")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_second)

    assert _run_receipted(paths, receipt_path) == 1
    assert receipt_path.exists()
    assert not first.exists()
    assert second.exists()
    assert _audit(paths[4]).actions[0].status is BackupCleanupAuditStatus.FAILED


def test_receipted_second_session_race_keeps_prior_deletion_and_stops_later(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _lifecycle(tmp_path, ("alpha", "middle", "zulu"))
    receipt_path = tmp_path / "reports" / "intent.json"
    middle = paths[0] / _session_name("middle")
    original = execution._validate_actual_session
    calls = 0

    def inject_during_second_action(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 5:
            (middle / "race.bin").write_bytes(b"race")
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(execution, "_validate_actual_session", inject_during_second_action)

    assert _run_receipted(paths, receipt_path) == 1
    assert receipt_path.exists()
    assert [action.status for action in _audit(paths[4]).actions] == [
        BackupCleanupAuditStatus.DELETED,
        BackupCleanupAuditStatus.FAILED,
        BackupCleanupAuditStatus.NOT_RUN,
    ]
    assert not (paths[0] / _session_name("alpha")).exists()
    assert middle.exists()
    assert (paths[0] / _session_name("zulu")).exists()


def test_receipted_input_change_between_sessions_stops_before_next_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _lifecycle(tmp_path, ("alpha", "middle", "zulu"))
    receipt_path = tmp_path / "reports" / "intent.json"
    original_delete = execution._delete_session
    changed_approval = b""
    deletions = 0

    def delete_then_change(*args: object, **kwargs: object) -> None:
        nonlocal changed_approval, deletions
        original_delete(*args, **kwargs)  # type: ignore[arg-type]
        deletions += 1
        if deletions == 1:
            changed_approval = paths[3].read_bytes().replace(b"{", b"{ ", 1)
            paths[3].write_bytes(changed_approval)

    monkeypatch.setattr(execution, "_delete_session", delete_then_change)

    assert _run_receipted(paths, receipt_path) == 2
    assert deletions == 1
    assert receipt_path.exists()
    assert paths[3].read_bytes() == changed_approval
    assert not (paths[0] / _session_name("alpha")).exists()
    assert (paths[0] / _session_name("middle")).exists()
    assert (paths[0] / _session_name("zulu")).exists()
    assert not paths[4].exists()


def test_receipted_audit_write_failure_keeps_receipt_and_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _lifecycle(tmp_path)
    receipt_path = tmp_path / "reports" / "intent.json"

    def fail_write(path: Path, audit: object) -> None:
        raise OSError("synthetic report failure")

    monkeypatch.setattr(cli, "write_backup_cleanup_audit", fail_write)

    assert _run_receipted(paths, receipt_path) == 2
    assert receipt_path.exists()
    assert not paths[4].exists()
    assert not (paths[0] / _session_name("alpha")).exists()


def test_receipted_concurrent_audit_writer_is_preserved_after_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _lifecycle(tmp_path)
    receipt_path = tmp_path / "reports" / "intent.json"
    original_link = execution.os.link
    link_calls = 0
    concurrent = b"concurrent audit"

    def claim_second_final(
        source: Path,
        destination: Path,
        *,
        follow_symlinks: bool,
    ) -> None:
        nonlocal link_calls
        link_calls += 1
        if link_calls == 2:
            Path(destination).write_bytes(concurrent)
        original_link(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(execution.os, "link", claim_second_final)

    assert _run_receipted(paths, receipt_path) == 2
    assert receipt_path.exists()
    assert paths[4].read_bytes() == concurrent
    assert not (paths[0] / _session_name("alpha")).exists()


def test_receipted_post_write_verification_uses_actual_audit_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _lifecycle(tmp_path)
    receipt_path = tmp_path / "reports" / "intent.json"
    original_verify = cli.verify_backup_cleanup_intent
    verified_audits: list[bytes] = []

    def record_verify(receipt_content: bytes, audit_content: bytes, **kwargs: bytes) -> None:
        verified_audits.append(audit_content)
        original_verify(receipt_content, audit_content, **kwargs)

    monkeypatch.setattr(cli, "verify_backup_cleanup_intent", record_verify)

    assert _run_receipted(paths, receipt_path) == 0
    assert len(verified_audits) == 2
    assert verified_audits[-1] == paths[4].read_bytes()


def test_tampered_receipt_binding_is_rejected_by_formal_verifier(tmp_path: Path) -> None:
    paths = _lifecycle(tmp_path)
    receipt_path = tmp_path / "reports" / "intent.json"
    assert _run_receipted(paths, receipt_path) == 0
    payload = json.loads(paths[4].read_text(encoding="utf-8"))
    payload["intent_receipt"]["sha256"] = "0" * 64
    tampered_audit = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()

    with pytest.raises(ValueError, match="binding mismatch"):
        verify_backup_cleanup_intent(
            receipt_path.read_bytes(),
            tampered_audit,
            inventory_content=paths[1].read_bytes(),
            plan_content=paths[2].read_bytes(),
            approval_content=paths[3].read_bytes(),
        )


def test_receipted_outputs_do_not_expose_local_details(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _lifecycle(tmp_path)
    receipt_path = tmp_path / "reports" / "intent.json"

    assert _run_receipted(paths, receipt_path) == 0

    captured = capsys.readouterr()
    combined = (
        captured.out
        + captured.err
        + receipt_path.read_text(encoding="utf-8")
        + paths[4].read_text(encoding="utf-8")
    )
    assert str(tmp_path) not in combined
    assert "Traceback" not in combined


def test_receipted_retry_requires_new_receipt_attempt_and_audit_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _lifecycle(tmp_path)
    first_receipt = tmp_path / "reports" / "intent-a.json"
    second_receipt = tmp_path / "reports" / "intent-b.json"
    second_audit = tmp_path / "reports" / "audit-b.json"
    backup = next((paths[0] / _session_name("alpha")).rglob("*.bak"))
    original = backup.read_bytes()
    original_write = execution.write_operation_intent
    changed = False

    def change_once(path: Path, receipt: object) -> None:
        nonlocal changed
        original_write(path, receipt)  # type: ignore[arg-type]
        if not changed:
            changed = True
            backup.write_bytes(b"temporary tamper")

    monkeypatch.setattr(execution, "write_operation_intent", change_once)

    assert _run_receipted(paths, first_receipt, attempt_id="attempt-a") == 1
    backup.write_bytes(original)
    retry_paths = (*paths[:4], second_audit, paths[5])
    assert _run_receipted(retry_paths, second_receipt, attempt_id="attempt-b") == 0
    assert first_receipt.exists()
    assert paths[4].exists()
    assert second_receipt.exists()
    assert second_audit.exists()


def test_legacy_audit_field_set_remains_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _lifecycle(tmp_path)

    def fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("receipted input gate must not run in legacy mode")

    monkeypatch.setattr(execution, "_verify_receipted_inputs_unchanged", fail_if_called)

    assert _run_lifecycle(paths) == 0

    payload = json.loads(paths[4].read_text(encoding="utf-8"))
    assert "intent_receipt" not in payload
