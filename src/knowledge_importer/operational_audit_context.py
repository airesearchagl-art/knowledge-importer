"""Immutable evidence envelope for Operational Audit and Intent Status reports."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from knowledge_importer.backup_cleanup_plan import read_input_bytes
from knowledge_importer.backup_inventory import path_uses_link_or_reparse
from knowledge_importer.intent_status import parse_operation_intent_status_bytes
from knowledge_importer.operational_audit import parse_operational_audit_bytes

OPERATIONAL_AUDIT_CONTEXT_REPORT_TYPE = "knowledge-importer-operational-audit-context"
OPERATIONAL_AUDIT_CONTEXT_SCHEMA_VERSION = 1

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class OperationalAuditContextInputError(ValueError):
    """Raised when context evidence or its immutable output boundary is invalid."""


@dataclass(frozen=True, slots=True)
class OperationalAuditProjection:
    schema_version: int
    sha256: str
    operations: int
    operator_action_required_count: int

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "sha256": self.sha256,
            "operations": self.operations,
            "operator_action_required_count": self.operator_action_required_count,
        }


@dataclass(frozen=True, slots=True)
class IntentStatusProjection:
    schema_version: int
    sha256: str
    operator_action_required: bool

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "sha256": self.sha256,
            "operator_action_required": self.operator_action_required,
        }


@dataclass(frozen=True, slots=True)
class OperationalAuditContext:
    operational_audit: OperationalAuditProjection
    intent_statuses: tuple[IntentStatusProjection, ...]

    def payload(self) -> dict[str, object]:
        operator_action_required = self.operational_audit.operator_action_required_count > 0 or any(
            status.operator_action_required for status in self.intent_statuses
        )
        return {
            "report_type": OPERATIONAL_AUDIT_CONTEXT_REPORT_TYPE,
            "schema_version": OPERATIONAL_AUDIT_CONTEXT_SCHEMA_VERSION,
            "operational_audit": self.operational_audit.payload(),
            "intent_statuses": [status.payload() for status in self.intent_statuses],
            "summary": {
                "audit_operations": self.operational_audit.operations,
                "intent_statuses": len(self.intent_statuses),
                "operator_action_required": operator_action_required,
            },
        }


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_operational_audit(path: Path) -> tuple[bytes, OperationalAuditProjection]:
    try:
        content = read_input_bytes(path)
        audit = parse_operational_audit_bytes(content)
    except (OSError, TypeError, ValueError) as exc:
        raise OperationalAuditContextInputError(
            "invalid Operational Audit Context audit source"
        ) from exc
    required = sum(operation.operator_action_required for operation in audit.operations)
    return content, OperationalAuditProjection(1, _sha256(content), len(audit.operations), required)


def _read_intent_status(path: Path) -> tuple[bytes, IntentStatusProjection]:
    try:
        content = read_input_bytes(path)
        status = parse_operation_intent_status_bytes(content)
    except (OSError, TypeError, ValueError) as exc:
        raise OperationalAuditContextInputError(
            "invalid Operational Audit Context Intent Status source"
        ) from exc
    return content, IntentStatusProjection(1, _sha256(content), status.operator_action_required)


def build_operational_audit_context(
    operational_audit_path: Path,
    *,
    intent_status_paths: Sequence[Path] = (),
) -> OperationalAuditContext:
    """Bind one Audit and zero or more Status reports without associating them."""

    _, audit_projection = _read_operational_audit(operational_audit_path)
    statuses = [_read_intent_status(path) for path in intent_status_paths]
    status_digests = [projection.sha256 for _, projection in statuses]
    if len(set(status_digests)) != len(status_digests):
        raise OperationalAuditContextInputError("duplicate Intent Status source")
    projections = tuple(
        sorted((projection for _, projection in statuses), key=lambda item: item.sha256)
    )
    return OperationalAuditContext(audit_projection, projections)


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _parse_audit_projection(value: object) -> OperationalAuditProjection:
    if not isinstance(value, dict):
        raise ValueError("invalid Operational Audit Context audit projection")
    schema_version = value.get("schema_version")
    sha256 = value.get("sha256")
    operations = value.get("operations")
    required = value.get("operator_action_required_count")
    if not (
        isinstance(schema_version, int)
        and not isinstance(schema_version, bool)
        and schema_version == 1
        and _is_sha256(sha256)
        and _is_nonnegative_int(operations)
        and _is_nonnegative_int(required)
        and required <= operations
    ):
        raise ValueError("invalid Operational Audit Context audit projection")
    return OperationalAuditProjection(schema_version, sha256, operations, required)


def _parse_status_projection(value: object) -> IntentStatusProjection:
    if not isinstance(value, dict):
        raise ValueError("invalid Operational Audit Context status projection")
    schema_version = value.get("schema_version")
    sha256 = value.get("sha256")
    operator_action_required = value.get("operator_action_required")
    if not (
        isinstance(schema_version, int)
        and not isinstance(schema_version, bool)
        and schema_version == 1
        and _is_sha256(sha256)
        and isinstance(operator_action_required, bool)
    ):
        raise ValueError("invalid Operational Audit Context status projection")
    return IntentStatusProjection(schema_version, sha256, operator_action_required)


def parse_operational_audit_context_bytes(content: bytes) -> OperationalAuditContext:
    """Validate Context v1 internal structure, not source projection authenticity."""

    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid Operational Audit Context JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid Operational Audit Context root")
    schema_version = payload.get("schema_version")
    raw_statuses = payload.get("intent_statuses")
    summary = payload.get("summary")
    if not (
        payload.get("report_type") == OPERATIONAL_AUDIT_CONTEXT_REPORT_TYPE
        and isinstance(schema_version, int)
        and not isinstance(schema_version, bool)
        and schema_version == OPERATIONAL_AUDIT_CONTEXT_SCHEMA_VERSION
        and isinstance(raw_statuses, list)
        and isinstance(summary, dict)
    ):
        raise ValueError("invalid Operational Audit Context schema")

    audit_projection = _parse_audit_projection(payload.get("operational_audit"))
    statuses = tuple(_parse_status_projection(value) for value in raw_statuses)
    status_digests = [status.sha256 for status in statuses]
    if status_digests != sorted(status_digests) or len(set(status_digests)) != len(status_digests):
        raise ValueError("invalid Operational Audit Context status order or duplicate")

    context = OperationalAuditContext(audit_projection, statuses)
    expected = context.payload()["summary"]
    assert isinstance(expected, dict)
    if not (
        _is_nonnegative_int(summary.get("audit_operations"))
        and _is_nonnegative_int(summary.get("intent_statuses"))
        and isinstance(summary.get("operator_action_required"), bool)
        and all(summary.get(key) == value for key, value in expected.items())
    ):
        raise ValueError("invalid Operational Audit Context summary")
    return context


def operational_audit_context_bytes(context: OperationalAuditContext) -> bytes:
    """Serialize deterministic UTF-8 JSON with no source path information."""

    content = (json.dumps(context.payload(), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    parse_operational_audit_context_bytes(content)
    return content


def validate_operational_audit_context_output_path(path: Path) -> None:
    """Require a new, link-free path for an immutable Context report."""

    try:
        path.lstat()
    except FileNotFoundError:
        if path_uses_link_or_reparse(path):
            raise OperationalAuditContextInputError(
                "unsafe Operational Audit Context output"
            ) from None
        return
    except OSError as exc:
        raise OperationalAuditContextInputError(
            "Operational Audit Context output cannot be verified"
        ) from exc
    raise OperationalAuditContextInputError("Operational Audit Context output already exists")


def write_operational_audit_context(path: Path, context: OperationalAuditContext) -> None:
    """Create one immutable deterministic Context without replacing an entry."""

    validate_operational_audit_context_output_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path_uses_link_or_reparse(path.parent) or not path.parent.is_dir():
        raise OSError("unsafe Operational Audit Context output parent")
    content = operational_audit_context_bytes(context)
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
            raise OSError("Operational Audit Context output verification failed")
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
