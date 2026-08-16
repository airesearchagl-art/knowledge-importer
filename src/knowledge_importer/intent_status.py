"""Read-only pairing status for one immutable Operation Intent Receipt."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from knowledge_importer.backup_cleanup_execution import (
    parse_backup_cleanup_audit_bytes,
    verify_backup_cleanup_operation_intent_lifecycle,
)
from knowledge_importer.backup_cleanup_plan import read_input_bytes
from knowledge_importer.operation_intent import (
    BACKUP_CLEANUP,
    REPAIR_EXECUTION,
    OperationIntentAction,
    OperationIntentLifecycleVerification,
    OperationIntentReceipt,
    operation_intent_sha256,
    parse_operation_intent_bytes,
    validate_operation_intent_attempt_id,
)
from knowledge_importer.operational_audit import (
    BACKUP_CLEANUP_SOURCE,
    REPAIR_EXECUTION_SOURCE,
)
from knowledge_importer.repair_execution import (
    parse_repair_execution_report_bytes,
    verify_repair_operation_intent_lifecycle,
)
from knowledge_importer.repair_preflight import (
    parse_repair_preflight_bytes,
    repair_preflight_current_state_matches,
)

INTENT_STATUS_REPORT_TYPE = "knowledge-importer-operation-intent-status"
INTENT_STATUS_SCHEMA_VERSION = 1

PAIRED = "paired"
ORPHAN = "orphan"
CONFLICTING = "conflicting"
STALE = "stale"

VERIFIED = "verified"
MISSING = "missing"
NOT_PROVIDED = "not-provided"
MISMATCH = "mismatch"
NOT_APPLICABLE = "not-applicable"

FINAL_REPORT_MISSING = "final-report-missing"
MULTIPLE_FINAL_REPORTS = "multiple-final-reports"
RECEIPT_SHA256_MISMATCH = "receipt-sha256-mismatch"
ATTEMPT_ID_MISMATCH = "attempt-id-mismatch"
OPERATION_TYPE_MISMATCH = "operation-type-mismatch"
ACTION_SCOPE_MISMATCH = "action-scope-mismatch"
BINDING_SCOPE_MISMATCH = "binding-scope-mismatch"
LEGACY_FINAL_REPORT = "legacy-final-report"
LIFECYCLE_BINDING_MISMATCH = "lifecycle-binding-mismatch"
LIFECYCLE_ACTION_SCOPE_MISMATCH = "lifecycle-action-scope-mismatch"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CLASSIFICATIONS = {PAIRED, ORPHAN, STALE, CONFLICTING}
_CONFLICT_REASONS = {
    MULTIPLE_FINAL_REPORTS,
    RECEIPT_SHA256_MISMATCH,
    ATTEMPT_ID_MISMATCH,
    OPERATION_TYPE_MISMATCH,
    ACTION_SCOPE_MISMATCH,
    BINDING_SCOPE_MISMATCH,
    LEGACY_FINAL_REPORT,
}


class IntentStatusInputError(ValueError):
    """Raised when an Intent Status source cannot be safely classified."""


@dataclass(frozen=True, slots=True)
class IntentStatusReceipt:
    sha256: str
    operation_type: str
    attempt_id: str

    def payload(self) -> dict[str, object]:
        return {
            "sha256": self.sha256,
            "operation_type": self.operation_type,
            "attempt_id": self.attempt_id,
        }


@dataclass(frozen=True, slots=True)
class IntentStatusFinalReport:
    source_type: str
    sha256: str

    def payload(self) -> dict[str, object]:
        return {"source_type": self.source_type, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class OperationIntentStatus:
    receipt: IntentStatusReceipt
    classification: str
    final_reports: tuple[IntentStatusFinalReport, ...]
    receipt_final_report: str
    reason: str
    lifecycle_inputs: str = NOT_PROVIDED
    current_preconditions: str = NOT_PROVIDED

    @property
    def operator_action_required(self) -> bool:
        return self.classification != PAIRED or self.lifecycle_inputs == MISMATCH

    @property
    def exit_code(self) -> int:
        return 0 if self.classification == PAIRED else 1

    def payload(self) -> dict[str, object]:
        return {
            "report_type": INTENT_STATUS_REPORT_TYPE,
            "schema_version": INTENT_STATUS_SCHEMA_VERSION,
            "receipt": self.receipt.payload(),
            "classification": self.classification,
            "final_reports": [report.payload() for report in self.final_reports],
            "bindings": {
                "receipt_final_report": self.receipt_final_report,
                "lifecycle_inputs": self.lifecycle_inputs,
                "current_preconditions": self.current_preconditions,
            },
            "operator_action_required": self.operator_action_required,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class _FinalReportEvidence:
    descriptor: IntentStatusFinalReport
    operation_type: str
    receipt_sha256: str | None
    attempt_id: str | None
    actions: tuple[OperationIntentAction, ...]
    bindings: tuple[tuple[str, str], ...]


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _repair_evidence(content: bytes) -> _FinalReportEvidence:
    report = parse_repair_execution_report_bytes(content)
    binding = report.intent_receipt
    return _FinalReportEvidence(
        IntentStatusFinalReport(REPAIR_EXECUTION_SOURCE, _sha256(content)),
        REPAIR_EXECUTION,
        binding.sha256 if binding is not None else None,
        binding.attempt_id if binding is not None else None,
        tuple(
            OperationIntentAction(
                index,
                result.repair_action.action.value,
                result.repair_action.path,
                result.repair_action.reason_category,
            )
            for index, result in enumerate(report.actions)
        ),
        (
            ("repair-plan", report.plan_sha256),
            ("repair-approval", report.approval_sha256),
            ("repair-preflight", report.preflight_sha256),
        ),
    )


def _cleanup_evidence(content: bytes) -> _FinalReportEvidence:
    report = parse_backup_cleanup_audit_bytes(content)
    binding = report.intent_receipt
    return _FinalReportEvidence(
        IntentStatusFinalReport(BACKUP_CLEANUP_SOURCE, _sha256(content)),
        BACKUP_CLEANUP,
        binding.sha256 if binding is not None else None,
        binding.attempt_id if binding is not None else None,
        tuple(
            OperationIntentAction(
                index,
                "delete-backup-session",
                result.session,
                "explicit-retention-release",
            )
            for index, result in enumerate(report.actions)
        ),
        (
            ("backup-inventory", report.inventory_sha256),
            ("backup-cleanup-plan", report.plan_sha256),
            ("backup-cleanup-approval", report.approval_sha256),
        ),
    )


def _conflict_reason(
    receipt: OperationIntentReceipt,
    receipt_sha256: str,
    final_report: _FinalReportEvidence,
) -> str | None:
    if final_report.receipt_sha256 is None:
        return LEGACY_FINAL_REPORT
    if final_report.receipt_sha256 != receipt_sha256:
        return RECEIPT_SHA256_MISMATCH
    if final_report.attempt_id != receipt.attempt_id:
        return ATTEMPT_ID_MISMATCH
    if final_report.operation_type != receipt.operation_type:
        return OPERATION_TYPE_MISMATCH
    if final_report.actions != receipt.actions:
        return ACTION_SCOPE_MISMATCH
    receipt_bindings = {binding.artifact_type: binding.sha256 for binding in receipt.bindings}
    if any(
        receipt_bindings.get(artifact_type) != sha256
        for artifact_type, sha256 in final_report.bindings
    ):
        return BINDING_SCOPE_MISMATCH
    return None


def build_operation_intent_status(
    receipt_content: bytes,
    *,
    repair_execution_contents: Sequence[bytes] = (),
    backup_cleanup_audit_contents: Sequence[bytes] = (),
    lifecycle_verification: OperationIntentLifecycleVerification | None = None,
) -> OperationIntentStatus:
    """Classify one Receipt against zero or more exact final report byte sources."""

    all_final_contents = (*repair_execution_contents, *backup_cleanup_audit_contents)
    if len(set(all_final_contents)) != len(all_final_contents):
        raise IntentStatusInputError("duplicate final report bytes")
    try:
        receipt = parse_operation_intent_bytes(receipt_content)
        receipt_sha256 = operation_intent_sha256(receipt_content)
        evidence = [
            *(_repair_evidence(content) for content in repair_execution_contents),
            *(_cleanup_evidence(content) for content in backup_cleanup_audit_contents),
        ]
    except (TypeError, ValueError) as exc:
        raise IntentStatusInputError("invalid Intent Status source") from exc

    evidence.sort(key=lambda item: (item.descriptor.source_type, item.descriptor.sha256))
    descriptor = IntentStatusReceipt(
        receipt_sha256,
        receipt.operation_type,
        receipt.attempt_id,
    )
    final_reports = tuple(item.descriptor for item in evidence)
    if lifecycle_verification is None:
        lifecycle_inputs = NOT_PROVIDED
        lifecycle_reason = None
    elif not lifecycle_verification.bindings_match:
        lifecycle_inputs = MISMATCH
        lifecycle_reason = LIFECYCLE_BINDING_MISMATCH
    elif lifecycle_verification.action_scope_matches is False:
        lifecycle_inputs = MISMATCH
        lifecycle_reason = LIFECYCLE_ACTION_SCOPE_MISMATCH
    else:
        lifecycle_inputs = VERIFIED
        lifecycle_reason = None

    if not evidence:
        classification = STALE if lifecycle_reason is not None else ORPHAN
        return OperationIntentStatus(
            descriptor,
            classification,
            (),
            MISSING,
            lifecycle_reason or FINAL_REPORT_MISSING,
            lifecycle_inputs,
            NOT_PROVIDED,
        )
    if len(evidence) > 1:
        return OperationIntentStatus(
            descriptor,
            CONFLICTING,
            final_reports,
            CONFLICTING,
            MULTIPLE_FINAL_REPORTS,
            lifecycle_inputs,
            NOT_PROVIDED,
        )
    reason = _conflict_reason(receipt, receipt_sha256, evidence[0])
    if reason is not None:
        return OperationIntentStatus(
            descriptor,
            CONFLICTING,
            final_reports,
            CONFLICTING,
            reason,
            lifecycle_inputs,
            NOT_PROVIDED,
        )
    return OperationIntentStatus(
        descriptor,
        PAIRED,
        final_reports,
        VERIFIED,
        lifecycle_reason or PAIRED,
        lifecycle_inputs,
        NOT_PROVIDED if lifecycle_verification is None else NOT_APPLICABLE,
    )


def inspect_operation_intent_status(
    receipt_path: Path,
    *,
    repair_execution_paths: Sequence[Path] = (),
    backup_cleanup_audit_paths: Sequence[Path] = (),
    manifest_path: Path | None = None,
    inventory_path: Path | None = None,
    plan_path: Path | None = None,
    approval_path: Path | None = None,
    preflight_path: Path | None = None,
    package_root: Path | None = None,
) -> OperationIntentStatus:
    """Stable-read source artifacts and return a read-only pairing snapshot."""

    try:
        receipt_content = read_input_bytes(receipt_path)
        receipt = parse_operation_intent_bytes(receipt_content)
        if package_root is not None and (
            receipt.operation_type != REPAIR_EXECUTION
            or inventory_path is not None
            or any(
                path is None for path in (manifest_path, plan_path, approval_path, preflight_path)
            )
        ):
            raise IntentStatusInputError(
                "Repair package root requires complete Repair lifecycle inputs"
            )
        lifecycle_paths = (
            manifest_path,
            inventory_path,
            plan_path,
            approval_path,
            preflight_path,
        )
        lifecycle_verification = None
        if any(path is not None for path in lifecycle_paths):
            if receipt.operation_type == REPAIR_EXECUTION:
                if inventory_path is not None or any(
                    path is None
                    for path in (manifest_path, plan_path, approval_path, preflight_path)
                ):
                    raise IntentStatusInputError(
                        "Repair lifecycle inputs must be provided as a complete set"
                    )
                assert manifest_path is not None
                assert plan_path is not None
                assert approval_path is not None
                assert preflight_path is not None
                lifecycle_verification = verify_repair_operation_intent_lifecycle(
                    receipt_content,
                    manifest_path=manifest_path,
                    plan_path=plan_path,
                    approval_path=approval_path,
                    preflight_path=preflight_path,
                )
            else:
                if (
                    manifest_path is not None
                    or preflight_path is not None
                    or any(path is None for path in (inventory_path, plan_path, approval_path))
                ):
                    raise IntentStatusInputError(
                        "Cleanup lifecycle inputs must be provided as a complete set"
                    )
                assert inventory_path is not None
                assert plan_path is not None
                assert approval_path is not None
                lifecycle_verification = verify_backup_cleanup_operation_intent_lifecycle(
                    receipt_content,
                    inventory_path=inventory_path,
                    plan_path=plan_path,
                    approval_path=approval_path,
                )
        repair_execution_contents = tuple(read_input_bytes(path) for path in repair_execution_paths)
        backup_cleanup_audit_contents = tuple(
            read_input_bytes(path) for path in backup_cleanup_audit_paths
        )
        status = build_operation_intent_status(
            receipt_content,
            repair_execution_contents=repair_execution_contents,
            backup_cleanup_audit_contents=backup_cleanup_audit_contents,
            lifecycle_verification=lifecycle_verification,
        )
        if package_root is None:
            return status
        if status.classification != ORPHAN or status.lifecycle_inputs != VERIFIED:
            return replace(status, current_preconditions=NOT_APPLICABLE)

        assert manifest_path is not None
        assert plan_path is not None
        assert approval_path is not None
        assert preflight_path is not None
        rebound = verify_repair_operation_intent_lifecycle(
            receipt_content,
            manifest_path=manifest_path,
            plan_path=plan_path,
            approval_path=approval_path,
            preflight_path=preflight_path,
        )
        if not rebound.bindings_match or rebound.action_scope_matches is not True:
            raise IntentStatusInputError(
                "Repair lifecycle inputs changed before current precondition verification"
            )
        expected_preflight = parse_repair_preflight_bytes(read_input_bytes(preflight_path))
        matches = repair_preflight_current_state_matches(
            package_root,
            manifest_path=manifest_path,
            plan_path=plan_path,
            approval_path=approval_path,
            expected=expected_preflight,
        )
        rebound = verify_repair_operation_intent_lifecycle(
            receipt_content,
            manifest_path=manifest_path,
            plan_path=plan_path,
            approval_path=approval_path,
            preflight_path=preflight_path,
        )
        if not rebound.bindings_match or rebound.action_scope_matches is not True:
            raise IntentStatusInputError(
                "Repair lifecycle inputs changed during current precondition verification"
            )
        return replace(
            status,
            current_preconditions=VERIFIED if matches else MISMATCH,
        )
    except (OSError, TypeError, ValueError) as exc:
        if isinstance(exc, IntentStatusInputError):
            raise
        raise IntentStatusInputError("Intent Status source cannot be read") from exc


def parse_operation_intent_status_bytes(content: bytes) -> OperationIntentStatus:
    """Parse and semantically validate Operation Intent Status schema v1."""

    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid Operation Intent Status JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid Operation Intent Status root")
    receipt_value = payload.get("receipt")
    final_values = payload.get("final_reports")
    bindings = payload.get("bindings")
    classification = payload.get("classification")
    reason = payload.get("reason")
    schema_version = payload.get("schema_version")
    if not (
        payload.get("report_type") == INTENT_STATUS_REPORT_TYPE
        and isinstance(schema_version, int)
        and not isinstance(schema_version, bool)
        and schema_version == INTENT_STATUS_SCHEMA_VERSION
        and isinstance(receipt_value, dict)
        and isinstance(final_values, list)
        and isinstance(bindings, dict)
        and classification in _CLASSIFICATIONS
        and isinstance(reason, str)
        and isinstance(payload.get("operator_action_required"), bool)
    ):
        raise ValueError("invalid Operation Intent Status schema")

    receipt_sha256 = receipt_value.get("sha256")
    operation_type = receipt_value.get("operation_type")
    attempt_id = receipt_value.get("attempt_id")
    if not (
        isinstance(receipt_sha256, str)
        and _SHA256.fullmatch(receipt_sha256) is not None
        and operation_type in {REPAIR_EXECUTION, BACKUP_CLEANUP}
    ):
        raise ValueError("invalid Operation Intent Status Receipt")
    attempt_id = validate_operation_intent_attempt_id(attempt_id)
    receipt = IntentStatusReceipt(receipt_sha256, operation_type, attempt_id)

    final_reports: list[IntentStatusFinalReport] = []
    for value in final_values:
        if not isinstance(value, dict):
            raise ValueError("invalid Operation Intent Status final report")
        source_type = value.get("source_type")
        sha256 = value.get("sha256")
        if not (
            source_type in {REPAIR_EXECUTION_SOURCE, BACKUP_CLEANUP_SOURCE}
            and isinstance(sha256, str)
            and _SHA256.fullmatch(sha256) is not None
        ):
            raise ValueError("invalid Operation Intent Status final report")
        final_reports.append(IntentStatusFinalReport(source_type, sha256))
    final_keys = [(report.source_type, report.sha256) for report in final_reports]
    if (
        final_keys != sorted(final_keys)
        or len(set(final_keys)) != len(final_keys)
        or len({report.sha256 for report in final_reports}) != len(final_reports)
    ):
        raise ValueError("invalid Operation Intent Status final report order")

    receipt_final_report = bindings.get("receipt_final_report")
    lifecycle_inputs = bindings.get("lifecycle_inputs")
    current_preconditions = bindings.get("current_preconditions")
    if not (
        receipt_final_report in {VERIFIED, MISSING, CONFLICTING}
        and lifecycle_inputs in {NOT_PROVIDED, VERIFIED, MISMATCH}
        and current_preconditions in {NOT_PROVIDED, VERIFIED, MISMATCH, NOT_APPLICABLE}
    ):
        raise ValueError("invalid Operation Intent Status binding state")

    operator_action_required = payload["operator_action_required"]
    expected_source_type = (
        REPAIR_EXECUTION_SOURCE
        if receipt.operation_type == REPAIR_EXECUTION
        else BACKUP_CLEANUP_SOURCE
    )
    source_type_matches = (
        len(final_reports) == 1 and final_reports[0].source_type == expected_source_type
    )
    lifecycle_reason = reason in {
        LIFECYCLE_BINDING_MISMATCH,
        LIFECYCLE_ACTION_SCOPE_MISMATCH,
    }
    semantic_valid = (
        classification == PAIRED
        and len(final_reports) == 1
        and source_type_matches
        and receipt_final_report == VERIFIED
        and (
            lifecycle_inputs in {NOT_PROVIDED, VERIFIED}
            and reason == PAIRED
            and not operator_action_required
            and current_preconditions
            == (NOT_PROVIDED if lifecycle_inputs == NOT_PROVIDED else NOT_APPLICABLE)
            or lifecycle_inputs == MISMATCH
            and lifecycle_reason
            and operator_action_required
            and current_preconditions == NOT_APPLICABLE
        )
        or classification == ORPHAN
        and not final_reports
        and receipt_final_report == MISSING
        and reason == FINAL_REPORT_MISSING
        and lifecycle_inputs in {NOT_PROVIDED, VERIFIED}
        and current_preconditions in {NOT_PROVIDED, VERIFIED, MISMATCH}
        and (
            current_preconditions == NOT_PROVIDED
            or lifecycle_inputs == VERIFIED
            and receipt.operation_type == REPAIR_EXECUTION
        )
        and operator_action_required
        or classification == STALE
        and not final_reports
        and receipt_final_report == MISSING
        and lifecycle_inputs == MISMATCH
        and current_preconditions in {NOT_PROVIDED, NOT_APPLICABLE}
        and (current_preconditions == NOT_PROVIDED or receipt.operation_type == REPAIR_EXECUTION)
        and lifecycle_reason
        and operator_action_required
        or classification == CONFLICTING
        and bool(final_reports)
        and receipt_final_report == CONFLICTING
        and reason in _CONFLICT_REASONS
        and lifecycle_inputs in {NOT_PROVIDED, VERIFIED, MISMATCH}
        and current_preconditions in {NOT_PROVIDED, NOT_APPLICABLE}
        and (current_preconditions == NOT_PROVIDED or receipt.operation_type == REPAIR_EXECUTION)
        and (len(final_reports) > 1) == (reason == MULTIPLE_FINAL_REPORTS)
        and (len(final_reports) > 1 or (reason == OPERATION_TYPE_MISMATCH) != source_type_matches)
        and operator_action_required
    )
    if not semantic_valid:
        raise ValueError("invalid Operation Intent Status semantics")
    return OperationIntentStatus(
        receipt,
        classification,
        tuple(final_reports),
        receipt_final_report,
        reason,
        lifecycle_inputs,
        current_preconditions,
    )


def operation_intent_status_bytes(status: OperationIntentStatus) -> bytes:
    """Serialize deterministic UTF-8 JSON for stdout without source paths."""

    content = (json.dumps(status.payload(), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    parse_operation_intent_status_bytes(content)
    return content
