from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

import knowledge_importer.cli as cli
from knowledge_importer.artifact_manifest import (
    ArtifactDigest,
    ArtifactManifest,
    ArtifactManifestItem,
    ArtifactManifestSettings,
    ManifestStatus,
    build_manifest_item,
    digest_file,
    write_artifact_manifest,
)


class RecordingConverter:
    def __init__(self, failing_names: set[str] | None = None) -> None:
        self.failing_names = failing_names or set()
        self.inputs: list[Path] = []

    def convert(self, input_path: Path) -> str:
        self.inputs.append(input_path)
        if input_path.name in self.failing_names:
            raise RuntimeError(f"synthetic failure for {input_path.name}")
        return f"# {input_path.stem}\n\nSynthetic Markdown body.\n"


def _create_pdf(path: Path, content: bytes = b"%PDF-1.4\nsynthetic\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _factory(converter: RecordingConverter):
    def create(*args: object) -> RecordingConverter:
        return converter

    return create


def test_digest_file_uses_file_bytes_and_lowercase_sha256(tmp_path: Path) -> None:
    path = tmp_path / "artifact.bin"
    content = b"one\r\ntwo\x00three"
    path.write_bytes(content)

    result = digest_file(path)

    assert result == ArtifactDigest(len(content), hashlib.sha256(content).hexdigest())
    assert result.sha256 is not None
    assert len(result.sha256) == 64
    assert result.sha256 == result.sha256.lower()


def test_digest_file_reads_in_bounded_chunks() -> None:
    requested_sizes: list[int] = []

    class TrackingBytesIO(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            requested_sizes.append(size)
            return super().read(size)

    class MemoryPath:
        def open(self, mode: str) -> TrackingBytesIO:
            assert mode == "rb"
            return TrackingBytesIO(b"x" * (1024 * 1024 + 7))

    result = digest_file(MemoryPath())  # type: ignore[arg-type]

    assert result.bytes == 1024 * 1024 + 7
    assert requested_sizes
    assert set(requested_sizes) == {1024 * 1024}


def test_failed_item_uses_optional_input_and_null_output_digest(tmp_path: Path) -> None:
    input_path = tmp_path / "failed.pdf"
    _create_pdf(input_path)

    item = build_manifest_item(
        input_path=input_path,
        output_path=tmp_path / "partial.md",
        input_name="failed.pdf",
        output_name="failed.md",
        status=ManifestStatus.FAILED,
        error_category="converter生成・変換処理関連",
        message="RuntimeError: synthetic failure",
    )

    assert item.input_digest.bytes == input_path.stat().st_size
    assert item.input_digest.sha256 is not None
    assert item.output_digest == ArtifactDigest(None, None)


def test_failed_item_allows_unreadable_or_missing_input_digest(tmp_path: Path) -> None:
    item = build_manifest_item(
        input_path=tmp_path / "missing.pdf",
        output_path=tmp_path / "missing.md",
        input_name="missing.pdf",
        output_name="missing.md",
        status=ManifestStatus.FAILED,
    )

    assert item.input_digest == ArtifactDigest(None, None)
    assert item.output_digest == ArtifactDigest(None, None)


def test_manifest_payload_has_fixed_schema_and_summary() -> None:
    digest = ArtifactDigest(3, "a" * 64)
    manifest = ArtifactManifest(
        settings=ArtifactManifestSettings(force=True),
        items=(
            ArtifactManifestItem("one.pdf", "one.md", ManifestStatus.SUCCEEDED, digest, digest),
            ArtifactManifestItem("two.pdf", "two.md", ManifestStatus.SKIPPED, digest, digest),
            ArtifactManifestItem(
                "three.pdf",
                "three.md",
                ManifestStatus.FAILED,
                digest,
                ArtifactDigest(None, None),
                "converter生成・変換処理関連",
                "RuntimeError: failed",
            ),
        ),
    )

    payload = manifest.payload()

    assert payload["report_type"] == "knowledge-artifact-manifest"
    assert payload["schema_version"] == 1
    assert payload["engine"] == {"name": "knowledge-importer", "version": "0.1.0"}
    assert payload["summary"] == {"items": 3, "succeeded": 1, "skipped": 1, "failed": 1}
    assert payload["settings"] == {
        "recursive": False,
        "include": [],
        "exclude": [],
        "force": True,
        "table_structure": False,
        "artifacts_path_configured": False,
        "normalization_profile": None,
    }


def test_manifest_writer_is_deterministic_and_replaces_atomically(tmp_path: Path) -> None:
    path = tmp_path / "reports" / "manifest.json"
    manifest = ArtifactManifest(ArtifactManifestSettings(), ())

    write_artifact_manifest(path, manifest)
    first = path.read_bytes()
    path.write_text("old", encoding="utf-8")
    write_artifact_manifest(path, manifest)

    assert path.read_bytes() == first
    assert first.endswith(b"\n")
    assert not list(path.parent.glob(".*.tmp"))


def test_cli_help_includes_manifest_option(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.build_parser().parse_args(["convert", "--help"])

    assert exc_info.value.code == 0
    assert "--manifest-json PATH" in capsys.readouterr().out


def test_omitted_manifest_does_not_calculate_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "one.pdf"
    _create_pdf(source)
    converter = RecordingConverter()

    def unexpected(*args: object, **kwargs: object) -> ArtifactManifestItem:
        raise AssertionError("manifest digest must remain opt-in")

    monkeypatch.setattr(cli, "build_manifest_item", unexpected)

    exit_code = cli.run(
        ["convert", str(source), "--output", str(tmp_path / "one.md")],
        converter_factory=_factory(converter),
    )

    assert exit_code == 0
    assert len(converter.inputs) == 1


def test_single_success_manifest_uses_filenames_hashes_and_settings(tmp_path: Path) -> None:
    source = tmp_path / "input" / "one.pdf"
    output = tmp_path / "output" / "one.md"
    manifest_path = tmp_path / "reports" / "manifest.json"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    _create_pdf(source, b"%PDF single bytes")

    exit_code = cli.run(
        [
            "convert",
            str(source),
            "--output",
            str(output),
            "--table-structure",
            "--artifacts-path",
            str(artifacts),
            "--manifest-json",
            str(manifest_path),
        ],
        converter_factory=_factory(RecordingConverter()),
    )

    report = _read_json(manifest_path)
    item = report["items"][0]  # type: ignore[index]
    assert exit_code == 0
    assert item["input"] == {  # type: ignore[index]
        "path": "one.pdf",
        "bytes": source.stat().st_size,
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    assert item["output"] == {  # type: ignore[index]
        "path": "one.md",
        "bytes": output.stat().st_size,
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }
    assert report["settings"] == {
        "recursive": False,
        "include": [],
        "exclude": [],
        "force": False,
        "table_structure": True,
        "artifacts_path_configured": True,
        "normalization_profile": None,
    }
    assert str(artifacts) not in manifest_path.read_text(encoding="utf-8")


def test_batch_recursive_manifest_is_relative_ordered_and_records_cli_settings(
    tmp_path: Path,
) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    manifest_path = tmp_path / "manifest.json"
    _create_pdf(input_dir / "section" / "b.pdf", b"b")
    _create_pdf(input_dir / "a.pdf", b"a")

    exit_code = cli.run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            "--recursive",
            "--include",
            "*.pdf",
            "--include",
            "section/**/*.pdf",
            "--exclude",
            "archive/**",
            "--force",
            "--manifest-json",
            str(manifest_path),
        ],
        converter_factory=_factory(RecordingConverter()),
    )

    report = _read_json(manifest_path)
    assert exit_code == 0
    assert [item["input"]["path"] for item in report["items"]] == [  # type: ignore[index]
        "a.pdf",
        "section/b.pdf",
    ]
    assert [item["output"]["path"] for item in report["items"]] == [  # type: ignore[index]
        "a.md",
        "section/b.md",
    ]
    assert report["settings"] == {
        "recursive": True,
        "include": ["*.pdf", "section/**/*.pdf"],
        "exclude": ["archive/**"],
        "force": True,
        "table_structure": False,
        "artifacts_path_configured": False,
        "normalization_profile": None,
    }


def test_batch_skip_manifest_hashes_existing_output_without_converter(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    source = input_dir / "one.pdf"
    output = output_dir / "one.md"
    _create_pdf(source)
    output.parent.mkdir()
    output.write_text("existing markdown", encoding="utf-8")

    def unexpected_factory(*args: object) -> RecordingConverter:
        raise AssertionError("converter must not be built for skipped items")

    manifest_path = tmp_path / "manifest.json"
    exit_code = cli.run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            "--manifest-json",
            str(manifest_path),
        ],
        converter_factory=unexpected_factory,
    )

    item = _read_json(manifest_path)["items"][0]  # type: ignore[index]
    assert exit_code == 0
    assert item["status"] == "skipped"  # type: ignore[index]
    assert item["input"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()  # type: ignore[index]
    assert item["output"]["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()  # type: ignore[index]


def test_partial_failure_manifest_preserves_status_and_safe_error(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    _create_pdf(input_dir / "bad.pdf")
    _create_pdf(input_dir / "good.pdf")
    manifest_path = tmp_path / "manifest.json"

    exit_code = cli.run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--manifest-json",
            str(manifest_path),
        ],
        converter_factory=_factory(RecordingConverter({"bad.pdf"})),
    )

    report = _read_json(manifest_path)
    failed, succeeded = report["items"]  # type: ignore[misc]
    assert exit_code == 1
    assert report["summary"] == {"items": 2, "succeeded": 1, "skipped": 0, "failed": 1}
    assert failed["status"] == "failed"
    assert failed["output"]["bytes"] is None
    assert failed["output"]["sha256"] is None
    assert failed["error_category"] == "converter生成・変換処理関連"
    assert failed["message"].startswith("RuntimeError:")
    assert succeeded["status"] == "succeeded"
    text = manifest_path.read_text(encoding="utf-8")
    assert str(tmp_path) not in text
    assert "Traceback" not in text


def test_all_factory_failures_still_generate_manifest(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    _create_pdf(input_dir / "one.pdf")
    _create_pdf(input_dir / "two.pdf")
    manifest_path = tmp_path / "manifest.json"

    def failing_factory(*args: object) -> RecordingConverter:
        raise RuntimeError("synthetic factory failure")

    exit_code = cli.run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--manifest-json",
            str(manifest_path),
        ],
        converter_factory=failing_factory,
    )

    report = _read_json(manifest_path)
    assert exit_code == 1
    assert report["summary"] == {"items": 2, "succeeded": 0, "skipped": 0, "failed": 2}
    assert all(item["status"] == "failed" for item in report["items"])  # type: ignore[union-attr]


@pytest.mark.parametrize("filtered", [False, True])
def test_zero_target_batch_writes_empty_manifest(tmp_path: Path, filtered: bool) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    args = [
        "convert",
        str(input_dir),
        "--output",
        str(tmp_path / "output"),
        "--manifest-json",
        str(tmp_path / "manifest.json"),
    ]
    if filtered:
        _create_pdf(input_dir / "ignored.pdf")
        args.extend(["--exclude", "*.pdf"])

    exit_code = cli.run(args, converter_factory=_factory(RecordingConverter()))

    report = _read_json(tmp_path / "manifest.json")
    assert exit_code == (0 if filtered else 2)
    assert report["summary"] == {"items": 0, "succeeded": 0, "skipped": 0, "failed": 0}
    assert report["items"] == []


def test_manifest_and_existing_reports_share_one_conversion_without_schema_changes(
    tmp_path: Path,
) -> None:
    input_dir = tmp_path / "input"
    _create_pdf(input_dir / "one.pdf")
    converter = RecordingConverter()
    batch_path = tmp_path / "batch.json"
    csv_path = tmp_path / "batch.csv"
    quality_path = tmp_path / "quality.json"
    manifest_path = tmp_path / "manifest.json"

    exit_code = cli.run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--report-json",
            str(batch_path),
            "--report-csv",
            str(csv_path),
            "--quality-report-json",
            str(quality_path),
            "--manifest-json",
            str(manifest_path),
        ],
        converter_factory=_factory(converter),
    )

    batch = _read_json(batch_path)
    quality = _read_json(quality_path)
    assert exit_code == 0
    assert len(converter.inputs) == 1
    assert batch["schema_version"] == 1
    assert set(batch) == {"schema_version", "summary", "exit_code", "items"}
    assert csv_path.read_text(encoding="utf-8-sig").splitlines()[0] == (
        "input,output,status,error_category,message"
    )
    assert quality["report_type"] == "markdown-quality"
    assert quality["schema_version"] == 1
    assert _read_json(manifest_path)["schema_version"] == 1


