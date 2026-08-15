"""Read-only aggregation of immutable repair and backup operation evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from collections import Counter
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

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
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_SOURCE_TYPES = {REPAIR_EXECUTION_SOURCE, BACKUP_CLEANUP_SOURCE}
_REPAIR_ACTIONS = {"repair-regenerate-sidecar", "repair-remove-stale-sidecar"}
_REPAIR_STATUS_ROLLBACK = {
    "succeeded": "available",
    "failed-precondition": "not-required",
    "failed": "not-required",
    "rolled-back": "completed",
    "rollback-failed": "failed",
    "not-run": "not-required",
}


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


@dataclass(frozen=True, slots=True)
class OperationalAuditVerification:
    sources_expected: int
    sources_provided: int
    matched: int
    missing: int
    unexpected: int
    invalid: int

    @property
    def exit_code(self) -> int:
        if self.invalid:
            return 2
        if self.missing or self.unexpected:
            return 1
        return 0

    @property
    def result(self) -> str:
        return {0: "verified", 1: "mismatch", 2: "invalid"}[self.exit_code]

    def console_summary(self) -> str:
        return (
            "Operational Audit Verify: "
            f"sources_expected={self.sources_expected} "
            f"sources_provided={self.sources_provided} matched={self.matched} "
            f"missing={self.missing} unexpected={self.unexpected} "
            f"invalid={self.invalid} result={self.result}"
        )


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


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_safe_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and not any(unicodedata.category(character) in {"Cc", "Cf"} for character in value)
    )


def _is_relative_posix_path(value: object) -> bool:
    if not _is_safe_text(value) or "\\" in value or _WINDOWS_DRIVE.match(value) is not None:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def _parse_repair_state(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("invalid Operational Audit repair state")
    exists = value.get("exists")
    size = value.get("bytes")
    sha256 = value.get("sha256")
    digest_valid = (size is None and sha256 is None) or (
        _is_nonnegative_int(size) and _is_sha256(sha256)
    )
    if not isinstance(exists, bool) or not digest_valid or (not exists and size is not None):
        raise ValueError("invalid Operational Audit repair state")
    return {"exists": exists, "bytes": size, "sha256": sha256}


def _parse_cleanup_before(value: object) -> dict[str, object]:
    if not (
        isinstance(value, dict)
        and _is_nonnegative_int(value.get("files"))
        and _is_nonnegative_int(value.get("bytes"))
        and _is_sha256(value.get("tree_sha256"))
    ):
        raise ValueError("invalid Operational Audit cleanup before state")
    return {
        "files": value["files"],
        "bytes": value["bytes"],
        "tree_sha256": value["tree_sha256"],
    }


def _parse_cleanup_after(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not isinstance(value.get("exists"), bool):
        raise ValueError("invalid Operational Audit cleanup after state")
    return {"exists": value["exists"]}


def _parse_operational_source(value: object) -> OperationalAuditSource:
    if not isinstance(value, dict):
        raise ValueError("invalid Operational Audit source")
    source_type = value.get("source_type")
    schema_version = value.get("schema_version")
    sha256 = value.get("sha256")
    if not (
        isinstance(source_type, str)
        and source_type in _SOURCE_TYPES
        and isinstance(schema_version, int)
        and not isinstance(schema_version, bool)
        and schema_version == 1
        and _is_sha256(sha256)
    ):
        raise ValueError("invalid Operational Audit source semantics")
    return OperationalAuditSource(source_type, schema_version, sha256)


def _parse_repair_operation(
    value: dict[str, object],
    source: OperationalAuditSource,
    source_action_index: int,
) -> OperationalAuditOperation:
    action = value.get("action")
    target = value.get("target")
    source_status = value.get("source_status")
    rollback = value.get("rollback")
    if not (
        isinstance(action, str)
        and action in _REPAIR_ACTIONS
        and _is_relative_posix_path(target)
        and isinstance(source_status, str)
        and _REPAIR_STATUS_ROLLBACK.get(source_status) == rollback
        and value.get("mutation_scope") == "package"
    ):
        raise ValueError("invalid Operational Audit repair operation")
    before = _parse_repair_state(value.get("before"))
    after = _parse_repair_state(value.get("after"))
    if source_status == "succeeded" and (
        action == "repair-regenerate-sidecar"
        and (
            before["exists"] is not False or after["exists"] is not True or after["sha256"] is None
        )
        or action == "repair-remove-stale-sidecar"
        and (
            before["exists"] is not True or before["sha256"] is None or after["exists"] is not False
        )
    ):
        raise ValueError("invalid Operational Audit successful repair state")
    if source_status in {"failed-precondition", "not-run", "rolled-back"} and before != after:
        raise ValueError("invalid Operational Audit unchanged repair state")
    package_change = _package_change(before, after)
    outcome, reason = _repair_outcome(source_status, package_change)
    if not (
        value.get("outcome") == outcome
        and value.get("package_change") == package_change
        and value.get("operator_action_required") is (outcome != "succeeded")
        and value.get("reason") == reason
    ):
        raise ValueError("invalid Operational Audit repair outcome semantics")
    return OperationalAuditOperation(
        source.source_type,
        source.sha256,
        source_action_index,
        action,
        target,
        "package",
        source_status,
        outcome,
        before,
        after,
        rollback,
        package_change,
        outcome != "succeeded",
        reason,
    )


def _parse_cleanup_operation(
    value: dict[str, object],
    source: OperationalAuditSource,
    source_action_index: int,
) -> OperationalAuditOperation:
    target = value.get("target")
    source_status = value.get("source_status")
    if not (
        value.get("action") == "backup-delete-session"
        and _is_safe_text(target)
        and "/" not in target
        and "\\" not in target
        and isinstance(source_status, str)
        and source_status in {status.value for status in BackupCleanupAuditStatus}
        and value.get("mutation_scope") == "backup"
        and value.get("rollback") is None
    ):
        raise ValueError("invalid Operational Audit cleanup operation")
    before = _parse_cleanup_before(value.get("before"))
    after = _parse_cleanup_after(value.get("after"))
    if source_status == BackupCleanupAuditStatus.DELETED.value:
        outcome, reason = "succeeded", "completed"
        if after["exists"] is not False:
            raise ValueError("invalid Operational Audit deleted state")
    elif source_status == BackupCleanupAuditStatus.FAILED.value:
        outcome, reason = "failed", "cleanup-failed"
    else:
        outcome, reason = "not_run", "not-run"
    if not (
        value.get("outcome") == outcome
        and value.get("package_change") == "unknown"
        and value.get("operator_action_required") is (outcome != "succeeded")
        and value.get("reason") == reason
    ):
        raise ValueError("invalid Operational Audit cleanup outcome semantics")
    return OperationalAuditOperation(
        source.source_type,
        source.sha256,
        source_action_index,
        "backup-delete-session",
        target,
        "backup",
        source_status,
        outcome,
        before,
        after,
        None,
        "unknown",
        outcome != "succeeded",
        reason,
    )


def _parse_operational_operation(
    value: object,
    sources: dict[tuple[str, str], OperationalAuditSource],
) -> OperationalAuditOperation:
    if not isinstance(value, dict):
        raise ValueError("invalid Operational Audit operation")
    source_type = value.get("source_type")
    source_sha256 = value.get("source_sha256")
    source_action_index = value.get("source_action_index")
    if not (
        isinstance(source_type, str)
        and isinstance(source_sha256, str)
        and _is_nonnegative_int(source_action_index)
        and (source_type, source_sha256) in sources
    ):
        raise ValueError("invalid Operational Audit operation source")
    source = sources[(source_type, source_sha256)]
    if source_type == REPAIR_EXECUTION_SOURCE:
        return _parse_repair_operation(value, source, source_action_index)
    return _parse_cleanup_operation(value, source, source_action_index)


def parse_operational_audit_bytes(content: bytes) -> OperationalAudit:
    """Parse and semantically validate Operational Audit Summary schema v1."""

    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid Operational Audit JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid Operational Audit root")
    schema_version = payload.get("schema_version")
    summary = payload.get("summary")
    raw_sources = payload.get("sources")
    raw_operations = payload.get("operations")
    if not (
        payload.get("report_type") == "knowledge-importer-operational-audit"
        and isinstance(schema_version, int)
        and not isinstance(schema_version, bool)
        and schema_version == 1
        and isinstance(summary, dict)
        and isinstance(raw_sources, list)
        and bool(raw_sources)
        and isinstance(raw_operations, list)
    ):
        raise ValueError("invalid Operational Audit schema")

    sources = tuple(_parse_operational_source(value) for value in raw_sources)
    source_keys = [(source.source_type, source.sha256) for source in sources]
    if source_keys != sorted(source_keys) or len(set(source_keys)) != len(source_keys):
        raise ValueError("invalid Operational Audit source order or duplicate")
    source_by_key = {(source.source_type, source.sha256): source for source in sources}
    operations = tuple(
        _parse_operational_operation(value, source_by_key) for value in raw_operations
    )
    source_order = {key: index for index, key in enumerate(source_keys)}
    operation_keys = [
        (
            source_order[(operation.source_type, operation.source_sha256)],
            operation.source_action_index,
        )
        for operation in operations
    ]
    if operation_keys != sorted(operation_keys):
        raise ValueError("invalid Operational Audit operation order")
    for source_key in source_keys:
        indices = [
            operation.source_action_index
            for operation in operations
            if (operation.source_type, operation.source_sha256) == source_key
        ]
        if indices != list(range(len(indices))):
            raise ValueError("invalid Operational Audit source action index")

    audit = OperationalAudit(sources, operations)
    expected_summary = audit.payload()["summary"]
    assert isinstance(expected_summary, dict)
    for key, expected in expected_summary.items():
        actual = summary.get(key)
        if isinstance(expected, int) and not isinstance(expected, bool):
            if not _is_nonnegative_int(actual) or actual != expected:
                raise ValueError("invalid Operational Audit summary")
        elif actual is not expected:
            raise ValueError("invalid Operational Audit package change summary")
    return audit


def verify_operational_audit_sources(
    audit_path: Path,
    *,
    repair_execution_paths: Sequence[Path] = (),
    backup_cleanup_audit_paths: Sequence[Path] = (),
) -> OperationalAuditVerification:
    """Verify the current exact source bytes against one immutable Audit Summary."""

    try:
        audit = parse_operational_audit_bytes(read_input_bytes(audit_path))
    except (OSError, TypeError, ValueError) as exc:
        raise OperationalAuditInputError("invalid Operational Audit Summary") from exc

    provided: list[tuple[str, str, bytes]] = []
    for source_type, paths in (
        (REPAIR_EXECUTION_SOURCE, repair_execution_paths),
        (BACKUP_CLEANUP_SOURCE, backup_cleanup_audit_paths),
    ):
        for path in paths:
            try:
                content = read_input_bytes(path)
            except (OSError, TypeError, ValueError) as exc:
                raise OperationalAuditInputError("operational audit source cannot be read") from exc
            provided.append((source_type, _sha256(content), content))

    expected_counter = Counter((source.source_type, source.sha256) for source in audit.sources)
    provided_counter = Counter((source_type, sha256) for source_type, sha256, _ in provided)
    matched = sum((expected_counter & provided_counter).values())
    missing = sum((expected_counter - provided_counter).values())
    unexpected = sum((provided_counter - expected_counter).values())
    if missing or unexpected:
        return OperationalAuditVerification(
            len(audit.sources), len(provided), matched, missing, unexpected, 0
        )

    invalid = 0
    parsed: list[tuple[OperationalAuditSource, RepairExecutionReport | BackupCleanupAudit]] = []
    for source_type, sha256, content in provided:
        try:
            report = (
                parse_repair_execution_report_bytes(content)
                if source_type == REPAIR_EXECUTION_SOURCE
                else parse_backup_cleanup_audit_bytes(content)
            )
        except (TypeError, ValueError):
            invalid += 1
            continue
        parsed.append((OperationalAuditSource(source_type, 1, sha256), report))
    if invalid:
        return OperationalAuditVerification(
            len(audit.sources), len(provided), matched, 0, 0, invalid
        )

    parsed.sort(key=lambda item: (item[0].source_type, item[0].sha256))
    reconstructed_sources: list[OperationalAuditSource] = []
    reconstructed_operations: list[OperationalAuditOperation] = []
    for source, report in parsed:
        reconstructed_sources.append(source)
        if isinstance(report, RepairExecutionReport):
            reconstructed_operations.extend(_repair_operations(report, source))
        else:
            reconstructed_operations.extend(_cleanup_operations(report, source))
    reconstructed = OperationalAudit(tuple(reconstructed_sources), tuple(reconstructed_operations))
    if reconstructed != audit:
        invalid = 1
    return OperationalAuditVerification(len(audit.sources), len(provided), matched, 0, 0, invalid)


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
