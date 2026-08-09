from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

import knowledge_importer.cli as cli
from knowledge_importer.artifact_manifest import (
    ArtifactDigest,
    ArtifactManifestItem,
    ManifestStatus,
)
from knowledge_importer.document_metadata import (
    DocumentMetadataSettings,
    build_document_metadata,
    metadata_sidecar_path,
    write_document_metadata,
)


class StaticConverter:
    def __init__(self, markdown: str = "架空の本文です。\n") -> None:
        self.markdown = markdown
        self.inputs: list[Path] = []

    def convert(self, input_path: Path) -> str:
        self.inputs.append(input_path)
        return self.markdown


class ConditionalConverter(StaticConverter):
    def convert(self, input_path: Path) -> str:
        self.inputs.append(input_path)
        if input_path.name.startswith("failed"):
            raise RuntimeError("synthetic conversion failure")
        return self.markdown


def _pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n% synthetic fixture\n")


def _factory(converter: StaticConverter):
    return lambda do_table_structure: converter


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_sidecar_writer_is_deterministic_and_atomic(tmp_path: Path) -> None:
    digest = ArtifactDigest(3, hashlib.sha256(b"abc").hexdigest())
    item = ArtifactManifestItem(
        "input.pdf",
        "output.md",
        ManifestStatus.SUCCEEDED,
        digest,
        digest,
    )
    sidecar = build_document_metadata(
        item,
        DocumentMetadataSettings(False, None, False),
    )
    path = tmp_path / "output.metadata.json"

    write_document_metadata(path, sidecar)
    first = path.read_bytes()
    write_document_metadata(path, sidecar)

    assert path.read_bytes() == first
    assert first.endswith(b"\n")
    assert list(tmp_path.glob(".output.metadata.json.*.tmp")) == []


def test_atomic_failure_preserves_existing_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = ArtifactDigest(3, hashlib.sha256(b"abc").hexdigest())
    item = ArtifactManifestItem(
        "input.pdf",
        "output.md",
        ManifestStatus.SUCCEEDED,
        digest,
        digest,
    )
    sidecar = build_document_metadata(
        item,
        DocumentMetadataSettings(False, None, False),
    )
    path = tmp_path / "output.metadata.json"
    path.write_text("existing\n", encoding="utf-8")
    original_replace = Path.replace

    def failing_replace(self: Path, target: Path) -> Path:
        if target == path:
            raise OSError("synthetic replace failure")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", failing_replace)

    with pytest.raises(OSError, match="synthetic replace failure"):
        write_document_metadata(path, sidecar)
    assert path.read_text(encoding="utf-8") == "existing\n"
    assert list(tmp_path.glob(".output.metadata.json.*.tmp")) == []


def test_option_omitted_preserves_existing_io_and_avoids_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "input.pdf"
    output = tmp_path / "output.md"
    _pdf(source)

    def unexpected_digest(*args: object, **kwargs: object) -> ArtifactManifestItem:
        raise AssertionError("digest must not be calculated")

    monkeypatch.setattr(cli, "build_manifest_item", unexpected_digest)
    assert (
        cli.run(
            ["convert", str(source), "--output", str(output)],
            converter_factory=_factory(StaticConverter()),
        )
        == 0
    )
    assert not metadata_sidecar_path(output).exists()


def test_single_succeeded_sidecar_schema_and_digests(tmp_path: Path) -> None:
    source = tmp_path / "入力.pdf"
    output = tmp_path / "出力.md"
    _pdf(source)
    markdown = "架空の本文です。\n"

    exit_code = cli.run(
        ["convert", str(source), "--output", str(output), "--metadata-sidecar"],
        converter_factory=_factory(StaticConverter(markdown)),
    )
    payload = _read_json(metadata_sidecar_path(output))

    assert exit_code == 0
    assert payload == {
        "report_type": "knowledge-document-metadata",
        "schema_version": 1,
        "engine": {"name": "knowledge-importer", "version": "0.1.0"},
        "document": {
            "input_path": "入力.pdf",
            "output_path": "出力.md",
            "status": "succeeded",
        },
        "artifact": {
            "bytes": len(output.read_bytes()),
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        },
        "source": {
            "bytes": len(source.read_bytes()),
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        },
        "settings": {
            "table_structure": False,
            "normalization_profile": None,
            "artifacts_path_configured": False,
        },
    }


