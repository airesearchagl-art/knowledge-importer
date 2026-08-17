from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from pathlib import Path

import pytest

import knowledge_importer.operational_audit_context as context_module
from knowledge_importer.intent_status import (
    FINAL_REPORT_MISSING,
    LIFECYCLE_BINDING_MISMATCH,
    MISMATCH,
    MISSING,
    NOT_APPLICABLE,
    PAIRED,
    VERIFIED,
    IntentStatusFinalReport,
    IntentStatusReceipt,
    OperationIntentStatus,
    operation_intent_status_bytes,
)
from knowledge_importer.operation_intent import REPAIR_EXECUTION
from knowledge_importer.operational_audit import (
    BACKUP_CLEANUP_SOURCE,
    REPAIR_EXECUTION_SOURCE,
    OperationalAudit,
    OperationalAuditOperation,
    OperationalAuditSource,
)
from knowledge_importer.operational_audit_context import (
    OperationalAuditContextInputError,
    build_operational_audit_context,
    operational_audit_context_bytes,
    parse_operational_audit_context_bytes,
    write_operational_audit_context,
)


def _json_bytes(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()


def _audit_bytes(*statuses: str) -> bytes:
    source = OperationalAuditSource(BACKUP_CLEANUP_SOURCE, 1, "a" * 64)
    operations: list[OperationalAuditOperation] = []
    for index, status in enumerate(statuses):
        failed = status == "failed"
        operations.append(
            OperationalAuditOperation(
                source.source_type,
                source.sha256,
                index,
                "backup-delete-session",
                f"knowledge-importer-repair-session-{index}",
                "backup",
                status,
                "failed" if failed else "succeeded",
                {"files": 1, "bytes": 12, "tree_sha256": "b" * 64},
                {"exists": failed},
                None,
                "unknown",
                failed,
                "cleanup-failed" if failed else "completed",
            )
        )
    return _json_bytes(OperationalAudit((source,), tuple(operations)).payload())


def _status_bytes(seed: str, *, operator_action_required: bool) -> bytes:
    receipt = IntentStatusReceipt(seed * 64, REPAIR_EXECUTION, f"attempt-{seed}")
    if operator_action_required:
        status = OperationIntentStatus(
            receipt,
            "orphan",
            (),
            MISSING,
            FINAL_REPORT_MISSING,
        )
    else:
        status = OperationIntentStatus(
            receipt,
            PAIRED,
            (IntentStatusFinalReport(REPAIR_EXECUTION_SOURCE, seed * 64),),
            VERIFIED,
            PAIRED,
        )
    return operation_intent_status_bytes(status)


def _paired_mismatch_status_bytes(seed: str) -> bytes:
    return operation_intent_status_bytes(
        OperationIntentStatus(
            IntentStatusReceipt(seed * 64, REPAIR_EXECUTION, f"attempt-{seed}"),
            PAIRED,
            (IntentStatusFinalReport(REPAIR_EXECUTION_SOURCE, "f" * 64),),
            VERIFIED,
            LIFECYCLE_BINDING_MISMATCH,
            MISMATCH,
            NOT_APPLICABLE,
        )
    )


def _write_sources(
    tmp_path: Path,
    *,
    audit: bytes | None = None,
    statuses: tuple[bytes, ...] = (),
) -> tuple[Path, tuple[Path, ...]]:
    audit_path = tmp_path / "audit.json"
    audit_path.write_bytes(_audit_bytes() if audit is None else audit)
    status_paths: list[Path] = []
    for index, content in enumerate(statuses):
        path = tmp_path / f"status-{index}.json"
        path.write_bytes(content)
        status_paths.append(path)
    return audit_path, tuple(status_paths)


def test_builds_audit_only_context_from_exact_bytes(tmp_path: Path) -> None:
    audit = _audit_bytes("deleted", "failed")
    audit_path, _ = _write_sources(tmp_path, audit=audit)

    payload = build_operational_audit_context(audit_path).payload()

    assert payload["operational_audit"] == {
        "schema_version": 1,
        "sha256": hashlib.sha256(audit).hexdigest(),
        "operations": 2,
        "operator_action_required_count": 1,
    }
    assert payload["intent_statuses"] == []
    assert payload["summary"] == {
        "audit_operations": 2,
        "intent_statuses": 0,
        "operator_action_required": True,
    }


def test_projects_one_intent_status_without_copying_classification(tmp_path: Path) -> None:
    status = _status_bytes("1", operator_action_required=True)
    audit_path, status_paths = _write_sources(tmp_path, statuses=(status,))

    payload = build_operational_audit_context(
        audit_path, intent_status_paths=status_paths
    ).payload()

    assert payload["intent_statuses"] == [
        {
            "schema_version": 1,
            "sha256": hashlib.sha256(status).hexdigest(),
            "operator_action_required": True,
        }
    ]
    serialized = operational_audit_context_bytes(
        build_operational_audit_context(audit_path, intent_status_paths=status_paths)
    )
    for absent in (
        b"classification",
        b"lifecycle_inputs",
        b"current_preconditions",
        b"outcome",
        b"failure",
        b"association",
    ):
        assert absent not in serialized


def test_status_order_is_sha256_canonical_and_cli_order_independent(tmp_path: Path) -> None:
    status_a = _status_bytes("1", operator_action_required=False)
    status_b = _status_bytes("2", operator_action_required=True)
    audit_path, status_paths = _write_sources(tmp_path, statuses=(status_b, status_a))

    forward = build_operational_audit_context(audit_path, intent_status_paths=status_paths)
    reverse = build_operational_audit_context(
        audit_path, intent_status_paths=tuple(reversed(status_paths))
    )

    assert operational_audit_context_bytes(forward) == operational_audit_context_bytes(reverse)
    digests = [item.sha256 for item in forward.intent_statuses]
    assert digests == sorted(digests)


def test_same_semantic_source_with_one_byte_difference_has_new_identity(tmp_path: Path) -> None:
    original = _status_bytes("1", operator_action_required=True)
    changed = original[:-1] + b" \n"
    audit_path, status_paths = _write_sources(tmp_path, statuses=(original, changed))

    context = build_operational_audit_context(audit_path, intent_status_paths=status_paths)

    assert len(context.intent_statuses) == 2
    assert len({item.sha256 for item in context.intent_statuses}) == 2


def test_duplicate_exact_status_bytes_are_rejected(tmp_path: Path) -> None:
    status = _status_bytes("1", operator_action_required=True)
    audit_path, status_paths = _write_sources(tmp_path, statuses=(status, status))

    with pytest.raises(OperationalAuditContextInputError, match="duplicate"):
        build_operational_audit_context(audit_path, intent_status_paths=status_paths)


@pytest.mark.parametrize(
    ("audit", "statuses", "expected"),
    [
        (_audit_bytes(), (), False),
        (_audit_bytes("deleted"), (_status_bytes("1", operator_action_required=False),), False),
        (_audit_bytes("failed"), (_status_bytes("1", operator_action_required=False),), True),
        (_audit_bytes(), (_status_bytes("1", operator_action_required=True),), True),
        (_audit_bytes(), (_paired_mismatch_status_bytes("1"),), True),
    ],
)
def test_operator_action_required_is_a_projection_or(
    tmp_path: Path,
    audit: bytes,
    statuses: tuple[bytes, ...],
    expected: bool,
) -> None:
    audit_path, status_paths = _write_sources(tmp_path, audit=audit, statuses=statuses)

    summary = build_operational_audit_context(
        audit_path, intent_status_paths=status_paths
    ).payload()["summary"]

    assert summary["operator_action_required"] is expected


def test_zero_audit_operations_is_valid(tmp_path: Path) -> None:
    audit_path, _ = _write_sources(tmp_path)

    context = build_operational_audit_context(audit_path)

    assert (
        parse_operational_audit_context_bytes(operational_audit_context_bytes(context)) == context
    )


def test_builder_rejects_invalid_formal_sources(tmp_path: Path) -> None:
    invalid_audit = tmp_path / "audit.json"
    invalid_audit.write_text("{}", encoding="utf-8")
    with pytest.raises(OperationalAuditContextInputError):
        build_operational_audit_context(invalid_audit)

    audit_path, status_paths = _write_sources(tmp_path, statuses=(b"{}",))
    with pytest.raises(OperationalAuditContextInputError):
        build_operational_audit_context(audit_path, intent_status_paths=status_paths)


def test_serialization_is_byte_identical_and_has_safe_format(tmp_path: Path) -> None:
    audit_path, status_paths = _write_sources(
        tmp_path, statuses=(_status_bytes("1", operator_action_required=True),)
    )
    context = build_operational_audit_context(audit_path, intent_status_paths=status_paths)

    first = operational_audit_context_bytes(context)
    second = operational_audit_context_bytes(context)

    assert first == second
    assert first.endswith(b"\n")
    assert b'  "report_type"' in first
    assert not any(unicodedata.category(character) == "Cf" for character in first.decode())


def test_parser_allows_unknown_v1_fields(tmp_path: Path) -> None:
    audit_path, status_paths = _write_sources(
        tmp_path, statuses=(_status_bytes("1", operator_action_required=True),)
    )
    payload = build_operational_audit_context(
        audit_path, intent_status_paths=status_paths
    ).payload()
    payload["future"] = {"ignored": True}
    payload["operational_audit"]["future"] = "ignored"
    payload["intent_statuses"][0]["future"] = "ignored"
    payload["summary"]["future"] = 1

    parsed = parse_operational_audit_context_bytes(_json_bytes(payload))

    assert parsed.operational_audit.operations == 0
    assert len(parsed.intent_statuses) == 1


def test_parser_rejects_future_schema() -> None:
    payload = {
        "report_type": context_module.OPERATIONAL_AUDIT_CONTEXT_REPORT_TYPE,
        "schema_version": 2,
        "operational_audit": {},
        "intent_statuses": [],
        "summary": {},
    }
    with pytest.raises(ValueError, match="schema"):
        parse_operational_audit_context_bytes(_json_bytes(payload))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("operational_audit", "sha256"), "A" * 64),
        (("operational_audit", "operations"), True),
        (("operational_audit", "operator_action_required_count"), -1),
        (("intent_statuses", 0, "sha256"), "not-a-digest"),
        (("intent_statuses", 0, "schema_version"), True),
        (("intent_statuses", 0, "operator_action_required"), 1),
        (("summary", "audit_operations"), True),
        (("summary", "operator_action_required"), 1),
    ],
)
def test_parser_rejects_malformed_projection_types(
    tmp_path: Path, path: tuple[object, ...], value: object
) -> None:
    audit_path, status_paths = _write_sources(
        tmp_path, statuses=(_status_bytes("1", operator_action_required=True),)
    )
    payload = build_operational_audit_context(
        audit_path, intent_status_paths=status_paths
    ).payload()
    target: object = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value

    with pytest.raises(ValueError):
        parse_operational_audit_context_bytes(_json_bytes(payload))


