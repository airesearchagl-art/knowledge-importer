from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import knowledge_importer.cli as cli
import knowledge_importer.operational_audit as audit_module
from knowledge_importer.backup_cleanup_execution import (
    BackupCleanupAudit,
    BackupCleanupAuditAction,
    BackupCleanupAuditBefore,
    BackupCleanupAuditStatus,
)
from knowledge_importer.operational_audit import (
    OperationalAudit,
    OperationalAuditInputError,
    OperationalAuditSource,
    build_operational_audit,
    parse_operational_audit_bytes,
    verify_operational_audit_sources,
    write_operational_audit,
)
from knowledge_importer.repair_execution import (
    ExecutionActionResult,
    RepairExecutionReport,
)
from knowledge_importer.repair_plan import RepairAction, RepairActionCategory
from knowledge_importer.repair_preflight import PreflightTarget


def _json_bytes(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()


def _state(path: str, content: bytes | None) -> PreflightTarget:
    if content is None:
        return PreflightTarget(path, False, None, None)
    return PreflightTarget(path, True, len(content), hashlib.sha256(content).hexdigest())


def _repair_action(
    path: str,
    *,
    category: RepairActionCategory = RepairActionCategory.REGENERATE_SIDECAR,
    status: str = "succeeded",
    before: bytes | None = None,
    after: bytes | None = b"sidecar",
) -> ExecutionActionResult:
    rollback = {
        "succeeded": "available",
        "failed-precondition": "not-required",
        "failed": "not-required",
        "rolled-back": "completed",
        "rollback-failed": "failed",
        "not-run": "not-required",
    }[status]
    reason = (
        "missing-sidecar"
        if category is RepairActionCategory.REGENERATE_SIDECAR
        else "stale-sidecar"
    )
    return ExecutionActionResult(
        RepairAction(path, category, reason, True),
        status,
        _state(path, before),
        _state(path, after),
        rollback,
    )


def _write_repair(path: Path, actions: tuple[ExecutionActionResult, ...]) -> bytes:
    content = _json_bytes(
        RepairExecutionReport("a" * 64, "b" * 64, "c" * 64, actions, "passed").payload()
    )
    path.write_bytes(content)
    return content


def _cleanup_action(
    session: str,
    status: BackupCleanupAuditStatus,
    *,
    after_exists: bool,
) -> BackupCleanupAuditAction:
    return BackupCleanupAuditAction(
        session,
        status,
        BackupCleanupAuditBefore(1, 12, hashlib.sha256(session.encode()).hexdigest()),
        after_exists,
    )


def _write_cleanup(path: Path, actions: tuple[BackupCleanupAuditAction, ...]) -> bytes:
    content = _json_bytes(BackupCleanupAudit("d" * 64, "e" * 64, "f" * 64, actions).payload())
    path.write_bytes(content)
    return content


def test_aggregates_sources_in_canonical_order_and_binds_exact_bytes(tmp_path: Path) -> None:
    repair_path = tmp_path / "repair.json"
    cleanup_path = tmp_path / "cleanup.json"
    repair_bytes = _write_repair(
        repair_path,
        (
            _repair_action("documents/a.metadata.json"),
            _repair_action(
                "documents/b.metadata.json",
                category=RepairActionCategory.REMOVE_STALE_SIDECAR,
                before=b"stale",
                after=None,
            ),
        ),
    )
    cleanup_bytes = _write_cleanup(
        cleanup_path,
        (
            _cleanup_action(
                "knowledge-importer-repair-alpha",
                BackupCleanupAuditStatus.DELETED,
                after_exists=False,
            ),
        ),
    )

    audit = build_operational_audit(
        repair_execution_paths=(repair_path,),
        backup_cleanup_audit_paths=(cleanup_path,),
    )
    payload = audit.payload()

    assert [source["source_type"] for source in payload["sources"]] == [
        "backup-cleanup-audit",
        "repair-execution",
    ]
    assert [source["sha256"] for source in payload["sources"]] == [
        hashlib.sha256(cleanup_bytes).hexdigest(),
        hashlib.sha256(repair_bytes).hexdigest(),
    ]
    assert [operation["source_action_index"] for operation in payload["operations"]] == [0, 0, 1]
    assert [operation["action"] for operation in payload["operations"]] == [
        "backup-delete-session",
        "repair-regenerate-sidecar",
        "repair-remove-stale-sidecar",
    ]
    assert payload["summary"] == {
        "operations": 3,
        "succeeded": 3,
        "partial": 0,
        "failed": 0,
        "rolled_back": 0,
        "not_run": 0,
        "operator_action_required": 0,
        "package_change_observed": True,
    }


@pytest.mark.parametrize(
    ("status", "before", "after", "outcome", "reason", "package_change"),
    [
        ("rolled-back", b"old", b"old", "rolled_back", "rolled-back", "unchanged"),
        ("rollback-failed", b"old", b"new", "partial", "rollback-failed", "changed"),
        ("failed-precondition", b"old", b"old", "failed", "precondition-failed", "unchanged"),
        ("failed", b"old", b"new", "partial", "execution-failed", "changed"),
        ("not-run", b"old", b"old", "not_run", "not-run", "unchanged"),
    ],
)
def test_normalizes_repair_outcomes_without_inference(
    tmp_path: Path,
    status: str,
    before: bytes,
    after: bytes,
    outcome: str,
    reason: str,
    package_change: str,
) -> None:
    source = tmp_path / "repair.json"
    _write_repair(
        source,
        (_repair_action("item.metadata.json", status=status, before=before, after=after),),
    )

    operation = build_operational_audit(repair_execution_paths=(source,)).payload()["operations"][0]

    assert operation["outcome"] == outcome
    assert operation["reason"] == reason
    assert operation["package_change"] == package_change


def test_failed_repair_with_incomplete_digest_does_not_claim_partial_mutation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "repair.json"
    payload = RepairExecutionReport(
        "a" * 64,
        "b" * 64,
        "c" * 64,
        (_repair_action("item.metadata.json", status="failed", before=b"old", after=b"new"),),
        "failed",
    ).payload()
    action = payload["actions"][0]
    action["after"] = {"exists": True, "bytes": None, "sha256": None}
    source.write_bytes(_json_bytes(payload))

    operation = build_operational_audit(repair_execution_paths=(source,)).payload()["operations"][0]

    assert operation["outcome"] == "failed"
    assert operation["package_change"] == "unknown"
    assert operation["reason"] == "source-failure-mutation-unknown"


@pytest.mark.parametrize(
    ("status", "after_exists", "outcome", "reason"),
    [
        (BackupCleanupAuditStatus.DELETED, False, "succeeded", "completed"),
        (BackupCleanupAuditStatus.FAILED, True, "failed", "cleanup-failed"),
        (BackupCleanupAuditStatus.NOT_RUN, True, "not_run", "not-run"),
    ],
)
def test_cleanup_never_infers_package_change_or_partial(
    tmp_path: Path,
    status: BackupCleanupAuditStatus,
    after_exists: bool,
    outcome: str,
    reason: str,
) -> None:
    source = tmp_path / "cleanup.json"
    _write_cleanup(
        source,
        (_cleanup_action("knowledge-importer-repair-alpha", status, after_exists=after_exists),),
    )

    payload = build_operational_audit(backup_cleanup_audit_paths=(source,)).payload()
    operation = payload["operations"][0]

    assert operation["outcome"] == outcome
    assert operation["reason"] == reason
    assert operation["package_change"] == "unknown"
    assert payload["summary"]["package_change_observed"] is None


def test_rejects_no_sources_and_duplicate_exact_source_bytes(tmp_path: Path) -> None:
    with pytest.raises(OperationalAuditInputError):
        build_operational_audit()
    first = tmp_path / "first.json"
    duplicate = tmp_path / "duplicate.json"
    content = _write_repair(first, ())
    duplicate.write_bytes(content)

    with pytest.raises(OperationalAuditInputError):
        build_operational_audit(repair_execution_paths=(first, duplicate))


def test_multiple_sources_use_type_and_exact_digest_order(tmp_path: Path) -> None:
    repair_paths = (tmp_path / "repair-b.json", tmp_path / "repair-a.json")
    cleanup_paths = (tmp_path / "cleanup-b.json", tmp_path / "cleanup-a.json")
    _write_repair(repair_paths[0], (_repair_action("b.metadata.json"),))
    second_payload = RepairExecutionReport(
        "a" * 64,
        "b" * 64,
        "c" * 64,
        (_repair_action("a.metadata.json"),),
        "passed",
    ).payload()
    second_payload["source_note"] = "distinct exact bytes"
    repair_paths[1].write_bytes(_json_bytes(second_payload))
    _write_cleanup(
        cleanup_paths[0],
        (
            _cleanup_action(
                "knowledge-importer-repair-b", BackupCleanupAuditStatus.DELETED, after_exists=False
            ),
        ),
    )
    second_cleanup = BackupCleanupAudit("d" * 64, "e" * 64, "f" * 64, ()).payload()
    second_cleanup["source_note"] = "distinct exact bytes"
    cleanup_paths[1].write_bytes(_json_bytes(second_cleanup))

    payload = build_operational_audit(
        repair_execution_paths=repair_paths,
        backup_cleanup_audit_paths=cleanup_paths,
    ).payload()

    source_keys = [(source["source_type"], source["sha256"]) for source in payload["sources"]]
    assert source_keys == sorted(source_keys)
    operation_source_keys = [
        (operation["source_type"], operation["source_sha256"])
        for operation in payload["operations"]
    ]
    assert operation_source_keys == sorted(operation_source_keys)


def test_package_change_summary_is_false_only_for_proven_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "repair.json"
    _write_repair(
        source,
        (_repair_action("item.metadata.json", status="not-run", before=b"same", after=b"same"),),
    )

    summary = build_operational_audit(repair_execution_paths=(source,)).payload()["summary"]

    assert summary["package_change_observed"] is False


@pytest.mark.parametrize(
    "payload",
    [
        b"not JSON",
        _json_bytes({"report_type": "unknown", "schema_version": 1}),
        _json_bytes(
            {
                "report_type": "knowledge-package-repair-execution",
                "schema_version": 2,
            }
        ),
    ],
)
def test_rejects_invalid_json_source_type_and_future_schema(tmp_path: Path, payload: bytes) -> None:
    source = tmp_path / "source.json"
    source.write_bytes(payload)

    with pytest.raises(OperationalAuditInputError):
        build_operational_audit(repair_execution_paths=(source,))


def test_allows_unknown_v1_fields_without_copying_them(tmp_path: Path) -> None:
    source = tmp_path / "repair.json"
    payload = RepairExecutionReport(
        "a" * 64,
        "b" * 64,
        "c" * 64,
        (_repair_action("item.metadata.json"),),
        "passed",
    ).payload()
    payload["future_annotation"] = {"safe_unknown": True}
    payload["actions"][0]["future_action_field"] = "ignored"
    source.write_bytes(_json_bytes(payload))

    output = build_operational_audit(repair_execution_paths=(source,)).payload()

    assert "future_annotation" not in output
    assert "future_action_field" not in output["operations"][0]


def test_cleanup_parser_type_error_is_sanitized_as_invalid_source(tmp_path: Path) -> None:
    source = tmp_path / "cleanup.json"
    payload = BackupCleanupAudit(
        "d" * 64,
        "e" * 64,
        "f" * 64,
        (
            _cleanup_action(
                "knowledge-importer-repair-a", BackupCleanupAuditStatus.FAILED, after_exists=True
            ),
        ),
    ).payload()
    payload["actions"][0]["status"] = []
    source.write_bytes(_json_bytes(payload))

    with pytest.raises(OperationalAuditInputError):
        build_operational_audit(backup_cleanup_audit_paths=(source,))


def test_source_files_are_byte_identical_after_build(tmp_path: Path) -> None:
    repair_path = tmp_path / "repair.json"
    cleanup_path = tmp_path / "cleanup.json"
    before = {
        repair_path: _write_repair(repair_path, (_repair_action("a.metadata.json"),)),
        cleanup_path: _write_cleanup(cleanup_path, ()),
    }

    build_operational_audit(
        repair_execution_paths=(repair_path,),
        backup_cleanup_audit_paths=(cleanup_path,),
    )

    assert {path: path.read_bytes() for path in before} == before


def test_write_is_deterministic_create_only_and_has_trailing_newline(tmp_path: Path) -> None:
    source = tmp_path / "repair.json"
    _write_repair(source, (_repair_action("section/a.metadata.json"),))
    audit = build_operational_audit(repair_execution_paths=(source,))
    first = tmp_path / "first.json"
    second = tmp_path / "nested" / "second.json"

    write_operational_audit(first, audit)
    write_operational_audit(second, audit)

    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes().endswith(b"\n")
    with pytest.raises(OperationalAuditInputError):
        write_operational_audit(first, audit)


def test_concurrent_output_is_preserved_and_temporary_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "repair.json"
    _write_repair(source, ())
    audit = build_operational_audit(repair_execution_paths=(source,))
    report = tmp_path / "audit.json"
    foreign = b"concurrent writer\n"
    real_link = audit_module.os.link

    def race_link(source_path: Path, final_path: Path, *, follow_symlinks: bool) -> None:
        final_path.write_bytes(foreign)
        real_link(source_path, final_path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(audit_module.os, "link", race_link)

    with pytest.raises(FileExistsError):
        write_operational_audit(report, audit)

    assert report.read_bytes() == foreign
    assert not tuple(tmp_path.glob(".audit.json.*.tmp"))


def test_symlink_output_is_rejected_without_changing_target(tmp_path: Path) -> None:
    source = tmp_path / "repair.json"
    _write_repair(source, ())
    audit = build_operational_audit(repair_execution_paths=(source,))
    target = tmp_path / "foreign.json"
    target.write_bytes(b"foreign\n")
    report = tmp_path / "audit.json"
    try:
        report.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable in this environment")

    with pytest.raises(OperationalAuditInputError):
        write_operational_audit(report, audit)

    assert target.read_bytes() == b"foreign\n"


def test_existing_directory_output_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "repair.json"
    _write_repair(source, ())
    audit = build_operational_audit(repair_execution_paths=(source,))
    report = tmp_path / "audit.json"
    report.mkdir()

    with pytest.raises(OperationalAuditInputError):
        write_operational_audit(report, audit)


def test_report_contains_no_source_path_or_forbidden_context(tmp_path: Path) -> None:
    source = tmp_path / "private-user" / "repair.json"
    source.parent.mkdir()
    _write_repair(source, (_repair_action("section/a.metadata.json"),))

    encoded = _json_bytes(build_operational_audit(repair_execution_paths=(source,)).payload())

    assert str(tmp_path).encode() not in encoded
    assert b"private-user" not in encoded
    assert b"Traceback (most recent call last)" not in encoded
    assert b"timestamp" not in encoded
    assert b"hostname" not in encoded
    assert b"username" not in encoded
    text = encoded.decode()
    assert not any(__import__("unicodedata").category(character) == "Cf" for character in text)


def _write_audit(
    path: Path,
    *,
    repair_paths: tuple[Path, ...] = (),
    cleanup_paths: tuple[Path, ...] = (),
) -> bytes:
    write_operational_audit(
        path,
        build_operational_audit(
            repair_execution_paths=repair_paths,
            backup_cleanup_audit_paths=cleanup_paths,
        ),
    )
    return path.read_bytes()


def test_operational_audit_parser_accepts_canonical_report(tmp_path: Path) -> None:
    repair = tmp_path / "repair.json"
    cleanup = tmp_path / "cleanup.json"
    report = tmp_path / "audit.json"
    _write_repair(repair, (_repair_action("section/a.metadata.json"),))
    _write_cleanup(
        cleanup,
        (
            _cleanup_action(
                "knowledge-importer-repair-alpha",
                BackupCleanupAuditStatus.DELETED,
                after_exists=False,
            ),
        ),
    )
    _write_audit(report, repair_paths=(repair,), cleanup_paths=(cleanup,))

    parsed = parse_operational_audit_bytes(report.read_bytes())

    assert parsed.payload() == json.loads(report.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "source_kind",
    ["repair", "cleanup", "mixed"],
)
def test_verify_accepts_exact_single_and_mixed_sources(
    tmp_path: Path,
    source_kind: str,
) -> None:
    repair = tmp_path / "repair.json"
    cleanup = tmp_path / "cleanup.json"
    _write_repair(repair, (_repair_action("section/a.metadata.json"),))
    _write_cleanup(cleanup, ())
    repair_paths = (repair,) if source_kind in {"repair", "mixed"} else ()
    cleanup_paths = (cleanup,) if source_kind in {"cleanup", "mixed"} else ()
    report = tmp_path / "audit.json"
    _write_audit(report, repair_paths=repair_paths, cleanup_paths=cleanup_paths)
    before = {path: path.read_bytes() for path in (report, *repair_paths, *cleanup_paths)}

    result = verify_operational_audit_sources(
        report,
        repair_execution_paths=repair_paths,
        backup_cleanup_audit_paths=cleanup_paths,
    )

    assert result.exit_code == 0
    assert result.result == "verified"
    assert result.matched == len(repair_paths) + len(cleanup_paths)
    assert {path: path.read_bytes() for path in before} == before


def test_verify_is_independent_of_cli_source_order(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    _write_repair(first, (_repair_action("a.metadata.json"),))
    payload = RepairExecutionReport(
        "a" * 64,
        "b" * 64,
        "c" * 64,
        (_repair_action("b.metadata.json"),),
        "passed",
    ).payload()
    payload["distinct"] = True
    second.write_bytes(_json_bytes(payload))
    report = tmp_path / "audit.json"
    _write_audit(report, repair_paths=(first, second))

    result = verify_operational_audit_sources(
        report,
        repair_execution_paths=(second, first),
    )

    assert result.exit_code == 0
    assert result.matched == 2


@pytest.mark.parametrize("source_kind", ["repair", "cleanup"])
def test_one_byte_source_tamper_is_binding_mismatch(tmp_path: Path, source_kind: str) -> None:
    source = tmp_path / f"{source_kind}.json"
    if source_kind == "repair":
        _write_repair(source, ())
        repair_paths, cleanup_paths = (source,), ()
    else:
        _write_cleanup(source, ())
        repair_paths, cleanup_paths = (), (source,)
    report = tmp_path / "audit.json"
    _write_audit(report, repair_paths=repair_paths, cleanup_paths=cleanup_paths)
    source.write_bytes(source.read_bytes() + b" ")

    result = verify_operational_audit_sources(
        report,
        repair_execution_paths=repair_paths,
        backup_cleanup_audit_paths=cleanup_paths,
    )

    assert result.exit_code == 1
    assert result.missing == 1
    assert result.unexpected == 1
    assert result.invalid == 0


def test_verify_reports_missing_unexpected_and_duplicate_as_mismatch(tmp_path: Path) -> None:
    expected = tmp_path / "expected.json"
    extra = tmp_path / "extra.json"
    _write_repair(expected, ())
    extra_payload = RepairExecutionReport("a" * 64, "b" * 64, "c" * 64, (), "passed").payload()
    extra_payload["distinct"] = True
    extra.write_bytes(_json_bytes(extra_payload))
    report = tmp_path / "audit.json"
    _write_audit(report, repair_paths=(expected,))

    missing = verify_operational_audit_sources(report)
    unexpected = verify_operational_audit_sources(
        report,
        repair_execution_paths=(expected, extra),
    )
    duplicate = verify_operational_audit_sources(
        report,
        repair_execution_paths=(expected, expected),
    )

    assert (missing.exit_code, missing.missing, missing.unexpected) == (1, 1, 0)
    assert (unexpected.exit_code, unexpected.missing, unexpected.unexpected) == (1, 0, 1)
    assert (duplicate.exit_code, duplicate.missing, duplicate.unexpected) == (1, 0, 1)


@pytest.mark.parametrize(
    "invalid_bytes",
    [
        b"not json",
        _json_bytes({"report_type": "invalid", "schema_version": 1}),
        _json_bytes(
            {
                "report_type": "knowledge-package-repair-execution",
                "schema_version": 2,
            }
        ),
    ],
)
def test_exact_bound_invalid_source_is_schema_error(
    tmp_path: Path,
    invalid_bytes: bytes,
) -> None:
    source = tmp_path / "source.json"
    source.write_bytes(invalid_bytes)
    descriptor = OperationalAuditSource(
        "repair-execution",
        1,
        hashlib.sha256(invalid_bytes).hexdigest(),
    )
    report = tmp_path / "audit.json"
    write_operational_audit(report, OperationalAudit((descriptor,), ()))

    result = verify_operational_audit_sources(report, repair_execution_paths=(source,))

    assert result.exit_code == 2
    assert result.invalid == 1
    assert result.missing == result.unexpected == 0


@pytest.mark.parametrize(
    "audit_bytes",
    [
        b"not json",
        _json_bytes({"report_type": "invalid", "schema_version": 1}),
        _json_bytes(
            {
                "report_type": "knowledge-importer-operational-audit",
                "schema_version": 2,
            }
        ),
    ],
)
def test_invalid_operational_audit_is_input_error(tmp_path: Path, audit_bytes: bytes) -> None:
    report = tmp_path / "audit.json"
    report.write_bytes(audit_bytes)

    with pytest.raises(OperationalAuditInputError):
        verify_operational_audit_sources(report)


def _valid_audit_payload(tmp_path: Path) -> dict[str, object]:
    source = tmp_path / "repair.json"
    _write_repair(source, (_repair_action("a.metadata.json"),))
    return build_operational_audit(repair_execution_paths=(source,)).payload()


def test_operational_audit_parser_rejects_source_duplicate(tmp_path: Path) -> None:
    payload = _valid_audit_payload(tmp_path)
    payload["sources"].append(dict(payload["sources"][0]))

    with pytest.raises(ValueError):
        parse_operational_audit_bytes(_json_bytes(payload))


def test_operational_audit_parser_rejects_noncanonical_source_order(tmp_path: Path) -> None:
    repair = tmp_path / "repair.json"
    cleanup = tmp_path / "cleanup.json"
    _write_repair(repair, ())
    _write_cleanup(cleanup, ())
    payload = build_operational_audit(
        repair_execution_paths=(repair,),
        backup_cleanup_audit_paths=(cleanup,),
    ).payload()
    payload["sources"].reverse()

    with pytest.raises(ValueError):
        parse_operational_audit_bytes(_json_bytes(payload))


def test_operational_audit_parser_rejects_unbound_operation_source(tmp_path: Path) -> None:
    payload = _valid_audit_payload(tmp_path)
    payload["operations"][0]["source_sha256"] = "f" * 64

    with pytest.raises(ValueError):
        parse_operational_audit_bytes(_json_bytes(payload))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("outcome", "failed"),
        ("reason", "execution-failed"),
        ("package_change", "unknown"),
        ("operator_action_required", True),
    ],
)
def test_operational_audit_parser_rejects_operation_semantic_mismatch(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    payload = _valid_audit_payload(tmp_path)
    payload["operations"][0][field] = value

    with pytest.raises(ValueError):
        parse_operational_audit_bytes(_json_bytes(payload))


def test_operational_audit_parser_rejects_package_change_summary_mismatch(
    tmp_path: Path,
) -> None:
    payload = _valid_audit_payload(tmp_path)
    payload["summary"]["package_change_observed"] = False

    with pytest.raises(ValueError):
        parse_operational_audit_bytes(_json_bytes(payload))


def test_operational_audit_parser_rejects_count_summary_mismatch(tmp_path: Path) -> None:
    payload = _valid_audit_payload(tmp_path)
    payload["summary"]["succeeded"] = 0

    with pytest.raises(ValueError):
        parse_operational_audit_bytes(_json_bytes(payload))


def test_operational_audit_parser_rejects_source_action_index_mismatch(
    tmp_path: Path,
) -> None:
    payload = _valid_audit_payload(tmp_path)
    payload["operations"][0]["source_action_index"] = 1

    with pytest.raises(ValueError):
        parse_operational_audit_bytes(_json_bytes(payload))


def test_verify_rejects_self_consistent_summary_operation_tamper(tmp_path: Path) -> None:
    source = tmp_path / "repair.json"
    _write_repair(source, (_repair_action("a.metadata.json"),))
    payload = build_operational_audit(repair_execution_paths=(source,)).payload()
    payload["operations"][0]["target"] = "different.metadata.json"
    report = tmp_path / "audit.json"
    report.write_bytes(_json_bytes(payload))

    result = verify_operational_audit_sources(report, repair_execution_paths=(source,))

    assert result.exit_code == 2
    assert result.matched == 1
    assert result.invalid == 1


def test_verify_rejects_self_consistent_missing_summary_operation(tmp_path: Path) -> None:
    source = tmp_path / "repair.json"
    _write_repair(source, (_repair_action("a.metadata.json"),))
    payload = build_operational_audit(repair_execution_paths=(source,)).payload()
    payload["operations"] = []
    payload["summary"] = {
        "operations": 0,
        "succeeded": 0,
        "partial": 0,
        "failed": 0,
        "rolled_back": 0,
        "not_run": 0,
        "operator_action_required": 0,
        "package_change_observed": None,
    }
    report = tmp_path / "audit.json"
    report.write_bytes(_json_bytes(payload))

    result = verify_operational_audit_sources(report, repair_execution_paths=(source,))

    assert result.exit_code == 2
    assert result.matched == 1
    assert result.invalid == 1


def test_operational_audit_parser_allows_unknown_v1_fields(tmp_path: Path) -> None:
    payload = _valid_audit_payload(tmp_path)
    payload["future"] = {"ignored": True}
    payload["sources"][0]["future"] = "ignored"
    payload["operations"][0]["future"] = "ignored"

    parsed = parse_operational_audit_bytes(_json_bytes(payload))

    assert "future" not in parsed.payload()
    assert "future" not in parsed.payload()["sources"][0]
    assert "future" not in parsed.payload()["operations"][0]


def test_audit_verify_cli_stdout_is_deterministic_and_path_free(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "private-user" / "repair.json"
    source.parent.mkdir()
    _write_repair(source, ())
    report = tmp_path / "audit.json"
    _write_audit(report, repair_paths=(source,))
    arguments = [
        "audit-verify",
        str(report),
        "--repair-execution",
        str(source),
    ]

    assert cli.run(arguments) == 0
    first = capsys.readouterr()
    assert cli.run(arguments) == 0
    second = capsys.readouterr()

    assert first.out == second.out
    assert "result=verified" in first.out
    assert "source_binding=verified" in first.out
    assert "internal_lifecycle_binding=not-provided" in first.out
    assert str(tmp_path) not in first.out + first.err
    assert "private-user" not in first.out + first.err
    assert "Traceback" not in first.out + first.err