def test_normalized_output_digest_and_settings_are_final(tmp_path: Path) -> None:
    source = tmp_path / "input.pdf"
    output = tmp_path / "output.md"
    _pdf(source)

    assert (
        cli.run(
            [
                "convert",
                str(source),
                "--output",
                str(output),
                "--normalize-markdown",
                "conservative",
                "--table-structure",
                "--metadata-sidecar",
            ],
            converter_factory=_factory(StaticConverter("\ufeff本文 \r\n\r\n")),
        )
        == 0
    )
    payload = _read_json(metadata_sidecar_path(output))

    assert output.read_bytes() == "本文\n".encode()
    assert payload["artifact"]["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()  # type: ignore[index]
    assert payload["settings"] == {
        "table_structure": True,
        "normalization_profile": "conservative",
        "artifacts_path_configured": False,
    }


def test_local_artifacts_setting_records_only_configuration(tmp_path: Path) -> None:
    source = tmp_path / "input.pdf"
    output = tmp_path / "output.md"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    _pdf(source)

    def factory(do_table_structure: bool, artifacts_path: Path) -> StaticConverter:
        assert artifacts_path == artifacts
        return StaticConverter()

    assert (
        cli.run(
            [
                "convert",
                str(source),
                "--output",
                str(output),
                "--artifacts-path",
                str(artifacts),
                "--metadata-sidecar",
            ],
            converter_factory=factory,
        )
        == 0
    )
    payload = _read_json(metadata_sidecar_path(output))

    assert payload["settings"]["artifacts_path_configured"] is True  # type: ignore[index]
    assert str(artifacts) not in metadata_sidecar_path(output).read_text(encoding="utf-8")


def test_recursive_batch_uses_relative_posix_paths_and_skip_status(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    generated = input_dir / "節" / "generated.pdf"
    skipped = input_dir / "節" / "skipped.pdf"
    _pdf(generated)
    _pdf(skipped)
    skipped_output = output_dir / "節" / "skipped.md"
    skipped_output.parent.mkdir(parents=True)
    skipped_bytes = "既存 \r\n".encode()
    skipped_output.write_bytes(skipped_bytes)

    assert (
        cli.run(
            [
                "convert",
                str(input_dir),
                "--output",
                str(output_dir),
                "--recursive",
                "--normalize-markdown",
                "conservative",
                "--metadata-sidecar",
            ],
            converter_factory=_factory(StaticConverter("新規 \r\n")),
        )
        == 0
    )
    generated_payload = _read_json(output_dir / "節" / "generated.metadata.json")
    skipped_payload = _read_json(output_dir / "節" / "skipped.metadata.json")

    assert generated_payload["document"] == {
        "input_path": "節/generated.pdf",
        "output_path": "節/generated.md",
        "status": "succeeded",
    }
    assert skipped_payload["document"] == {
        "input_path": "節/skipped.pdf",
        "output_path": "節/skipped.md",
        "status": "skipped",
    }
    assert skipped_output.read_bytes() == skipped_bytes
    assert skipped_payload["settings"]["normalization_profile"] == "conservative"  # type: ignore[index]
    assert skipped_payload["artifact"]["sha256"] == hashlib.sha256(skipped_bytes).hexdigest()  # type: ignore[index]


def test_failed_items_have_no_sidecar_and_other_items_continue(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    _pdf(input_dir / "failed.pdf")
    _pdf(input_dir / "passed.pdf")

    exit_code = cli.run(
        ["convert", str(input_dir), "--output", str(output_dir), "--metadata-sidecar"],
        converter_factory=_factory(ConditionalConverter()),
    )

    assert exit_code == 1
    assert not (output_dir / "failed.metadata.json").exists()
    assert (output_dir / "passed.metadata.json").is_file()


def test_all_failed_and_filtered_zero_generate_no_sidecars(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    _pdf(input_dir / "failed.pdf")

    assert (
        cli.run(
            ["convert", str(input_dir), "--output", str(output_dir), "--metadata-sidecar"],
            converter_factory=_factory(ConditionalConverter()),
        )
        == 1
    )
    assert list(output_dir.glob("*.metadata.json")) == []

    filtered_output = tmp_path / "filtered-output"
    assert (
        cli.run(
            [
                "convert",
                str(input_dir),
                "--output",
                str(filtered_output),
                "--include",
                "selected.pdf",
                "--metadata-sidecar",
            ],
            converter_factory=_factory(StaticConverter()),
        )
        == 0
    )
    assert not filtered_output.exists()


def test_force_regenerates_sidecar_with_stable_digest(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    _pdf(input_dir / "one.pdf")
    arguments = [
        "convert",
        str(input_dir),
        "--output",
        str(output_dir),
        "--force",
        "--normalize-markdown",
        "conservative",
        "--metadata-sidecar",
    ]

    assert cli.run(arguments, converter_factory=_factory(StaticConverter("本文 \r\n"))) == 0
    first = (output_dir / "one.metadata.json").read_bytes()
    assert cli.run(arguments, converter_factory=_factory(StaticConverter("本文 \r\n"))) == 0

    assert (output_dir / "one.metadata.json").read_bytes() == first


def test_manifest_and_sidecar_share_paths_digests_and_one_artifact_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    manifest_path = tmp_path / "manifest.json"
    _pdf(input_dir / "one.pdf")
    original_builder = cli.build_manifest_item
    calls = 0

    def recording_builder(*args: object, **kwargs: object) -> ArtifactManifestItem:
        nonlocal calls
        calls += 1
        return original_builder(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "build_manifest_item", recording_builder)
    assert (
        cli.run(
            [
                "convert",
                str(input_dir),
                "--output",
                str(output_dir),
                "--metadata-sidecar",
                "--manifest-json",
                str(manifest_path),
            ],
            converter_factory=_factory(StaticConverter()),
        )
        == 0
    )
    manifest_item = _read_json(manifest_path)["items"][0]  # type: ignore[index]
    sidecar = _read_json(output_dir / "one.metadata.json")

    assert calls == 1
    assert sidecar["document"]["input_path"] == manifest_item["input"]["path"]  # type: ignore[index]
    assert sidecar["document"]["output_path"] == manifest_item["output"]["path"]  # type: ignore[index]
    assert sidecar["source"] == {
        "bytes": manifest_item["input"]["bytes"],  # type: ignore[index]
        "sha256": manifest_item["input"]["sha256"],  # type: ignore[index]
    }
    assert sidecar["artifact"] == {
        "bytes": manifest_item["output"]["bytes"],  # type: ignore[index]
        "sha256": manifest_item["output"]["sha256"],  # type: ignore[index]
    }


def test_sidecar_write_failure_is_safe_and_other_reports_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: object
) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    report = tmp_path / "batch.json"
    _pdf(input_dir / "one.pdf")

    def failing_writer(path: Path, sidecar: object) -> None:
        raise OSError(f"cannot write {tmp_path} Traceback")

    monkeypatch.setattr(cli, "write_document_metadata", failing_writer)
    exit_code = cli.run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            "--metadata-sidecar",
            "--report-json",
            str(report),
        ],
        converter_factory=_factory(StaticConverter()),
    )
    stderr = capsys.readouterr().err  # type: ignore[attr-defined]

    assert exit_code == 2
    report_payload = _read_json(report)
    assert report_payload["exit_code"] == 2
    assert report_payload["items"][0]["status"] == "succeeded"  # type: ignore[index]
    assert stderr == "Metadata sidecarを書き込めませんでした。\n"
    assert str(tmp_path) not in stderr
    assert "Traceback" not in stderr


def test_one_sidecar_failure_does_not_stop_later_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    _pdf(input_dir / "a.pdf")
    _pdf(input_dir / "b.pdf")
    original_writer = cli.write_document_metadata

    def conditional_writer(path: Path, sidecar: object) -> None:
        if path.name == "a.metadata.json":
            raise OSError("synthetic failure")
        original_writer(path, sidecar)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "write_document_metadata", conditional_writer)
    exit_code = cli.run(
        ["convert", str(input_dir), "--output", str(output_dir), "--metadata-sidecar"],
        converter_factory=_factory(StaticConverter()),
    )

    assert exit_code == 2
    assert not (output_dir / "a.metadata.json").exists()
    assert (output_dir / "b.metadata.json").is_file()


def test_batch_report_path_conflict_is_rejected_before_converter(
    tmp_path: Path, capsys: object
) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    _pdf(input_dir / "one.pdf")
    converter = StaticConverter()

    exit_code = cli.run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            "--metadata-sidecar",
            "--report-json",
            str(output_dir / "one.metadata.json"),
        ],
        converter_factory=_factory(converter),
    )

    assert exit_code == 2
    assert converter.inputs == []
    assert "競合" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_single_sidecar_report_conflict_is_rejected_before_converter(tmp_path: Path) -> None:
    source = tmp_path / "one.pdf"
    output = tmp_path / "one.md"
    _pdf(source)
    converter = StaticConverter()

    assert (
        cli.run(
            [
                "convert",
                str(source),
                "--output",
                str(output),
                "--metadata-sidecar",
                "--quality-report-json",
                str(metadata_sidecar_path(output)),
            ],
            converter_factory=_factory(converter),
        )
        == 2
    )
    assert converter.inputs == []


