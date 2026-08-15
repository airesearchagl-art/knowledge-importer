from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import Path

import pytest

import knowledge_importer.operation_intent as intent_module
from knowledge_importer.operation_intent import (
    APPROVED_FOR_EXECUTION,
    BACKUP_CLEANUP,
    REPAIR_EXECUTION,
    OperationIntentAction,
    OperationIntentBinding,
    OperationIntentOutputError,
    OperationIntentReceipt,
    operation_intent_bytes,
    operation_intent_sha256,
    parse_operation_intent_bytes,
    write_operation_intent,
)


def _repair_receipt(
    *,
    attempt_id: str = "repair-attempt-001",
    actions: tuple[OperationIntentAction, ...] | None = None,
) -> OperationIntentReceipt:
    if actions is None:
        actions = (
            OperationIntentAction(
                0,
                "regenerate-sidecar",
                "section/a.metadata.json",
                "missing-sidecar",
            ),
            OperationIntentAction(
                1,
                "remove-stale-sidecar",
                "section/b.metadata.json",
                "stale-sidecar",
            ),
        )
    return OperationIntentReceipt(
        attempt_id,
        REPAIR_EXECUTION,
        (
            OperationIntentBinding("artifact-manifest", 1, "1" * 64),
            OperationIntentBinding("repair-plan", 1, "2" * 64),
            OperationIntentBinding("repair-approval", 1, "3" * 64),
            OperationIntentBinding("repair-preflight", 1, "4" * 64),
        ),
        actions,
    )


def _cleanup_receipt() -> OperationIntentReceipt:
    return OperationIntentReceipt(
        "cleanup-attempt-001",
        BACKUP_CLEANUP,
        (
            OperationIntentBinding("backup-inventory", 1, "5" * 64),
            OperationIntentBinding("backup-cleanup-plan", 1, "6" * 64),
            OperationIntentBinding("backup-cleanup-approval", 1, "7" * 64),
        ),
        (
            OperationIntentAction(
                0,
                "delete-backup-session",
                "knowledge-importer-repair-alpha",
                "explicit-retention-release",
            ),
        ),
    )


def _payload(receipt: OperationIntentReceipt) -> dict[str, object]:
    return json.loads(operation_intent_bytes(receipt).decode("utf-8"))


def _json_bytes(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


@pytest.mark.parametrize("receipt_factory", [_repair_receipt, _cleanup_receipt])
def test_valid_repair_and_cleanup_receipts(receipt_factory: object) -> None:
    receipt = receipt_factory()  # type: ignore[operator]
    content = operation_intent_bytes(receipt)

    assert parse_operation_intent_bytes(content) == receipt
    assert content.endswith(b"\n")
    assert operation_intent_sha256(content) == hashlib.sha256(content).hexdigest()


@pytest.mark.parametrize(
    "attempt_id",
    ["a", "A-0._z", "attempt-2026-08-16.001", "x" * 64],
)
def test_attempt_id_accepts_fixed_safe_ascii_format(attempt_id: str) -> None:
    receipt = _repair_receipt(attempt_id=attempt_id)

    assert parse_operation_intent_bytes(operation_intent_bytes(receipt)).attempt_id == attempt_id


@pytest.mark.parametrize(
    "attempt_id",
    ["", ".hidden", "-start", "a/b", "a\\b", "two words", "日本語", "a\u2066b", "x" * 65],
)
def test_attempt_id_rejects_unsafe_or_non_ascii_values(attempt_id: str) -> None:
    receipt = _repair_receipt(attempt_id=attempt_id)

    with pytest.raises(ValueError):
        operation_intent_bytes(receipt)


def test_attempt_id_is_not_receipt_security_identity() -> None:
    first = _repair_receipt(actions=())
    second = _repair_receipt(
        actions=(
            OperationIntentAction(
                0,
                "regenerate-sidecar",
                "section/a.metadata.json",
                "missing-sidecar",
            ),
        )
    )
    first_bytes = operation_intent_bytes(first)
    second_bytes = operation_intent_bytes(second)

    assert first.attempt_id == second.attempt_id
    assert operation_intent_sha256(first_bytes) != operation_intent_sha256(second_bytes)


def test_binding_order_and_exact_set_are_required() -> None:
    payload = _payload(_repair_receipt())
    payload["bindings"][0], payload["bindings"][1] = (
        payload["bindings"][1],
        payload["bindings"][0],
    )

    with pytest.raises(ValueError):
        parse_operation_intent_bytes(_json_bytes(payload))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("report_type", "other-report"),
        ("schema_version", True),
        ("attempt_id", 1),
        ("operation_type", "unknown-operation"),
        ("bindings", {}),
        ("actions", {}),
    ],
)
def test_required_fields_have_exact_types_and_known_values(field: str, value: object) -> None:
    payload = _payload(_repair_receipt())
    payload[field] = value

    with pytest.raises(ValueError):
        parse_operation_intent_bytes(_json_bytes(payload))


