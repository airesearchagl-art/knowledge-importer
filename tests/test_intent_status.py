from __future__ import annotations

import json
import unicodedata
from collections.abc import Callable
from pathlib import Path

import pytest

from knowledge_importer.backup_cleanup_execution import (
    BackupCleanupAudit,
    BackupCleanupAuditAction,
    BackupCleanupAuditBefore,
    BackupCleanupAuditIntentReceipt,
    BackupCleanupAuditStatus,
    backup_cleanup_audit_bytes,
)
from knowledge_importer.intent_status import (
    ACTION_SCOPE_MISMATCH,
    ATTEMPT_ID_MISMATCH,
    BINDING_SCOPE_MISMATCH,
    CONFLICTING,
    FINAL_REPORT_MISSING,
    LEGACY_FINAL_REPORT,
    MULTIPLE_FINAL_REPORTS,
    OPERATION_TYPE_MISMATCH,
    ORPHAN,
    PAIRED,
    RECEIPT_SHA256_MISMATCH,
    IntentStatusInputError,
    build_operation_intent_status,
    inspect_operation_intent_status,
    operation_intent_status_bytes,
    parse_operation_intent_status_bytes,
)
from knowledge_importer.operation_intent import (
    BACKUP_CLEANUP,
    REPAIR_EXECUTION,
    OperationIntentAction,
    OperationIntentBinding,
    OperationIntentReceipt,
    operation_intent_bytes,
    operation_intent_sha256,
)
from knowledge_importer.repair_execution import (
    ExecutionActionResult,
    RepairExecutionIntentReceipt,
    RepairExecutionReport,
    repair_execution_report_bytes,
)
from knowledge_importer.repair_plan import RepairAction, RepairActionCategory
from knowledge_importer.repair_preflight import PreflightTarget


def _repair_receipt(*, attempt_id: str = "repair-attempt-001") -> bytes:
    return operation_intent_bytes(
        OperationIntentReceipt(
            attempt_id,
            REPAIR_EXECUTION,
            (
                OperationIntentBinding("artifact-manifest", 1, "a" * 64),
                OperationIntentBinding("repair-plan", 1, "b" * 64),
                OperationIntentBinding("repair-approval", 1, "c" * 64),
                OperationIntentBinding("repair-preflight", 1, "d" * 64),
            ),
            (
                OperationIntentAction(
                    0,
                    "regenerate-sidecar",
                    "documents/item.metadata.json",
                    "missing-sidecar",
                ),
            ),
        )
    )


def _repair_report(
    receipt: bytes,
    *,
    attempt_id: str = "repair-attempt-001",
    receipt_sha256: str | None = None,
    path: str = "documents/item.metadata.json",
    plan_sha256: str = "b" * 64,
    legacy: bool = False,
) -> bytes:
    action = RepairAction(
        path,
        RepairActionCategory.REGENERATE_SIDECAR,
        "missing-sidecar",
        True,
    )
    result = ExecutionActionResult(
        action,
        "succeeded",
        PreflightTarget(path, False, None, None),
        PreflightTarget(path, True, 8, "e" * 64),
        "available",
    )
    binding = (
        None
        if legacy
        else RepairExecutionIntentReceipt(
            1,
            attempt_id,
            receipt_sha256 or operation_intent_sha256(receipt),
        )
    )
    return repair_execution_report_bytes(
        RepairExecutionReport(
            plan_sha256,
            "c" * 64,
            "d" * 64,
            (result,),
            "passed",
            binding,
        )
    )


def _cleanup_receipt(*, attempt_id: str = "cleanup-attempt-001") -> bytes:
    return operation_intent_bytes(
        OperationIntentReceipt(
            attempt_id,
            BACKUP_CLEANUP,
            (
                OperationIntentBinding("backup-inventory", 1, "a" * 64),
                OperationIntentBinding("backup-cleanup-plan", 1, "b" * 64),
                OperationIntentBinding("backup-cleanup-approval", 1, "c" * 64),
            ),
            (
                OperationIntentAction(
                    0,
                    "delete-backup-session",
                    "knowledge-importer-repair-alpha",
                    "explicit-retention-release",
                ),
            ),
        )
    )


