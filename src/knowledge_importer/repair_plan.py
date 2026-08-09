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


def _is_repair_action_payload(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    action_values = {category.value for category in RepairActionCategory}
    return (
        _is_relative_posix_path(value.get("path"))
        and value.get("action") in action_values
        and isinstance(value.get("reason_category"), str)
        and bool(value.get("reason_category"))
        and isinstance(value.get("safe"), bool)
    )


def is_repair_plan_report(path: Path) -> bool:
    """Return whether an existing regular file is a Repair Plan v1 report."""

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
    if not isinstance(summary, dict) or not isinstance(actions, list):
        return False
    issue_count = summary.get("issues")
    action_count = summary.get("actions")
    manual_review = summary.get("manual_review")
    return (
        payload.get("report_type") == "knowledge-package-repair-plan"
        and payload.get("schema_version") == 1
        and not isinstance(payload.get("schema_version"), bool)
        and all(_is_nonnegative_int(value) for value in (issue_count, action_count, manual_review))
        and action_count == len(actions)
        and issue_count >= action_count
        and manual_review
        == sum(
            isinstance(action, dict)
            and action.get("action") == RepairActionCategory.MANUAL_REVIEW.value
            for action in actions
        )
        and all(_is_repair_action_payload(action) for action in actions)
    )
