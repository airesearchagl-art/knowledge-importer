from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import knowledge_importer.operational_audit as audit_module
from knowledge_importer.backup_cleanup_execution import (
    BackupCleanupAudit,
    BackupCleanupAuditAction,
    BackupCleanupAuditBefore,
    BackupCleanupAuditStatus,
)
from knowledge_importer.operational_audit import (
    OperationalAuditInputError,
    build_operational_audit,
    write_operational_audit,
)
from knowledge_importer.repair_execution import (
    ExecutionActionResult,
    RepairExecutionReport,
)
from knowledge_importer.repair_plan import RepairAction, RepairActionCategory
from knowledge_importer.repair_preflight import PreflightTarget


def _json_bytes(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()


def _state(path: str, content: bytes | None) -> PreflightTarget:
    if content is None:
        return PreflightTarget(path, False, None, None)
    return PreflightTarget(path, True, len(content), hashlib.sha256(content).hexdigest())


def _repair_action(
    path: str,
    *,
    category: RepairActionCategory = RepairActionCategory.REGENERATE_SIDECAR,
    status: str = "succeeded",
    before: bytes | None = None,
    after: bytes | None = b"sidecar",
) -> ExecutionActionResult:
    rollback = {
        "succeeded": "available",
        "failed-precondition": "not-required",
        "failed": "not-required",
        "rolled-back": "completed",
        "rollback-failed": "failed",
        "not-run": "not-required",
    }[status]
    reason = (
        "missing-sidecar"
        if category is RepairActionCategory.REGENERATE_SIDECAR
        else "stale-sidecar"
    )
    return ExecutionActionResult(
        RepairAction(path, category, reason, True),
        status,
        _state(path, before),
        _state(path, after),
        rollback,
    )


def _write_repair(path: Path, actions: tuple[ExecutionActionResult, ...]) -> bytes:
    content = _json_bytes(
        RepairExecutionReport("a" * 64, "b" * 64, "c" * 64, actions, "passed").payload()
    )
    path.write_bytes(content)
    return content


def _cleanup_action(
    session: str,
    status: BackupCleanupAuditStatus,
    *,
    after_exists: bool,
) -> BackupCleanupAuditAction:
    return BackupCleanupAuditAction(
        session,
        status,
        BackupCleanupAuditBefore(1, 12, hashlib.sha256(session.encode()).hexdigest()),
        after_exists,
    )


def _write_cleanup(path: Path, actions: tuple[BackupCleanupAuditAction, ...]) -> bytes:
    content = _json_bytes(BackupCleanupAudit("d" * 64, "e" * 64, "f" * 64, actions).payload())
    path.write_bytes(content)
    return content


def test_aggregates_sources_in_canonical_order_and_binds_exact_bytes(tmp_path: Path) -> None:
    repair_path = tmp_path / "repair.json"
    cleanup_path = tmp_path / "cleanup.json"
    repair_bytes = _write_repair(
        repair_path,
        (
            _repair_action("documents/a.metadata.json"),
            _repair_action(
                "documents/b.metadata.json",
                category=RepairActionCategory.REMOVE_STALE_SIDECAR,
                before=b"stale",
                after=None,
            ),
        ),
    )
    cleanup_bytes = _write_cleanup(
        cleanup_path,
        (
            _cleanup_action(
                "knowledge-importer-repair-alpha",
                BackupCleanupAuditStatus.DELETED,
                after_exists=False,
            ),
        ),
    )

    audit = build_operational_audit(
        repair_execution_paths=(repair_path,),
        backup_cleanup_audit_paths=(cleanup_path,),
    )
    payload = audit.payload()

    assert [source["source_type"] for source in payload["sources"]] == [
        "backup-cleanup-audit",
        "repair-execution",
    ]
    assert [source["sha256"] for source in payload["sources"]] == [
        hashlib.sha256(cleanup_bytes).hexdigest(),
        hashlib.sha256(repair_bytes).hexdigest(),
    ]
    assert [operation["source_action_index"] for operation in payload["operations"]] == [0, 0, 1]
    assert [operation["action"] for operation in payload["operations"]] == [
        "backup-delete-session",
        "repair-regenerate-sidecar",
        "repair-remove-stale-sidecar",
    ]
    assert payload["summary"] == {
        "operations": 3,
        "succeeded": 3,
        "partial": 0,
        "failed": 0,
        "rolled_back": 0,
        "not_run": 0,
        "operator_action_required": 0,
        "package_change_observed": True,
    }


@pytest.mark.parametrize(
    ("status", "before", "after", "outcome", "reason", "package_change"),
    [
        ("rolled-back", b"old", b"old", "rolled_back", "rolled-back", "unchanged"),
        ("rollback-failed", b"old", b"new", "partial", "rollback-failed", "changed"),
        ("failed-precondition", b"old", b"old", "failed", "precondition-failed", "unchanged"),
        ("failed", b"old", b"new", "partial", "execution-failed", "changed"),
        ("not-run", b"old", b"old", "not_run", "not-run", "unchanged"),
    ],
)
def test_normalizes_repair_outcomes_without_inference(
    tmp_path: Path,
    status: str,
    before: bytes,
    after: bytes,
    outcome: str,
    reason: str,
    package_change: str,
) -> None:
    source = tmp_path / "repair.json"
    _write_repair(
        source,
        (_repair_action("item.metadata.json", status=status, before=before, after=after),),
    )

    operation = build_operational_audit(repair_execution_paths=(source,)).payload()["operations"][0]

    assert operation["outcome"] == outcome
    assert operation["reason"] == reason
    assert operation["package_change"] == package_change


def test_failed_repair_with_incomplete_digest_does_not_claim_partial_mutation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "repair.json"
    payload = RepairExecutionReport(
        "a" * 64,
        "b" * 64,
        "c" * 64,
        (_repair_action("item.metadata.json", status="failed", before=b"old", after=b"new"),),
        "failed",
    ).payload()
    action = payload["actions"][0]
    action["after"] = {"exists": True, "bytes": None, "sha256": None}
    source.write_bytes(_json_bytes(payload))

    operation = build_operational_audit(repair_execution_paths=(source,)).payload()["operations"][0]

    assert operation["outcome"] == "failed"
    assert operation["package_change"] == "unknown"
    assert operation["reason"] == "source-failure-mutation-unknown"


@pytest.mark.parametrize(
    ("status", "after_exists", "outcome", "reason"),
    [
        (BackupCleanupAuditStatus.DELETED, False, "succeeded", "completed"),
        (BackupCleanupAuditStatus.FAILED, True, "failed", "cleanup-failed"),
        (BackupCleanupAuditStatus.NOT_RUN, True, "not_run", "not-run"),
    ],
)
def test_cleanup_never_infers_package_change_or_partial(
    tmp_path: Path,
    status: BackupCleanupAuditStatus,
    after_exists: bool,
    outcome: str,
    reason: str,
) -> None:
    source = tmp_path / "cleanup.json"
    _write_cleanup(
        source,
        (_cleanup_action("knowledge-importer-repair-alpha", status, after_exists=after_exists),),
    )

    payload = build_operational_audit(backup_cleanup_audit_paths=(source,)).payload()
    operation = payload["operations"][0]

    assert operation["outcome"] == outcome
    assert operation["reason"] == reason
    assert operation["package_change"] == "unknown"
    assert payload["summary"]["package_change_observed"] is None


def test_rejects_no_sources_and_duplicate_exact_source_bytes(tmp_path: Path) -> None:
    with pytest.raises(OperationalAuditInputError):
        build_operational_audit()
    first = tmp_path / "first.json"
    duplicate = tmp_path / "duplicate.json"
    content = _write_repair(first, ())
    duplicate.write_bytes(content)

    with pytest.raises(OperationalAuditInputError):
        build_operational_audit(repair_execution_paths=(first, duplicate))


def test_multiple_sources_use_type_and_exact_digest_order(tmp_path: Path) -> None:
    repair_paths = (tmp_path / "repair-b.json", tmp_path / "repair-a.json")
    cleanup_paths = (tmp_path / "cleanup-b.json", tmp_path / "cleanup-a.json")
    _write_repair(repair_paths[0], (_repair_action("b.metadata.json"),))
    second_payload = RepairExecutionReport(
        "a" * 64,
        "b" * 64,
        "c" * 64,
        (_repair_action("a.metadata.json"),),
        "passed",
    ).payload()
    second_payload["source_note"] = "distinct exact bytes"
    repair_paths[1].write_bytes(_json_bytes(second_payload))
    _write_cleanup(
        cleanup_paths[0],
        (
            _cleanup_action(
                "knowledge-importer-repair-b", BackupCleanupAuditStatus.DELETED, after_exists=False
            ),
        ),
    )
    second_cleanup = BackupCleanupAudit("d" * 64, "e" * 64, "f" * 64, ()).payload()
    second_cleanup["source_note"] = "distinct exact bytes"
    cleanup_paths[1].write_bytes(_json_bytes(second_cleanup))

    payload = build_operational_audit(
        repair_execution_paths=repair_paths,
        backup_cleanup_audit_paths=cleanup_paths,
    ).payload()

    source_keys = [(source["source_type"], source["sha256"]) for source in payload["sources"]]
    assert source_keys == sorted(source_keys)
    operation_source_keys = [
        (operation["source_type"], operation["source_sha256"])
        for operation in payload["operations"]
    ]
    assert operation_source_keys == sorted(operation_source_keys)


def test_package_change_summary_is_false_only_for_proven_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "repair.json"
    _write_repair(
        source,
        (_repair_action("item.metadata.json", status="not-run", before=b"same", after=b"same"),),
    )

    summary = build_operational_audit(repair_execution_paths=(source,)).payload()["summary"]

    assert summary["package_change_observed"] is False


@pytest.mark.parametrize(
    "payload",
    [
        b"not JSON",
        _json_bytes({"report_type": "unknown", "schema_version": 1}),
        _json_bytes(
            {
                "report_type": "knowledge-package-repair-execution",
                "schema_version": 2,
            }
        ),
    ],
)
def test_rejects_invalid_json_source_type_and_future_schema(tmp_path: Path, payload: bytes) -> None:
    source = tmp_path / "source.json"
    source.write_bytes(payload)

    with pytest.raises(OperationalAuditInputError):
        build_operational_audit(repair_execution_paths=(source,))


def test_allows_unknown_v1_fields_without_copying_them(tmp_path: Path) -> None:
    source = tmp_path / "repair.json"
    payload = RepairExecutionReport(
        "a" * 64,
        "b" * 64,
        "c" * 64,
        (_repair_action("item.metadata.json"),),
        "passed",
    ).payload()
    payload["future_annotation"] = {"safe_unknown": True}
    payload["actions"][0]["future_action_field"] = "ignored"
    source.write_bytes(_json_bytes(payload))

    output = build_operational_audit(repair_execution_paths=(source,)).payload()

    assert "future_annotation" not in output
    assert "future_action_field" not in output["operations"][0]


def test_cleanup_parser_type_error_is_sanitized_as_invalid_source(tmp_path: Path) -> None:
    source = tmp_path / "cleanup.json"
    payload = BackupCleanupAudit(
        "d" * 64,
        "e" * 64,
        "f" * 64,
        (
            _cleanup_action(
                "knowledge-importer-repair-a", BackupCleanupAuditStatus.FAILED, after_exists=True
            ),
        ),
    ).payload()
    payload["actions"][0]["status"] = []
    source.write_bytes(_json_bytes(payload))

    with pytest.raises(OperationalAuditInputError):
        build_operational_audit(backup_cleanup_audit_paths=(source,))


def test_source_files_are_byte_identical_after_build(tmp_path: Path) -> None:
    repair_path = tmp_path / "repair.json"
    cleanup_path = tmp_path / "cleanup.json"
    before = {
        repair_path: _write_repair(repair_path, (_repair_action("a.metadata.json"),)),
        cleanup_path: _write_cleanup(cleanup_path, ()),
    }

    build_operational_audit(
        repair_execution_paths=(repair_path,),
        backup_cleanup_audit_paths=(cleanup_path,),
    )

    assert {path: path.read_bytes() for path in before} == before


def test_write_is_deterministic_create_only_and_has_trailing_newline(tmp_path: Path) -> None:
    source = tmp_path / "repair.json"
    _write_repair(source, (_repair_action("section/a.metadata.json"),))
    audit = build_operational_audit(repair_execution_paths=(source,))
    first = tmp_path / "first.json"
    second = tmp_path / "nested" / "second.json"

    write_operational_audit(first, audit)
    write_operational_audit(second, audit)

    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes().endswith(b"\n")
    with pytest.raises(OperationalAuditInputError):
        write_operational_audit(first, audit)


def test_concurrent_output_is_preserved_and_temporary_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "repair.json"
    _write_repair(source, ())
    audit = build_operational_audit(repair_execution_paths=(source,))
    report = tmp_path / "audit.json"
    foreign = b"concurrent writer\n"
    real_link = audit_module.os.link

    def race_link(source_path: Path, final_path: Path, *, follow_symlinks: bool) -> None:
        final_path.write_bytes(foreign)
        real_link(source_path, final_path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(audit_module.os, "link", race_link)

    with pytest.raises(FileExistsError):
        write_operational_audit(report, audit)

    assert report.read_bytes() == foreign
    assert not tuple(tmp_path.glob(".audit.json.*.tmp"))


def test_symlink_output_is_rejected_without_changing_target(tmp_path: Path) -> None:
    source = tmp_path / "repair.json"
    _write_repair(source, ())
    audit = build_operational_audit(repair_execution_paths=(source,))
    target = tmp_path / "foreign.json"
    target.write_bytes(b"foreign\n")
    report = tmp_path / "audit.json"
    try:
        report.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable in this environment")

    with pytest.raises(OperationalAuditInputError):
        write_operational_audit(report, audit)

    assert target.read_bytes() == b"foreign\n"


def test_existing_directory_output_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "repair.json"
    _write_repair(source, ())
    audit = build_operational_audit(repair_execution_paths=(source,))
    report = tmp_path / "audit.json"
    report.mkdir()

    with pytest.raises(OperationalAuditInputError):
        write_operational_audit(report, audit)


def test_report_contains_no_source_path_or_forbidden_context(tmp_path: Path) -> None:
    source = tmp_path / "private-user" / "repair.json"
    source.parent.mkdir()
    _write_repair(source, (_repair_action("section/a.metadata.json"),))

    encoded = _json_bytes(build_operational_audit(repair_execution_paths=(source,)).payload())

    assert str(tmp_path).encode() not in encoded
    assert b"private-user" not in encoded
    assert b"Traceback (most recent call last)" not in encoded
    assert b"timestamp" not in encoded
    assert b"hostname" not in encoded
    assert b"username" not in encoded
    text = encoded.decode()
    assert not any(__import__("unicodedata").category(character) == "Cf" for character in text)
