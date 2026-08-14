from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import knowledge_importer.backup_inventory as inventory
import knowledge_importer.cli as cli
from knowledge_importer.artifact_manifest import ArtifactDigest
from knowledge_importer.backup_inventory import (
    MANAGED_SESSION_PREFIX,
    SESSION_MANIFEST_FILENAME,
    BackupInventoryInputError,
    BackupSessionBindings,
    BackupSessionClassification,
    BackupSessionItem,
    BackupSessionManifest,
    BackupSessionState,
    build_backup_inventory,
    parse_backup_inventory_bytes,
    parse_backup_session_manifest_bytes,
    transition_backup_session,
    write_backup_session_manifest,
)

_DIGEST = "a" * 64


@pytest.fixture(autouse=True)
def _ignore_workspace_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inventory, "repository_roots", lambda package_root: ())


def _bindings() -> BackupSessionBindings:
    return BackupSessionBindings(_DIGEST, "b" * 64, "c" * 64, "d" * 64)


def _managed_session(
    backup_root: Path,
    *,
    suffix: str = "alpha",
    state: BackupSessionState = BackupSessionState.COMPLETE,
    source: str = "日本語/文書.metadata.json",
) -> tuple[Path, BackupSessionManifest]:
    session = backup_root / f"{MANAGED_SESSION_PREFIX}{suffix}"
    session.mkdir(parents=True)
    backup_name = f"0000/{source}.bak"
    backup = session / Path(*backup_name.split("/"))
    backup.parent.mkdir(parents=True)
    content = "架空のバックアップです。\n".encode()
    backup.write_bytes(content)
    manifest = BackupSessionManifest(
        state,
        _bindings(),
        (
            BackupSessionItem(
                source,
                backup_name,
                ArtifactDigest(len(content), hashlib.sha256(content).hexdigest()),
            ),
        ),
    )
    write_backup_session_manifest(
        session / SESSION_MANIFEST_FILENAME,
        manifest,
        expected_current=None,
    )
    return session, manifest


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    package_root = tmp_path / "package"
    backup_root = tmp_path / "backups"
    package_root.mkdir()
    backup_root.mkdir()
    return package_root, backup_root


def test_session_manifest_is_deterministic_and_accepts_unknown_fields(tmp_path: Path) -> None:
    _, backup_root = _roots(tmp_path)
    session, manifest = _managed_session(backup_root)
    path = session / SESSION_MANIFEST_FILENAME
    first = path.read_bytes()

    parsed = parse_backup_session_manifest_bytes(first)
    payload = json.loads(first)
    payload["future_field"] = {"ignored": True}

    assert parsed == manifest
    assert parse_backup_session_manifest_bytes(json.dumps(payload).encode()) == manifest
    assert first.endswith(b"\n")
    assert b'  "schema_version": 1' in first


def test_session_state_transitions_are_bounded() -> None:
    opened = BackupSessionManifest(BackupSessionState.OPEN, _bindings(), ())

    for state in (
        BackupSessionState.COMPLETE,
        BackupSessionState.ROLLED_BACK,
        BackupSessionState.ROLLBACK_FAILED,
    ):
        assert transition_backup_session(opened, state).state is state
    with pytest.raises(ValueError):
        transition_backup_session(
            transition_backup_session(opened, BackupSessionState.COMPLETE),
            BackupSessionState.ROLLED_BACK,
        )


def test_complete_session_is_managed_and_planning_eligible(tmp_path: Path) -> None:
    package_root, backup_root = _roots(tmp_path)
    _managed_session(backup_root)

    result = build_backup_inventory(package_root, backup_root)
    item = result.sessions[0]

    assert result.exit_code == 0
    assert item.classification is BackupSessionClassification.MANAGED
    assert item.state is BackupSessionState.COMPLETE
    assert item.planning_eligible
    assert item.session_manifest_sha256 is not None
    assert item.tree_sha256 is not None
    assert item.items[0].source == "日本語/文書.metadata.json"


@pytest.mark.parametrize(
    ("state", "classification", "eligible", "exit_code"),
    [
        (
            BackupSessionState.OPEN,
            BackupSessionClassification.INTERRUPTED,
            False,
            1,
        ),
        (
            BackupSessionState.ROLLED_BACK,
            BackupSessionClassification.MANAGED,
            False,
            0,
        ),
        (
            BackupSessionState.ROLLBACK_FAILED,
            BackupSessionClassification.MANAGED,
            False,
            1,
        ),
    ],
)
def test_non_complete_states_are_never_planning_eligible(
    tmp_path: Path,
    state: BackupSessionState,
    classification: BackupSessionClassification,
    eligible: bool,
    exit_code: int,
) -> None:
    package_root, backup_root = _roots(tmp_path)
    _managed_session(backup_root, state=state)

    result = build_backup_inventory(package_root, backup_root)

    assert result.sessions[0].classification is classification
    assert result.sessions[0].planning_eligible is eligible
    assert result.exit_code == exit_code


