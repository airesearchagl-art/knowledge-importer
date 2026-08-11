"""Deterministic read-only repair planning for Knowledge Packages."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

from knowledge_importer.json_writer import write_json_atomically
from knowledge_importer.package_validation import (
    PackageValidationResult,
    ValidationIssue,
    ValidationSeverity,
)

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


class RepairActionCategory(Enum):
    REGENERATE_SIDECAR = "regenerate-sidecar"
    REMOVE_STALE_SIDECAR = "remove-stale-sidecar"
    REGENERATE_MANIFEST = "regenerate-manifest"
    VERIFY_ARTIFACT = "verify-artifact"
    MANUAL_REVIEW = "manual-review"


@dataclass(frozen=True, slots=True)
class RepairAction:
    path: str
    action: RepairActionCategory
    reason_category: str
    safe: bool

    def payload(self) -> dict[str, object]:
        return {
            "path": self.path,
            "action": self.action.value,
            "reason_category": self.reason_category,
            "safe": self.safe,
        }


@dataclass(frozen=True, slots=True)
class RepairPlan:
    issues: int
    actions: tuple[RepairAction, ...]

    @property
    def manual_review(self) -> int:
        return sum(action.action is RepairActionCategory.MANUAL_REVIEW for action in self.actions)

    def payload(self) -> dict[str, object]:
        return {
            "report_type": "knowledge-package-repair-plan",
            "schema_version": 1,
            "summary": {
                "issues": self.issues,
                "actions": len(self.actions),
                "manual_review": self.manual_review,
            },
            "actions": [action.payload() for action in self.actions],
        }


def _sort_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _action_sort_key(action: RepairAction) -> tuple[str, str, str]:
    return (_sort_key(action.path), action.action.value, action.reason_category)


def _manifest_is_trusted(
    result: PackageValidationResult,
    manifest_name: str | None,
) -> bool:
    if manifest_name is None:
        return False
    invalid_categories = {
        "invalid-json",
        "invalid-schema",
        "unsupported-schema",
        "outside-package-root",
    }
    manifest_key = _sort_key(manifest_name)
    return not any(
        _sort_key(issue.path) == manifest_key and issue.category in invalid_categories
        for issue in result.issues
    )


def _action_for_issue(issue: ValidationIssue, *, manifest_trusted: bool) -> RepairAction | None:
    if issue.severity is ValidationSeverity.WARNING:
        return None
    if issue.category == "missing-sidecar" and manifest_trusted:
        category = RepairActionCategory.REGENERATE_SIDECAR
        safe = True
    elif issue.category == "stale-sidecar" and manifest_trusted:
        category = RepairActionCategory.REMOVE_STALE_SIDECAR
        safe = True
    elif issue.category == "extra-artifact":
        category = RepairActionCategory.REGENERATE_MANIFEST
        safe = False
    else:
        category = RepairActionCategory.MANUAL_REVIEW
        safe = False
    return RepairAction(issue.path, category, issue.category, safe)


def build_repair_plan(
    result: PackageValidationResult,
    *,
    manifest_name: str | None = None,
) -> RepairPlan:
    """Convert an existing validation result into a deterministic repair plan."""

    manifest_trusted = _manifest_is_trusted(result, manifest_name)
    actions = tuple(
        sorted(
            (
                action
                for issue in result.issues
                if (action := _action_for_issue(issue, manifest_trusted=manifest_trusted))
                is not None
            ),
            key=_action_sort_key,
        )
    )
    return RepairPlan(issues=len(result.issues), actions=actions)


def write_repair_plan(path: Path, plan: RepairPlan) -> None:
    """Write a deterministic repair plan atomically."""

    write_json_atomically(path, plan.payload())


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_relative_posix_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or _WINDOWS_DRIVE.match(value):
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def parse_repair_action(value: object) -> RepairAction:
    """Parse one Repair Plan v1 action without reinterpreting its fields."""

    if not isinstance(value, dict):
        raise ValueError("invalid Repair Plan action")
    action_values = {category.value for category in RepairActionCategory}
    valid = (
        _is_relative_posix_path(value.get("path"))
        and value.get("action") in action_values
        and isinstance(value.get("reason_category"), str)
        and bool(value.get("reason_category"))
        and isinstance(value.get("safe"), bool)
    )
    if not valid:
        raise ValueError("invalid Repair Plan action")
    action = RepairAction(
        path=value["path"],
        action=RepairActionCategory(value["action"]),
        reason_category=value["reason_category"],
        safe=value["safe"],
    )
    valid_semantics = {
        RepairActionCategory.REGENERATE_SIDECAR: (
            action.reason_category == "missing-sidecar" and action.safe
        ),
        RepairActionCategory.REMOVE_STALE_SIDECAR: (
            action.reason_category == "stale-sidecar" and action.safe
        ),
        RepairActionCategory.REGENERATE_MANIFEST: (
            action.reason_category == "extra-artifact" and not action.safe
        ),
        RepairActionCategory.VERIFY_ARTIFACT: not action.safe,
        RepairActionCategory.MANUAL_REVIEW: not action.safe,
    }
    if not valid_semantics[action.action]:
        raise ValueError("invalid Repair Plan action semantics")
    return action


def parse_repair_plan_bytes(content: bytes) -> RepairPlan:
    """Parse and validate Repair Plan v1 from its original file bytes."""

    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid Repair Plan JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid Repair Plan root")
    summary = payload.get("summary")
    raw_actions = payload.get("actions")
    if not isinstance(summary, dict) or not isinstance(raw_actions, list):
        raise ValueError("invalid Repair Plan containers")
    issue_count = summary.get("issues")
    action_count = summary.get("actions")
    manual_review = summary.get("manual_review")
    if not (
        payload.get("report_type") == "knowledge-package-repair-plan"
        and payload.get("schema_version") == 1
        and not isinstance(payload.get("schema_version"), bool)
        and all(_is_nonnegative_int(value) for value in (issue_count, action_count, manual_review))
    ):
        raise ValueError("invalid Repair Plan schema")
    actions = tuple(parse_repair_action(action) for action in raw_actions)
    if not (
        action_count == len(actions)
        and issue_count >= action_count
        and manual_review
        == sum(action.action is RepairActionCategory.MANUAL_REVIEW for action in actions)
    ):
        raise ValueError("invalid Repair Plan summary")
    return RepairPlan(issues=issue_count, actions=actions)


def read_repair_plan(path: Path) -> RepairPlan:
    """Read and validate a Repair Plan v1 file."""

    return parse_repair_plan_bytes(path.read_bytes())


def is_repair_plan_report(path: Path) -> bool:
    """Return whether an existing regular file is a Repair Plan v1 report."""

    if path.is_symlink() or not path.is_file():
        return False
    try:
        read_repair_plan(path)
    except (OSError, ValueError):
        return False
    return True