@pytest.mark.parametrize("other_option", ["--report-json", "--report-csv", "--quality-report-json"])
def test_manifest_report_path_collision_is_rejected_before_conversion(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    other_option: str,
) -> None:
    input_dir = tmp_path / "input"
    _create_pdf(input_dir / "one.pdf")
    converter = RecordingConverter()
    shared = tmp_path / "shared.json"

    exit_code = cli.run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--manifest-json",
            str(shared),
            other_option,
            str(shared),
        ],
        converter_factory=_factory(converter),
    )

    assert exit_code == 2
    assert converter.inputs == []
    assert str(shared) not in capsys.readouterr().err


@pytest.mark.parametrize("batch", [False, True])
def test_manifest_markdown_path_collision_is_rejected_before_conversion(
    tmp_path: Path, batch: bool
) -> None:
    converter = RecordingConverter()
    if batch:
        input_path = tmp_path / "input"
        _create_pdf(input_path / "one.pdf")
        output_path = tmp_path / "output"
        manifest_path = output_path / "one.md"
    else:
        input_path = tmp_path / "one.pdf"
        _create_pdf(input_path)
        output_path = tmp_path / "one.md"
        manifest_path = output_path

    exit_code = cli.run(
        [
            "convert",
            str(input_path),
            "--output",
            str(output_path),
            "--manifest-json",
            str(manifest_path),
        ],
        converter_factory=_factory(converter),
    )

    assert exit_code == 2
    assert converter.inputs == []


