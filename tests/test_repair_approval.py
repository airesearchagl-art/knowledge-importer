from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import knowledge_importer.cli as cli
from knowledge_importer.repair_approval import (
    build_repair_approval,
    parse_repair_approval_bytes,
    write_repair_approval,
)
from knowledge_importer.repair_plan import (
    RepairAction,
    RepairActionCategory,
    RepairPlan,
    parse_repair_plan_bytes,
    write_repair_plan,
)


def _action(
    path: str,
    action: RepairActionCategory,
    reason: str,
    *,
    safe: bool,
) -> RepairAction:
    return RepairAction(path, action, reason, safe)


def _write_plan(path: Path, actions: tuple[RepairAction, ...]) -> bytes:
    write_repair_plan(path, RepairPlan(issues=len(actions), actions=actions))
    return path.read_bytes()


def _mixed_actions() -> tuple[RepairAction, ...]:
    return (
        _action(
            "日本語/a.metadata.json",
            RepairActionCategory.REGENERATE_SIDECAR,
            "missing-sidecar",
            safe=True,
        ),
        _action(
            "section/b.metadata.json",
            RepairActionCategory.REMOVE_STALE_SIDECAR,
            "stale-sidecar",
            safe=True,
        ),
        _action(
            "section/c.metadata.json",
            RepairActionCategory.REGENERATE_MANIFEST,
            "extra-artifact",
            safe=False,
        ),
        _action(
            "section/d.metadata.json",
            RepairActionCategory.VERIFY_ARTIFACT,
            "artifact-digest-mismatch",
            safe=False,
        ),
        _action(
            "section/e.metadata.json",
            RepairActionCategory.MANUAL_REVIEW,
            "path-mismatch",
            safe=False,
        ),
    )


def test_all_safe_approval_binds_exact_plan_bytes(tmp_path: Path) -> None:
    plan_path = tmp_path / "repair-plan.json"
    plan_bytes = _write_plan(plan_path, _mixed_actions())

    approval = build_repair_approval(plan_path)

    assert approval.plan_sha256 == hashlib.sha256(plan_bytes).hexdigest()
    assert approval.approved_actions == _mixed_actions()[:2]
    assert approval.payload() == {
        "report_type": "knowledge-package-repair-approval",
        "schema_version": 1,
        "plan": {"sha256": approval.plan_sha256, "schema_version": 1},
        "scope": {"mode": "all-safe"},
        "approved_actions": [action.payload() for action in _mixed_actions()[:2]],
    }


@pytest.mark.parametrize(
    "tampered_action",
    [
        _action(
            "section/a.metadata.json",
            RepairActionCategory.REGENERATE_MANIFEST,
            "extra-artifact",
            safe=True,
        ),
        _action(
            "section/a.metadata.json",
            RepairActionCategory.VERIFY_ARTIFACT,
            "artifact-digest-mismatch",
            safe=True,
        ),
        _action(
            "section/a.metadata.json",
            RepairActionCategory.MANUAL_REVIEW,
            "artifact-digest-mismatch",
            safe=True,
        ),
        _action(
            "section/a.metadata.json",
            RepairActionCategory.REGENERATE_SIDECAR,
            "stale-sidecar",
            safe=True,
        ),
        _action(
            "section/a.metadata.json",
            RepairActionCategory.REMOVE_STALE_SIDECAR,
            "missing-sidecar",
            safe=True,
        ),
    ],
)
def test_tampered_safe_semantics_are_rejected_without_approval(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    tampered_action: RepairAction,
) -> None:
    plan = tmp_path / "tampered-plan.json"
    plan_bytes = _write_plan(plan, (tampered_action,))
    report = tmp_path / "approval.json"

    with pytest.raises(ValueError, match="action semantics"):
        parse_repair_plan_bytes(plan_bytes)
    assert (
        cli.run(
            [
                "approve-repair",
                str(plan),
                "--all-safe",
                "--report-json",
                str(report),
            ]
        )
        == 2
    )

    captured = capsys.readouterr()
    assert not report.exists()
    assert captured.err == "Repair Planを検証できませんでした。\n"
    assert str(tmp_path) not in captured.err
    assert "Traceback" not in captured.err


