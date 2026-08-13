from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

import knowledge_importer.cli as cli
import knowledge_importer.repair_execution as execution
from knowledge_importer.artifact_manifest import digest_file
from knowledge_importer.json_writer import write_json_atomically
from knowledge_importer.package_validation import validate_package


class SyntheticConverter:
    def __init__(self) -> None:
        self.inputs: list[Path] = []

    def convert(self, input_path: Path) -> str:
        self.inputs.append(input_path)
        return (
            f"# Synthetic {input_path.stem}\n\n"
            "This fictional document verifies the complete local Knowledge Package lifecycle.\n\n"
            "## Details\n\n"
            "- deterministic conversion\n"
            "- metadata and digest validation\n"
            "- safe repair execution\n"
        )


@dataclass(frozen=True)
class PackageFixture:
    root: Path
    manifest: Path
    quality: Path
    validation: Path
    markdown: Path
    sidecar: Path


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _factory(converter: SyntheticConverter):
    def create(*args: object) -> SyntheticConverter:
        return converter

    return create


def _build_package(base: Path) -> PackageFixture:
    input_root = base / "input"
    package_root = base / "package"
    reports = base / "reports"
    source = input_root / "section" / "document.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"%PDF-1.4\n% synthetic lifecycle fixture\n")
    manifest = reports / "manifest.json"
    quality = reports / "quality.json"
    validation = reports / "validation.json"
    converter = SyntheticConverter()
    arguments = [
        "convert",
        str(input_root),
        "--output",
        str(package_root),
        "--recursive",
        "--normalize-markdown",
        "conservative",
        "--metadata-sidecar",
        "--manifest-json",
        str(manifest),
        "--quality-report-json",
        str(quality),
    ]

    assert cli.run(arguments, converter_factory=_factory(converter)) == 0
    assert len(converter.inputs) == 1
    assert (
        cli.run(
            [
                "validate",
                str(package_root),
                "--manifest",
                str(manifest),
                "--strict",
                "--report-json",
                str(validation),
            ]
        )
        == 0
    )
    return PackageFixture(
        package_root,
        manifest,
        quality,
        validation,
        package_root / "section" / "document.md",
        package_root / "section" / "document.metadata.json",
    )


def _repair_paths(base: Path) -> tuple[Path, Path, Path, Path]:
    reports = base / "repair"
    return (
        reports / "plan.json",
        reports / "approval.json",
        reports / "preflight.json",
        reports / "execution.json",
    )


def _prepare_repair(package: PackageFixture, base: Path) -> tuple[Path, Path, Path, Path]:
    plan, approval, preflight, report = _repair_paths(base)
    assert (
        cli.run(
            [
                "repair-plan",
                str(package.root),
                "--manifest",
                str(package.manifest),
                "--report-json",
                str(plan),
            ]
        )
        == 0
    )
    assert (
        cli.run(
            [
                "approve-repair",
                str(plan),
                "--all-safe",
                "--report-json",
                str(approval),
            ]
        )
        == 0
    )
    assert (
        cli.run(
            [
                "repair-preflight",
                str(package.root),
                "--manifest",
                str(package.manifest),
                "--plan",
                str(plan),
                "--approval",
                str(approval),
                "--report-json",
                str(preflight),
            ]
        )
        == 0
    )
    return plan, approval, preflight, report


def _execute_arguments(
    package: PackageFixture,
    plan: Path,
    approval: Path,
    preflight: Path,
    report: Path,
) -> list[str]:
    return [
        "repair-execute",
        str(package.root),
        "--manifest",
        str(package.manifest),
        "--plan",
        str(plan),
        "--approval",
        str(approval),
        "--preflight",
        str(preflight),
        "--report-json",
        str(report),
    ]