def test_multiple_sessions_and_items_have_deterministic_order(tmp_path: Path) -> None:
    package_root, backup_root = _roots(tmp_path)
    _managed_session(backup_root, suffix="zulu", source="z/文書.metadata.json")
    _managed_session(backup_root, suffix="alpha", source="a/文書.metadata.json")

    first = build_backup_inventory(package_root, backup_root)
    second = build_backup_inventory(package_root, backup_root)

    assert [item.session for item in first.sessions] == [
        f"{MANAGED_SESSION_PREFIX}alpha",
        f"{MANAGED_SESSION_PREFIX}zulu",
    ]
    assert first.payload() == second.payload()
    assert json.dumps(first.payload(), ensure_ascii=False, indent=2) == json.dumps(
        second.payload(), ensure_ascii=False, indent=2
    )


def test_legacy_v010_session_is_detected_but_not_migrated(tmp_path: Path) -> None:
    package_root, backup_root = _roots(tmp_path)
    legacy = backup_root / "knowledge-importer-repair-old123"
    backup = legacy / "0000" / "section" / "a.metadata.json.bak"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"legacy")
    before = backup.read_bytes()

    result = build_backup_inventory(package_root, backup_root)

    assert result.sessions[0].classification is BackupSessionClassification.LEGACY_UNMANAGED
    assert not result.sessions[0].planning_eligible
    assert result.exit_code == 1
    assert backup.read_bytes() == before
    assert not (legacy / SESSION_MANIFEST_FILENAME).exists()


def test_missing_and_invalid_session_manifests_are_classified(tmp_path: Path) -> None:
    package_root, backup_root = _roots(tmp_path)
    missing = backup_root / f"{MANAGED_SESSION_PREFIX}missing"
    missing.mkdir()
    invalid = backup_root / f"{MANAGED_SESSION_PREFIX}invalid"
    invalid.mkdir()
    (invalid / SESSION_MANIFEST_FILENAME).write_text("not json\n", encoding="utf-8")

    result = build_backup_inventory(package_root, backup_root)

    assert [item.classification for item in result.sessions] == [
        BackupSessionClassification.INVALID_MANIFEST,
        BackupSessionClassification.MISSING_MANIFEST,
    ]
    assert result.exit_code == 1


@pytest.mark.parametrize("entry_kind", ["file", "directory"])
def test_undeclared_session_entries_are_rejected(tmp_path: Path, entry_kind: str) -> None:
    package_root, backup_root = _roots(tmp_path)
    session, _ = _managed_session(backup_root)
    extra = session / "undeclared"
    if entry_kind == "file":
        extra.write_bytes(b"unexpected")
    else:
        extra.mkdir()

    result = build_backup_inventory(package_root, backup_root)

    assert result.sessions[0].classification is BackupSessionClassification.UNEXPECTED_ENTRY
    assert not result.sessions[0].planning_eligible


@pytest.mark.parametrize("change", ["bytes", "digest"])
def test_backup_content_mismatch_is_rejected(tmp_path: Path, change: str) -> None:
    package_root, backup_root = _roots(tmp_path)
    session, manifest = _managed_session(backup_root)
    backup = session / Path(*manifest.items[0].backup.split("/"))
    if change == "bytes":
        backup.write_bytes(b"changed backup")
    else:
        payload = json.loads((session / SESSION_MANIFEST_FILENAME).read_text(encoding="utf-8"))
        payload["items"][0]["sha256"] = "0" * 64
        (session / SESSION_MANIFEST_FILENAME).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    result = build_backup_inventory(package_root, backup_root)

    assert result.sessions[0].classification is BackupSessionClassification.INVALID_MANIFEST


@pytest.mark.parametrize(
    ("source", "backup"),
    [
        ("C:/private/a.metadata.json", "0000/a.metadata.json.bak"),
        ("../a.metadata.json", "0000/a.metadata.json.bak"),
        ("a.metadata.json", "../a.metadata.json.bak"),
        ("a.metadata.json", "/tmp/a.metadata.json.bak"),
        ("a.metadata.json", "0000/other.metadata.json.bak"),
    ],
)
def test_session_manifest_rejects_unsafe_or_escaping_paths(source: str, backup: str) -> None:
    payload = BackupSessionManifest(
        BackupSessionState.OPEN,
        _bindings(),
        (BackupSessionItem(source, backup, ArtifactDigest(1, _DIGEST)),),
    ).payload()

    with pytest.raises(ValueError):
        parse_backup_session_manifest_bytes(
            (json.dumps(payload, ensure_ascii=False) + "\n").encode()
        )


@pytest.mark.parametrize("duplicate_field", ["source", "backup"])
def test_session_manifest_rejects_duplicate_items(duplicate_field: str) -> None:
    first = BackupSessionItem(
        "a.metadata.json",
        "0000/a.metadata.json.bak",
        ArtifactDigest(1, _DIGEST),
    )
    second = BackupSessionItem(
        "b.metadata.json",
        "0001/b.metadata.json.bak",
        ArtifactDigest(1, "b" * 64),
    )
    payload = BackupSessionManifest(
        BackupSessionState.OPEN,
        _bindings(),
        (first, second),
    ).payload()
    payload["items"][1][duplicate_field] = payload["items"][0][duplicate_field]  # type: ignore[index]

    with pytest.raises(ValueError):
        parse_backup_session_manifest_bytes(json.dumps(payload).encode())