def test_parser_rejects_noncanonical_or_duplicate_statuses(tmp_path: Path) -> None:
    audit_path, status_paths = _write_sources(
        tmp_path,
        statuses=(
            _status_bytes("1", operator_action_required=False),
            _status_bytes("2", operator_action_required=True),
        ),
    )
    payload = build_operational_audit_context(
        audit_path, intent_status_paths=status_paths
    ).payload()
    payload["intent_statuses"].reverse()
    with pytest.raises(ValueError, match="order"):
        parse_operational_audit_context_bytes(_json_bytes(payload))

    payload["intent_statuses"] = [payload["intent_statuses"][0]] * 2
    with pytest.raises(ValueError, match="duplicate"):
        parse_operational_audit_context_bytes(_json_bytes(payload))


def test_parser_rejects_internally_inconsistent_summary(tmp_path: Path) -> None:
    audit_path, _ = _write_sources(tmp_path, audit=_audit_bytes("deleted"))
    payload = build_operational_audit_context(audit_path).payload()
    payload["summary"]["audit_operations"] = 2

    with pytest.raises(ValueError, match="summary"):
        parse_operational_audit_context_bytes(_json_bytes(payload))


def test_parser_does_not_claim_projection_authenticity(tmp_path: Path) -> None:
    audit_path, _ = _write_sources(tmp_path, audit=_audit_bytes("deleted"))
    payload = build_operational_audit_context(audit_path).payload()
    payload["operational_audit"]["operations"] = 2
    payload["summary"]["audit_operations"] = 2

    parsed = parse_operational_audit_context_bytes(_json_bytes(payload))

    assert parsed.operational_audit.operations == 2


