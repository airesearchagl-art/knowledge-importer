"""Read-only preflight contract for future Knowledge Package repair execution."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from knowledge_importer.artifact_manifest import ArtifactDigest, digest_file
from knowledge_importer.json_writer import write_json_atomically
from knowledge_importer.package_validation import (
    ManifestRecord,
    PackageValidationResult,
    read_manifest_records,
    validate_package,
)
from knowledge_importer.repair_approval import parse_repair_approval_bytes
from knowledge_importer.repair_plan import (
    RepairAction,
    RepairActionCategory,
    parse_repair_plan_bytes,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SUPPORTED_ACTIONS = {
    RepairActionCategory.REGENERATE_SIDECAR,
    RepairActionCategory.REMOVE_STALE_SIDECAR,
}


@dataclass(frozen=True, slots=True)
class PreflightTarget:
    path: str
    exists: bool
    bytes: int | None
    sha256: str | None

    def payload(self) -> dict[str, object]:
        return {
            "path": self.path,
            "exists": self.exists,
            "bytes": self.bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class PreflightAction:
    repair_action: RepairAction
    status: str
    block_reason: str | None
    package_state_matches: bool
    backup_required: bool
    target: PreflightTarget

    def payload(self) -> dict[str, object]:
        return {
            "path": self.repair_action.path,
            "action": self.repair_action.action.value,
            "reason_category": self.repair_action.reason_category,
            "status": self.status,
            "block_reason": self.block_reason,
            "preconditions": {
                "plan_approved": True,
                "safe": self.repair_action.safe,
                "package_state_matches": self.package_state_matches,
                "backup_required": self.backup_required,
            },
            "target": self.target.payload(),
        }


@dataclass(frozen=True, slots=True)
class RepairPreflight:
    plan_sha256: str
    approval_sha256: str
    actions: tuple[PreflightAction, ...]

    @property
    def ready(self) -> int:
        return sum(action.status == "ready" for action in self.actions)

    @property
    def blocked(self) -> int:
        return sum(action.status == "blocked" for action in self.actions)

    @property
    def exit_code(self) -> int:
        return 1 if self.blocked else 0

    def payload(self) -> dict[str, object]:
        return {
            "report_type": "knowledge-package-repair-preflight",
            "schema_version": 1,
            "summary": {
                "actions": len(self.actions),
                "ready": self.ready,
                "blocked": self.blocked,
            },
            "plan": {"sha256": self.plan_sha256},
            "approval": {"sha256": self.approval_sha256},
            "actions": [action.payload() for action in self.actions],
        }


def _sort_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _action_sort_key(action: PreflightAction) -> tuple[str, str, str]:
    repair_action = action.repair_action
    return (
        _sort_key(repair_action.path),
        repair_action.action.value,
        repair_action.reason_category,
    )


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _resolve_safe_target(root: Path, relative_path: str) -> Path | None:
    root_resolved = root.resolve()
    candidate = root.joinpath(*PurePosixPath(relative_path).parts)
    current = root
    for part in PurePosixPath(relative_path).parts:
        current /= part
        if _is_link(current):
            return None
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        return None
    return candidate if resolved.is_relative_to(root_resolved) else None


def _target_state(path: Path | None, relative_path: str) -> PreflightTarget:
    if path is None or not path.exists() or _is_link(path):
        return PreflightTarget(relative_path, path is not None and path.exists(), None, None)
    if not path.is_file():
        return PreflightTarget(relative_path, True, None, None)
    try:
        actual = digest_file(path)
    except OSError:
        return PreflightTarget(relative_path, True, None, None)
    return PreflightTarget(relative_path, True, actual.bytes, actual.sha256)


def _issue_is_current(
    validation: PackageValidationResult,
    action: RepairAction,
) -> bool:
    path_key = _sort_key(action.path)
    return any(
        _sort_key(issue.path) == path_key and issue.category == action.reason_category
        for issue in validation.issues
    )


def _matching_manifest_records(
    records: tuple[ManifestRecord, ...],
    sidecar_path: str,
) -> tuple[ManifestRecord, ...]:
    key = _sort_key(sidecar_path)
    return tuple(
        record
        for record in records
        if _sort_key(PurePosixPath(record.output_path).with_suffix(".metadata.json").as_posix())
        == key
    )


def _matches_digest(path: Path, expected_bytes: int | None, expected_sha256: str | None) -> bool:
    if expected_bytes is None or expected_sha256 is None:
        return False
    try:
        actual = digest_file(path)
    except OSError:
        return False
    return actual == ArtifactDigest(expected_bytes, expected_sha256)


def _evaluate_action(
    action: RepairAction,
    *,
    package_root: Path,
    validation: PackageValidationResult,
    manifest_records: tuple[ManifestRecord, ...] | None,
) -> PreflightAction:
    target_path = _resolve_safe_target(package_root, action.path)
    target = _target_state(target_path, action.path)
    backup_required = action.action is RepairActionCategory.REMOVE_STALE_SIDECAR
    matches = False
    block_reason = "package-state-changed"

    if action.action not in _SUPPORTED_ACTIONS:
        block_reason = "unsupported-action"
    elif manifest_records is None:
        block_reason = "manifest-invalid"
    elif target_path is None:
        block_reason = "path-unsafe"
    else:
        records = _matching_manifest_records(manifest_records, action.path)
        if len(records) == 1 and _issue_is_current(validation, action):
            record = records[0]
            if action.action is RepairActionCategory.REGENERATE_SIDECAR:
                markdown = _resolve_safe_target(package_root, record.output_path)
                matches = (
                    record.status in {"succeeded", "skipped"}
                    and record.input_bytes is not None
                    and record.input_sha256 is not None
                    and not target.exists
                    and markdown is not None
                    and markdown.is_file()
                    and not _is_link(markdown)
                    and _matches_digest(markdown, record.output_bytes, record.output_sha256)
                )
            else:
                matches = (
                    record.status == "failed"
                    and target.exists
                    and target.bytes is not None
                    and target.sha256 is not None
                )

    return PreflightAction(
        action,
        "ready" if matches else "blocked",
        None if matches else block_reason,
        matches,
        backup_required,
        target,
    )


def build_repair_preflight(
    package_root: Path,
    *,
    manifest_path: Path | None,
    plan_path: Path,
    approval_path: Path,
) -> RepairPreflight:
    """Validate bindings and current package state without changing package artifacts."""

    plan_content = plan_path.read_bytes()
    approval_content = approval_path.read_bytes()
    plan_sha256 = _sha256(plan_content)
    approval_sha256 = _sha256(approval_content)
    plan = parse_repair_plan_bytes(plan_content)
    approval = parse_repair_approval_bytes(approval_content)
    expected_approved = tuple(
        action
        for action in plan.actions
        if action.safe and action.action is not RepairActionCategory.MANUAL_REVIEW
    )
    if approval.plan_sha256 != plan_sha256 or approval.approved_actions != expected_approved:
        raise ValueError("Repair Plan and Approval binding mismatch")

    validation = validate_package(package_root, manifest_path=manifest_path)
    try:
        manifest_records: tuple[ManifestRecord, ...] | None = (
            read_manifest_records(manifest_path) if manifest_path is not None else None
        )
    except ValueError:
        manifest_records = None
    actions = tuple(
        sorted(
            (
                _evaluate_action(
                    action,
                    package_root=package_root,
                    validation=validation,
                    manifest_records=manifest_records,
                )
                for action in approval.approved_actions
            ),
            key=_action_sort_key,
        )
    )
    return RepairPreflight(plan_sha256, approval_sha256, actions)


def write_repair_preflight(path: Path, preflight: RepairPreflight) -> None:
    """Write a deterministic Preflight schema v1 report atomically."""

    write_json_atomically(path, preflight.payload())


def parse_repair_preflight_bytes(content: bytes) -> RepairPreflight:
    """Parse Preflight v1 for safe validation of an existing output file."""

    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid Repair Preflight JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid Repair Preflight root")
    summary = payload.get("summary")
    plan = payload.get("plan")
    approval = payload.get("approval")
    actions = payload.get("actions")
    if not all(isinstance(value, dict) for value in (summary, plan, approval)) or not isinstance(
        actions, list
    ):
        raise ValueError("invalid Repair Preflight containers")
    assert isinstance(summary, dict)
    assert isinstance(plan, dict)
    assert isinstance(approval, dict)
    counts = (summary.get("actions"), summary.get("ready"), summary.get("blocked"))
    valid_counts = all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in counts
    )
    if not (
        payload.get("report_type") == "knowledge-package-repair-preflight"
        and payload.get("schema_version") == 1
        and not isinstance(payload.get("schema_version"), bool)
        and valid_counts
        and counts[0] == len(actions)
        and counts[1] + counts[2] == counts[0]
        and isinstance(plan.get("sha256"), str)
        and _SHA256.fullmatch(plan["sha256"]) is not None
        and isinstance(approval.get("sha256"), str)
        and _SHA256.fullmatch(approval["sha256"]) is not None
    ):
        raise ValueError("invalid Repair Preflight schema")
    parsed_actions: list[PreflightAction] = []
    for action in actions:
        if not isinstance(action, dict):
            raise ValueError("invalid Repair Preflight action")
        target = action.get("target")
        preconditions = action.get("preconditions")
        if not isinstance(target, dict) or not isinstance(preconditions, dict):
            raise ValueError("invalid Repair Preflight action")
        raw_action = action.get("action")
        raw_reason = action.get("reason_category")
        path = action.get("path")
        status = action.get("status")
        block_reason = action.get("block_reason")
        if (
            not isinstance(path, str)
            or not path
            or PurePosixPath(path).is_absolute()
            or "\\" in path
            or any(part in {"", ".", ".."} for part in PurePosixPath(path).parts)
            or raw_action not in {category.value for category in _SUPPORTED_ACTIONS}
            or not isinstance(raw_reason, str)
            or status not in {"ready", "blocked"}
        ):
            raise ValueError("invalid Repair Preflight action")
        category = RepairActionCategory(raw_action)
        expected_reason = (
            "missing-sidecar"
            if category is RepairActionCategory.REGENERATE_SIDECAR
            else "stale-sidecar"
        )
        expected_backup = category is RepairActionCategory.REMOVE_STALE_SIDECAR
        state_matches = preconditions.get("package_state_matches")
        if not (
            raw_reason == expected_reason
            and preconditions.get("plan_approved") is True
            and preconditions.get("safe") is True
            and isinstance(state_matches, bool)
            and preconditions.get("backup_required") is expected_backup
            and target.get("path") == path
            and isinstance(target.get("exists"), bool)
        ):
            raise ValueError("invalid Repair Preflight action")
        target_bytes = target.get("bytes")
        target_sha256 = target.get("sha256")
        target_exists = target["exists"]
        target_digest_complete = (
            isinstance(target_bytes, int)
            and not isinstance(target_bytes, bool)
            and target_bytes >= 0
            and isinstance(target_sha256, str)
            and _SHA256.fullmatch(target_sha256) is not None
        )
        target_digest_valid = (
            target_bytes is None and target_sha256 is None or target_digest_complete
        )
        status_valid = (
            status == "ready"
            and block_reason is None
            and state_matches
            or status == "blocked"
            and isinstance(block_reason, str)
            and bool(block_reason)
            and not state_matches
        )
        ready_target_valid = status != "ready" or (
            category is RepairActionCategory.REGENERATE_SIDECAR
            and not target_exists
            and target_bytes is None
            and target_sha256 is None
            or category is RepairActionCategory.REMOVE_STALE_SIDECAR
            and target_exists
            and target_digest_complete
        )
        if (
            not target_digest_valid
            or not target_exists
            and (target_bytes is not None or target_sha256 is not None)
            or not status_valid
            or not ready_target_valid
        ):
            raise ValueError("invalid Repair Preflight action")
        parsed_actions.append(
            PreflightAction(
                RepairAction(path, category, raw_reason, True),
                status,
                block_reason,
                state_matches,
                expected_backup,
                PreflightTarget(path, target_exists, target_bytes, target_sha256),
            )
        )
    if (
        sum(action.status == "ready" for action in parsed_actions) != counts[1]
        or sum(action.status == "blocked" for action in parsed_actions) != counts[2]
    ):
        raise ValueError("invalid Repair Preflight summary")
    return RepairPreflight(plan["sha256"], approval["sha256"], tuple(parsed_actions))


def is_repair_preflight_report(path: Path) -> bool:
    """Return whether an existing regular file is a Preflight v1 report."""

    if path.is_symlink() or not path.is_file():
        return False
    try:
        parse_repair_preflight_bytes(path.read_bytes())
    except (OSError, ValueError):
        return False
    return True
