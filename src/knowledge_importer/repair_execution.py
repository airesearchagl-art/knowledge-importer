"""Safe, bounded Repair Execution v1 for approved Knowledge Package actions."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from knowledge_importer.artifact_manifest import (
    ArtifactDigest,
    ArtifactManifestItem,
    ManifestStatus,
    digest_file,
)
from knowledge_importer.document_metadata import (
    DocumentMetadataSettings,
    build_document_metadata,
    write_document_metadata,
)
from knowledge_importer.json_writer import write_json_atomically
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
class RepairExecutionReport:
    plan_sha256: str
    approval_sha256: str
    preflight_sha256: str
    actions: tuple[ExecutionActionResult, ...]
    post_validation: str

    @property
    def exit_code(self) -> int:
        successful = all(action.status == "succeeded" for action in self.actions)
        return 0 if successful and self.post_validation in {"passed", "not-run"} else 1

    def payload(self) -> dict[str, object]:
        statuses = [action.status for action in self.actions]
        return {
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


@dataclass(frozen=True, slots=True)
class _ExecutionInputs:
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


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _state_payload(state: PreflightTarget) -> dict[str, object]:
    return {
        "exists": state.exists,
        "bytes": state.bytes,
        "sha256": state.sha256,
    }


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


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
        manifest = read_manifest_state(manifest_path)
        plan_content = plan_path.read_bytes()
        approval_content = approval_path.read_bytes()
        preflight_content = preflight_path.read_bytes()
        plan = parse_repair_plan_bytes(plan_content)
        approval = parse_repair_approval_bytes(approval_content)
        preflight = parse_repair_preflight_bytes(preflight_content)
    except (OSError, ValueError) as exc:
        raise RepairExecutionInputError("invalid execution input") from exc

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
        plan_sha256,
        approval_sha256,
        preflight_sha256,
        preflight,
        manifest,
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
    session = Path(tempfile.mkdtemp(prefix="knowledge-importer-repair-", dir=root))
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


def _delete_target(target: Path) -> None:
    target.unlink()


def _write_bytes_atomically(path: Path, content: bytes) -> None:
    temporary: Path | None = None
    path.parent.mkdir(parents=True, exist_ok=True)
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
        temporary.replace(path)
    except Exception:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
        raise


def _restore_backup(target: Path, backup_path: Path) -> None:
    if target.exists() or _is_link(target):
        raise FileExistsError
    _write_bytes_atomically(target, backup_path.read_bytes())
    if digest_file(target) != digest_file(backup_path):
        raise OSError("rollback verification failed")


def _remove_generated(target: Path, expected: ArtifactDigest) -> None:
    if _is_link(target) or not target.is_file() or digest_file(target) != expected:
        raise OSError("generated target changed")
    target.unlink()


def _sidecar_for_record(record: ManifestRecord, manifest: ManifestState) -> object:
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


def _rollback(applied: list[_AppliedAction]) -> bool:
    all_succeeded = True
    for item in reversed(applied):
        try:
            if item.result.repair_action.action is RepairActionCategory.REGENERATE_SIDECAR:
                assert item.applied_digest is not None
                _remove_generated(item.target_path, item.applied_digest)
            else:
                assert item.backup_path is not None
                _restore_backup(item.target_path, item.backup_path)
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
) -> RepairExecutionReport:
    """Execute the two approved v1 actions with fail-fast rollback."""

    inputs = _read_and_bind_inputs(
        manifest_path=manifest_path,
        plan_path=plan_path,
        approval_path=approval_path,
        preflight_path=preflight_path,
    )
    results = _not_run_results(inputs.preflight)
    if not results:
        return RepairExecutionReport(
            inputs.plan_sha256,
            inputs.approval_sha256,
            inputs.preflight_sha256,
            (),
            "not-run",
        )

    try:
        current = _current_preflight(
            package_root,
            manifest_path=manifest_path,
            plan_path=plan_path,
            approval_path=approval_path,
        )
    except Exception:  # noqa: BLE001 - current state became unverifiable.
        results[0].status = "failed-precondition"
        return RepairExecutionReport(
            inputs.plan_sha256,
            inputs.approval_sha256,
            inputs.preflight_sha256,
            tuple(results),
            "not-run",
        )
    mismatches = [
        index
        for index, action in enumerate(inputs.preflight.actions)
        if not _precondition_matches(action, current)
    ]
    if mismatches:
        results[mismatches[0]].status = "failed-precondition"
        return RepairExecutionReport(
            inputs.plan_sha256,
            inputs.approval_sha256,
            inputs.preflight_sha256,
            tuple(results),
            "not-run",
        )

    needs_backup = any(
        action.repair_action.action is RepairActionCategory.REMOVE_STALE_SIDECAR
        for action in inputs.preflight.actions
    )
    backup_root: Path | None = None
    if needs_backup:
        try:
            backup_root = _prepare_backup_session(package_root, backup_dir)
        except Exception:  # noqa: BLE001 - unsafe/colliding destination is action failure.
            first_stale = next(
                index
                for index, action in enumerate(inputs.preflight.actions)
                if action.repair_action.action is RepairActionCategory.REMOVE_STALE_SIDECAR
            )
            results[first_stale].status = "failed"
            return RepairExecutionReport(
                inputs.plan_sha256,
                inputs.approval_sha256,
                inputs.preflight_sha256,
                tuple(results),
                "not-run",
            )
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
            _rollback(applied)
            return RepairExecutionReport(
                inputs.plan_sha256,
                inputs.approval_sha256,
                inputs.preflight_sha256,
                tuple(results),
                "not-run",
            )
        if not _precondition_matches(submitted, current):
            results[index].status = "failed-precondition"
            _rollback(applied)
            return RepairExecutionReport(
                inputs.plan_sha256,
                inputs.approval_sha256,
                inputs.preflight_sha256,
                tuple(results),
                "not-run",
            )

        action = submitted.repair_action
        target = _resolve_target(package_root, action.path)
        if target is None:
            results[index].status = "failed-precondition"
            _rollback(applied)
            return RepairExecutionReport(
                inputs.plan_sha256,
                inputs.approval_sha256,
                inputs.preflight_sha256,
                tuple(results),
                "not-run",
            )
        record = _find_record(inputs.manifest, action)
        results[index].before = _target_state(target, action.path)
        try:
            if action.action is RepairActionCategory.REGENERATE_SIDECAR:
                write_document_metadata(target, _sidecar_for_record(record, inputs.manifest))
                applied_digest = digest_file(target)
                applied.append(_AppliedAction(results[index], target, applied_digest, None))
            else:
                assert backup_root is not None
                backup_path = backup_root / f"{index:04d}" / f"{action.path}.bak"
                backup_digest = _backup_target(target, backup_path, backup_root)
                if backup_digest.sha256 != submitted.target.sha256:
                    raise OSError("backup no longer matches preflight")
                try:
                    _delete_target(target)
                except Exception:
                    if not target.exists():
                        applied.append(_AppliedAction(results[index], target, None, backup_path))
                    raise
                if target.exists():
                    raise OSError("target deletion failed")
                applied.append(_AppliedAction(results[index], target, None, backup_path))
        except Exception:  # noqa: BLE001 - safe report status replaces local exception details.
            results[index].status = "failed"
            _rollback(applied)
            results[index].after = _target_state(target, action.path)
            return RepairExecutionReport(
                inputs.plan_sha256,
                inputs.approval_sha256,
                inputs.preflight_sha256,
                tuple(results),
                "not-run",
            )
        results[index].status = "succeeded"
        results[index].rollback = "available"
        results[index].after = _target_state(target, action.path)

    try:
        post_validation = validate_package(package_root, manifest_path=manifest_path)
        post_validation_failed = post_validation.exit_code != 0
    except Exception:  # noqa: BLE001 - post-state must be provably valid.
        post_validation_failed = True
    if post_validation_failed:
        _rollback(applied)
        return RepairExecutionReport(
            inputs.plan_sha256,
            inputs.approval_sha256,
            inputs.preflight_sha256,
            tuple(results),
            "failed",
        )
    return RepairExecutionReport(
        inputs.plan_sha256,
        inputs.approval_sha256,
        inputs.preflight_sha256,
        tuple(results),
        "passed",
    )


def write_execution_report(path: Path, report: RepairExecutionReport) -> None:
    """Write Execution Report v1 atomically."""

    write_json_atomically(path, report.payload())


def is_execution_report(path: Path) -> bool:
    """Return whether an existing regular file is an Execution Report v1."""

    if path.is_symlink() or not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    summary = payload.get("summary")
    actions = payload.get("actions")
    plan = payload.get("plan")
    approval = payload.get("approval")
    preflight = payload.get("preflight")
    if not (
        payload.get("report_type") == "knowledge-package-repair-execution"
        and payload.get("schema_version") == 1
        and not isinstance(payload.get("schema_version"), bool)
        and isinstance(summary, dict)
        and isinstance(actions, list)
        and all(isinstance(value, dict) for value in (plan, approval, preflight))
        and payload.get("post_validation") in {"passed", "failed", "not-run"}
    ):
        return False
    assert isinstance(summary, dict)
    assert isinstance(plan, dict)
    assert isinstance(approval, dict)
    assert isinstance(preflight, dict)
    counts = tuple(
        summary.get(key)
        for key in ("planned", "executed", "succeeded", "failed", "rolled_back", "not_run")
    )
    if not (
        all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in counts
        )
        and counts[0] == len(actions)
        and counts[1] + counts[5] == counts[0]
        and all(
            isinstance(container.get("sha256"), str)
            and len(container["sha256"]) == 64
            and all(character in "0123456789abcdef" for character in container["sha256"])
            for container in (plan, approval, preflight)
        )
    ):
        return False
    valid_statuses = {
        "succeeded",
        "failed-precondition",
        "failed",
        "rolled-back",
        "rollback-failed",
        "not-run",
    }
    valid_rollbacks = {"not-required", "available", "completed", "failed"}
    parsed_statuses: list[str] = []
    for action in actions:
        if not isinstance(action, dict):
            return False
        before = action.get("before")
        after = action.get("after")
        if not (
            isinstance(action.get("path"), str)
            and action.get("action") in {category.value for category in _SUPPORTED_ACTIONS}
            and action.get("status") in valid_statuses
            and action.get("rollback") in valid_rollbacks
            and isinstance(before, dict)
            and isinstance(after, dict)
        ):
            return False
        path = PurePosixPath(action["path"])
        if (
            path.is_absolute()
            or "\\" in action["path"]
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            return False
        for state in (before, after):
            exists = state.get("exists")
            size = state.get("bytes")
            sha256 = state.get("sha256")
            digest_valid = (
                size is None
                and sha256 is None
                or isinstance(size, int)
                and not isinstance(size, bool)
                and size >= 0
                and isinstance(sha256, str)
                and len(sha256) == 64
                and all(character in "0123456789abcdef" for character in sha256)
            )
            if not isinstance(exists, bool) or not digest_valid:
                return False
        parsed_statuses.append(action["status"])
    expected_counts = (
        len(parsed_statuses),
        sum(status != "not-run" for status in parsed_statuses),
        parsed_statuses.count("succeeded"),
        sum(
            status in {"failed-precondition", "failed", "rollback-failed"}
            for status in parsed_statuses
        ),
        parsed_statuses.count("rolled-back"),
        parsed_statuses.count("not-run"),
    )
    return counts == expected_counts
