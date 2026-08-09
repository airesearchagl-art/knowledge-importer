"""Deterministic read-only repair planning for Knowledge Packages."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from knowledge_importer.json_writer import write_json_atomically
from knowledge_importer.package_validation import (
    PackageValidationResult,
    ValidationIssue,
    ValidationSeverity,
)


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