def test_plan_one_byte_change_changes_digest(tmp_path: Path) -> None:
    plan_path = tmp_path / "repair-plan.json"
    _write_plan(plan_path, (_mixed_actions()[0],))
    first = build_repair_approval(plan_path).plan_sha256

    with plan_path.open("ab") as plan_file:
        plan_file.write(b" ")
    second = build_repair_approval(plan_path).plan_sha256

    assert first != second
    assert second == hashlib.sha256(plan_path.read_bytes()).hexdigest()


def test_zero_safe_actions_produces_valid_empty_approval(tmp_path: Path) -> None:
    plan_path = tmp_path / "repair-plan.json"
    _write_plan(
        plan_path,
        (
            _action(
                "section/a.metadata.json",
                RepairActionCategory.MANUAL_REVIEW,
                "artifact-digest-mismatch",
                safe=False,
            ),
        ),
    )

    approval = build_repair_approval(plan_path)

    assert approval.approved_actions == ()
    assert (
        parse_repair_approval_bytes(json.dumps(approval.payload(), ensure_ascii=False).encode())
        == approval
    )


def test_approval_json_is_deterministic_and_has_no_identity_fields(tmp_path: Path) -> None:
    plan_path = tmp_path / "repair-plan.json"
    _write_plan(plan_path, (_mixed_actions()[0],))
    approval = build_repair_approval(plan_path)
    report = tmp_path / "approval.json"

    write_repair_approval(report, approval)
    first = report.read_bytes()
    write_repair_approval(report, approval)

    assert report.read_bytes() == first
    assert first.endswith(b"\n")
    text = first.decode()
    for forbidden in (
        "timestamp",
        "approver",
        "username",
        "email",
        "hostname",
        "cwd",
        "command_line",
        "absolute_path",
        "random_id",
    ):
        assert forbidden not in text


@pytest.mark.parametrize(
    "content",
    [
        b"{broken",
        b'{"report_type":"knowledge-package-repair-plan","schema_version":2}',
        b'{"report_type":"wrong","schema_version":1,"summary":{},"actions":[]}',
        (
            b'{"report_type":"knowledge-package-repair-plan","schema_version":1,'
            b'"summary":{"issues":1,"actions":1,"manual_review":0},'
            b'"actions":[{"path":"../outside","action":"regenerate-sidecar",'
            b'"reason_category":"missing-sidecar","safe":true}]}'
        ),
    ],
)
def test_invalid_plan_is_rejected_without_approval(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    content: bytes,
) -> None:
    plan = tmp_path / "invalid-plan.json"
    plan.write_bytes(content)
    report = tmp_path / "approval.json"

    assert (
        cli.run(
            [
                "approve-repair",
                str(plan),
                "--all-safe",
                "--report-json",
                str(report),
            ]
        )
        == 2
    )

    captured = capsys.readouterr()
    assert not report.exists()
    assert captured.err == "Repair Planを検証できませんでした。\n"
    assert str(tmp_path) not in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("repair-plan.json", None),
        (
            "manifest.json",
            b'{"report_type":"knowledge-artifact-manifest","schema_version":1}',
        ),
        (
            "document.metadata.json",
            b'{"report_type":"knowledge-document-metadata","schema_version":1}',
        ),
        ("document.md", "# 既存Markdown\n".encode()),
        ("batch.csv", b"input,output,status,error_category,message\n"),
        (
            "quality.json",
            b'{"report_type":"markdown-quality","schema_version":1}',
        ),
        (
            "batch.json",
            b'{"report_type":"knowledge-importer-batch","schema_version":1}',
        ),
        (
            "invalid-approval.json",
            b'{"report_type":"knowledge-package-repair-approval","schema_version":1}',
        ),
    ],
)
def test_existing_nonapproval_output_is_rejected_before_plan_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    content: bytes | None,
) -> None:
    plan = tmp_path / "input-plan.json"
    _write_plan(plan, (_mixed_actions()[0],))
    output = tmp_path / name
    if content is None:
        _write_plan(output, (_mixed_actions()[0],))
    else:
        output.write_bytes(content)
    before = output.read_bytes()

    def unexpected_build(path: Path) -> object:
        raise AssertionError("Plan must not be read when output conflicts")

    monkeypatch.setattr(cli, "build_repair_approval", unexpected_build)

    assert (
        cli.run(
            [
                "approve-repair",
                str(plan),
                "--all-safe",
                "--report-json",
                str(output),
            ]
        )
        == 2
    )

    captured = capsys.readouterr()
    assert output.read_bytes() == before
    assert captured.err == "エラー: 既存のRepair Approval以外は上書きできません\n"
    assert str(tmp_path) not in captured.err
    assert "Traceback" not in captured.err


