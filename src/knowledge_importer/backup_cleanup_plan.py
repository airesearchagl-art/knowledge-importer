"""Deterministic dry-run cleanup planning for Repair Execution backups."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from knowledge_importer.backup_inventory import (
    BackupInventorySession,
    BackupSessionClassification,
    BackupSessionState,
    parse_backup_inventory_bytes,
    path_uses_link_or_reparse,
)
from knowledge_importer.json_writer import write_json_atomically

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_READ_CHUNK_SIZE = 1024 * 1024
_ACTION = "delete-backup-session"
_REASON = "explicit-retention-release"


class BackupCleanupPlanInputError(ValueError):
    """Raised when cleanup planning input cannot be read safely."""


@dataclass(frozen=True, slots=True)
class BackupCleanupAction:
    session: str
    session_manifest_sha256: str | None
    tree_sha256: str | None
    backup_files: int
    backup_bytes: int
    eligible: bool

    def payload(self) -> dict[str, object]:
        return {
            "action": _ACTION,
            "reason_category": _REASON,
            "session": self.session,
            "session_manifest_sha256": self.session_manifest_sha256,
            "tree_sha256": self.tree_sha256,
            "backup_files": self.backup_files,
            "backup_bytes": self.backup_bytes,
            "eligible": self.eligible,
        }


@dataclass(frozen=True, slots=True)
class BackupCleanupPlan:
    inventory_sha256: str
    actions: tuple[BackupCleanupAction, ...]

    @property
    def planned(self) -> int:
        return sum(action.eligible for action in self.actions)

    def payload(self) -> dict[str, object]:
        return {
            "report_type": "knowledge-importer-backup-cleanup-plan",
            "schema_version": 1,
            "inventory": {
                "sha256": self.inventory_sha256,
                "schema_version": 1,
            },
            "policy": {"mode": "explicit-sessions"},
            "summary": {
                "requested": len(self.actions),
                "planned": self.planned,
                "blocked": len(self.actions) - self.planned,
            },
            "actions": [action.payload() for action in self.actions],
        }


def _comparison_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_safe_session(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and not any(unicodedata.category(character) in {"Cc", "Cf"} for character in value)
    )


def _stable_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns


def read_input_bytes(path: Path) -> bytes:
    """Read one stable regular input file without following links or reparses."""

    if path_uses_link_or_reparse(path):
        raise BackupCleanupPlanInputError("unsafe cleanup lifecycle input")
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise BackupCleanupPlanInputError("cleanup lifecycle input is not a regular file")
        digest = bytearray()
        with path.open("rb") as source:
            opened = os.fstat(source.fileno())
            if _stable_identity(opened) != _stable_identity(before):
                raise BackupCleanupPlanInputError("cleanup lifecycle input changed")
            while chunk := source.read(_READ_CHUNK_SIZE):
                digest.extend(chunk)
        after = path.lstat()
    except OSError as exc:
        raise BackupCleanupPlanInputError("cleanup lifecycle input cannot be read") from exc
    if (
        path_uses_link_or_reparse(path)
        or not stat.S_ISREG(after.st_mode)
        or _stable_identity(after) != _stable_identity(before)
    ):
        raise BackupCleanupPlanInputError("cleanup lifecycle input changed")
    return bytes(digest)


def _action_for_session(
    session_name: str,
    inventory_session: BackupInventorySession | None,
) -> BackupCleanupAction:
    if inventory_session is None:
        return BackupCleanupAction(session_name, None, None, 0, 0, False)

    eligible = (
        inventory_session.classification is BackupSessionClassification.MANAGED
        and inventory_session.state is BackupSessionState.COMPLETE
        and inventory_session.planning_eligible
    )
    if not eligible:
        return BackupCleanupAction(inventory_session.session, None, None, 0, 0, False)
    return BackupCleanupAction(
        inventory_session.session,
        inventory_session.session_manifest_sha256,
        inventory_session.tree_sha256,
        len(inventory_session.items),
        sum(item.digest.bytes or 0 for item in inventory_session.items),
        True,
    )


def build_backup_cleanup_plan(
    inventory_path: Path,
    requested_sessions: tuple[str, ...],
) -> BackupCleanupPlan:
    """Bind a dry-run plan to exact Inventory bytes and explicit sessions."""

    if not requested_sessions or any(not _is_safe_session(item) for item in requested_sessions):
        raise BackupCleanupPlanInputError("invalid requested backup session")
    keys = [_comparison_key(item) for item in requested_sessions]
    if len(set(keys)) != len(keys):
        raise BackupCleanupPlanInputError("duplicate requested backup session")

    content = read_input_bytes(inventory_path)
    try:
        inventory = parse_backup_inventory_bytes(content)
    except ValueError as exc:
        raise BackupCleanupPlanInputError("invalid Backup Inventory") from exc
    sessions = {_comparison_key(item.session): item for item in inventory.sessions}
    actions = tuple(
        sorted(
            (
                _action_for_session(requested, sessions.get(_comparison_key(requested)))
                for requested in requested_sessions
            ),
            key=lambda action: _comparison_key(action.session),
        )
    )
    return BackupCleanupPlan(hashlib.sha256(content).hexdigest(), actions)


def parse_backup_cleanup_action(value: object) -> BackupCleanupAction:
    """Parse one Cleanup Plan v1 action and enforce its safe semantics."""

    if not isinstance(value, dict):
        raise ValueError("invalid Backup Cleanup action")
    manifest_sha256 = value.get("session_manifest_sha256")
    tree_sha256 = value.get("tree_sha256")
    eligible = value.get("eligible")
    if not (
        value.get("action") == _ACTION
        and value.get("reason_category") == _REASON
        and _is_safe_session(value.get("session"))
        and (manifest_sha256 is None or _is_sha256(manifest_sha256))
        and (tree_sha256 is None or _is_sha256(tree_sha256))
        and (manifest_sha256 is None) is (tree_sha256 is None)
        and _is_nonnegative_int(value.get("backup_files"))
        and _is_nonnegative_int(value.get("backup_bytes"))
        and isinstance(eligible, bool)
        and eligible is (manifest_sha256 is not None)
        and (eligible or value.get("backup_files") == 0)
        and (eligible or value.get("backup_bytes") == 0)
    ):
        raise ValueError("invalid Backup Cleanup action semantics")
    return BackupCleanupAction(
        value["session"],
        manifest_sha256,
        tree_sha256,
        value["backup_files"],
        value["backup_bytes"],
        eligible,
    )


def parse_backup_cleanup_plan_bytes(content: bytes) -> BackupCleanupPlan:
    """Parse and validate Cleanup Plan schema v1."""

    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid Backup Cleanup Plan JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid Backup Cleanup Plan root")
    inventory = payload.get("inventory")
    policy = payload.get("policy")
    summary = payload.get("summary")
    raw_actions = payload.get("actions")
    schema_version = payload.get("schema_version")
    if not (
        payload.get("report_type") == "knowledge-importer-backup-cleanup-plan"
        and schema_version == 1
        and not isinstance(schema_version, bool)
        and isinstance(inventory, dict)
        and inventory.get("schema_version") == 1
        and not isinstance(inventory.get("schema_version"), bool)
        and _is_sha256(inventory.get("sha256"))
        and isinstance(policy, dict)
        and policy.get("mode") == "explicit-sessions"
        and isinstance(summary, dict)
        and isinstance(raw_actions, list)
    ):
        raise ValueError("invalid Backup Cleanup Plan schema")
    actions = tuple(parse_backup_cleanup_action(value) for value in raw_actions)
    requested = summary.get("requested")
    planned = summary.get("planned")
    blocked = summary.get("blocked")
    if not (
        all(_is_nonnegative_int(value) for value in (requested, planned, blocked))
        and requested == len(actions)
        and planned == sum(action.eligible for action in actions)
        and blocked == requested - planned
        and list(actions) == sorted(actions, key=lambda action: _comparison_key(action.session))
        and len({_comparison_key(action.session) for action in actions}) == len(actions)
    ):
        raise ValueError("invalid Backup Cleanup Plan summary or action order")
    return BackupCleanupPlan(inventory["sha256"], actions)


def is_backup_cleanup_plan_report(path: Path) -> bool:
    """Return whether an existing regular file is a Cleanup Plan v1."""

    if path_uses_link_or_reparse(path):
        return False
    try:
        parse_backup_cleanup_plan_bytes(read_input_bytes(path))
    except (OSError, ValueError):
        return False
    return True


def write_backup_cleanup_plan(path: Path, plan: BackupCleanupPlan) -> None:
    """Write a deterministic Cleanup Plan v1 atomically."""

    write_json_atomically(path, plan.payload())