def test_conversion_validation_and_artifacts_are_deterministic(tmp_path: Path) -> None:
    first = _build_package(tmp_path / "first")
    second = _build_package(tmp_path / "second")

    assert first.markdown.read_bytes() == second.markdown.read_bytes()
    assert first.sidecar.read_bytes() == second.sidecar.read_bytes()
    assert first.manifest.read_bytes() == second.manifest.read_bytes()
    assert first.quality.read_bytes() == second.quality.read_bytes()
    assert first.validation.read_bytes() == second.validation.read_bytes()
    assert _read_json(first.manifest)["summary"] == {
        "items": 1,
        "succeeded": 1,
        "skipped": 0,
        "failed": 0,
    }
    assert _read_json(first.quality)["summary"] == {
        "checked": 1,
        "passed": 1,
        "warned": 0,
    }
    assert validate_package(first.root, manifest_path=first.manifest, strict=True).exit_code == 0


def test_missing_sidecar_full_cli_lifecycle_is_safe_and_deterministic(tmp_path: Path) -> None:
    artifacts: list[tuple[bytes, ...]] = []
    for name in ("first", "second"):
        base = tmp_path / name
        package = _build_package(base)
        markdown_before = package.markdown.read_bytes()
        manifest_before = package.manifest.read_bytes()
        package.sidecar.unlink()
        assert cli.run(["validate", str(package.root), "--manifest", str(package.manifest)]) == 1
        plan, approval, preflight, report = _prepare_repair(package, base)
        assert _read_json(plan)["actions"] == [
            {
                "path": "section/document.metadata.json",
                "action": "regenerate-sidecar",
                "reason_category": "missing-sidecar",
                "safe": True,
            }
        ]
        assert cli.run(_execute_arguments(package, plan, approval, preflight, report)) == 0
        assert cli.run(["validate", str(package.root), "--manifest", str(package.manifest)]) == 0
        assert package.markdown.read_bytes() == markdown_before
        assert package.manifest.read_bytes() == manifest_before
        assert digest_file(package.sidecar).sha256 is not None
        assert not (base / "backup").exists()
        artifacts.append(
            tuple(
                path.read_bytes() for path in (plan, approval, preflight, report, package.sidecar)
            )
        )

    assert artifacts[0] == artifacts[1]


def test_stale_sidecar_lifecycle_creates_verified_backup(tmp_path: Path) -> None:
    base = tmp_path / "stale"
    package = _build_package(base)
    stale_bytes = package.sidecar.read_bytes()
    markdown_before = package.markdown.read_bytes()
    payload = _read_json(package.manifest)
    item = payload["items"][0]  # type: ignore[index]
    item["status"] = "failed"  # type: ignore[index]
    item["output"]["bytes"] = None  # type: ignore[index]
    item["output"]["sha256"] = None  # type: ignore[index]
    item["error_category"] = "converter"  # type: ignore[index]
    item["message"] = "synthetic failure"  # type: ignore[index]
    payload["summary"] = {"items": 1, "succeeded": 0, "skipped": 0, "failed": 1}
    write_json_atomically(package.manifest, payload)
    manifest_before = package.manifest.read_bytes()

    assert cli.run(["validate", str(package.root), "--manifest", str(package.manifest)]) == 1
    plan, approval, preflight, report = _prepare_repair(package, base)
    assert _read_json(plan)["actions"][0]["action"] == "remove-stale-sidecar"  # type: ignore[index]
    with tempfile.TemporaryDirectory(prefix="knowledge-importer-lifecycle-") as backup_name:
        backup_root = Path(backup_name)
        arguments = _execute_arguments(package, plan, approval, preflight, report)
        arguments.extend(["--backup-dir", str(backup_root)])
        assert cli.run(arguments) == 0
        backups = tuple(backup_root.rglob("*.bak"))
        assert len(backups) == 1
        assert backups[0].read_bytes() == stale_bytes
        assert digest_file(backups[0]) == digest_file_from_bytes(stale_bytes)

    assert not package.sidecar.exists()
    assert package.markdown.read_bytes() == markdown_before
    assert package.manifest.read_bytes() == manifest_before
    assert cli.run(["validate", str(package.root), "--manifest", str(package.manifest)]) == 0


def digest_file_from_bytes(content: bytes):
    import hashlib

    from knowledge_importer.artifact_manifest import ArtifactDigest

    return ArtifactDigest(len(content), hashlib.sha256(content).hexdigest())


