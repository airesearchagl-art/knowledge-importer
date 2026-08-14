import hashlib
import json
from pathlib import Path

import pytest

import knowledge_importer.cli as cli
from knowledge_importer.artifact_manifest import ArtifactDigest
from knowledge_importer.backup_cleanup_plan import (
    BackupCleanupPlanInputError,
    build_backup_cleanup_plan,
    parse_backup_cleanup_plan_bytes,
)
from knowledge_importer.backup_inventory import (
    BackupInventory,
    BackupInventorySession,
    BackupSessionClassification,
    BackupSessionItem,
    BackupSessionState,
    write_backup_inventory,
)

_SHA_A = "a" * 64
_SHA_B = "b" * 64


@pytest.fixture(autouse=True)
def _ignore_workspace_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "repository_roots", lambda backup_root: ())


def _session(
    name: str,
    *,
    classification: BackupSessionClassification = BackupSessionClassification.MANAGED,
    state: BackupSessionState | None = BackupSessionState.COMPLETE,
    eligible: bool = True,
) -> BackupInventorySession:
    managed = classification in {
        BackupSessionClassification.MANAGED,
        BackupSessionClassification.INTERRUPTED,
    }
    items = (
        (
            BackupSessionItem(
                "section/a.metadata.json",
                "0000/section/a.metadata.json.bak",
                ArtifactDigest(17, "c" * 64),
            ),
        )
        if managed
        else ()
    )
    return BackupInventorySession(
        name,
        classification,
        state,
        eligible,
        _SHA_A if managed else None,
        _SHA_B if managed else None,
        items,
    )


def _write_inventory(path: Path, sessions: tuple[BackupInventorySession, ...]) -> None:
    write_backup_inventory(path, BackupInventory(sessions))


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    inventory = tmp_path / "inventory.json"
    report = tmp_path / "cleanup-plan.json"
    return backup_root, inventory, report


def _plan_args(
    backup_root: Path,
    inventory: Path,
    report: Path,
    *sessions: str,
) -> list[str]:
    args = [
        "backup-cleanup-plan",
        str(inventory),
        "--backup-root",
        str(backup_root),
    ]
    for session in sessions:
        args.extend(("--session", session))
    args.extend(("--report-json", str(report)))
    return args


def test_explicit_single_session_plan_uses_exact_inventory_binding(tmp_path: Path) -> None:
    backup_root, inventory, report = _roots(tmp_path)
    session = _session("knowledge-importer-repair-v1-alpha")
    _write_inventory(inventory, (session,))
    inventory_bytes = inventory.read_bytes()

    assert cli.run(_plan_args(backup_root, inventory, report, session.session)) == 0

    plan = parse_backup_cleanup_plan_bytes(report.read_bytes())
    assert plan.inventory_sha256 == hashlib.sha256(inventory_bytes).hexdigest()
    assert len(plan.actions) == 1
    assert plan.actions[0].eligible
    assert plan.actions[0].backup_files == 1
    assert plan.actions[0].backup_bytes == 17


def test_multiple_sessions_use_canonical_order_and_are_deterministic(tmp_path: Path) -> None:
    backup_root, inventory, report = _roots(tmp_path)
    alpha = _session("knowledge-importer-repair-v1-alpha")
    zulu = _session("knowledge-importer-repair-v1-zulu")
    _write_inventory(inventory, (alpha, zulu))
    args = _plan_args(backup_root, inventory, report, zulu.session, alpha.session)

    assert cli.run(args) == 0
    first = report.read_bytes()
    assert cli.run(args) == 0

    assert report.read_bytes() == first
    assert [action.session for action in parse_backup_cleanup_plan_bytes(first).actions] == [
        alpha.session,
        zulu.session,
    ]


@pytest.mark.parametrize(
    "inventory_session",
    [
        None,
        _session(
            "legacy-session",
            classification=BackupSessionClassification.LEGACY_UNMANAGED,
            state=None,
            eligible=False,
        ),
        _session(
            "knowledge-importer-repair-v1-open",
            classification=BackupSessionClassification.INTERRUPTED,
            state=BackupSessionState.OPEN,
            eligible=False,
        ),
        _session(
            "knowledge-importer-repair-v1-rollback-failed",
            state=BackupSessionState.ROLLBACK_FAILED,
            eligible=False,
        ),
        _session(
            "invalid-session-example",
            classification=BackupSessionClassification.INVALID_MANIFEST,
            state=None,
            eligible=False,
        ),
        _session(
            "unexpected-session",
            classification=BackupSessionClassification.UNEXPECTED_ENTRY,
            state=None,
            eligible=False,
        ),
    ],
    ids=("unknown", "legacy", "interrupted", "rollback-failed", "invalid", "unexpected"),
)
def test_ineligible_or_unknown_session_is_blocked_but_plan_succeeds(
    tmp_path: Path,
    inventory_session: BackupInventorySession | None,
) -> None:
    backup_root, inventory, report = _roots(tmp_path)
    sessions = (inventory_session,) if inventory_session is not None else ()
    _write_inventory(inventory, sessions)
    requested = inventory_session.session if inventory_session is not None else "unknown-session"

    assert cli.run(_plan_args(backup_root, inventory, report, requested)) == 0

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["summary"] == {"requested": 1, "planned": 0, "blocked": 1}
    assert payload["actions"][0]["eligible"] is False


