"""Read-only aggregation of immutable repair and backup operation evidence."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from knowledge_importer.backup_cleanup_execution import (
    BackupCleanupAudit,
    BackupCleanupAuditStatus,
    parse_backup_cleanup_audit_bytes,
)
from knowledge_importer.backup_cleanup_plan import read_input_bytes
from knowledge_importer.backup_inventory import path_uses_link_or_reparse
from knowledge_importer.repair_execution import (
    ExecutionActionResult,
    RepairExecutionReport,
    parse_repair_execution_report_bytes,
)

REPAIR_EXECUTION_SOURCE = "repair-execution"
BACKUP_CLEANUP_SOURCE = "backup-cleanup-audit"


class OperationalAuditInputError(ValueError):
    """Raised when source evidence or the immutable output boundary is invalid."""


@dataclass(frozen=True, slots=True)
class OperationalAuditSource:
    source_type: str
    schema_version: int
    sha256: str

    def payload(self) -> dict[str, object]:
        return {
            "source_type": self.source_type,
            "schema_version": self.schema_version,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class OperationalAuditOperation:
    source_type: str
    source_sha256: str
    source_action_index: int
    action: str
    target: str
    mutation_scope: str
    source_status: str
    outcome: str
    before: dict[str, object]
    after: dict[str, object]
    rollback: str | None
    package_change: str
    operator_action_required: bool
    reason: str

    def payload(self) -> dict[str, object]:
        return {
            "source_type": self.source_type,
            "source_sha256": self.source_sha256,
            "source_action_index": self.source_action_index,
            "action": self.action,
            "target": self.target,
            "mutation_scope": self.mutation_scope,
            "source_status": self.source_status,
            "outcome": self.outcome,
            "before": self.before,
            "after": self.after,
            "rollback": self.rollback,
            "package_change": self.package_change,
            "operator_action_required": self.operator_action_required,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class OperationalAudit:
    sources: tuple[OperationalAuditSource, ...]
    operations: tuple[OperationalAuditOperation, ...]

    def payload(self) -> dict[str, object]:
        outcomes = [operation.outcome for operation in self.operations]
        changes = [operation.package_change for operation in self.operations]
        if "changed" in changes:
            package_change_observed: bool | None = True
        elif changes and all(change == "unchanged" for change in changes):
            package_change_observed = False
        else:
            package_change_observed = None
        return {
            "report_type": "knowledge-importer-operational-audit",
            "schema_version": 1,
            "summary": {
                "operations": len(self.operations),
                "succeeded": outcomes.count("succeeded"),
                "partial": outcomes.count("partial"),
                "failed": outcomes.count("failed"),
                "rolled_back": outcomes.count("rolled_back"),
                "not_run": outcomes.count("not_run"),
                "operator_action_required": sum(
                    operation.operator_action_required for operation in self.operations
                ),
                "package_change_observed": package_change_observed,
            },
            "sources": [source.payload() for source in self.sources],
            "operations": [operation.payload() for operation in self.operations],
        }


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _target_state(result: ExecutionActionResult, *, before: bool) -> dict[str, object]:
    state = result.before if before else result.after
    return {"exists": state.exists, "bytes": state.bytes, "sha256": state.sha256}


def _has_complete_digest(state: dict[str, object]) -> bool:
    return state["exists"] is False or (
        isinstance(state["bytes"], int) and isinstance(state["sha256"], str)
    )


def _package_change(before: dict[str, object], after: dict[str, object]) -> str:
    if not (_has_complete_digest(before) and _has_complete_digest(after)):
        return "unknown"
    return "unchanged" if before == after else "changed"


def _repair_outcome(status: str, package_change: str) -> tuple[str, str]:
    if status == "succeeded":
        return "succeeded", "completed"
    if status == "rolled-back":
        return "rolled_back", "rolled-back"
    if status == "rollback-failed":
        return "partial", "rollback-failed"
    if status == "not-run":
        return "not_run", "not-run"
    if status == "failed-precondition":
        return "failed", "precondition-failed"
    if package_change == "changed":
        return "partial", "execution-failed"
    if package_change == "unknown":
        return "failed", "source-failure-mutation-unknown"
    return "failed", "execution-failed"


def _repair_operations(
    report: RepairExecutionReport,
    source: OperationalAuditSource,
) -> tuple[OperationalAuditOperation, ...]:
    operations: list[OperationalAuditOperation] = []
    for index, result in enumerate(report.actions):
        before = _target_state(result, before=True)
        after = _target_state(result, before=False)
        package_change = _package_change(before, after)
        outcome, reason = _repair_outcome(result.status, package_change)
        operations.append(
            OperationalAuditOperation(
                source.source_type,
                source.sha256,
                index,
                f"repair-{result.repair_action.action.value}",
                result.repair_action.path,
                "package",
                result.status,
                outcome,
                before,
                after,
                result.rollback,
                package_change,
                outcome != "succeeded",
                reason,
            )
        )
    return tuple(operations)


def _cleanup_operations(
    report: BackupCleanupAudit,
    source: OperationalAuditSource,
) -> tuple[OperationalAuditOperation, ...]:
    operations: list[OperationalAuditOperation] = []
    for index, result in enumerate(report.actions):
        if result.status is BackupCleanupAuditStatus.DELETED:
            outcome, reason = "succeeded", "completed"
        elif result.status is BackupCleanupAuditStatus.FAILED:
            outcome, reason = "failed", "cleanup-failed"
        else:
            outcome, reason = "not_run", "not-run"
        operations.append(
            OperationalAuditOperation(
                source.source_type,
                source.sha256,
                index,
                "backup-delete-session",
                result.session,
                "backup",
                result.status.value,
                outcome,
                {
                    "files": result.before.files,
                    "bytes": result.before.bytes,
                    "tree_sha256": result.before.tree_sha256,
                },
                {"exists": result.after_exists},
                None,
                "unknown",
                outcome != "succeeded",
                reason,
            )
        )
    return tuple(operations)


def _read_sources(
    paths: Sequence[Path],
    source_type: str,
) -> list[tuple[bytes, OperationalAuditSource, RepairExecutionReport | BackupCleanupAudit]]:
    parsed: list[
        tuple[bytes, OperationalAuditSource, RepairExecutionReport | BackupCleanupAudit]
    ] = []
    for path in paths:
        try:
            content = read_input_bytes(path)
            report = (
                parse_repair_execution_report_bytes(content)
                if source_type == REPAIR_EXECUTION_SOURCE
                else parse_backup_cleanup_audit_bytes(content)
            )
        except (OSError, TypeError, ValueError) as exc:
            raise OperationalAuditInputError("invalid operational audit source") from exc
        parsed.append((content, OperationalAuditSource(source_type, 1, _sha256(content)), report))
    return parsed


def build_operational_audit(
    *,
    repair_execution_paths: Sequence[Path] = (),
    backup_cleanup_audit_paths: Sequence[Path] = (),
) -> OperationalAudit:
    """Build a deterministic summary without changing source reports or packages."""

    parsed = _read_sources(repair_execution_paths, REPAIR_EXECUTION_SOURCE)
    parsed.extend(_read_sources(backup_cleanup_audit_paths, BACKUP_CLEANUP_SOURCE))
    if not parsed:
        raise OperationalAuditInputError("at least one operational audit source is required")
    if len({content for content, _, _ in parsed}) != len(parsed):
        raise OperationalAuditInputError("duplicate operational audit source bytes")
    parsed.sort(key=lambda item: (item[1].source_type, item[1].sha256))
    sources: list[OperationalAuditSource] = []
    operations: list[OperationalAuditOperation] = []
    for _, source, report in parsed:
        sources.append(source)
        if isinstance(report, RepairExecutionReport):
            operations.extend(_repair_operations(report, source))
        else:
            operations.extend(_cleanup_operations(report, source))
    return OperationalAudit(tuple(sources), tuple(operations))


def validate_operational_audit_output_path(path: Path) -> None:
    """Require a new, link-free path for an immutable Operational Audit."""

    try:
        path.lstat()
    except FileNotFoundError:
        if path_uses_link_or_reparse(path):
            raise OperationalAuditInputError("unsafe operational audit output") from None
        return
    except OSError as exc:
        raise OperationalAuditInputError("operational audit output cannot be verified") from exc
    raise OperationalAuditInputError("operational audit output already exists")


def write_operational_audit(path: Path, audit: OperationalAudit) -> None:
    """Create an immutable deterministic report without replacing an existing entry."""

    validate_operational_audit_output_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path_uses_link_or_reparse(path.parent) or not path.parent.is_dir():
        raise OSError("unsafe operational audit output parent")
    content = (json.dumps(audit.payload(), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path, follow_symlinks=False)
        if read_input_bytes(path) != content:
            raise OSError("operational audit output verification failed")
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