def _cleanup_report(
    receipt: bytes,
    *,
    attempt_id: str = "cleanup-attempt-001",
    receipt_sha256: str | None = None,
    session: str = "knowledge-importer-repair-alpha",
    inventory_sha256: str = "a" * 64,
    legacy: bool = False,
) -> bytes:
    binding = (
        None
        if legacy
        else BackupCleanupAuditIntentReceipt(
            1,
            attempt_id,
            receipt_sha256 or operation_intent_sha256(receipt),
        )
    )
    return backup_cleanup_audit_bytes(
        BackupCleanupAudit(
            inventory_sha256,
            "b" * 64,
            "c" * 64,
            (
                BackupCleanupAuditAction(
                    session,
                    BackupCleanupAuditStatus.DELETED,
                    BackupCleanupAuditBefore(1, 8, "d" * 64),
                    False,
                ),
            ),
            binding,
        )
    )


@pytest.mark.parametrize(
    ("receipt", "keyword", "report_source"),
    [
        (_repair_receipt(), "repair_execution_contents", _repair_report),
        (_cleanup_receipt(), "backup_cleanup_audit_contents", _cleanup_report),
    ],
)
def test_valid_receipt_and_matching_final_report_are_paired(
    receipt: bytes,
    keyword: str,
    report_source: Callable[[bytes], bytes],
) -> None:
    report = report_source(receipt)

    status = build_operation_intent_status(receipt, **{keyword: (report,)})

    assert status.classification == PAIRED
    assert status.reason == PAIRED
    assert status.exit_code == 0
    assert not status.operator_action_required
    assert status.payload()["bindings"] == {
        "receipt_final_report": "verified",
        "lifecycle_inputs": "not-provided",
        "current_preconditions": "not-provided",
    }


def test_valid_receipt_without_final_report_is_orphan() -> None:
    status = build_operation_intent_status(_repair_receipt())

    assert status.classification == ORPHAN
    assert status.reason == FINAL_REPORT_MISSING
    assert status.final_reports == ()
    assert status.exit_code == 1
    assert status.operator_action_required


@pytest.mark.parametrize(
    ("report_factory", "reason"),
    [
        (lambda receipt: _repair_report(receipt, receipt_sha256="0" * 64), RECEIPT_SHA256_MISMATCH),
        (
            lambda receipt: _repair_report(receipt, attempt_id="repair-attempt-002"),
            ATTEMPT_ID_MISMATCH,
        ),
        (
            lambda receipt: _repair_report(receipt, path="documents/other.metadata.json"),
            ACTION_SCOPE_MISMATCH,
        ),
        (lambda receipt: _repair_report(receipt, plan_sha256="9" * 64), BINDING_SCOPE_MISMATCH),
        (lambda receipt: _repair_report(receipt, legacy=True), LEGACY_FINAL_REPORT),
    ],
)
def test_repair_pairing_conflicts_use_fixed_reasons(
    report_factory: Callable[[bytes], bytes],
    reason: str,
) -> None:
    receipt = _repair_receipt()
    report = report_factory(receipt)

    status = build_operation_intent_status(
        receipt,
        repair_execution_contents=(report,),
    )

    assert status.classification == CONFLICTING
    assert status.reason == reason
    assert status.exit_code == 1
    assert status.operator_action_required


def test_operation_type_mismatch_is_conflicting() -> None:
    receipt = _cleanup_receipt(attempt_id="shared-attempt-001")
    report = _repair_report(
        receipt,
        attempt_id="shared-attempt-001",
        receipt_sha256=operation_intent_sha256(receipt),
    )

    status = build_operation_intent_status(
        receipt,
        repair_execution_contents=(report,),
    )

    assert status.classification == CONFLICTING
    assert status.reason == OPERATION_TYPE_MISMATCH


@pytest.mark.parametrize(
    ("report", "reason"),
    [
        (
            _cleanup_report(_cleanup_receipt(), session="knowledge-importer-repair-other"),
            ACTION_SCOPE_MISMATCH,
        ),
        (
            _cleanup_report(_cleanup_receipt(), inventory_sha256="9" * 64),
            BINDING_SCOPE_MISMATCH,
        ),
        (_cleanup_report(_cleanup_receipt(), legacy=True), LEGACY_FINAL_REPORT),
    ],
)
def test_cleanup_pairing_conflicts_are_detected(report: bytes, reason: str) -> None:
    receipt = _cleanup_receipt()

    status = build_operation_intent_status(
        receipt,
        backup_cleanup_audit_contents=(report,),
    )

    assert status.classification == CONFLICTING
    assert status.reason == reason