def test_writer_creates_and_rereads_exact_deterministic_bytes(tmp_path: Path) -> None:
    audit_path, _ = _write_sources(tmp_path)
    context = build_operational_audit_context(audit_path)
    report = tmp_path / "nested" / "context.json"

    write_operational_audit_context(report, context)

    assert report.read_bytes() == operational_audit_context_bytes(context)


@pytest.mark.parametrize("kind", ["valid", "foreign", "directory"])
def test_writer_rejects_every_existing_entry(tmp_path: Path, kind: str) -> None:
    audit_path, _ = _write_sources(tmp_path)
    context = build_operational_audit_context(audit_path)
    report = tmp_path / "context.json"
    if kind == "valid":
        write_operational_audit_context(report, context)
    elif kind == "foreign":
        report.write_bytes(b"foreign\n")
    else:
        report.mkdir()

    with pytest.raises(OperationalAuditContextInputError, match="already exists"):
        write_operational_audit_context(report, context)


def test_writer_rejects_symlink_without_changing_target(tmp_path: Path) -> None:
    audit_path, _ = _write_sources(tmp_path)
    context = build_operational_audit_context(audit_path)
    target = tmp_path / "target.json"
    target.write_bytes(b"foreign\n")
    report = tmp_path / "context.json"
    try:
        report.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(OperationalAuditContextInputError):
        write_operational_audit_context(report, context)

    assert target.read_bytes() == b"foreign\n"


