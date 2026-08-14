import hashlib
import json
from pathlib import Path

import pytest

import knowledge_importer.cli as cli
from knowledge_importer.backup_cleanup_approval import (
    build_backup_cleanup_approval,
    parse_backup_cleanup_approval_bytes,
)
from knowledge_importer.backup_cleanup_plan import (
    BackupCleanupAction,
    BackupCleanupPlan,
    write_backup_cleanup_plan,
)

_SHA_A = "a" * 64
_SHA_B = "b" * 64


@pytest.fixture(autouse=True)
def _ignore_workspace_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "repository_roots", lambda backup_root: ())


def _action(session: str, *, eligible: bool) -> BackupCleanupAction:
    return BackupCleanupAction(
        session,
        _SHA_A if eligible else None,
        _SHA_B if eligible else None,
        1 if eligible else 0,
        23 if eligible else 0,
        eligible,
    )


def _write_plan(path: Path, actions: tuple[BackupCleanupAction, ...]) -> None:
    write_backup_cleanup_plan(path, BackupCleanupPlan("c" * 64, actions))


def _approval_args(backup_root: Path, plan: Path, report: Path) -> list[str]:
    return [
        "approve-backup-cleanup",
        str(plan),
        "--backup-root",
        str(backup_root),
        "--all-planned",
        "--report-json",
        str(report),
    ]


def test_approval_binds_exact_plan_bytes_and_excludes_blocked_actions(
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    plan = tmp_path / "plan.json"
    report = tmp_path / "approval.json"
    planned = _action("knowledge-importer-repair-v1-alpha", eligible=True)
    blocked = _action("unknown-session", eligible=False)
    _write_plan(plan, (planned, blocked))
    plan_bytes = plan.read_bytes()

    assert cli.run(_approval_args(backup_root, plan, report)) == 0

    approval = parse_backup_cleanup_approval_bytes(report.read_bytes())
    assert approval.plan_sha256 == hashlib.sha256(plan_bytes).hexdigest()
    assert approval.approved_actions == (planned,)


def test_approval_is_deterministic_and_can_replace_itself(tmp_path: Path) -> None:
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    plan = tmp_path / "plan.json"
    report = tmp_path / "approval.json"
    _write_plan(plan, (_action("knowledge-importer-repair-v1-alpha", eligible=True),))
    args = _approval_args(backup_root, plan, report)

    assert cli.run(args) == 0
    first = report.read_bytes()
    assert cli.run(args) == 0
    assert report.read_bytes() == first


def test_zero_planned_actions_produces_valid_empty_approval(tmp_path: Path) -> None:
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    plan = tmp_path / "plan.json"
    report = tmp_path / "approval.json"
    _write_plan(plan, (_action("unknown-session", eligible=False),))

    assert cli.run(_approval_args(backup_root, plan, report)) == 0
    assert parse_backup_cleanup_approval_bytes(report.read_bytes()).approved_actions == ()


def test_tampered_plan_semantics_are_rejected_without_approval(tmp_path: Path) -> None:
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    plan = tmp_path / "plan.json"
    report = tmp_path / "approval.json"
    _write_plan(plan, (_action("unknown-session", eligible=False),))
    payload = json.loads(plan.read_text(encoding="utf-8"))
    payload["actions"][0]["eligible"] = True
    plan.write_text(json.dumps(payload), encoding="utf-8")

    assert cli.run(_approval_args(backup_root, plan, report)) == 2
    assert not report.exists()


def test_plan_exact_bytes_change_approval_binding(tmp_path: Path) -> None:
    plan = tmp_path / "plan.json"
    _write_plan(plan, (_action("knowledge-importer-repair-v1-alpha", eligible=True),))
    first = build_backup_cleanup_approval(plan)
    plan.write_bytes(plan.read_bytes().replace(b"\n", b"\r\n"))
    second = build_backup_cleanup_approval(plan)

    assert first.plan_sha256 != second.plan_sha256
    assert first.approved_actions == second.approved_actions


def test_approval_parser_rejects_blocked_action() -> None:
    blocked = _action("unknown-session", eligible=False)
    payload = {
        "report_type": "knowledge-importer-backup-cleanup-approval",
        "schema_version": 1,
        "plan": {"sha256": "c" * 64, "schema_version": 1},
        "scope": {"mode": "all-planned"},
        "approved_actions": [blocked.payload()],
    }

    with pytest.raises(ValueError):
        parse_backup_cleanup_approval_bytes(json.dumps(payload).encode())


def test_approval_report_path_protection_preserves_foreign_file(tmp_path: Path) -> None:
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    plan = tmp_path / "plan.json"
    report = tmp_path / "approval.json"
    _write_plan(plan, ())
    report.write_bytes(b"foreign bytes")

    assert cli.run(_approval_args(backup_root, plan, report)) == 2
    assert report.read_bytes() == b"foreign bytes"


def test_approval_input_and_output_must_be_outside_backup_root(tmp_path: Path) -> None:
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    plan = tmp_path / "plan.json"
    _write_plan(plan, ())

    assert cli.run(_approval_args(backup_root, plan, backup_root / "approval.json")) == 2
    inside_plan = backup_root / "plan.json"
    inside_plan.write_bytes(plan.read_bytes())
    assert cli.run(_approval_args(backup_root, inside_plan, tmp_path / "approval.json")) == 2


def test_approval_output_has_no_absolute_path_or_traceback(tmp_path: Path) -> None:
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    plan = tmp_path / "plan.json"
    report = tmp_path / "approval.json"
    _write_plan(plan, ())

    assert cli.run(_approval_args(backup_root, plan, report)) == 0
    text = report.read_text(encoding="utf-8")
    assert str(tmp_path) not in text
    assert "Traceback" not in text
    assert Path.home().name not in text
