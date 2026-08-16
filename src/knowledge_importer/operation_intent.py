"""Common immutable intent evidence for future destructive lifecycle integration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from knowledge_importer.backup_cleanup_plan import read_input_bytes
from knowledge_importer.backup_inventory import path_uses_link_or_reparse

OPERATION_INTENT_REPORT_TYPE = "knowledge-importer-operation-intent"
OPERATION_INTENT_SCHEMA_VERSION = 1
APPROVED_FOR_EXECUTION = "approved-for-execution"
REPAIR_EXECUTION = "repair-execution"
BACKUP_CLEANUP = "backup-cleanup"

_ATTEMPT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", re.ASCII)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_BINDING_ORDER = {
    REPAIR_EXECUTION: (
        "artifact-manifest",
        "repair-plan",
        "repair-approval",
        "repair-preflight",
    ),
    BACKUP_CLEANUP: (
        "backup-inventory",
        "backup-cleanup-plan",
        "backup-cleanup-approval",
    ),
}
_ACTION_REASONS = {
    REPAIR_EXECUTION: {
        "regenerate-sidecar": "missing-sidecar",
        "remove-stale-sidecar": "stale-sidecar",
    },
    BACKUP_CLEANUP: {
        "delete-backup-session": "explicit-retention-release",
    },
}


class OperationIntentOutputError(ValueError):
    """Raised when an immutable Receipt output path is unsafe or already occupied."""


@dataclass(frozen=True, slots=True)
class OperationIntentBinding:
    artifact_type: str
    schema_version: int
    sha256: str

    def payload(self) -> dict[str, object]:
        return {
            "artifact_type": self.artifact_type,
            "schema_version": self.schema_version,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class OperationIntentAction:
    action_index: int
    action: str
    target: str
    reason_category: str
    intent: str = APPROVED_FOR_EXECUTION

    def payload(self) -> dict[str, object]:
        return {
            "action_index": self.action_index,
            "action": self.action,
            "target": self.target,
            "reason_category": self.reason_category,
            "intent": self.intent,
        }


@dataclass(frozen=True, slots=True)
class OperationIntentReceipt:
    attempt_id: str
    operation_type: str
    bindings: tuple[OperationIntentBinding, ...]
    actions: tuple[OperationIntentAction, ...]

    def payload(self) -> dict[str, object]:
        return {
            "report_type": OPERATION_INTENT_REPORT_TYPE,
            "schema_version": OPERATION_INTENT_SCHEMA_VERSION,
            "attempt_id": self.attempt_id,
            "operation_type": self.operation_type,
            "bindings": [binding.payload() for binding in self.bindings],
            "actions": [action.payload() for action in self.actions],
        }


@dataclass(frozen=True, slots=True)
class OperationIntentLifecycleVerification:
    """Read-only comparison of current lifecycle inputs with one Receipt."""

    bindings_match: bool
    action_scope_matches: bool | None

    def __post_init__(self) -> None:
        if not isinstance(self.bindings_match, bool) or self.bindings_match != (
            self.action_scope_matches is not None
        ):
            raise ValueError("invalid lifecycle verification result")
        if self.action_scope_matches is not None and not isinstance(
            self.action_scope_matches, bool
        ):
            raise ValueError("invalid lifecycle action scope result")


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def validate_operation_intent_attempt_id(value: object) -> str:
    """Return one valid operator correlation label without treating it as identity."""

    if not isinstance(value, str) or _ATTEMPT_ID.fullmatch(value) is None:
        raise ValueError("invalid Operation Intent attempt_id")
    return value


def _is_safe_relative_posix_path(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if (
        "\\" in value
        or _WINDOWS_DRIVE.match(value) is not None
        or any(unicodedata.category(character) in {"Cc", "Cf"} for character in value)
    ):
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and path.as_posix() == value
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _comparison_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _action_sort_key(action: OperationIntentAction) -> tuple[str, str, str]:
    return (
        _comparison_key(action.target),
        action.action,
        action.reason_category,
    )


def _parse_binding(value: object) -> OperationIntentBinding:
    if not isinstance(value, dict):
        raise ValueError("invalid Operation Intent binding")
    artifact_type = value.get("artifact_type")
    schema_version = value.get("schema_version")
    sha256 = value.get("sha256")
    if not (
        isinstance(artifact_type, str)
        and isinstance(schema_version, int)
        and not isinstance(schema_version, bool)
        and schema_version == 1
        and _is_sha256(sha256)
    ):
        raise ValueError("invalid Operation Intent binding semantics")
    return OperationIntentBinding(artifact_type, schema_version, sha256)


def _parse_action(value: object, operation_type: str) -> OperationIntentAction:
    if not isinstance(value, dict):
        raise ValueError("invalid Operation Intent action")
    action_index = value.get("action_index")
    action = value.get("action")
    target = value.get("target")
    reason_category = value.get("reason_category")
    if not (
        _is_nonnegative_int(action_index)
        and isinstance(action, str)
        and action in _ACTION_REASONS[operation_type]
        and _is_safe_relative_posix_path(target)
        and reason_category == _ACTION_REASONS[operation_type][action]
        and value.get("intent") == APPROVED_FOR_EXECUTION
    ):
        raise ValueError("invalid Operation Intent action semantics")
    if operation_type == REPAIR_EXECUTION and not target.casefold().endswith(".metadata.json"):
        raise ValueError("invalid Repair Intent target")
    if operation_type == BACKUP_CLEANUP and "/" in target:
        raise ValueError("invalid Backup Cleanup Intent target")
    return OperationIntentAction(
        action_index,
        action,
        target,
        reason_category,
    )


def parse_operation_intent_bytes(content: bytes) -> OperationIntentReceipt:
    """Parse and semantically validate Operation Intent Receipt schema v1."""

    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid Operation Intent Receipt JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid Operation Intent Receipt root")
    schema_version = payload.get("schema_version")
    attempt_id = payload.get("attempt_id")
    operation_type = payload.get("operation_type")
    raw_bindings = payload.get("bindings")
    raw_actions = payload.get("actions")
    if not (
        payload.get("report_type") == OPERATION_INTENT_REPORT_TYPE
        and isinstance(schema_version, int)
        and not isinstance(schema_version, bool)
        and schema_version == OPERATION_INTENT_SCHEMA_VERSION
        and isinstance(attempt_id, str)
        and isinstance(operation_type, str)
        and operation_type in _BINDING_ORDER
        and isinstance(raw_bindings, list)
        and isinstance(raw_actions, list)
    ):
        raise ValueError("invalid Operation Intent Receipt schema")
    attempt_id = validate_operation_intent_attempt_id(attempt_id)

    bindings = tuple(_parse_binding(value) for value in raw_bindings)
    if tuple(binding.artifact_type for binding in bindings) != _BINDING_ORDER[operation_type]:
        raise ValueError("invalid Operation Intent binding order or set")
    if len({binding.artifact_type for binding in bindings}) != len(bindings):
        raise ValueError("duplicate Operation Intent binding")

    actions = tuple(_parse_action(value, operation_type) for value in raw_actions)
    if [action.action_index for action in actions] != list(range(len(actions))):
        raise ValueError("non-contiguous Operation Intent action index")
    if list(actions) != sorted(actions, key=_action_sort_key):
        raise ValueError("non-canonical Operation Intent action order")
    targets = [_comparison_key(action.target) for action in actions]
    if len(set(targets)) != len(targets):
        raise ValueError("duplicate Operation Intent action target")

    return OperationIntentReceipt(attempt_id, operation_type, bindings, actions)


def operation_intent_sha256(content: bytes) -> str:
    """Return the security identity of one valid Receipt from its exact bytes."""

    parse_operation_intent_bytes(content)
    return hashlib.sha256(content).hexdigest()


def operation_intent_bytes(receipt: OperationIntentReceipt) -> bytes:
    """Serialize one valid Receipt deterministically."""

    content = (json.dumps(receipt.payload(), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    parse_operation_intent_bytes(content)
    return content


def validate_operation_intent_output_path(path: Path) -> None:
    """Require a new, link-free path for an immutable Receipt."""

    try:
        path.lstat()
    except FileNotFoundError:
        if path_uses_link_or_reparse(path):
            raise OperationIntentOutputError("unsafe Operation Intent output") from None
        return
    except OSError as exc:
        raise OperationIntentOutputError("Operation Intent output cannot be verified") from exc
    raise OperationIntentOutputError("Operation Intent output already exists")


def write_operation_intent(path: Path, receipt: OperationIntentReceipt) -> None:
    """Create an immutable deterministic Receipt without replacing any existing entry."""

    validate_operation_intent_output_path(path)
    content = operation_intent_bytes(receipt)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path_uses_link_or_reparse(path.parent) or not path.parent.is_dir():
        raise OSError("unsafe Operation Intent output parent")
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
            raise OSError("Operation Intent output verification failed")
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
