"""Human Approval contract for an exact Backup Cleanup Plan v1 file."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from knowledge_importer.backup_cleanup_plan import (
    BackupCleanupAction,
    parse_backup_cleanup_action,
    parse_backup_cleanup_plan_bytes,
    read_input_bytes,
)
from knowledge_importer.backup_inventory import path_uses_link_or_reparse
from knowledge_importer.json_writer import write_json_atomically

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _comparison_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


@dataclass(frozen=True, slots=True)
class BackupCleanupApproval:
    plan_sha256: str
    approved_actions: tuple[BackupCleanupAction, ...]

    def payload(self) -> dict[str, object]:
        return {
            "report_type": "knowledge-importer-backup-cleanup-approval",
            "schema_version": 1,
            "plan": {"sha256": self.plan_sha256, "schema_version": 1},
            "scope": {"mode": "all-planned"},
            "approved_actions": [action.payload() for action in self.approved_actions],
        }


def build_backup_cleanup_approval(plan_path: Path) -> BackupCleanupApproval:
    """Bind approval to exact Plan bytes and include eligible actions only."""

    content = read_input_bytes(plan_path)
    plan = parse_backup_cleanup_plan_bytes(content)
    return BackupCleanupApproval(
        hashlib.sha256(content).hexdigest(),
        tuple(action for action in plan.actions if action.eligible),
    )


def parse_backup_cleanup_approval_bytes(content: bytes) -> BackupCleanupApproval:
    """Parse and validate Backup Cleanup Approval schema v1."""

    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid Backup Cleanup Approval JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid Backup Cleanup Approval root")
    plan = payload.get("plan")
    scope = payload.get("scope")
    raw_actions = payload.get("approved_actions")
    schema_version = payload.get("schema_version")
    if not (
        payload.get("report_type") == "knowledge-importer-backup-cleanup-approval"
        and schema_version == 1
        and not isinstance(schema_version, bool)
        and isinstance(plan, dict)
        and plan.get("schema_version") == 1
        and not isinstance(plan.get("schema_version"), bool)
        and isinstance(plan.get("sha256"), str)
        and _SHA256.fullmatch(plan["sha256"]) is not None
        and isinstance(scope, dict)
        and scope.get("mode") == "all-planned"
        and isinstance(raw_actions, list)
    ):
        raise ValueError("invalid Backup Cleanup Approval schema")
    actions = tuple(parse_backup_cleanup_action(value) for value in raw_actions)
    if (
        any(not action.eligible for action in actions)
        or list(actions) != sorted(actions, key=lambda action: _comparison_key(action.session))
        or len({_comparison_key(action.session) for action in actions}) != len(actions)
    ):
        raise ValueError("invalid Backup Cleanup Approval actions")
    return BackupCleanupApproval(plan["sha256"], actions)


def verify_backup_cleanup_approval(
    plan_bytes: bytes,
    approval_bytes: bytes,
) -> BackupCleanupApproval:
    """Verify exact Plan bytes and the complete ordered eligible action set."""

    plan = parse_backup_cleanup_plan_bytes(plan_bytes)
    approval = parse_backup_cleanup_approval_bytes(approval_bytes)
    if hashlib.sha256(plan_bytes).hexdigest() != approval.plan_sha256:
        raise ValueError("Backup Cleanup Approval Plan binding mismatch")
    expected_actions = tuple(action for action in plan.actions if action.eligible)
    if approval.approved_actions != expected_actions:
        raise ValueError("Backup Cleanup Approval actions do not match Plan")
    return approval


def is_backup_cleanup_approval_report(path: Path) -> bool:
    """Return whether an existing regular file is a Cleanup Approval v1."""

    if path_uses_link_or_reparse(path):
        return False
    try:
        parse_backup_cleanup_approval_bytes(read_input_bytes(path))
    except (OSError, ValueError):
        return False
    return True


def write_backup_cleanup_approval(path: Path, approval: BackupCleanupApproval) -> None:
    """Write a deterministic Cleanup Approval v1 atomically."""

    write_json_atomically(path, approval.payload())