def test_duplicate_session_is_rejected_before_report_write(tmp_path: Path) -> None:
    backup_root, inventory, report = _roots(tmp_path)
    session = _session("knowledge-importer-repair-v1-alpha")
    _write_inventory(inventory, (session,))

    assert (
        cli.run(
            _plan_args(backup_root, inventory, report, session.session, session.session.upper())
        )
        == 2
    )
    assert not report.exists()


@pytest.mark.parametrize("tamper", ["schema", "summary"])
def test_tampered_inventory_is_rejected(
    tmp_path: Path,
    tamper: str,
) -> None:
    backup_root, inventory, report = _roots(tmp_path)
    session = _session("knowledge-importer-repair-v1-alpha")
    _write_inventory(inventory, (session,))
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    if tamper == "schema":
        payload["schema_version"] = 2
    else:
        payload["summary"]["sessions"] = 99
    inventory.write_text(json.dumps(payload), encoding="utf-8")

    assert cli.run(_plan_args(backup_root, inventory, report, session.session)) == 2
    assert not report.exists()


def test_inventory_exact_bytes_change_plan_binding(tmp_path: Path) -> None:
    _, inventory, _ = _roots(tmp_path)
    session = _session("knowledge-importer-repair-v1-alpha")
    _write_inventory(inventory, (session,))
    first = build_backup_cleanup_plan(inventory, (session.session,))
    inventory.write_bytes(inventory.read_bytes().replace(b"\n", b"\r\n"))
    second = build_backup_cleanup_plan(inventory, (session.session,))

    assert first.inventory_sha256 != second.inventory_sha256
    assert first.actions == second.actions


def test_semantically_tampered_plan_is_rejected(tmp_path: Path) -> None:
    backup_root, inventory, report = _roots(tmp_path)
    _write_inventory(inventory, ())
    assert cli.run(_plan_args(backup_root, inventory, report, "unknown-session")) == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["actions"][0]["eligible"] = True
    content = json.dumps(payload).encode()

    with pytest.raises(ValueError):
        parse_backup_cleanup_plan_bytes(content)


def test_report_path_protection_preserves_foreign_file(tmp_path: Path) -> None:
    backup_root, inventory, report = _roots(tmp_path)
    session = _session("knowledge-importer-repair-v1-alpha")
    _write_inventory(inventory, (session,))
    report.write_bytes(b"foreign bytes")

    assert cli.run(_plan_args(backup_root, inventory, report, session.session)) == 2
    assert report.read_bytes() == b"foreign bytes"


def test_report_and_inventory_inside_backup_root_are_rejected(tmp_path: Path) -> None:
    backup_root, inventory, report = _roots(tmp_path)
    session = _session("knowledge-importer-repair-v1-alpha")
    _write_inventory(inventory, (session,))

    assert (
        cli.run(_plan_args(backup_root, inventory, backup_root / "plan.json", session.session)) == 2
    )
    inside_inventory = backup_root / "inventory.json"
    inside_inventory.write_bytes(inventory.read_bytes())
    assert cli.run(_plan_args(backup_root, inside_inventory, report, session.session)) == 2


def test_backup_root_inside_repository_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_root, inventory, report = _roots(tmp_path)
    session = _session("knowledge-importer-repair-v1-alpha")
    _write_inventory(inventory, (session,))
    monkeypatch.setattr(cli, "repository_roots", lambda backup: (tmp_path,))

    assert cli.run(_plan_args(backup_root, inventory, report, session.session)) == 2
    assert not report.exists()


def test_cleanup_plan_does_not_modify_backup_tree(tmp_path: Path) -> None:
    backup_root, inventory, report = _roots(tmp_path)
    session = _session("knowledge-importer-repair-v1-alpha")
    _write_inventory(inventory, (session,))
    backup_file = backup_root / session.session / "0000" / "payload.bak"
    backup_file.parent.mkdir(parents=True)
    backup_file.write_bytes(b"backup remains byte-identical")
    before = {
        path.relative_to(backup_root): path.read_bytes()
        for path in backup_root.rglob("*")
        if path.is_file()
    }

    assert cli.run(_plan_args(backup_root, inventory, report, session.session)) == 0

    after = {
        path.relative_to(backup_root): path.read_bytes()
        for path in backup_root.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_cleanup_plan_output_has_no_absolute_path_or_traceback(tmp_path: Path) -> None:
    backup_root, inventory, report = _roots(tmp_path)
    session = _session("knowledge-importer-repair-v1-alpha")
    _write_inventory(inventory, (session,))

    assert cli.run(_plan_args(backup_root, inventory, report, session.session)) == 0

    text = report.read_text(encoding="utf-8")
    assert str(tmp_path) not in text
    assert "Traceback" not in text
    assert Path.home().name not in text


def test_cleanup_plan_does_not_follow_report_symlink(tmp_path: Path) -> None:
    backup_root, inventory, report = _roots(tmp_path)
    session = _session("knowledge-importer-repair-v1-alpha")
    _write_inventory(inventory, (session,))
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"outside remains unchanged")
    try:
        report.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is not permitted")

    assert cli.run(_plan_args(backup_root, inventory, report, session.session)) == 2
    assert outside.read_bytes() == b"outside remains unchanged"


def test_invalid_session_argument_is_rejected() -> None:
    with pytest.raises(BackupCleanupPlanInputError):
        build_backup_cleanup_plan(Path("unused"), ("../escape",))