def test_existing_approval_can_be_atomically_updated(tmp_path: Path) -> None:
    plan = tmp_path / "repair-plan.json"
    _write_plan(plan, (_mixed_actions()[0],))
    report = tmp_path / "approval.json"
    write_repair_approval(report, build_repair_approval(plan))
    first = report.read_bytes()

    with plan.open("ab") as plan_file:
        plan_file.write(b" ")
    assert (
        cli.run(
            [
                "approve-repair",
                str(plan),
                "--all-safe",
                "--report-json",
                str(report),
            ]
        )
        == 0
    )

    assert report.read_bytes() != first
    assert (
        json.loads(report.read_text(encoding="utf-8"))["plan"]["sha256"]
        == hashlib.sha256(plan.read_bytes()).hexdigest()
    )


def test_approval_generation_does_not_modify_plan_or_package(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    package_files = {
        package / "document.md": b"# synthetic\n",
        package / "document.metadata.json": b"{}\n",
        package / "manifest.json": b"{}\n",
        package / "source.pdf": b"%PDF-1.4\n",
        package / "batch.json": b"{}\n",
        package / "quality.json": b"{}\n",
        package / "batch.csv": b"input,output\n",
    }
    for path, content in package_files.items():
        path.write_bytes(content)
    plan = tmp_path / "repair-plan.json"
    _write_plan(plan, (_mixed_actions()[0],))
    before = {path: path.read_bytes() for path in (*package_files, plan)}

    assert (
        cli.run(
            [
                "approve-repair",
                str(plan),
                "--all-safe",
                "--report-json",
                str(tmp_path / "approval.json"),
            ]
        )
        == 0
    )

    assert {path: path.read_bytes() for path in before} == before


def test_directory_and_symlink_outputs_are_rejected(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = tmp_path / "repair-plan.json"
    _write_plan(plan, (_mixed_actions()[0],))
    directory = tmp_path / "report-dir"
    directory.mkdir()

    assert (
        cli.run(
            [
                "approve-repair",
                str(plan),
                "--all-safe",
                "--report-json",
                str(directory),
            ]
        )
        == 2
    )
    capsys.readouterr()

    target = tmp_path / "target.json"
    write_repair_approval(target, build_repair_approval(plan))
    link = tmp_path / "approval-link.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is not permitted")

    assert (
        cli.run(
            [
                "approve-repair",
                str(plan),
                "--all-safe",
                "--report-json",
                str(link),
            ]
        )
        == 2
    )
    assert target.is_file()


def test_report_write_failure_preserves_existing_and_hides_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = tmp_path / "repair-plan.json"
    _write_plan(plan, (_mixed_actions()[0],))
    report = tmp_path / "approval.json"
    write_repair_approval(report, build_repair_approval(plan))
    before = report.read_bytes()

    def failing_writer(path: Path, approval: object) -> None:
        raise OSError(f"cannot write {tmp_path} Traceback")

    monkeypatch.setattr(cli, "write_repair_approval", failing_writer)

    assert (
        cli.run(
            [
                "approve-repair",
                str(plan),
                "--all-safe",
                "--report-json",
                str(report),
            ]
        )
        == 2
    )

    captured = capsys.readouterr()
    assert report.read_bytes() == before
    assert captured.err == "Repair Approvalを書き込めませんでした。\n"
    assert str(tmp_path) not in captured.err
    assert "Traceback" not in captured.err