def test_multiple_distinct_final_reports_are_conflicting_and_canonical() -> None:
    receipt = _repair_receipt()
    paired = _repair_report(receipt)
    conflicting = _repair_report(receipt, receipt_sha256="0" * 64)

    first = build_operation_intent_status(
        receipt,
        repair_execution_contents=(paired, conflicting),
    )
    second = build_operation_intent_status(
        receipt,
        repair_execution_contents=(conflicting, paired),
    )

    assert first == second
    assert first.classification == CONFLICTING
    assert first.reason == MULTIPLE_FINAL_REPORTS
    assert [report.sha256 for report in first.final_reports] == sorted(
        report.sha256 for report in first.final_reports
    )


def test_duplicate_exact_final_bytes_are_invalid_input() -> None:
    receipt = _repair_receipt()
    report = _repair_report(receipt)

    with pytest.raises(IntentStatusInputError, match="duplicate"):
        build_operation_intent_status(
            receipt,
            repair_execution_contents=(report, report),
        )


def test_one_byte_semantic_preserving_receipt_change_breaks_exact_pairing() -> None:
    original = _repair_receipt()
    report = _repair_report(original)
    changed = original.replace(b"{", b"{ ", 1)

    status = build_operation_intent_status(
        changed,
        repair_execution_contents=(report,),
    )

    assert status.reason == RECEIPT_SHA256_MISMATCH
    assert operation_intent_sha256(changed) != operation_intent_sha256(original)


@pytest.mark.parametrize(
    ("receipt", "reports"),
    [
        (b"not-json", ()),
        (_repair_receipt(), (b"not-json",)),
    ],
)
def test_invalid_source_is_not_classified(receipt: bytes, reports: tuple[bytes, ...]) -> None:
    with pytest.raises(IntentStatusInputError, match="invalid"):
        build_operation_intent_status(
            receipt,
            repair_execution_contents=reports,
        )


def test_status_bytes_are_deterministic_utf8_and_parseable() -> None:
    receipt = _repair_receipt()
    status = build_operation_intent_status(
        receipt,
        repair_execution_contents=(_repair_report(receipt),),
    )

    first = operation_intent_status_bytes(status)
    second = operation_intent_status_bytes(status)

    assert first == second
    assert first.endswith(b"\n")
    assert parse_operation_intent_status_bytes(first) == status


def test_status_parser_allows_unknown_v1_fields_and_rejects_future_schema() -> None:
    status = build_operation_intent_status(_repair_receipt())
    payload = status.payload()
    payload["future_optional"] = {"ignored": True}
    content = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()

    assert parse_operation_intent_status_bytes(content) == status

    payload["schema_version"] = 2
    future = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
    with pytest.raises(ValueError, match="schema"):
        parse_operation_intent_status_bytes(future)


def test_status_parser_rejects_paired_operation_type_mismatch() -> None:
    receipt = _repair_receipt()
    status = build_operation_intent_status(
        receipt,
        repair_execution_contents=(_repair_report(receipt),),
    )
    payload = status.payload()
    final_reports = payload["final_reports"]
    assert isinstance(final_reports, list)
    first_report = final_reports[0]
    assert isinstance(first_report, dict)
    first_report["source_type"] = "backup-cleanup-audit"
    content = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()

    with pytest.raises(ValueError, match="semantics"):
        parse_operation_intent_status_bytes(content)


def test_status_output_contains_no_environment_or_source_path_details(tmp_path: Path) -> None:
    receipt = _repair_receipt()
    content = operation_intent_status_bytes(
        build_operation_intent_status(
            receipt,
            repair_execution_contents=(_repair_report(receipt),),
        )
    ).decode("utf-8")

    assert str(tmp_path) not in content
    assert "C:\\Users\\" not in content
    assert "Traceback" not in content
    assert "timestamp" not in content
    assert not any(unicodedata.category(character) == "Cf" for character in content)


def test_status_inspection_never_changes_source_bytes(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "intent.json"
    report = tmp_path / "report.json"
    receipt.write_bytes(_repair_receipt())
    report.write_bytes(_repair_report(receipt.read_bytes()))
    before = {path: path.read_bytes() for path in (receipt, report)}

    status = inspect_operation_intent_status(
        receipt,
        repair_execution_paths=(report,),
    )

    assert status.classification == PAIRED
    assert {path: path.read_bytes() for path in (receipt, report)} == before