def test_unsafe_digest_mismatch_is_not_approved_or_mutated(tmp_path: Path) -> None:
    base = tmp_path / "unsafe"
    package = _build_package(base)
    package.markdown.write_text("tampered synthetic Markdown\n", encoding="utf-8")
    before = {
        path: path.read_bytes() for path in (package.markdown, package.sidecar, package.manifest)
    }
    plan, approval, preflight, _ = _prepare_repair(package, base)

    action = _read_json(plan)["actions"][0]  # type: ignore[index]
    assert action["action"] == "manual-review"  # type: ignore[index]
    assert action["safe"] is False  # type: ignore[index]
    assert _read_json(approval)["approved_actions"] == []
    assert _read_json(preflight)["actions"] == []
    assert {path: path.read_bytes() for path in before} == before


def test_toctou_change_after_preflight_blocks_execution_without_mutation(tmp_path: Path) -> None:
    base = tmp_path / "toctou"
    package = _build_package(base)
    package.sidecar.unlink()
    plan, approval, preflight, report = _prepare_repair(package, base)
    changed = b"# externally changed after preflight\n"
    package.markdown.write_bytes(changed)
    manifest_before = package.manifest.read_bytes()

    assert cli.run(_execute_arguments(package, plan, approval, preflight, report)) == 1
    assert package.markdown.read_bytes() == changed
    assert package.manifest.read_bytes() == manifest_before
    assert not package.sidecar.exists()
    assert _read_json(report)["actions"][0]["status"] == "failed-precondition"  # type: ignore[index]


def test_later_failure_rolls_back_first_action_and_leaves_package_validly_broken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "rollback"
    input_root = base / "input"
    package_root = base / "package"
    for name in ("a", "b", "c"):
        source = input_root / f"{name}.pdf"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(f"%PDF synthetic {name}\n".encode())
    converter = SyntheticConverter()
    manifest = base / "manifest.json"
    assert (
        cli.run(
            [
                "convert",
                str(input_root),
                "--output",
                str(package_root),
                "--metadata-sidecar",
                "--manifest-json",
                str(manifest),
            ],
            converter_factory=_factory(converter),
        )
        == 0
    )
    for sidecar in package_root.glob("*.metadata.json"):
        sidecar.unlink()
    package = PackageFixture(
        package_root,
        manifest,
        base / "unused-quality.json",
        base / "unused-validation.json",
        package_root / "a.md",
        package_root / "a.metadata.json",
    )
    before = {path: path.read_bytes() for path in package_root.glob("*.md")}
    plan, approval, preflight, report = _prepare_repair(package, base)
    original = execution._write_new_sidecar
    calls = 0

    def fail_second(path: Path, sidecar: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic lifecycle failure")
        original(path, sidecar)  # type: ignore[arg-type]

    monkeypatch.setattr(execution, "_write_new_sidecar", fail_second)
    assert cli.run(_execute_arguments(package, plan, approval, preflight, report)) == 1
    assert [item["status"] for item in _read_json(report)["actions"]] == [  # type: ignore[index]
        "rolled-back",
        "failed",
        "not-run",
    ]
    assert not tuple(package_root.glob("*.metadata.json"))
    assert {path: path.read_bytes() for path in before} == before
    assert validate_package(package_root, manifest_path=manifest).failed == 3


def test_execution_report_failure_keeps_successful_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base = tmp_path / "report-failure"
    package = _build_package(base)
    package.sidecar.unlink()
    plan, approval, preflight, report = _prepare_repair(package, base)
    monkeypatch.setattr(
        cli,
        "write_execution_report",
        lambda path, result: (_ for _ in ()).throw(OSError("hidden machine detail")),
    )

    assert cli.run(_execute_arguments(package, plan, approval, preflight, report)) == 2
    captured = capsys.readouterr()
    assert package.sidecar.is_file()
    assert validate_package(package.root, manifest_path=package.manifest).exit_code == 0
    assert str(tmp_path) not in captured.err
    assert "Traceback" not in captured.err