def test_repeated_force_execution_produces_byte_identical_manifest(tmp_path: Path) -> None:
    source = tmp_path / "one.pdf"
    output = tmp_path / "one.md"
    manifest_path = tmp_path / "manifest.json"
    _create_pdf(source)
    args = [
        "convert",
        str(source),
        "--output",
        str(output),
        "--force",
        "--manifest-json",
        str(manifest_path),
    ]

    assert cli.run(args, converter_factory=_factory(RecordingConverter())) == 0
    first = manifest_path.read_bytes()
    assert cli.run(args, converter_factory=_factory(RecordingConverter())) == 0

    assert manifest_path.read_bytes() == first


def test_manifest_hashes_change_with_input_and_output_bytes(tmp_path: Path) -> None:
    input_path = tmp_path / "one.pdf"
    output_path = tmp_path / "one.md"
    _create_pdf(input_path, b"first")
    output_path.write_bytes(b"output-one")
    first = build_manifest_item(
        input_path=input_path,
        output_path=output_path,
        input_name="one.pdf",
        output_name="one.md",
        status=ManifestStatus.SUCCEEDED,
    )
    input_path.write_bytes(b"second")
    output_path.write_bytes(b"output-two")
    second = build_manifest_item(
        input_path=input_path,
        output_path=output_path,
        input_name="one.pdf",
        output_name="one.md",
        status=ManifestStatus.SUCCEEDED,
    )

    assert first.input_digest.sha256 != second.input_digest.sha256
    assert first.output_digest.sha256 != second.output_digest.sha256


