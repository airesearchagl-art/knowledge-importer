from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from knowledge_importer.cli import run
from knowledge_importer.markdown_normalization import (
    normalize_markdown,
    normalize_markdown_file,
)


class StaticConverter:
    def __init__(self, markdown: str) -> None:
        self.markdown = markdown
        self.inputs: list[Path] = []

    def convert(self, input_path: Path) -> str:
        self.inputs.append(input_path)
        return self.markdown


def _pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n% synthetic fixture\n")


def _factory(converter: StaticConverter):
    return lambda do_table_structure: converter


def test_conservative_normalizes_bom_newlines_trailing_whitespace_and_eof() -> None:
    source = "\ufeff# Heading \r\n\rParagraph\t\r\nHard break  \r\n\r\n\r\n"

    assert normalize_markdown(source) == "# Heading\n\nParagraph\nHard break  \n"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "```python\r\nvalue = 1  \r\n  indented\t\r\n```\r\n",
            "```python\nvalue = 1  \n  indented\t\n```\n",
        ),
        ("~~~~ lang\nvalue  \n~~~\n~~~~\n", "~~~~ lang\nvalue  \n~~~\n~~~~\n"),
        ("   ```\n  value \t\n   ````\n", "   ```\n  value \t\n   ````\n"),
    ],
)
def test_conservative_preserves_fenced_code_content(source: str, expected: str) -> None:
    assert normalize_markdown(source) == expected


def test_conservative_preserves_markdown_structures_and_unicode() -> None:
    source = (
        "# 見出し\n\n"
        "- 箇条書き\n"
        "1. 番号付き\n\n"
        "| 列 A | 列 B |\n"
        "| :--- | ---: |\n"
        "| `値` | [リンク](https://example.invalid/a) |\n\n"
        "e\u0301 と é  \n"
    )

    assert normalize_markdown(source) == source


def test_conservative_is_idempotent() -> None:
    source = "\ufeff本文 \r\n\r\n```\r\ncode  \r\n```\r\n\r\n"
    once = normalize_markdown(source)

    assert normalize_markdown(once) == once


def test_unclosed_fence_preserves_trailing_code_blank_lines() -> None:
    source = "```\ncode  \n\n"

    assert normalize_markdown(source) == source


def test_atomic_file_normalization_removes_temporary_file(tmp_path: Path) -> None:
    output = tmp_path / "output.md"
    output.write_text("本文 \r\n\r\n", encoding="utf-8", newline="")

    normalize_markdown_file(output)

    assert output.read_bytes() == "本文\n".encode()
    assert list(tmp_path.glob(".output.md.*.tmp")) == []


def test_invalid_profile_is_rejected_before_converter(tmp_path: Path, capsys: object) -> None:
    source = tmp_path / "input.pdf"
    _pdf(source)
    factory_called = False

    def unexpected_factory(do_table_structure: bool) -> StaticConverter:
        nonlocal factory_called
        factory_called = True
        raise AssertionError("converter must not be created")

    exit_code = run(
        [
            "convert",
            str(source),
            "--output",
            str(tmp_path / "output.md"),
            "--normalize-markdown",
            "unknown",
        ],
        converter_factory=unexpected_factory,
    )
    stderr = capsys.readouterr().err  # type: ignore[attr-defined]

    assert exit_code == 2
    assert not factory_called
    assert "conservative" in stderr
    assert str(tmp_path) not in stderr
    assert "Traceback" not in stderr


def test_default_single_conversion_preserves_converter_bytes(tmp_path: Path) -> None:
    source = tmp_path / "input.pdf"
    output = tmp_path / "output.md"
    markdown = "\ufeff本文 \r\n\r\n"
    _pdf(source)

    assert (
        run(
            ["convert", str(source), "--output", str(output)],
            converter_factory=_factory(StaticConverter(markdown)),
        )
        == 0
    )
    assert output.read_bytes() == markdown.encode("utf-8")


def test_single_normalization_precedes_quality_and_manifest_digest(tmp_path: Path) -> None:
    source = tmp_path / "input.pdf"
    output = tmp_path / "output.md"
    quality = tmp_path / "quality.json"
    manifest = tmp_path / "manifest.json"
    _pdf(source)
    markdown = "\ufeff" + ("架空の本文です。" * 10) + " \r\n\r\n"

    exit_code = run(
        [
            "convert",
            str(source),
            "--output",
            str(output),
            "--normalize-markdown",
            "conservative",
            "--quality-report-json",
            str(quality),
            "--manifest-json",
            str(manifest),
        ],
        converter_factory=_factory(StaticConverter(markdown)),
    )
    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    quality_payload = json.loads(quality.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert output.read_bytes() == (("架空の本文です。" * 10) + "\n").encode()
    assert quality_payload["items"][0]["status"] == "passed"
    assert manifest_payload["settings"]["normalization_profile"] == "conservative"
    assert (
        manifest_payload["items"][0]["output"]["sha256"]
        == hashlib.sha256(output.read_bytes()).hexdigest()
    )


def test_manifest_profile_is_null_when_option_is_omitted(tmp_path: Path) -> None:
    source = tmp_path / "input.pdf"
    manifest = tmp_path / "manifest.json"
    _pdf(source)

    assert (
        run(
            [
                "convert",
                str(source),
                "--output",
                str(tmp_path / "output.md"),
                "--manifest-json",
                str(manifest),
            ],
            converter_factory=_factory(StaticConverter("本文")),
        )
        == 0
    )
    assert (
        json.loads(manifest.read_text(encoding="utf-8"))["settings"]["normalization_profile"]
        is None
    )


def test_batch_recursive_normalizes_selected_outputs(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    selected = input_dir / "section" / "selected.pdf"
    excluded = input_dir / "section" / "excluded.pdf"
    _pdf(selected)
    _pdf(excluded)
    converter = StaticConverter("本文 \r\n\r\n")

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            "--recursive",
            "--include",
            "**/selected.pdf",
            "--normalize-markdown",
            "conservative",
        ],
        converter_factory=_factory(converter),
    )

    assert exit_code == 0
    assert converter.inputs == [selected]
    assert (output_dir / "section" / "selected.md").read_bytes() == "本文\n".encode()
    assert not (output_dir / "section" / "excluded.md").exists()