def test_sidecar_collision_logic_is_case_insensitive_and_nfc(tmp_path: Path) -> None:
    first = cli.ConversionRequest(tmp_path / "A.pdf", tmp_path / "é.md")
    second = cli.ConversionRequest(tmp_path / "B.pdf", tmp_path / "E\u0301.md")

    assert cli._metadata_sidecars_conflict((first, second), ())


def test_existing_batch_csv_quality_and_manifest_schemas_are_unchanged(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    _pdf(input_dir / "one.pdf")
    batch_json = tmp_path / "batch.json"
    batch_csv = tmp_path / "batch.csv"
    quality_json = tmp_path / "quality.json"
    manifest_json = tmp_path / "manifest.json"

    assert (
        cli.run(
            [
                "convert",
                str(input_dir),
                "--output",
                str(output_dir),
                "--metadata-sidecar",
                "--report-json",
                str(batch_json),
                "--report-csv",
                str(batch_csv),
                "--quality-report-json",
                str(quality_json),
                "--manifest-json",
                str(manifest_json),
            ],
            converter_factory=_factory(StaticConverter()),
        )
        == 0
    )
    batch = _read_json(batch_json)
    quality = _read_json(quality_json)
    manifest = _read_json(manifest_json)
    with batch_csv.open(encoding="utf-8-sig", newline="") as csv_file:
        rows = list(csv.DictReader(csv_file))

    assert set(batch) == {"schema_version", "summary", "exit_code", "items"}
    assert list(rows[0]) == ["input", "output", "status", "error_category", "message"]
    assert quality["report_type"] == "markdown-quality"
    assert quality["schema_version"] == 1
    assert manifest["report_type"] == "knowledge-artifact-manifest"
    assert manifest["schema_version"] == 1