def test_manifest_write_failure_returns_two_and_other_reports_continue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    input_dir = tmp_path / "input"
    _create_pdf(input_dir / "one.pdf")
    manifest_path = tmp_path / "manifest-directory"
    manifest_path.mkdir()
    batch_path = tmp_path / "batch.json"
    csv_path = tmp_path / "batch.csv"

    exit_code = cli.run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--manifest-json",
            str(manifest_path),
            "--report-json",
            str(batch_path),
            "--report-csv",
            str(csv_path),
        ],
        converter_factory=_factory(RecordingConverter()),
    )

    stderr = capsys.readouterr().err
    assert exit_code == 2
    assert _read_json(batch_path)["exit_code"] == 2
    assert csv_path.is_file()
    assert stderr.strip() == "Artifact Manifestを書き込めませんでした。"
    assert str(tmp_path) not in stderr
    assert "Traceback" not in stderr


def test_single_conversion_failure_generates_failed_manifest(tmp_path: Path) -> None:
    source = tmp_path / "one.pdf"
    _create_pdf(source)
    manifest_path = tmp_path / "manifest.json"

    exit_code = cli.run(
        [
            "convert",
            str(source),
            "--output",
            str(tmp_path / "one.md"),
            "--manifest-json",
            str(manifest_path),
        ],
        converter_factory=_factory(RecordingConverter({"one.pdf"})),
    )

    item = _read_json(manifest_path)["items"][0]  # type: ignore[index]
    assert exit_code == 1
    assert item["status"] == "failed"  # type: ignore[index]
    assert item["input"]["sha256"] is not None  # type: ignore[index]
    assert item["output"] == {"path": "one.md", "bytes": None, "sha256": None}  # type: ignore[index]