def test_missing_required_field_is_rejected() -> None:
    payload = _payload(_repair_receipt())
    del payload["attempt_id"]

    with pytest.raises(ValueError):
        parse_operation_intent_bytes(_json_bytes(payload))


def test_unknown_binding_type_and_future_binding_schema_are_rejected() -> None:
    unknown = _payload(_repair_receipt())
    unknown["bindings"][0]["artifact_type"] = "unknown-artifact"
    future = _payload(_repair_receipt())
    future["bindings"][0]["schema_version"] = 2

    with pytest.raises(ValueError):
        parse_operation_intent_bytes(_json_bytes(unknown))
    with pytest.raises(ValueError):
        parse_operation_intent_bytes(_json_bytes(future))


def test_duplicate_binding_is_rejected() -> None:
    payload = _payload(_repair_receipt())
    payload["bindings"][1] = dict(payload["bindings"][0])

    with pytest.raises(ValueError):
        parse_operation_intent_bytes(_json_bytes(payload))


def test_action_order_is_canonical() -> None:
    payload = _payload(_repair_receipt())
    payload["actions"].reverse()
    for index, action in enumerate(payload["actions"]):
        action["action_index"] = index

    with pytest.raises(ValueError):
        parse_operation_intent_bytes(_json_bytes(payload))


def test_duplicate_action_target_is_rejected_case_insensitively() -> None:
    payload = _payload(_repair_receipt(actions=(_repair_receipt().actions[0],)))
    duplicate = dict(payload["actions"][0])
    duplicate["action_index"] = 1
    duplicate["target"] = "SECTION/A.METADATA.JSON"
    payload["actions"].append(duplicate)

    with pytest.raises(ValueError):
        parse_operation_intent_bytes(_json_bytes(payload))


def test_non_contiguous_action_index_is_rejected() -> None:
    payload = _payload(_repair_receipt(actions=(_repair_receipt().actions[0],)))
    payload["actions"][0]["action_index"] = 1

    with pytest.raises(ValueError):
        parse_operation_intent_bytes(_json_bytes(payload))


@pytest.mark.parametrize(
    "target",
    [
        "/absolute.metadata.json",
        "C:/private.metadata.json",
        "../up.metadata.json",
        "a\\b.metadata.json",
        "a/./b.metadata.json",
        "a\u202eb.metadata.json",
    ],
)
def test_unsafe_action_target_is_rejected(target: str) -> None:
    action = OperationIntentAction(0, "regenerate-sidecar", target, "missing-sidecar")

    with pytest.raises(ValueError):
        operation_intent_bytes(_repair_receipt(actions=(action,)))


def test_invalid_action_semantics_are_rejected() -> None:
    payload = _payload(_cleanup_receipt())
    payload["actions"][0]["reason_category"] = "missing-sidecar"

    with pytest.raises(ValueError):
        parse_operation_intent_bytes(_json_bytes(payload))


def test_action_intent_is_fixed() -> None:
    payload = _payload(_cleanup_receipt())
    payload["actions"][0]["intent"] = "executed"

    with pytest.raises(ValueError):
        parse_operation_intent_bytes(_json_bytes(payload))


def test_invalid_sha256_is_rejected() -> None:
    payload = _payload(_repair_receipt())
    payload["bindings"][0]["sha256"] = "A" * 64

    with pytest.raises(ValueError):
        parse_operation_intent_bytes(_json_bytes(payload))