def test_binding_errors_have_a_distinct_inventory_classification(tmp_path: Path) -> None:
    package_root, backup_root = _roots(tmp_path)
    session, _ = _managed_session(backup_root)
    manifest = session / SESSION_MANIFEST_FILENAME
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["bindings"]["plan"]["sha256"] = "INVALID"
    manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    result = build_backup_inventory(package_root, backup_root)

    assert result.sessions[0].classification is BackupSessionClassification.BINDING_UNVERIFIABLE


def test_session_symlink_is_not_followed(tmp_path: Path) -> None:
    package_root, backup_root = _roots(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    session = backup_root / f"{MANAGED_SESSION_PREFIX}linked"
    try:
        session.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not permitted")

    result = build_backup_inventory(package_root, backup_root)

    assert result.sessions[0].classification is BackupSessionClassification.UNEXPECTED_ENTRY
    assert tuple(outside.iterdir()) == ()


def test_backup_file_symlink_is_not_followed(tmp_path: Path) -> None:
    package_root, backup_root = _roots(tmp_path)
    session, manifest = _managed_session(backup_root)
    backup = session / Path(*manifest.items[0].backup.split("/"))
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside remains unchanged")
    backup.unlink()
    try:
        backup.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is not permitted")

    result = build_backup_inventory(package_root, backup_root)

    assert result.sessions[0].classification is BackupSessionClassification.UNEXPECTED_ENTRY
    assert outside.read_bytes() == b"outside remains unchanged"


def test_inventory_report_is_deterministic_and_can_replace_itself(tmp_path: Path) -> None:
    package_root, backup_root = _roots(tmp_path)
    _managed_session(backup_root)
    report = tmp_path / "inventory.json"
    args = [
        "backup-inventory",
        str(backup_root),
        "--package-root",
        str(package_root),
        "--report-json",
        str(report),
    ]

    assert cli.run(args) == 0
    first = report.read_bytes()
    assert cli.run(args) == 0

    assert report.read_bytes() == first
    assert parse_backup_inventory_bytes(first).sessions[0].planning_eligible


def test_inventory_report_inside_backup_root_is_rejected_before_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root, backup_root = _roots(tmp_path)
    report = backup_root / "inventory.json"
    monkeypatch.setattr(
        cli,
        "build_backup_inventory",
        lambda package, backup: (_ for _ in ()).throw(AssertionError("must not scan")),
    )

    exit_code = cli.run(
        [
            "backup-inventory",
            str(backup_root),
            "--package-root",
            str(package_root),
            "--report-json",
            str(report),
        ]
    )

    assert exit_code == 2
    assert not report.exists()


def test_existing_noninventory_report_is_preserved(tmp_path: Path) -> None:
    package_root, backup_root = _roots(tmp_path)
    report = tmp_path / "report.json"
    report.write_text('{"report_type":"other"}\n', encoding="utf-8")
    before = report.read_bytes()

    exit_code = cli.run(
        [
            "backup-inventory",
            str(backup_root),
            "--package-root",
            str(package_root),
            "--report-json",
            str(report),
        ]
    )

    assert exit_code == 2
    assert report.read_bytes() == before


def test_backup_root_cannot_be_inside_package(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    backup_root = package_root / "backups"
    backup_root.mkdir(parents=True)

    with pytest.raises(BackupInventoryInputError):
        build_backup_inventory(package_root, backup_root)


def test_backup_root_cannot_be_inside_detected_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_root = tmp_path / "package"
    repository_root = tmp_path / "repository"
    backup_root = repository_root / "backups"
    package_root.mkdir()
    backup_root.mkdir(parents=True)
    monkeypatch.setattr(inventory, "repository_roots", lambda package: (repository_root,))

    with pytest.raises(BackupInventoryInputError):
        build_backup_inventory(package_root, backup_root)


def test_inventory_output_does_not_expose_absolute_paths_or_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    package_root, backup_root = _roots(tmp_path)
    _managed_session(backup_root)
    report = tmp_path / "inventory.json"

    assert (
        cli.run(
            [
                "backup-inventory",
                str(backup_root),
                "--package-root",
                str(package_root),
                "--report-json",
                str(report),
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    combined = captured.out + captured.err + report.read_text(encoding="utf-8")
    assert str(tmp_path) not in combined
    assert "Traceback" not in combined
    assert Path.home().name not in combined


def test_backup_inventory_help_exposes_read_only_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        cli.run(["backup-inventory", "--help"])

    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "BACKUP_ROOT" in output
    assert "--package-root PACKAGE_ROOT" in output
    assert "--report-json PATH" in output