def test_input_validation_error_does_not_generate_manifest(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"

    exit_code = cli.run(
        [
            "convert",
            str(tmp_path / "missing.pdf"),
            "--output",
            str(tmp_path / "missing.md"),
            "--manifest-json",
            str(manifest_path),
        ],
        converter_factory=_factory(RecordingConverter()),
    )

    assert exit_code == 2
    assert not manifest_path.exists()


def test_manifest_atomic_replace_failure_preserves_existing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "one.pdf"
    _create_pdf(source)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("original", encoding="utf-8")
    original_replace = Path.replace

    def failing_replace(path: Path, target: Path) -> Path:
        if target == manifest_path:
            raise OSError("synthetic replace failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", failing_replace)

    exit_code = cli.run(
        [
            "convert",
            str(source),
            "--output",
            str(tmp_path / "one.md"),
            "--manifest-json",
            str(manifest_path),
        ],
        converter_factory=_factory(RecordingConverter()),
    )

    assert exit_code == 2
    assert manifest_path.read_text(encoding="utf-8") == "original"
    assert not list(tmp_path.glob(".manifest.json.*.tmp"))


def test_checksum_failure_preserves_manifest_and_still_writes_batch_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir = tmp_path / "input"
    _create_pdf(input_dir / "one.pdf")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("original", encoding="utf-8")
    batch_path = tmp_path / "batch.json"

    def failing_item(*args: object, **kwargs: object) -> ArtifactManifestItem:
        raise PermissionError("synthetic checksum failure")

    monkeypatch.setattr(cli, "build_manifest_item", failing_item)

    exit_code = cli.run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--manifest-json",
            str(manifest_path),
            "--report-json",
            str(batch_path),
        ],
        converter_factory=_factory(RecordingConverter()),
    )

    assert exit_code == 2
    assert manifest_path.read_text(encoding="utf-8") == "original"
    assert _read_json(batch_path)["exit_code"] == 2