def test_future_schema_is_rejected() -> None:
    payload = _payload(_repair_receipt())
    payload["schema_version"] = 2

    with pytest.raises(ValueError):
        parse_operation_intent_bytes(_json_bytes(payload))


def test_unknown_v1_fields_are_allowed_without_copying_them() -> None:
    payload = _payload(_repair_receipt())
    payload["future"] = {"ignored": True}
    payload["bindings"][0]["future"] = "ignored"
    payload["actions"][0]["future"] = "ignored"

    parsed = parse_operation_intent_bytes(_json_bytes(payload)).payload()

    assert "future" not in parsed
    assert "future" not in parsed["bindings"][0]
    assert "future" not in parsed["actions"][0]


def test_writer_is_deterministic_create_only_and_immutable(tmp_path: Path) -> None:
    receipt = _repair_receipt()
    first = tmp_path / "first.json"
    second = tmp_path / "nested" / "second.json"

    write_operation_intent(first, receipt)
    write_operation_intent(second, receipt)

    assert first.read_bytes() == second.read_bytes() == operation_intent_bytes(receipt)
    with pytest.raises(OperationIntentOutputError):
        write_operation_intent(first, receipt)


def test_foreign_file_and_directory_are_rejected_without_change(tmp_path: Path) -> None:
    receipt = _repair_receipt()
    foreign = tmp_path / "foreign.json"
    directory = tmp_path / "directory.json"
    foreign.write_bytes(b"foreign\n")
    directory.mkdir()

    with pytest.raises(OperationIntentOutputError):
        write_operation_intent(foreign, receipt)
    with pytest.raises(OperationIntentOutputError):
        write_operation_intent(directory, receipt)

    assert foreign.read_bytes() == b"foreign\n"
    assert directory.is_dir()


def test_symlink_output_is_rejected_without_changing_target(tmp_path: Path) -> None:
    target = tmp_path / "foreign.json"
    target.write_bytes(b"foreign\n")
    output = tmp_path / "intent.json"
    try:
        output.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable in this environment")

    with pytest.raises(OperationIntentOutputError):
        write_operation_intent(output, _repair_receipt())

    assert target.read_bytes() == b"foreign\n"


def test_linked_parent_is_rejected_without_writing_outside(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable in this environment")

    with pytest.raises(OperationIntentOutputError):
        write_operation_intent(linked / "intent.json", _repair_receipt())

    assert not (outside / "intent.json").exists()


def test_reparse_output_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "intent.json"
    original = intent_module.path_uses_link_or_reparse
    monkeypatch.setattr(
        intent_module,
        "path_uses_link_or_reparse",
        lambda path: path == output or original(path),
    )

    with pytest.raises(OperationIntentOutputError):
        write_operation_intent(output, _repair_receipt())

    assert not output.exists()


def test_concurrent_writer_is_not_overwritten_and_temp_is_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "intent.json"
    foreign = b"concurrent foreign bytes\n"
    real_link = intent_module.os.link

    def race_link(source: Path, final: Path, *, follow_symlinks: bool) -> None:
        final.write_bytes(foreign)
        real_link(source, final, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(intent_module.os, "link", race_link)

    with pytest.raises(FileExistsError):
        write_operation_intent(output, _repair_receipt())

    assert output.read_bytes() == foreign
    assert not tuple(tmp_path.glob(".intent.json.*.tmp"))


def test_receipt_contains_no_machine_context_or_format_controls(tmp_path: Path) -> None:
    output = tmp_path / "private-user" / "intent.json"
    write_operation_intent(output, _repair_receipt())
    content = output.read_bytes()
    text = content.decode("utf-8")

    assert str(tmp_path).encode() not in content
    assert b"private-user" not in content
    for forbidden in (b"Traceback", b"timestamp", b"hostname", b"username", b"cwd", b"command"):
        assert forbidden not in content
    assert not any(unicodedata.category(character) == "Cf" for character in text)
    assert APPROVED_FOR_EXECUTION in text


def test_writing_receipt_does_not_mutate_unrelated_package(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    markdown = package / "document.md"
    markdown.write_bytes(b"# unchanged\n")
    before = markdown.read_bytes()

    write_operation_intent(tmp_path / "reports" / "intent.json", _cleanup_receipt())

    assert markdown.read_bytes() == before
