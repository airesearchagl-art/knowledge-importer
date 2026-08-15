"""Safe, bounded Repair Execution v1 for approved Knowledge Package actions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from knowledge_importer.artifact_manifest import (
    ArtifactDigest,
    ArtifactManifestItem,
    ManifestStatus,
    digest_file,
)
from knowledge_importer.backup_cleanup_plan import read_input_bytes
from knowledge_importer.backup_inventory import (
    MANAGED_SESSION_PREFIX,
    SESSION_MANIFEST_FILENAME,
    BackupSessionBindings,
    BackupSessionItem,
    BackupSessionManifest,
    BackupSessionState,
    is_link_or_reparse,
    path_is_within,
    transition_backup_session,
    write_backup_session_manifest,
)
from knowledge_importer.document_metadata import (
    DocumentMetadataSettings,
    DocumentMetadataSidecar,
    build_document_metadata,
)
from knowledge_importer.json_writer import write_json_atomically
from knowledge_importer.operation_intent import (
    OPERATION_INTENT_SCHEMA_VERSION,
    REPAIR_EXECUTION,
    OperationIntentAction,
    OperationIntentBinding,
    OperationIntentReceipt,
    operation_intent_sha256,
    parse_operation_intent_bytes,
    validate_operation_intent_attempt_id,
    validate_operation_intent_output_path,
    write_operation_intent,
)
from knowledge_importer.package_validation import (
    ManifestRecord,
    ManifestState,
    read_manifest_state,
    validate_package,
)
from knowledge_importer.repair_approval import parse_repair_approval_bytes
from knowledge_importer.repair_plan import (
    RepairAction,
    RepairActionCategory,
    parse_repair_plan_bytes,
)
from knowledge_importer.repair_preflight import (
    PreflightAction,
    PreflightTarget,
    RepairPreflight,
    build_repair_preflight,
    parse_repair_preflight_bytes,
)

_SUPPORTED_ACTIONS = {
    RepairActionCategory.REGENERATE_SIDECAR,
    RepairActionCategory.REMOVE_STALE_SIDECAR,
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_ACTION_REASONS = {
    RepairActionCategory.REGENERATE_SIDECAR: "missing-sidecar",
    RepairActionCategory.REMOVE_STALE_SIDECAR: "stale-sidecar",
}
_STATUS_ROLLBACK = {
    "succeeded": "available",
    "failed-precondition": "not-required",
    "failed": "not-required",
    "rolled-back": "completed",
    "rollback-failed": "failed",
    "not-run": "not-required",
}


class RepairExecutionInputError(ValueError):
    """Raised before mutation when the execution contract is invalid."""


@dataclass(slots=True)
class ExecutionActionResult:
    repair_action: RepairAction
    status: str
    before: PreflightTarget
    after: PreflightTarget
    rollback: str

    def payload(self) -> dict[str, object]:
        return {
            "path": self.repair_action.path,
            "action": self.repair_action.action.value,
            "status": self.status,
            "before": _state_payload(self.before),
            "after": _state_payload(self.after),
            "rollback": self.rollback,
        }


@dataclass(frozen=True, slots=True)
class RepairExecutionIntentReceipt:
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
class RepairExecutionReport:
    plan_sha256: str
    approval_sha256: str
    preflight_sha256: str
    actions: tuple[ExecutionActionResult, ...]
    post_validation: str
    intent_receipt: RepairExecutionIntentReceipt | None = None

    @property
    def exit_code(self) -> int:
        successful = all(action.status == "succeeded" for action in self.actions)
        return 0 if successful and self.post_validation in {"passed", "not-run"} else 1

    def payload(self) -> dict[str, object]:
        statuses = [action.status for action in self.actions]
        payload: dict[str, object] = {
            "report_type": "knowledge-package-repair-execution",
            "schema_version": 1,
            "summary": {
                "planned": len(self.actions),
                "executed": sum(status != "not-run" for status in statuses),
                "succeeded": statuses.count("succeeded"),
                "failed": sum(
                    status in {"failed-precondition", "failed", "rollback-failed"}
                    for status in statuses
                ),
                "rolled_back": statuses.count("rolled-back"),
                "not_run": statuses.count("not-run"),
            },
            "plan": {"sha256": self.plan_sha256},
            "approval": {"sha256": self.approval_sha256},
            "preflight": {"sha256": self.preflight_sha256},
            "post_validation": self.post_validation,
            "actions": [action.payload() for action in self.actions],
        }
        if self.intent_receipt is not None:
            payload["intent_receipt"] = self.intent_receipt.payload()
        return payload


@dataclass(frozen=True, slots=True)
class _ExecutionInputs:
    manifest_sha256: str
    plan_sha256: str
    approval_sha256: str
    preflight_sha256: str
    preflight: RepairPreflight
    manifest: ManifestState


@dataclass(frozen=True, slots=True)
class _AppliedAction:
    result: ExecutionActionResult
    target_path: Path
    applied_digest: ArtifactDigest | None
    backup_path: Path | None
    backup_digest: ArtifactDigest | None


@dataclass(slots=True)
class _BackupSessionContext:
    root: Path
    manifest_path: Path
    manifest: BackupSessionManifest


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _state_payload(state: PreflightTarget) -> dict[str, object]:
    return {
        "exists": state.exists,
        "bytes": state.bytes,
        "sha256": state.sha256,
    }


def _is_link(path: Path) -> bool:
    return is_link_or_reparse(path)


def _resolve_target(root: Path, relative_path: str) -> Path | None:
    root_resolved = root.resolve()
    current = root
    for part in PurePosixPath(relative_path).parts:
        current /= part
        if _is_link(current):
            return None
    try:
        resolved = current.resolve(strict=False)
    except OSError:
        return None
    return current if resolved.is_relative_to(root_resolved) else None


def _target_state(path: Path | None, relative_path: str) -> PreflightTarget:
    if path is None or not path.exists() or _is_link(path):
        return PreflightTarget(relative_path, path is not None and path.exists(), None, None)
    if not path.is_file():
        return PreflightTarget(relative_path, True, None, None)
    try:
        digest = digest_file(path)
    except OSError:
        return PreflightTarget(relative_path, True, None, None)
    return PreflightTarget(relative_path, True, digest.bytes, digest.sha256)


def _find_record(manifest: ManifestState, action: RepairAction) -> ManifestRecord:
    matches = [
        record
        for record in manifest.records
        if PurePosixPath(record.output_path).with_suffix(".metadata.json").as_posix() == action.path
    ]
    if len(matches) != 1:
        raise RepairExecutionInputError("Manifest action target mismatch")
    return matches[0]


def _read_and_bind_inputs(
    *,
    manifest_path: Path,
    plan_path: Path,
    approval_path: Path,
    preflight_path: Path,
) -> _ExecutionInputs:
    try:
        manifest_content = read_input_bytes(manifest_path)
        manifest = read_manifest_state(manifest_path)
        manifest_content_after = read_input_bytes(manifest_path)
        plan_content = read_input_bytes(plan_path)
        approval_content = read_input_bytes(approval_path)
        preflight_content = read_input_bytes(preflight_path)
        plan = parse_repair_plan_bytes(plan_content)
        approval = parse_repair_approval_bytes(approval_content)
        preflight = parse_repair_preflight_bytes(preflight_content)
    except (OSError, ValueError) as exc:
        raise RepairExecutionInputError("invalid execution input") from exc

    if manifest_content != manifest_content_after:
        raise RepairExecutionInputError("Manifest changed while binding execution")

    manifest_sha256 = _sha256(manifest_content)
    plan_sha256 = _sha256(plan_content)
    approval_sha256 = _sha256(approval_content)
    preflight_sha256 = _sha256(preflight_content)
    canonical_preflight = (
        json.dumps(preflight.payload(), ensure_ascii=False, indent=2) + "\n"
    ).encode()
    expected_approved = tuple(
        action
        for action in plan.actions
        if action.safe and action.action is not RepairActionCategory.MANUAL_REVIEW
    )
    preflight_actions = tuple(action.repair_action for action in preflight.actions)
    if not (
        approval.plan_sha256 == plan_sha256
        and preflight.plan_sha256 == plan_sha256
        and preflight.approval_sha256 == approval_sha256
        and approval.approved_actions == expected_approved
        and approval.approved_actions == preflight_actions
        and preflight_content == canonical_preflight
        and all(action.safe and action.action in _SUPPORTED_ACTIONS for action in preflight_actions)
        and all(action.status == "ready" for action in preflight.actions)
    ):
        raise RepairExecutionInputError("execution binding mismatch")
    for action in preflight_actions:
        _find_record(manifest, action)
    return _ExecutionInputs(
        manifest_sha256,
        plan_sha256,
        approval_sha256,
        preflight_sha256,
        preflight,
        manifest,
    )


def _input_binding_identity(inputs: _ExecutionInputs) -> tuple[str, str, str, str]:
    return (
        inputs.manifest_sha256,
        inputs.plan_sha256,
        inputs.approval_sha256,
        inputs.preflight_sha256,
    )


def _repair_intent_bindings(inputs: _ExecutionInputs) -> tuple[OperationIntentBinding, ...]:
    return (
        OperationIntentBinding("artifact-manifest", 1, inputs.manifest_sha256),
        OperationIntentBinding("repair-plan", 1, inputs.plan_sha256),
        OperationIntentBinding("repair-approval", 1, inputs.approval_sha256),
        OperationIntentBinding("repair-preflight", 1, inputs.preflight_sha256),
    )


def _repair_intent_actions(preflight: RepairPreflight) -> tuple[OperationIntentAction, ...]:
    return tuple(
        OperationIntentAction(
            index,
            action.repair_action.action.value,
            action.repair_action.path,
            action.repair_action.reason_category,
        )
        for index, action in enumerate(preflight.actions)
    )


def _build_repair_operation_intent(
    inputs: _ExecutionInputs,
    *,
    attempt_id: str,
) -> OperationIntentReceipt:
    """Build the canonical Receipt from the already bound execution scope."""

    return OperationIntentReceipt(
        validate_operation_intent_attempt_id(attempt_id),
        REPAIR_EXECUTION,
        _repair_intent_bindings(inputs),
        _repair_intent_actions(inputs.preflight),
    )


def _create_repair_operation_intent(
    path: Path,
    *,
    inputs: _ExecutionInputs,
    attempt_id: str,
) -> RepairExecutionIntentReceipt:
    receipt = _build_repair_operation_intent(inputs, attempt_id=attempt_id)
    try:
        write_operation_intent(path, receipt)
        content = read_input_bytes(path)
        if parse_operation_intent_bytes(content) != receipt:
            raise ValueError("Operation Intent Receipt changed after creation")
        sha256 = operation_intent_sha256(content)
    except (OSError, ValueError) as exc:
        raise RepairExecutionInputError("Operation Intent Receipt cannot be created") from exc
    return RepairExecutionIntentReceipt(
        OPERATION_INTENT_SCHEMA_VERSION,
        receipt.attempt_id,
        sha256,
    )


def _paths_are_equal(first: Path, second: Path) -> bool:
    return (
        str(first.resolve(strict=False)).casefold() == str(second.resolve(strict=False)).casefold()
    )


def _validate_receipted_mode(
    package_root: Path,
    *,
    manifest_path: Path,
    plan_path: Path,
    approval_path: Path,
    preflight_path: Path,
    report_path: Path | None,
    backup_dir: Path | None,
    intent_receipt_path: Path | None,
    attempt_id: str | None,
) -> None:
    if intent_receipt_path is None:
        if attempt_id is not None:
            raise RepairExecutionInputError("attempt_id requires Operation Intent Receipt")
        return
    if attempt_id is None or report_path is None:
        raise RepairExecutionInputError("receipted execution requires attempt_id and report")
    try:
        validate_operation_intent_attempt_id(attempt_id)
        validate_operation_intent_output_path(intent_receipt_path)
    except ValueError as exc:
        raise RepairExecutionInputError("invalid Operation Intent Receipt output") from exc
    protected = (manifest_path, plan_path, approval_path, preflight_path, report_path)
    if (
        path_is_within(intent_receipt_path, package_root)
        or backup_dir is not None
        and path_is_within(intent_receipt_path, backup_dir)
        or any(_paths_are_equal(intent_receipt_path, path) for path in protected)
    ):
        raise RepairExecutionInputError("Operation Intent Receipt output conflicts")


def _execution_report(
    inputs: _ExecutionInputs,
    actions: tuple[ExecutionActionResult, ...],
    post_validation: str,
    intent_receipt: RepairExecutionIntentReceipt | None,
) -> RepairExecutionReport:
    return RepairExecutionReport(
        inputs.plan_sha256,
        inputs.approval_sha256,
        inputs.preflight_sha256,
        actions,
        post_validation,
        intent_receipt,
    )


def _precondition_matches(
    submitted: PreflightAction,
    current: RepairPreflight,
) -> bool:
    matches = [
        action for action in current.actions if action.repair_action == submitted.repair_action
    ]
    return (
        len(matches) == 1 and matches[0].status == "ready" and matches[0].target == submitted.target
    )


def _current_preflight(
    package_root: Path,
    *,
    manifest_path: Path,
    plan_path: Path,
    approval_path: Path,
) -> RepairPreflight:
    return build_repair_preflight(
        package_root,
        manifest_path=manifest_path,
        plan_path=plan_path,
        approval_path=approval_path,
    )


def _repository_roots(package_root: Path) -> tuple[Path, ...]:
    roots: set[Path] = set()
    for start in (package_root.resolve(), Path.cwd().resolve()):
        for candidate in (start, *start.parents):
            if (candidate / ".git").exists():
                roots.add(candidate)
                break
    return tuple(roots)


def _ensure_directory_without_links(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.exists() or current.is_symlink():
            if _is_link(current) or not current.is_dir():
                raise OSError("unsafe backup directory")
        else:
            current.mkdir()
            if _is_link(current) or not current.is_dir():
                raise OSError("unsafe backup directory")


def _prepare_backup_session(package_root: Path, backup_dir: Path | None) -> Path:
    root = backup_dir if backup_dir is not None else Path(tempfile.gettempdir())
    resolved = root.resolve(strict=False)
    forbidden = (package_root.resolve(), *_repository_roots(package_root))
    if any(resolved == item or resolved.is_relative_to(item) for item in forbidden):
        raise OSError("backup directory must be outside package and repository")
    _ensure_directory_without_links(root)
    session = Path(tempfile.mkdtemp(prefix=MANAGED_SESSION_PREFIX, dir=root))
    if _is_link(session) or session.parent.resolve() != root.resolve():
        raise OSError("unsafe backup session")
    return session


def _backup_target(target: Path, backup_path: Path, session_root: Path) -> ArtifactDigest:
    session_resolved = session_root.resolve()
    if _is_link(session_root) or not session_root.is_dir():
        raise OSError("unsafe backup session")
    current = session_root
    relative_parent = backup_path.parent.relative_to(session_root)
    for part in relative_parent.parts:
        current /= part
        current.mkdir()
        if _is_link(current) or not current.is_dir():
            raise OSError("unsafe backup directory")
    if backup_path.exists() or backup_path.is_symlink():
        raise FileExistsError
    if not backup_path.parent.resolve().is_relative_to(session_resolved):
        raise OSError("backup path escaped session")

    source_digest = digest_file(target)
    try:
        with target.open("rb") as source, backup_path.open("xb") as destination:
            shutil.copyfileobj(source, destination)
        if _is_link(backup_path) or not backup_path.resolve().is_relative_to(session_resolved):
            raise OSError("unsafe backup file")
        backup_digest = digest_file(backup_path)
        if backup_digest != source_digest:
            raise OSError("backup verification failed")
    except Exception:
        if backup_path.is_file() and not _is_link(backup_path):
            with suppress(OSError):
                backup_path.unlink()
        raise
    return backup_digest


def _start_backup_session(root: Path, inputs: _ExecutionInputs) -> _BackupSessionContext:
    manifest = BackupSessionManifest(
        BackupSessionState.OPEN,
        BackupSessionBindings(
            inputs.manifest_sha256,
            inputs.plan_sha256,
            inputs.approval_sha256,
            inputs.preflight_sha256,
        ),
        (),
    )
    path = root / SESSION_MANIFEST_FILENAME
    write_backup_session_manifest(path, manifest, expected_current=None)
    return _BackupSessionContext(root, path, manifest)


def _record_backup(
    context: _BackupSessionContext,
    *,
    source: str,
    backup_path: Path,
    digest: ArtifactDigest,
) -> None:
    relative_backup = backup_path.relative_to(context.root).as_posix()
    item = BackupSessionItem(source, relative_backup, digest)
    updated = BackupSessionManifest(
        context.manifest.state,
        context.manifest.bindings,
        tuple(sorted((*context.manifest.items, item), key=lambda value: value.backup)),
    )
    write_backup_session_manifest(
        context.manifest_path,
        updated,
        expected_current=context.manifest,
    )
    context.manifest = updated


def _set_backup_session_state(
    context: _BackupSessionContext,
    state: BackupSessionState,
) -> None:
    updated = transition_backup_session(context.manifest, state)
    write_backup_session_manifest(
        context.manifest_path,
        updated,
        expected_current=context.manifest,
    )
    context.manifest = updated


def _delete_target(target: Path) -> None:
    target.unlink()


def _write_new_bytes_no_clobber(path: Path, content: bytes) -> None:
    temporary: Path | None = None
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or _is_link(path):
        raise FileExistsError
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
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def _read_verified_backup(backup_path: Path, expected_digest: ArtifactDigest) -> bytes:
    if _is_link(backup_path):
        raise OSError("unsafe backup file")
    before = backup_path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise OSError("unsafe backup file")
    with backup_path.open("rb") as source:
        opened = os.fstat(source.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise OSError("backup changed")
        content = source.read()
    after = backup_path.lstat()
    if _is_link(backup_path) or not stat.S_ISREG(after.st_mode):
        raise OSError("unsafe backup file")
    if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
        raise OSError("backup changed")
    actual_digest = ArtifactDigest(len(content), hashlib.sha256(content).hexdigest())
    if actual_digest != expected_digest:
        raise OSError("backup verification failed")
    return content


def _restore_backup(
    target: Path, backup_path: Path, expected_backup_digest: ArtifactDigest
) -> None:
    if target.exists() or _is_link(target):
        raise FileExistsError
    content = _read_verified_backup(backup_path, expected_backup_digest)
    _write_new_bytes_no_clobber(target, content)
    if digest_file(target) != expected_backup_digest:
        raise OSError("rollback verification failed")


def _remove_generated(target: Path, expected: ArtifactDigest) -> None:
    if _is_link(target) or not target.is_file() or digest_file(target) != expected:
        raise OSError("generated target changed")
    target.unlink()


def _sidecar_for_record(record: ManifestRecord, manifest: ManifestState) -> DocumentMetadataSidecar:
    item = ArtifactManifestItem(
        record.input_path,
        record.output_path,
        ManifestStatus(record.status),
        ArtifactDigest(record.input_bytes, record.input_sha256),
        ArtifactDigest(record.output_bytes, record.output_sha256),
    )
    settings = DocumentMetadataSettings(
        table_structure=bool(manifest.settings["table_structure"]),
        normalization_profile=manifest.settings["normalization_profile"],  # type: ignore[arg-type]
        artifacts_path_configured=bool(manifest.settings["artifacts_path_configured"]),
    )
    return build_document_metadata(item, settings)


def _write_new_sidecar(path: Path, sidecar: DocumentMetadataSidecar) -> None:
    content = (json.dumps(sidecar.payload(), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _write_new_bytes_no_clobber(path, content)


def _rollback(applied: list[_AppliedAction]) -> bool:
    all_succeeded = True
    for item in reversed(applied):
        try:
            if item.result.repair_action.action is RepairActionCategory.REGENERATE_SIDECAR:
                assert item.applied_digest is not None
                _remove_generated(item.target_path, item.applied_digest)
            else:
                assert item.backup_path is not None
                assert item.backup_digest is not None
                _restore_backup(item.target_path, item.backup_path, item.backup_digest)
        except Exception:  # noqa: BLE001 - rollback state is represented safely in report.
            item.result.status = "rollback-failed"
            item.result.rollback = "failed"
            item.result.after = _target_state(item.target_path, item.result.repair_action.path)
            all_succeeded = False
        else:
            item.result.status = "rolled-back"
            item.result.rollback = "completed"
            item.result.after = _target_state(item.target_path, item.result.repair_action.path)
    return all_succeeded


def _rollback_with_session(
    applied: list[_AppliedAction],
    backup_session: _BackupSessionContext | None,
) -> bool:
    succeeded = _rollback(applied)
    if backup_session is None or not applied:
        return succeeded
    state = BackupSessionState.ROLLED_BACK if succeeded else BackupSessionState.ROLLBACK_FAILED
    try:
        _set_backup_session_state(backup_session, state)
    except Exception:  # noqa: BLE001 - an open session remains visible to inventory.
        return False
    return succeeded


def _not_run_results(preflight: RepairPreflight) -> list[ExecutionActionResult]:
    return [
        ExecutionActionResult(
            action.repair_action, "not-run", action.target, action.target, "not-required"
        )
        for action in preflight.actions
    ]


def execute_repair(
    package_root: Path,
    *,
    manifest_path: Path,
    plan_path: Path,
    approval_path: Path,
    preflight_path: Path,
    backup_dir: Path | None = None,
    report_path: Path | None = None,
    intent_receipt_path: Path | None = None,
    attempt_id: str | None = None,
) -> RepairExecutionReport:
    """Execute the two approved v1 actions with fail-fast rollback."""

    _validate_receipted_mode(
        package_root,
        manifest_path=manifest_path,
        plan_path=plan_path,
        approval_path=approval_path,
        preflight_path=preflight_path,
        report_path=report_path,
        backup_dir=backup_dir,
        intent_receipt_path=intent_receipt_path,
        attempt_id=attempt_id,
    )
    inputs = _read_and_bind_inputs(
        manifest_path=manifest_path,
        plan_path=plan_path,
        approval_path=approval_path,
        preflight_path=preflight_path,
    )
    intent_receipt: RepairExecutionIntentReceipt | None = None
    if intent_receipt_path is not None:
        assert attempt_id is not None
        intent_receipt = _create_repair_operation_intent(
            intent_receipt_path,
            inputs=inputs,
            attempt_id=attempt_id,
        )
        rebound_inputs = _read_and_bind_inputs(
            manifest_path=manifest_path,
            plan_path=plan_path,
            approval_path=approval_path,
            preflight_path=preflight_path,
        )
        if _input_binding_identity(rebound_inputs) != _input_binding_identity(inputs):
            raise RepairExecutionInputError("execution inputs changed after Intent Receipt")
        inputs = rebound_inputs
    results = _not_run_results(inputs.preflight)
    if not results:
        return _execution_report(inputs, (), "not-run", intent_receipt)

    try:
        current = _current_preflight(
            package_root,
            manifest_path=manifest_path,
            plan_path=plan_path,
            approval_path=approval_path,
        )
    except Exception:  # noqa: BLE001 - current state became unverifiable.
        results[0].status = "failed-precondition"
        return _execution_report(inputs, tuple(results), "not-run", intent_receipt)
    mismatches = [
        index
        for index, action in enumerate(inputs.preflight.actions)
        if not _precondition_matches(action, current)
    ]
    if mismatches:
        results[mismatches[0]].status = "failed-precondition"
        return _execution_report(inputs, tuple(results), "not-run", intent_receipt)

    needs_backup = any(
        action.repair_action.action is RepairActionCategory.REMOVE_STALE_SIDECAR
        for action in inputs.preflight.actions
    )
    backup_root: Path | None = None
    backup_session: _BackupSessionContext | None = None
    if needs_backup:
        try:
            backup_root = _prepare_backup_session(package_root, backup_dir)
            backup_session = _start_backup_session(backup_root, inputs)
        except Exception:  # noqa: BLE001 - unsafe/colliding destination is action failure.
            first_stale = next(
                index
                for index, action in enumerate(inputs.preflight.actions)
                if action.repair_action.action is RepairActionCategory.REMOVE_STALE_SIDECAR
            )
            results[first_stale].status = "failed"
            return _execution_report(inputs, tuple(results), "not-run", intent_receipt)
    applied: list[_AppliedAction] = []
    for index, submitted in enumerate(inputs.preflight.actions):
        try:
            current = _current_preflight(
                package_root,
                manifest_path=manifest_path,
                plan_path=plan_path,
                approval_path=approval_path,
            )
        except Exception:  # noqa: BLE001 - fail-fast and rollback on unverifiable state.
            results[index].status = "failed-precondition"
            _rollback_with_session(applied, backup_session)
            return _execution_report(inputs, tuple(results), "not-run", intent_receipt)
        if not _precondition_matches(submitted, current):
            results[index].status = "failed-precondition"
            _rollback_with_session(applied, backup_session)
            return _execution_report(inputs, tuple(results), "not-run", intent_receipt)

        action = submitted.repair_action
        target = _resolve_target(package_root, action.path)
        if target is None:
            results[index].status = "failed-precondition"
            _rollback_with_session(applied, backup_session)
            return _execution_report(inputs, tuple(results), "not-run", intent_receipt)
        record = _find_record(inputs.manifest, action)
        results[index].before = _target_state(target, action.path)
        try:
            if action.action is RepairActionCategory.REGENERATE_SIDECAR:
                _write_new_sidecar(target, _sidecar_for_record(record, inputs.manifest))
                applied_digest = digest_file(target)
                applied.append(_AppliedAction(results[index], target, applied_digest, None, None))
            else:
                assert backup_root is not None
                assert backup_session is not None
                backup_path = backup_root / f"{index:04d}" / f"{action.path}.bak"
                backup_digest = _backup_target(target, backup_path, backup_root)
                if backup_digest.sha256 != submitted.target.sha256:
                    raise OSError("backup no longer matches preflight")
                _record_backup(
                    backup_session,
                    source=action.path,
                    backup_path=backup_path,
                    digest=backup_digest,
                )
                try:
                    _delete_target(target)
                except Exception:
                    if not target.exists():
                        applied.append(
                            _AppliedAction(results[index], target, None, backup_path, backup_digest)
                        )
                    raise
                if target.exists():
                    raise OSError("target deletion failed")
                applied.append(
                    _AppliedAction(results[index], target, None, backup_path, backup_digest)
                )
        except Exception:  # noqa: BLE001 - safe report status replaces local exception details.
            results[index].status = "failed"
            _rollback_with_session(applied, backup_session)
            results[index].after = _target_state(target, action.path)
            return _execution_report(inputs, tuple(results), "not-run", intent_receipt)
        results[index].status = "succeeded"
        results[index].rollback = "available"
        results[index].after = _target_state(target, action.path)

    try:
        post_validation = validate_package(package_root, manifest_path=manifest_path)
        post_validation_failed = post_validation.exit_code != 0
    except Exception:  # noqa: BLE001 - post-state must be provably valid.
        post_validation_failed = True
    if post_validation_failed:
        _rollback_with_session(applied, backup_session)
        return _execution_report(inputs, tuple(results), "failed", intent_receipt)
    if backup_session is not None:
        try:
            _set_backup_session_state(backup_session, BackupSessionState.COMPLETE)
        except Exception:  # noqa: BLE001 - completion must be durably recorded.
            _rollback_with_session(applied, backup_session)
            return _execution_report(inputs, tuple(results), "failed", intent_receipt)
    return _execution_report(inputs, tuple(results), "passed", intent_receipt)


def write_execution_report(path: Path, report: RepairExecutionReport) -> None:
    """Write Execution Report v1 atomically."""

    write_json_atomically(path, report.payload())


def repair_execution_report_bytes(report: RepairExecutionReport) -> bytes:
    """Serialize the exact deterministic bytes written by the shared JSON writer."""

    return (json.dumps(report.payload(), ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _parse_execution_state(value: object, path: str) -> PreflightTarget:
    if not isinstance(value, dict):
        raise ValueError("invalid Repair Execution target state")
    exists = value.get("exists")
    size = value.get("bytes")
    sha256 = value.get("sha256")
    digest_valid = (
        size is None
        and sha256 is None
        or isinstance(size, int)
        and not isinstance(size, bool)
        and size >= 0
        and isinstance(sha256, str)
        and _SHA256.fullmatch(sha256) is not None
    )
    if not isinstance(exists, bool) or not digest_valid or (not exists and size is not None):
        raise ValueError("invalid Repair Execution target state")
    return PreflightTarget(path, exists, size, sha256)


def _execution_action_key(result: ExecutionActionResult) -> tuple[str, str, str]:
    action = result.repair_action
    return (
        unicodedata.normalize("NFC", action.path).casefold(),
        action.action.value,
        action.reason_category,
    )


def _parse_intent_receipt_binding(value: object) -> RepairExecutionIntentReceipt:
    if not isinstance(value, dict):
        raise ValueError("invalid Repair Execution Intent Receipt binding")
    schema_version = value.get("schema_version")
    sha256 = value.get("sha256")
    if not (
        isinstance(schema_version, int)
        and not isinstance(schema_version, bool)
        and schema_version == OPERATION_INTENT_SCHEMA_VERSION
        and isinstance(sha256, str)
        and _SHA256.fullmatch(sha256) is not None
    ):
        raise ValueError("invalid Repair Execution Intent Receipt binding")
    attempt_id = validate_operation_intent_attempt_id(value.get("attempt_id"))
    return RepairExecutionIntentReceipt(schema_version, attempt_id, sha256)


def parse_repair_execution_report_bytes(content: bytes) -> RepairExecutionReport:
    """Parse and semantically validate a Repair Execution Report v1."""

    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid Repair Execution Report JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid Repair Execution Report root")
    summary = payload.get("summary")
    raw_actions = payload.get("actions")
    bindings = tuple(payload.get(key) for key in ("plan", "approval", "preflight"))
    schema_version = payload.get("schema_version")
    post_validation = payload.get("post_validation")
    intent_receipt = (
        _parse_intent_receipt_binding(payload["intent_receipt"])
        if "intent_receipt" in payload
        else None
    )
    if not (
        payload.get("report_type") == "knowledge-package-repair-execution"
        and schema_version == 1
        and isinstance(schema_version, int)
        and not isinstance(schema_version, bool)
        and isinstance(summary, dict)
        and isinstance(raw_actions, list)
        and all(isinstance(value, dict) for value in bindings)
        and all(
            isinstance(value.get("sha256"), str) and _SHA256.fullmatch(value["sha256"]) is not None
            for value in bindings
        )
        and isinstance(post_validation, str)
        and post_validation in {"passed", "failed", "not-run"}
    ):
        raise ValueError("invalid Repair Execution Report schema")

    actions: list[ExecutionActionResult] = []
    for value in raw_actions:
        if not isinstance(value, dict):
            raise ValueError("invalid Repair Execution action")
        path_value = value.get("path")
        action_value = value.get("action")
        status = value.get("status")
        rollback = value.get("rollback")
        if (
            not isinstance(path_value, str)
            or not path_value
            or "\\" in path_value
            or _WINDOWS_DRIVE.match(path_value)
            or any(unicodedata.category(character) in {"Cc", "Cf"} for character in path_value)
        ):
            raise ValueError("invalid Repair Execution action path")
        path = PurePosixPath(path_value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("invalid Repair Execution action path")
        try:
            category = RepairActionCategory(action_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid Repair Execution action category") from exc
        if (
            category not in _SUPPORTED_ACTIONS
            or not isinstance(status, str)
            or _STATUS_ROLLBACK.get(status) != rollback
        ):
            raise ValueError("invalid Repair Execution action semantics")
        before = _parse_execution_state(value.get("before"), path_value)
        after = _parse_execution_state(value.get("after"), path_value)
        if status == "succeeded" and (
            category is RepairActionCategory.REGENERATE_SIDECAR
            and (before.exists or not after.exists or after.sha256 is None)
            or category is RepairActionCategory.REMOVE_STALE_SIDECAR
            and (not before.exists or before.sha256 is None or after.exists)
        ):
            raise ValueError("invalid Repair Execution successful action state")
        if status in {"failed-precondition", "not-run"} and before != after:
            raise ValueError("invalid Repair Execution unchanged action state")
        if status == "rolled-back" and before != after:
            raise ValueError("invalid Repair Execution rollback state")
        action = RepairAction(path_value, category, _ACTION_REASONS[category], True)
        actions.append(ExecutionActionResult(action, status, before, after, rollback))

    plan, approval, preflight = bindings
    assert isinstance(plan, dict)
    assert isinstance(approval, dict)
    assert isinstance(preflight, dict)
    report = RepairExecutionReport(
        plan["sha256"],
        approval["sha256"],
        preflight["sha256"],
        tuple(actions),
        post_validation,
        intent_receipt,
    )
    expected_summary = report.payload()["summary"]
    if (
        any(
            not isinstance(summary.get(key), int)
            or isinstance(summary.get(key), bool)
            or summary.get(key) != value
            for key, value in expected_summary.items()
        )
        or actions != sorted(actions, key=_execution_action_key)
        or len({_execution_action_key(action) for action in actions}) != len(actions)
    ):
        raise ValueError("invalid Repair Execution summary or order")
    return report


def verify_repair_execution_intent(
    receipt_content: bytes,
    report_content: bytes,
    *,
    manifest_content: bytes,
    plan_content: bytes,
    approval_content: bytes,
    preflight_content: bytes,
) -> None:
    """Verify exact Receipt/report/input binding without inferring execution from intent."""

    receipt = parse_operation_intent_bytes(receipt_content)
    report = parse_repair_execution_report_bytes(report_content)
    try:
        manifest_payload = json.loads(manifest_content.decode("utf-8"))
        plan = parse_repair_plan_bytes(plan_content)
        approval = parse_repair_approval_bytes(approval_content)
        preflight = parse_repair_preflight_bytes(preflight_content)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid Repair Execution Intent source") from exc
    manifest_schema_version = (
        manifest_payload.get("schema_version") if isinstance(manifest_payload, dict) else None
    )
    if not (
        isinstance(manifest_payload, dict)
        and manifest_payload.get("report_type") == "knowledge-artifact-manifest"
        and isinstance(manifest_schema_version, int)
        and not isinstance(manifest_schema_version, bool)
        and manifest_schema_version == 1
    ):
        raise ValueError("invalid Repair Execution Intent Manifest")

    plan_sha256 = _sha256(plan_content)
    approval_sha256 = _sha256(approval_content)
    preflight_sha256 = _sha256(preflight_content)
    expected_bindings = (
        OperationIntentBinding("artifact-manifest", 1, _sha256(manifest_content)),
        OperationIntentBinding("repair-plan", 1, plan_sha256),
        OperationIntentBinding("repair-approval", 1, approval_sha256),
        OperationIntentBinding("repair-preflight", 1, preflight_sha256),
    )
    expected_actions = _repair_intent_actions(preflight)
    expected_approved = tuple(
        action
        for action in plan.actions
        if action.safe and action.action is not RepairActionCategory.MANUAL_REVIEW
    )
    preflight_actions = tuple(action.repair_action for action in preflight.actions)
    canonical_preflight = (
        json.dumps(preflight.payload(), ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    report_actions = tuple(
        OperationIntentAction(
            index,
            action.repair_action.action.value,
            action.repair_action.path,
            action.repair_action.reason_category,
        )
        for index, action in enumerate(report.actions)
    )
    receipt_sha256 = operation_intent_sha256(receipt_content)
    binding = report.intent_receipt
    if not (
        binding is not None
        and binding.schema_version == OPERATION_INTENT_SCHEMA_VERSION
        and binding.attempt_id == receipt.attempt_id
        and binding.sha256 == receipt_sha256
        and receipt.operation_type == REPAIR_EXECUTION
        and receipt.bindings == expected_bindings
        and receipt.actions == expected_actions
        and report_actions == expected_actions
        and report.plan_sha256 == plan_sha256
        and report.approval_sha256 == approval_sha256
        and report.preflight_sha256 == preflight_sha256
        and approval.plan_sha256 == plan_sha256
        and preflight.plan_sha256 == plan_sha256
        and preflight.approval_sha256 == approval_sha256
        and approval.approved_actions == expected_approved
        and approval.approved_actions == preflight_actions
        and preflight_content == canonical_preflight
        and all(action.safe and action.action in _SUPPORTED_ACTIONS for action in preflight_actions)
        and all(action.status == "ready" for action in preflight.actions)
    ):
        raise ValueError("Repair Execution Intent binding mismatch")


def is_execution_report(path: Path) -> bool:
    """Return whether an existing regular file is an Execution Report v1."""

    if path.is_symlink() or not path.is_file():
        return False
    try:
        parse_repair_execution_report_bytes(path.read_bytes())
    except (OSError, ValueError):
        return False
    return True
