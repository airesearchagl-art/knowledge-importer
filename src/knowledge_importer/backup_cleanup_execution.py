"""Approved, explicit, irreversible cleanup of managed backup sessions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

from knowledge_importer.artifact_manifest import ArtifactDigest
from knowledge_importer.backup_cleanup_approval import (
    parse_backup_cleanup_approval_bytes,
    verify_backup_cleanup_approval,
)
from knowledge_importer.backup_cleanup_plan import (
    BackupCleanupAction,
    parse_backup_cleanup_plan_bytes,
    read_input_bytes,
)
from knowledge_importer.backup_inventory import (
    SESSION_MANIFEST_FILENAME,
    BackupInventoryInputError,
    BackupInventorySession,
    BackupSessionClassification,
    BackupSessionItem,
    BackupSessionState,
    inspect_managed_backup_session,
    is_link_or_reparse,
    parse_backup_inventory_bytes,
    path_is_within,
    path_uses_link_or_reparse,
    repository_roots,
    validate_backup_root,
)
from knowledge_importer.operation_intent import (
    BACKUP_CLEANUP,
    OPERATION_INTENT_SCHEMA_VERSION,
    OperationIntentAction,
    OperationIntentBinding,
    OperationIntentLifecycleVerification,
    OperationIntentReceipt,
    operation_intent_sha256,
    parse_operation_intent_bytes,
    validate_operation_intent_attempt_id,
    validate_operation_intent_output_path,
    write_operation_intent,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_READ_CHUNK_SIZE = 1024 * 1024


class BackupCleanupExecutionInputError(ValueError):
    """Raised when Cleanup Execution inputs or exact bindings are invalid."""


class _CleanupActionError(RuntimeError):
    pass


class BackupCleanupAuditStatus(str, Enum):
    DELETED = "deleted"
    FAILED = "failed"
    NOT_RUN = "not-run"


@dataclass(frozen=True, slots=True)
class BackupCleanupAuditBefore:
    files: int
    bytes: int
    tree_sha256: str

    def payload(self) -> dict[str, object]:
        return {
            "files": self.files,
            "bytes": self.bytes,
            "tree_sha256": self.tree_sha256,
        }


@dataclass(frozen=True, slots=True)
class BackupCleanupAuditAction:
    session: str
    status: BackupCleanupAuditStatus
    before: BackupCleanupAuditBefore
    after_exists: bool

    def payload(self) -> dict[str, object]:
        return {
            "session": self.session,
            "status": self.status.value,
            "before": self.before.payload(),
            "after": {"exists": self.after_exists},
        }


@dataclass(frozen=True, slots=True)
class BackupCleanupAuditIntentReceipt:
    schema_version: int
    attempt_id: str
    sha256: str

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "attempt_id": self.attempt_id,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class BackupCleanupAudit:
    inventory_sha256: str
    plan_sha256: str
    approval_sha256: str
    actions: tuple[BackupCleanupAuditAction, ...]
    intent_receipt: BackupCleanupAuditIntentReceipt | None = None

    @property
    def exit_code(self) -> int:
        return (
            1
            if any(action.status is BackupCleanupAuditStatus.FAILED for action in self.actions)
            else 0
        )

    def payload(self) -> dict[str, object]:
        statuses = [action.status for action in self.actions]
        payload: dict[str, object] = {
            "report_type": "knowledge-importer-backup-cleanup-audit",
            "schema_version": 1,
            "bindings": {
                "inventory_sha256": self.inventory_sha256,
                "plan_sha256": self.plan_sha256,
                "approval_sha256": self.approval_sha256,
            },
            "summary": {
                "planned": len(self.actions),
                "deleted": statuses.count(BackupCleanupAuditStatus.DELETED),
                "failed": statuses.count(BackupCleanupAuditStatus.FAILED),
                "not_run": statuses.count(BackupCleanupAuditStatus.NOT_RUN),
            },
            "actions": [action.payload() for action in self.actions],
        }
        if self.intent_receipt is not None:
            payload["intent_receipt"] = self.intent_receipt.payload()
        return payload


@dataclass(frozen=True, slots=True)
class _ExecutionInputs:
    inventory_sha256: str
    plan_sha256: str
    approval_sha256: str
    inventory_sessions: dict[str, BackupInventorySession]
    actions: tuple[BackupCleanupAction, ...]


def _comparison_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _stable_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns


def _directory_identity(path: Path) -> tuple[int, int]:
    if is_link_or_reparse(path):
        raise _CleanupActionError("unsafe cleanup directory")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise _CleanupActionError("cleanup directory cannot be verified") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise _CleanupActionError("cleanup directory was replaced")
    return metadata.st_dev, metadata.st_ino


def _digest_regular_file(path: Path) -> tuple[ArtifactDigest, tuple[int, int, int, int]]:
    if path_uses_link_or_reparse(path):
        raise _CleanupActionError("unsafe cleanup file")
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise _CleanupActionError("cleanup target is not a regular file")
        identity = _stable_identity(before)
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            if _stable_identity(os.fstat(source.fileno())) != identity:
                raise _CleanupActionError("cleanup file identity changed")
            while chunk := source.read(_READ_CHUNK_SIZE):
                size += len(chunk)
                digest.update(chunk)
        after = path.lstat()
    except OSError as exc:
        raise _CleanupActionError("cleanup file cannot be verified") from exc
    if (
        path_uses_link_or_reparse(path)
        or not stat.S_ISREG(after.st_mode)
        or _stable_identity(after) != identity
    ):
        raise _CleanupActionError("cleanup file changed during verification")
    return ArtifactDigest(size, digest.hexdigest()), identity


def _scan_session_tree(session: Path) -> tuple[set[str], set[str]]:
    if is_link_or_reparse(session) or not session.is_dir():
        raise _CleanupActionError("unsafe cleanup session")
    files: set[str] = set()
    directories: set[str] = set()
    pending: list[tuple[Path, PurePosixPath]] = [(session, PurePosixPath())]
    while pending:
        directory, relative_parent = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: _comparison_key(entry.name))
        except OSError as exc:
            raise _CleanupActionError("cleanup session cannot be scanned") from exc
        for entry in entries:
            path = Path(entry.path)
            relative = relative_parent / entry.name
            name = relative.as_posix()
            if is_link_or_reparse(path):
                raise _CleanupActionError("cleanup session contains linked entry")
            if entry.is_dir(follow_symlinks=False):
                directories.add(name)
                pending.append((path, relative))
            elif entry.is_file(follow_symlinks=False):
                files.add(name)
            else:
                raise _CleanupActionError("cleanup session contains special entry")
    return files, directories


def _expected_directories(items: tuple[BackupSessionItem, ...]) -> set[str]:
    directories: set[str] = set()
    for item in items:
        parent = PurePosixPath(item.backup).parent
        while parent.parts:
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def _assert_remaining_tree(
    backup_root: Path,
    backup_root_identity: tuple[int, int],
    session: Path,
    expected_files: set[str],
    expected_directories: set[str],
    directory_identities: dict[str, tuple[int, int]],
    file_identities: dict[str, tuple[int, int, int, int]],
) -> None:
    if _directory_identity(backup_root) != backup_root_identity:
        raise _CleanupActionError("cleanup backup root identity changed")
    files, directories = _scan_session_tree(session)
    if files != expected_files or directories != expected_directories:
        raise _CleanupActionError("cleanup session tree changed")
    for relative in {"", *expected_directories}:
        path = session if not relative else session / Path(*PurePosixPath(relative).parts)
        if _directory_identity(path) != directory_identities[relative]:
            raise _CleanupActionError("cleanup directory identity changed")
    for relative in expected_files:
        path = session / Path(*PurePosixPath(relative).parts)
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise _CleanupActionError("cleanup file identity cannot be verified") from exc
        if (
            is_link_or_reparse(path)
            or not stat.S_ISREG(metadata.st_mode)
            or _stable_identity(metadata) != file_identities[relative]
        ):
            raise _CleanupActionError("cleanup file identity changed")


def _delete_verified_file(
    path: Path,
    expected_digest: ArtifactDigest,
    expected_identity: tuple[int, int, int, int],
) -> None:
    actual_digest, identity = _digest_regular_file(path)
    if (
        identity != expected_identity
        or actual_digest.sha256 != expected_digest.sha256
        or (expected_digest.bytes is not None and actual_digest.bytes != expected_digest.bytes)
    ):
        raise _CleanupActionError("cleanup file digest changed")
    try:
        immediately_before = path.lstat()
    except OSError as exc:
        raise _CleanupActionError("cleanup file disappeared") from exc
    if (
        is_link_or_reparse(path)
        or not stat.S_ISREG(immediately_before.st_mode)
        or _stable_identity(immediately_before) != identity
    ):
        raise _CleanupActionError("cleanup file identity changed before deletion")
    try:
        path.unlink()
    except OSError as exc:
        raise _CleanupActionError("cleanup file deletion failed") from exc
    if _entry_exists(path):
        raise _CleanupActionError("cleanup file remains after deletion")


def _empty_directory_identity(path: Path) -> tuple[int, int]:
    if is_link_or_reparse(path):
        raise _CleanupActionError("unsafe cleanup directory")
    try:
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise _CleanupActionError("cleanup directory was replaced")
        with os.scandir(path) as entries:
            if next(entries, None) is not None:
                raise _CleanupActionError("cleanup directory is not empty")
    except OSError as exc:
        raise _CleanupActionError("cleanup directory cannot be verified") from exc
    return metadata.st_dev, metadata.st_ino


def _remove_verified_empty_directory(
    path: Path,
    expected_identity: tuple[int, int],
) -> None:
    identity = _empty_directory_identity(path)
    if identity != expected_identity:
        raise _CleanupActionError("cleanup directory identity changed")
    try:
        immediately_before = path.lstat()
    except OSError as exc:
        raise _CleanupActionError("cleanup directory disappeared") from exc
    if (
        is_link_or_reparse(path)
        or not stat.S_ISDIR(immediately_before.st_mode)
        or (immediately_before.st_dev, immediately_before.st_ino) != identity
    ):
        raise _CleanupActionError("cleanup directory identity changed")
    try:
        path.rmdir()
    except OSError as exc:
        raise _CleanupActionError("cleanup directory deletion failed") from exc
    if _entry_exists(path):
        raise _CleanupActionError("cleanup directory remains after deletion")


def _inventory_action_matches(
    action: BackupCleanupAction,
    session: BackupInventorySession | None,
) -> bool:
    return (
        session is not None
        and session.classification is BackupSessionClassification.MANAGED
        and session.state is BackupSessionState.COMPLETE
        and session.planning_eligible
        and action.eligible
        and action.session == session.session
        and action.session_manifest_sha256 == session.session_manifest_sha256
        and action.tree_sha256 == session.tree_sha256
        and action.backup_files == len(session.items)
        and action.backup_bytes == sum(item.digest.bytes or 0 for item in session.items)
    )


def _parse_execution_inputs(
    inventory_bytes: bytes,
    plan_bytes: bytes,
    approval_bytes: bytes,
) -> _ExecutionInputs:
    try:
        inventory = parse_backup_inventory_bytes(inventory_bytes)
        plan = parse_backup_cleanup_plan_bytes(plan_bytes)
        approval = verify_backup_cleanup_approval(plan_bytes, approval_bytes)
    except ValueError as exc:
        raise BackupCleanupExecutionInputError("invalid cleanup execution input") from exc
    inventory_sha256 = _sha256(inventory_bytes)
    if plan.inventory_sha256 != inventory_sha256:
        raise BackupCleanupExecutionInputError("cleanup Inventory binding mismatch")
    sessions = {_comparison_key(session.session): session for session in inventory.sessions}
    for action in approval.approved_actions:
        if not _inventory_action_matches(action, sessions.get(_comparison_key(action.session))):
            raise BackupCleanupExecutionInputError("cleanup action does not match Inventory")
    return _ExecutionInputs(
        inventory_sha256,
        _sha256(plan_bytes),
        _sha256(approval_bytes),
        sessions,
        approval.approved_actions,
    )


def _load_execution_inputs(
    inventory_path: Path,
    plan_path: Path,
    approval_path: Path,
) -> _ExecutionInputs:
    try:
        return _parse_execution_inputs(
            read_input_bytes(inventory_path),
            read_input_bytes(plan_path),
            read_input_bytes(approval_path),
        )
    except (OSError, ValueError) as exc:
        raise BackupCleanupExecutionInputError("invalid cleanup execution input") from exc


def _input_binding_identity(inputs: _ExecutionInputs) -> tuple[str, str, str]:
    return inputs.inventory_sha256, inputs.plan_sha256, inputs.approval_sha256


def _verify_receipted_inputs_unchanged(
    inventory_path: Path,
    plan_path: Path,
    approval_path: Path,
    expected: _ExecutionInputs,
) -> _ExecutionInputs:
    """Reparse exact lifecycle bytes and preserve the Receipt-approved scope."""

    current = _load_execution_inputs(inventory_path, plan_path, approval_path)
    if (
        _input_binding_identity(current) != _input_binding_identity(expected)
        or current.actions != expected.actions
    ):
        raise BackupCleanupExecutionInputError(
            "cleanup inputs changed after Operation Intent Receipt"
        )
    return current


def _cleanup_intent_bindings(inputs: _ExecutionInputs) -> tuple[OperationIntentBinding, ...]:
    return (
        OperationIntentBinding("backup-inventory", 1, inputs.inventory_sha256),
        OperationIntentBinding("backup-cleanup-plan", 1, inputs.plan_sha256),
        OperationIntentBinding("backup-cleanup-approval", 1, inputs.approval_sha256),
    )


def _cleanup_intent_actions(
    actions: tuple[BackupCleanupAction, ...],
) -> tuple[OperationIntentAction, ...]:
    return tuple(
        OperationIntentAction(
            index,
            "delete-backup-session",
            action.session,
            "explicit-retention-release",
        )
        for index, action in enumerate(actions)
    )


def verify_backup_cleanup_operation_intent_lifecycle(
    receipt_content: bytes,
    *,
    inventory_path: Path,
    plan_path: Path,
    approval_path: Path,
) -> OperationIntentLifecycleVerification:
    """Compare stable current Cleanup lifecycle inputs without inspecting backup state."""

    receipt = parse_operation_intent_bytes(receipt_content)
    if receipt.operation_type != BACKUP_CLEANUP:
        raise BackupCleanupExecutionInputError("Receipt operation type mismatch")
    try:
        inventory_content = read_input_bytes(inventory_path)
        plan_content = read_input_bytes(plan_path)
        approval_content = read_input_bytes(approval_path)
        parse_backup_inventory_bytes(inventory_content)
        parse_backup_cleanup_plan_bytes(plan_content)
        parse_backup_cleanup_approval_bytes(approval_content)
        stable_contents = (
            read_input_bytes(inventory_path),
            read_input_bytes(plan_path),
            read_input_bytes(approval_path),
        )
    except (OSError, ValueError) as exc:
        raise BackupCleanupExecutionInputError("invalid Cleanup lifecycle input") from exc
    contents = (inventory_content, plan_content, approval_content)
    if contents != stable_contents:
        raise BackupCleanupExecutionInputError(
            "Cleanup lifecycle inputs changed during verification"
        )
    current_bindings = (
        OperationIntentBinding("backup-inventory", 1, _sha256(inventory_content)),
        OperationIntentBinding("backup-cleanup-plan", 1, _sha256(plan_content)),
        OperationIntentBinding("backup-cleanup-approval", 1, _sha256(approval_content)),
    )
    if receipt.bindings != current_bindings:
        return OperationIntentLifecycleVerification(False, None)

    inputs = _load_execution_inputs(inventory_path, plan_path, approval_path)
    if _cleanup_intent_bindings(inputs) != current_bindings:
        raise BackupCleanupExecutionInputError(
            "Cleanup lifecycle inputs changed during verification"
        )
    return OperationIntentLifecycleVerification(
        True,
        receipt.actions == _cleanup_intent_actions(inputs.actions),
    )


def _create_cleanup_operation_intent(
    path: Path,
    *,
    inputs: _ExecutionInputs,
    attempt_id: str,
) -> tuple[BackupCleanupAuditIntentReceipt, bytes]:
    receipt = OperationIntentReceipt(
        validate_operation_intent_attempt_id(attempt_id),
        BACKUP_CLEANUP,
        _cleanup_intent_bindings(inputs),
        _cleanup_intent_actions(inputs.actions),
    )
    try:
        write_operation_intent(path, receipt)
        content = read_input_bytes(path)
        if parse_operation_intent_bytes(content) != receipt:
            raise ValueError("Operation Intent Receipt changed after creation")
        digest = operation_intent_sha256(content)
    except (OSError, ValueError) as exc:
        raise BackupCleanupExecutionInputError(
            "Operation Intent Receipt cannot be created"
        ) from exc
    return (
        BackupCleanupAuditIntentReceipt(
            OPERATION_INTENT_SCHEMA_VERSION,
            receipt.attempt_id,
            digest,
        ),
        content,
    )


def _paths_are_equal(first: Path, second: Path) -> bool:
    return (
        str(first.resolve(strict=False)).casefold() == str(second.resolve(strict=False)).casefold()
    )


def _validate_receipted_mode(
    package_root: Path,
    backup_root: Path,
    *,
    inventory_path: Path,
    plan_path: Path,
    approval_path: Path,
    report_path: Path | None,
    intent_receipt_path: Path | None,
    attempt_id: str | None,
) -> None:
    if intent_receipt_path is None:
        if attempt_id is not None:
            raise BackupCleanupExecutionInputError("attempt_id requires Operation Intent Receipt")
        return
    if attempt_id is None or report_path is None:
        raise BackupCleanupExecutionInputError(
            "receipted cleanup requires attempt_id and Cleanup Audit"
        )
    try:
        validate_operation_intent_attempt_id(attempt_id)
        validate_operation_intent_output_path(intent_receipt_path)
        validate_backup_cleanup_audit_output_path(report_path)
    except ValueError as exc:
        raise BackupCleanupExecutionInputError("invalid receipted cleanup output") from exc
    protected = (inventory_path, plan_path, approval_path, report_path)
    try:
        conflicts = (
            path_is_within(intent_receipt_path, package_root)
            or path_is_within(intent_receipt_path, backup_root)
            or any(_paths_are_equal(intent_receipt_path, path) for path in protected)
        )
    except OSError as exc:
        raise BackupCleanupExecutionInputError(
            "Operation Intent Receipt output cannot be verified"
        ) from exc
    if conflicts:
        raise BackupCleanupExecutionInputError("Operation Intent Receipt output conflicts")


def _validate_cleanup_roots(
    package_root: Path,
    backup_root: Path,
) -> tuple[int, int]:
    try:
        validate_backup_root(package_root, backup_root)
        package_resolved = package_root.resolve()
        backup_resolved = backup_root.resolve(strict=False)
        overlaps_repository = any(
            backup_resolved == root or backup_resolved.is_relative_to(root)
            for root in repository_roots(backup_root)
        )
        package_inside_backup = package_resolved.is_relative_to(backup_resolved)
    except (BackupInventoryInputError, OSError) as exc:
        raise BackupCleanupExecutionInputError("unsafe cleanup backup root") from exc
    if overlaps_repository or package_inside_backup:
        raise BackupCleanupExecutionInputError("unsafe cleanup backup root")
    try:
        return _directory_identity(backup_root)
    except _CleanupActionError as exc:
        raise BackupCleanupExecutionInputError("unsafe cleanup backup root") from exc


def backup_cleanup_current_state_matches(
    package_root: Path,
    backup_root: Path,
    *,
    inventory_path: Path,
    plan_path: Path,
    approval_path: Path,
) -> bool:
    """Compare only approved sessions with current backup state, without mutation."""

    backup_root_identity = _validate_cleanup_roots(package_root, backup_root)
    inputs = _load_execution_inputs(inventory_path, plan_path, approval_path)
    for action in inputs.actions:
        expected = inputs.inventory_sessions[_comparison_key(action.session)]
        try:
            actual = _validate_actual_session(backup_root, action, expected)
            _capture_session_directory_identities(backup_root / action.session, actual.items)
            _capture_session_file_identities(backup_root / action.session, actual.items)
        except _CleanupActionError:
            return False
    if _validate_cleanup_roots(package_root, backup_root) != backup_root_identity:
        raise BackupCleanupExecutionInputError("cleanup backup root identity changed")
    return True


def _verify_receipted_output_state(
    receipt_path: Path,
    receipt_content: bytes,
    receipt_binding: BackupCleanupAuditIntentReceipt,
    report_path: Path,
) -> None:
    try:
        validate_backup_cleanup_audit_output_path(report_path)
        current_receipt_content = read_input_bytes(receipt_path)
        if (
            current_receipt_content != receipt_content
            or operation_intent_sha256(current_receipt_content) != receipt_binding.sha256
        ):
            raise ValueError("Operation Intent Receipt changed")
    except (OSError, ValueError) as exc:
        raise BackupCleanupExecutionInputError(
            "receipted cleanup output changed after Operation Intent Receipt"
        ) from exc


def _validate_actual_session(
    backup_root: Path,
    action: BackupCleanupAction,
    expected: BackupInventorySession,
) -> BackupInventorySession:
    session_path = backup_root / action.session
    actual = inspect_managed_backup_session(session_path)
    if actual != expected or not _inventory_action_matches(action, actual):
        raise _CleanupActionError("cleanup session no longer matches Inventory")
    return actual


def _capture_session_directory_identities(
    session_path: Path,
    items: tuple[BackupSessionItem, ...],
) -> dict[str, tuple[int, int]]:
    identities = {"": _directory_identity(session_path)}
    for relative in sorted(_expected_directories(items), key=_comparison_key):
        path = session_path / Path(*PurePosixPath(relative).parts)
        identities[relative] = _directory_identity(path)
    return identities


def _capture_session_file_identities(
    session_path: Path,
    items: tuple[BackupSessionItem, ...],
) -> dict[str, tuple[int, int, int, int]]:
    relative_paths = {SESSION_MANIFEST_FILENAME, *(item.backup for item in items)}
    identities: dict[str, tuple[int, int, int, int]] = {}
    for relative in sorted(relative_paths, key=_comparison_key):
        path = session_path / Path(*PurePosixPath(relative).parts)
        if is_link_or_reparse(path):
            raise _CleanupActionError("unsafe cleanup file")
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise _CleanupActionError("cleanup file identity cannot be verified") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise _CleanupActionError("cleanup file was replaced")
        identities[relative] = _stable_identity(metadata)
    return identities


def _delete_session(
    backup_root: Path,
    backup_root_identity: tuple[int, int],
    action: BackupCleanupAction,
    session: BackupInventorySession,
    directory_identities: dict[str, tuple[int, int]],
    file_identities: dict[str, tuple[int, int, int, int]],
) -> None:
    session_path = backup_root / action.session
    backup_files = sorted(
        session.items,
        key=lambda item: (-len(PurePosixPath(item.backup).parts), _comparison_key(item.backup)),
    )
    remaining_files = {SESSION_MANIFEST_FILENAME, *(item.backup for item in session.items)}
    remaining_directories = _expected_directories(session.items)
    _assert_remaining_tree(
        backup_root,
        backup_root_identity,
        session_path,
        remaining_files,
        remaining_directories,
        directory_identities,
        file_identities,
    )

    for item in backup_files:
        _assert_remaining_tree(
            backup_root,
            backup_root_identity,
            session_path,
            remaining_files,
            remaining_directories,
            directory_identities,
            file_identities,
        )
        path = session_path / Path(*PurePosixPath(item.backup).parts)
        _delete_verified_file(path, item.digest, file_identities[item.backup])
        remaining_files.remove(item.backup)

    _assert_remaining_tree(
        backup_root,
        backup_root_identity,
        session_path,
        remaining_files,
        remaining_directories,
        directory_identities,
        file_identities,
    )
    manifest_path = session_path / SESSION_MANIFEST_FILENAME
    _delete_verified_file(
        manifest_path,
        ArtifactDigest(None, action.session_manifest_sha256),
        file_identities[SESSION_MANIFEST_FILENAME],
    )
    remaining_files.remove(SESSION_MANIFEST_FILENAME)

    for relative in sorted(
        remaining_directories,
        key=lambda value: (-len(PurePosixPath(value).parts), _comparison_key(value)),
    ):
        _assert_remaining_tree(
            backup_root,
            backup_root_identity,
            session_path,
            remaining_files,
            remaining_directories,
            directory_identities,
            file_identities,
        )
        directory = session_path / Path(*PurePosixPath(relative).parts)
        _remove_verified_empty_directory(directory, directory_identities[relative])
        remaining_directories.remove(relative)

    _assert_remaining_tree(
        backup_root,
        backup_root_identity,
        session_path,
        set(),
        set(),
        directory_identities,
        file_identities,
    )
    _remove_verified_empty_directory(session_path, directory_identities[""])


def _before(action: BackupCleanupAction) -> BackupCleanupAuditBefore:
    assert action.tree_sha256 is not None
    return BackupCleanupAuditBefore(
        action.backup_files,
        action.backup_bytes,
        action.tree_sha256,
    )


def _failed_precondition_audit(
    inputs: _ExecutionInputs,
    *,
    failed_index: int,
    backup_root: Path,
    intent_receipt: BackupCleanupAuditIntentReceipt,
) -> BackupCleanupAudit:
    actions = tuple(
        BackupCleanupAuditAction(
            action.session,
            (
                BackupCleanupAuditStatus.FAILED
                if index == failed_index
                else BackupCleanupAuditStatus.NOT_RUN
            ),
            _before(action),
            _entry_exists(backup_root / action.session),
        )
        for index, action in enumerate(inputs.actions)
    )
    return BackupCleanupAudit(
        inputs.inventory_sha256,
        inputs.plan_sha256,
        inputs.approval_sha256,
        actions,
        intent_receipt,
    )


def _prevalidate_receipted_actions(
    backup_root: Path,
    inputs: _ExecutionInputs,
) -> int | None:
    for index, action in enumerate(inputs.actions):
        try:
            expected = inputs.inventory_sessions[_comparison_key(action.session)]
            _validate_actual_session(backup_root, action, expected)
            _capture_session_directory_identities(backup_root / action.session, expected.items)
            _capture_session_file_identities(backup_root / action.session, expected.items)
        except Exception:  # noqa: BLE001 - precondition result is sanitized in the Audit.
            return index
    return None


def execute_backup_cleanup(
    package_root: Path,
    backup_root: Path,
    *,
    inventory_path: Path,
    plan_path: Path,
    approval_path: Path,
    report_path: Path | None = None,
    intent_receipt_path: Path | None = None,
    attempt_id: str | None = None,
) -> BackupCleanupAudit:
    """Execute only exactly approved sessions; no rollback is attempted."""

    _validate_receipted_mode(
        package_root,
        backup_root,
        inventory_path=inventory_path,
        plan_path=plan_path,
        approval_path=approval_path,
        report_path=report_path,
        intent_receipt_path=intent_receipt_path,
        attempt_id=attempt_id,
    )
    backup_root_identity = _validate_cleanup_roots(package_root, backup_root)
    inputs = _load_execution_inputs(inventory_path, plan_path, approval_path)
    intent_receipt: BackupCleanupAuditIntentReceipt | None = None
    receipt_content: bytes | None = None
    if intent_receipt_path is not None:
        assert attempt_id is not None
        intent_receipt, receipt_content = _create_cleanup_operation_intent(
            intent_receipt_path,
            inputs=inputs,
            attempt_id=attempt_id,
        )
        rebound_inputs = _verify_receipted_inputs_unchanged(
            inventory_path,
            plan_path,
            approval_path,
            inputs,
        )
        assert report_path is not None
        _verify_receipted_output_state(
            intent_receipt_path,
            receipt_content,
            intent_receipt,
            report_path,
        )
        inputs = rebound_inputs
        try:
            rebound_root_identity = _validate_cleanup_roots(package_root, backup_root)
        except BackupCleanupExecutionInputError:
            if inputs.actions:
                return _failed_precondition_audit(
                    inputs,
                    failed_index=0,
                    backup_root=backup_root,
                    intent_receipt=intent_receipt,
                )
            raise
        if rebound_root_identity != backup_root_identity:
            if inputs.actions:
                return _failed_precondition_audit(
                    inputs,
                    failed_index=0,
                    backup_root=backup_root,
                    intent_receipt=intent_receipt,
                )
            raise BackupCleanupExecutionInputError("cleanup backup root identity changed")
        backup_root_identity = rebound_root_identity
        failed_precondition = _prevalidate_receipted_actions(backup_root, inputs)
        if failed_precondition is not None:
            return _failed_precondition_audit(
                inputs,
                failed_index=failed_precondition,
                backup_root=backup_root,
                intent_receipt=intent_receipt,
            )
    audit_actions: list[BackupCleanupAuditAction] = []
    failed = False
    for action in inputs.actions:
        session_path = backup_root / action.session
        if failed:
            audit_actions.append(
                BackupCleanupAuditAction(
                    action.session,
                    BackupCleanupAuditStatus.NOT_RUN,
                    _before(action),
                    _entry_exists(session_path),
                )
            )
            continue
        if intent_receipt_path is not None:
            assert receipt_content is not None
            assert intent_receipt is not None
            assert report_path is not None
            _verify_receipted_output_state(
                intent_receipt_path,
                receipt_content,
                intent_receipt,
                report_path,
            )
            _verify_receipted_inputs_unchanged(
                inventory_path,
                plan_path,
                approval_path,
                inputs,
            )
        try:
            if (
                intent_receipt is not None
                and _validate_cleanup_roots(package_root, backup_root) != backup_root_identity
            ):
                raise _CleanupActionError("cleanup root identity changed")
            expected = inputs.inventory_sessions[_comparison_key(action.session)]
            directory_identities = _capture_session_directory_identities(
                session_path, expected.items
            )
            file_identities = _capture_session_file_identities(session_path, expected.items)
            actual = _validate_actual_session(backup_root, action, expected)
            if directory_identities != _capture_session_directory_identities(
                session_path, expected.items
            ) or file_identities != _capture_session_file_identities(session_path, expected.items):
                raise _CleanupActionError("cleanup directory identity changed")
            _delete_session(
                backup_root,
                backup_root_identity,
                action,
                actual,
                directory_identities,
                file_identities,
            )
            if _entry_exists(session_path):
                raise _CleanupActionError("cleanup session remains after deletion")
        except Exception:  # noqa: BLE001 - action failure is sanitized in the Audit.
            failed = True
            audit_actions.append(
                BackupCleanupAuditAction(
                    action.session,
                    BackupCleanupAuditStatus.FAILED,
                    _before(action),
                    _entry_exists(session_path),
                )
            )
        else:
            audit_actions.append(
                BackupCleanupAuditAction(
                    action.session,
                    BackupCleanupAuditStatus.DELETED,
                    _before(action),
                    _entry_exists(session_path),
                )
            )
    return BackupCleanupAudit(
        inputs.inventory_sha256,
        inputs.plan_sha256,
        inputs.approval_sha256,
        tuple(audit_actions),
        intent_receipt,
    )


def parse_backup_cleanup_audit_bytes(content: bytes) -> BackupCleanupAudit:
    """Parse and validate Backup Cleanup Audit schema v1."""

    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid Backup Cleanup Audit JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid Backup Cleanup Audit root")
    bindings = payload.get("bindings")
    summary = payload.get("summary")
    raw_actions = payload.get("actions")
    schema_version = payload.get("schema_version")
    intent_receipt: BackupCleanupAuditIntentReceipt | None = None
    if "intent_receipt" in payload:
        value = payload["intent_receipt"]
        if not isinstance(value, dict):
            raise ValueError("invalid Cleanup Audit Intent Receipt binding")
        intent_schema_version = value.get("schema_version")
        intent_sha256 = value.get("sha256")
        if not (
            isinstance(intent_schema_version, int)
            and not isinstance(intent_schema_version, bool)
            and intent_schema_version == OPERATION_INTENT_SCHEMA_VERSION
            and _is_sha256(intent_sha256)
        ):
            raise ValueError("invalid Cleanup Audit Intent Receipt binding")
        intent_receipt = BackupCleanupAuditIntentReceipt(
            intent_schema_version,
            validate_operation_intent_attempt_id(value.get("attempt_id")),
            intent_sha256,
        )
    if not (
        payload.get("report_type") == "knowledge-importer-backup-cleanup-audit"
        and schema_version == 1
        and not isinstance(schema_version, bool)
        and isinstance(bindings, dict)
        and all(
            _is_sha256(bindings.get(key))
            for key in ("inventory_sha256", "plan_sha256", "approval_sha256")
        )
        and isinstance(summary, dict)
        and isinstance(raw_actions, list)
    ):
        raise ValueError("invalid Backup Cleanup Audit schema")
    actions: list[BackupCleanupAuditAction] = []
    for value in raw_actions:
        if not isinstance(value, dict):
            raise ValueError("invalid Backup Cleanup Audit action")
        before = value.get("before")
        after = value.get("after")
        status = value.get("status")
        if not (
            isinstance(value.get("session"), str)
            and value["session"]
            and "/" not in value["session"]
            and "\\" not in value["session"]
            and not any(
                unicodedata.category(character) in {"Cc", "Cf"} for character in value["session"]
            )
            and status in {candidate.value for candidate in BackupCleanupAuditStatus}
            and isinstance(before, dict)
            and _is_nonnegative_int(before.get("files"))
            and _is_nonnegative_int(before.get("bytes"))
            and _is_sha256(before.get("tree_sha256"))
            and isinstance(after, dict)
            and isinstance(after.get("exists"), bool)
            and (status != BackupCleanupAuditStatus.DELETED.value or not after["exists"])
        ):
            raise ValueError("invalid Backup Cleanup Audit action semantics")
        actions.append(
            BackupCleanupAuditAction(
                value["session"],
                BackupCleanupAuditStatus(status),
                BackupCleanupAuditBefore(before["files"], before["bytes"], before["tree_sha256"]),
                after["exists"],
            )
        )
    parsed = BackupCleanupAudit(
        bindings["inventory_sha256"],
        bindings["plan_sha256"],
        bindings["approval_sha256"],
        tuple(actions),
        intent_receipt,
    )
    expected_summary = parsed.payload()["summary"]
    if (
        summary != expected_summary
        or list(actions) != sorted(actions, key=lambda action: _comparison_key(action.session))
        or len({_comparison_key(action.session) for action in actions}) != len(actions)
    ):
        raise ValueError("invalid Backup Cleanup Audit summary or order")
    return parsed


def backup_cleanup_audit_bytes(audit: BackupCleanupAudit) -> bytes:
    """Serialize one deterministic Cleanup Audit using its v1 field order."""

    content = (json.dumps(audit.payload(), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    parse_backup_cleanup_audit_bytes(content)
    return content


def verify_backup_cleanup_intent(
    receipt_content: bytes,
    audit_content: bytes,
    *,
    inventory_content: bytes,
    plan_content: bytes,
    approval_content: bytes,
) -> None:
    """Verify exact Receipt/Audit/input/action binding for one cleanup attempt."""

    receipt = parse_operation_intent_bytes(receipt_content)
    audit = parse_backup_cleanup_audit_bytes(audit_content)
    inputs = _parse_execution_inputs(inventory_content, plan_content, approval_content)
    receipt_sha256 = operation_intent_sha256(receipt_content)
    binding = audit.intent_receipt
    audit_actions = tuple(
        OperationIntentAction(
            index,
            "delete-backup-session",
            action.session,
            "explicit-retention-release",
        )
        for index, action in enumerate(audit.actions)
    )
    if not (
        binding is not None
        and binding.schema_version == OPERATION_INTENT_SCHEMA_VERSION
        and binding.attempt_id == receipt.attempt_id
        and binding.sha256 == receipt_sha256
        and receipt.operation_type == BACKUP_CLEANUP
        and receipt.bindings == _cleanup_intent_bindings(inputs)
        and receipt.actions == _cleanup_intent_actions(inputs.actions)
        and audit_actions == receipt.actions
        and audit.inventory_sha256 == inputs.inventory_sha256
        and audit.plan_sha256 == inputs.plan_sha256
        and audit.approval_sha256 == inputs.approval_sha256
        and len(audit.actions) == len(inputs.actions)
        and all(
            audit_action.before == _before(input_action)
            for audit_action, input_action in zip(audit.actions, inputs.actions, strict=True)
        )
    ):
        raise ValueError("Backup Cleanup Intent binding mismatch")


def is_backup_cleanup_audit_report(path: Path) -> bool:
    """Return whether an existing regular file is a Cleanup Audit v1."""

    try:
        if path_uses_link_or_reparse(path):
            return False
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            return False
        content = read_input_bytes(path)
        parse_backup_cleanup_audit_bytes(content)
        after = path.lstat()
    except (OSError, ValueError):
        return False
    return _stable_identity(after) == _stable_identity(before) and not path_uses_link_or_reparse(
        path
    )


def validate_backup_cleanup_audit_output_path(path: Path) -> None:
    """Require a new, link-free path for an immutable Cleanup Audit."""

    try:
        path.lstat()
    except FileNotFoundError:
        if path_uses_link_or_reparse(path):
            raise ValueError("unsafe Cleanup Audit output") from None
        return
    except OSError as exc:
        raise ValueError("Cleanup Audit output cannot be verified") from exc
    raise ValueError("Cleanup Audit output already exists")


def write_backup_cleanup_audit(
    path: Path,
    audit: BackupCleanupAudit,
) -> None:
    """Create a deterministic Audit without replacing any existing entry."""

    try:
        validate_backup_cleanup_audit_output_path(path)
    except ValueError as exc:
        raise OSError("unsafe Cleanup Audit output") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    if path_uses_link_or_reparse(path.parent) or not path.parent.is_dir():
        raise OSError("unsafe Cleanup Audit output parent")
    content = backup_cleanup_audit_bytes(audit)
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
            raise OSError("Cleanup Audit output verification failed")
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