def test_writer_rejects_reparse_path_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit_path, _ = _write_sources(tmp_path)
    context = build_operational_audit_context(audit_path)
    report = tmp_path / "context.json"
    monkeypatch.setattr(
        context_module,
        "path_uses_link_or_reparse",
        lambda path: path == report,
    )

    with pytest.raises(OperationalAuditContextInputError, match="unsafe"):
        write_operational_audit_context(report, context)

    assert not report.exists()


def test_concurrent_writer_is_preserved_and_temp_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit_path, _ = _write_sources(tmp_path)
    context = build_operational_audit_context(audit_path)
    report = tmp_path / "context.json"
    foreign = b"concurrent writer\n"
    real_link = os.link

    def race_link(source: Path, final: Path, *, follow_symlinks: bool) -> None:
        final.write_bytes(foreign)
        real_link(source, final, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(context_module.os, "link", race_link)

    with pytest.raises(FileExistsError):
        write_operational_audit_context(report, context)

    assert report.read_bytes() == foreign
    assert list(tmp_path.glob(".context.json.*.tmp")) == []


def test_sources_are_byte_identical_and_paths_are_not_serialized(tmp_path: Path) -> None:
    private = tmp_path / "local-user" / "private"
    private.mkdir(parents=True)
    status = _status_bytes("1", operator_action_required=True)
    audit_path, status_paths = _write_sources(private, statuses=(status,))
    before = {path: path.read_bytes() for path in (audit_path, *status_paths)}

    content = operational_audit_context_bytes(
        build_operational_audit_context(audit_path, intent_status_paths=status_paths)
    )

    assert before == {path: path.read_bytes() for path in before}
    assert str(private).encode() not in content
    for forbidden in (b"local-user", b"timestamp", b"hostname", b"traceback"):
        assert forbidden not in content.lower()
