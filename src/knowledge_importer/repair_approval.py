"""Human Gate approval contract for a specific Repair Plan file."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from knowledge_importer.json_writer import write_json_atomically
from knowledge_importer.repair_plan import (
    RepairAction,
    RepairActionCategory,
    parse_repair_action,
    parse_repair_plan_bytes,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_READ_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class RepairApproval:
    plan_sha256: str
    approved_actions: tuple[RepairAction, ...]

    def payload(self) -> dict[str, object]:
        return {
            "report_type": "knowledge-package-repair-approval",
            "schema_version": 1,
            "plan": {
                "sha256": self.plan_sha256,
                "schema_version": 1,
            },
            "scope": {"mode": "all-safe"},
            "approved_actions": [action.payload() for action in self.approved_actions],
        }


def build_repair_approval(plan_path: Path) -> RepairApproval:
    """Validate and bind an all-safe approval to the exact Repair Plan bytes."""

    digest = hashlib.sha256()
    content = bytearray()
    with plan_path.open("rb") as source:
        while chunk := source.read(_READ_CHUNK_SIZE):
            digest.update(chunk)
            content.extend(chunk)
    plan = parse_repair_plan_bytes(bytes(content))
    approved_actions = tuple(
        action
        for action in plan.actions
        if action.safe and action.action is not RepairActionCategory.MANUAL_REVIEW
    )
    return RepairApproval(digest.hexdigest(), approved_actions)


def parse_repair_approval_bytes(content: bytes) -> RepairApproval:
    """Parse and validate Approval schema v1."""

    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid Repair Approval JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid Repair Approval root")
    plan = payload.get("plan")
    scope = payload.get("scope")
    raw_actions = payload.get("approved_actions")
    if (
        not isinstance(plan, dict)
        or not isinstance(scope, dict)
        or not isinstance(raw_actions, list)
    ):
        raise ValueError("invalid Repair Approval containers")
    schema_version = payload.get("schema_version")
    plan_schema_version = plan.get("schema_version")
    plan_sha256 = plan.get("sha256")
    if not (
        payload.get("report_type") == "knowledge-package-repair-approval"
        and schema_version == 1
        and not isinstance(schema_version, bool)
        and plan_schema_version == 1
        and not isinstance(plan_schema_version, bool)
        and isinstance(plan_sha256, str)
        and _SHA256.fullmatch(plan_sha256) is not None
        and scope.get("mode") == "all-safe"
    ):
        raise ValueError("invalid Repair Approval schema")
    actions = tuple(parse_repair_action(action) for action in raw_actions)
    if any(
        not action.safe or action.action is RepairActionCategory.MANUAL_REVIEW for action in actions
    ):
        raise ValueError("unsafe Repair Approval action")
    return RepairApproval(plan_sha256, actions)


def is_repair_approval_report(path: Path) -> bool:
    """Return whether an existing regular file is an Approval schema v1 report."""

    if path.is_symlink() or not path.is_file():
        return False
    try:
        parse_repair_approval_bytes(path.read_bytes())
    except (OSError, ValueError):
        return False
    return True


def write_repair_approval(path: Path, approval: RepairApproval) -> None:
    """Write a deterministic Repair Approval atomically."""

    write_json_atomically(path, approval.payload())