def test_skipped_output_is_not_normalized_but_force_regenerates_it(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    source = input_dir / "one.pdf"
    output = output_dir / "one.md"
    _pdf(source)
    output.parent.mkdir()
    original = "既存 \r\n\r\n"
    output.write_text(original, encoding="utf-8", newline="")

    skipped = StaticConverter("未使用")
    manifest = tmp_path / "manifest.json"
    assert (
        run(
            [
                "convert",
                str(input_dir),
                "--output",
                str(output_dir),
                "--normalize-markdown",
                "conservative",
                "--manifest-json",
                str(manifest),
            ],
            converter_factory=_factory(skipped),
        )
        == 0
    )
    assert skipped.inputs == []
    assert output.read_bytes() == original.encode()
    assert (
        json.loads(manifest.read_text(encoding="utf-8"))["settings"]["normalization_profile"]
        == "conservative"
    )

    forced = StaticConverter("再生成 \r\n\r\n")
    assert (
        run(
            [
                "convert",
                str(input_dir),
                "--output",
                str(output_dir),
                "--force",
                "--normalize-markdown",
                "conservative",
            ],
            converter_factory=_factory(forced),
        )
        == 0
    )
    assert output.read_bytes() == "再生成\n".encode()


def test_normalization_failure_is_classified_and_batch_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    _pdf(input_dir / "a.pdf")
    _pdf(input_dir / "b.pdf")
    converter = StaticConverter("本文 \r\n")
    calls: list[str] = []

    def recording_normalizer(path: Path, profile: str) -> None:
        calls.append(path.name)
        if path.name == "a.md":
            raise OSError("synthetic output failure")
        normalize_markdown_file(path, profile)

    monkeypatch.setattr("knowledge_importer.cli.normalize_markdown_file", recording_normalizer)
    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            "--normalize-markdown",
            "conservative",
        ],
        converter_factory=_factory(converter),
    )

    assert exit_code == 1
    assert calls == ["a.md", "b.md"]
    assert (output_dir / "b.md").read_bytes() == "本文\n".encode()


def test_failed_conversion_is_not_normalized_and_batch_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    _pdf(input_dir / "a.pdf")
    _pdf(input_dir / "b.pdf")
    normalized: list[str] = []

    class ConditionalConverter:
        def convert(self, input_path: Path) -> str:
            if input_path.name == "a.pdf":
                raise RuntimeError("synthetic conversion failure")
            return "本文 \r\n"

    original_normalizer = normalize_markdown_file

    def recording_normalizer(path: Path, profile: str) -> None:
        normalized.append(path.name)
        original_normalizer(path, profile)

    monkeypatch.setattr("knowledge_importer.cli.normalize_markdown_file", recording_normalizer)
    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            "--normalize-markdown",
            "conservative",
        ],
        converter_factory=lambda do_table_structure: ConditionalConverter(),
    )

    assert exit_code == 1
    assert normalized == ["b.md"]
    assert not (output_dir / "a.md").exists()
    assert (output_dir / "b.md").read_bytes() == "本文\n".encode()


def test_existing_report_schemas_and_batch_status_are_unchanged(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    _pdf(input_dir / "one.pdf")
    batch_json = tmp_path / "batch.json"
    batch_csv = tmp_path / "batch.csv"
    quality_json = tmp_path / "quality.json"
    manifest_json = tmp_path / "manifest.json"

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            "--normalize-markdown",
            "conservative",
            "--report-json",
            str(batch_json),
            "--report-csv",
            str(batch_csv),
            "--quality-report-json",
            str(quality_json),
            "--manifest-json",
            str(manifest_json),
        ],
        converter_factory=_factory(StaticConverter("短文 \r\n")),
    )
    batch = json.loads(batch_json.read_text(encoding="utf-8"))
    quality = json.loads(quality_json.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_json.read_text(encoding="utf-8"))
    with batch_csv.open(encoding="utf-8-sig", newline="") as csv_file:
        csv_rows = list(csv.DictReader(csv_file))

    assert exit_code == 0
    assert set(batch) == {"schema_version", "summary", "exit_code", "items"}
    assert set(batch["items"][0]) == {
        "input",
        "output",
        "status",
        "error_category",
        "message",
    }
    assert batch["items"][0]["status"] == "succeeded"
    assert list(csv_rows[0]) == ["input", "output", "status", "error_category", "message"]
    assert quality["schema_version"] == 1
    assert set(quality["items"][0]) == {"input", "output", "status", "warnings"}
    assert manifest["schema_version"] == 1
    assert manifest["settings"]["normalization_profile"] == "conservative"


def test_output_contract_documents_profile_and_skip_semantics() -> None:
    contract = (Path(__file__).resolve().parents[1] / "docs" / "output-contract.md").read_text(
        encoding="utf-8"
    )

    assert 'conservative指定時は`"conservative"`' in contract
    assert "global requested setting" in contract
    assert "skipされた場合はartifact非変更" in contract
