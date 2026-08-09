import csv
import json
import logging
from pathlib import Path

import pytest

from knowledge_importer.cli import (
    BatchFailureCategory,
    BatchItemStatus,
    BatchResult,
    BatchResultItem,
    _build_batch_requests,
    _find_pdf_files,
    _matches_posix_glob,
    _write_batch_csv,
    build_parser,
    run,
)
from knowledge_importer.converter import Converter
from knowledge_importer.models import ConversionRequest, KnowledgeImporterError


class FakeConverter:
    def convert(self, input_path: Path) -> str:
        return "# Converted\n\nSynthetic content.\n"


class RecordingConverter:
    def __init__(self, *, failing_names: set[str] | None = None) -> None:
        self.failing_names = failing_names or set()
        self.inputs: list[Path] = []

    def convert(self, input_path: Path) -> str:
        self.inputs.append(input_path)
        if input_path.name in self.failing_names:
            raise RuntimeError(f"synthetic failure for {input_path.name}")
        return f"# {input_path.stem}\n"


class ContentConverter:
    def __init__(self, outputs: dict[str, str]) -> None:
        self.outputs = outputs
        self.inputs: list[Path] = []

    def convert(self, input_path: Path) -> str:
        self.inputs.append(input_path)
        return self.outputs[input_path.name]


def fake_converter_factory(do_table_structure: bool = False) -> FakeConverter:
    return FakeConverter()


def _create_pdf_tree(root: Path, relative_paths: tuple[str, ...]) -> None:
    for relative_path in relative_paths:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4\n")


def _read_json_report(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_csv_report(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as report_file:
        return list(csv.DictReader(report_file))


def test_help_describes_convert_command() -> None:
    help_text = build_parser().format_help()

    assert "convert" in help_text
    assert "knowledge-importer" in help_text


def test_convert_help_describes_directory_options(capsys: object) -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["convert", "--help"])

    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "--table-structure" in help_text
    assert "--artifacts-path PATH" in help_text
    assert "--recursive" in help_text
    assert "--include" in help_text
    assert "--exclude" in help_text
    assert "--report-json" in help_text
    assert "--report-csv" in help_text
    assert "--quality-warnings" in help_text
    assert "--quality-report-json" in help_text
    assert "--manifest-json" in help_text
    assert "--normalize-markdown PROFILE" in help_text


def test_convert_command_uses_injected_converter(tmp_path: Path) -> None:
    source = tmp_path / "fixture.pdf"
    source.write_bytes(b"%PDF-1.4\n% synthetic fixture\n")
    output = tmp_path / "result.md"

    exit_code = run(
        ["convert", str(source), "--output", str(output)],
        converter_factory=fake_converter_factory,
    )

    assert exit_code == 0
    assert output.read_text(encoding="utf-8").startswith("# Converted")


@pytest.mark.parametrize(
    ("extra_args", "expected_table_structure"),
    [([], False), (["--table-structure"], True)],
)
def test_convert_passes_table_structure_setting_to_factory(
    tmp_path: Path,
    extra_args: list[str],
    expected_table_structure: bool,
) -> None:
    source = tmp_path / "fixture.pdf"
    source.write_bytes(b"%PDF-1.4\n% synthetic fixture\n")
    output = tmp_path / "result.md"
    received: list[bool] = []

    def recording_factory(do_table_structure: bool) -> FakeConverter:
        received.append(do_table_structure)
        return FakeConverter()

    exit_code = run(
        [
            "convert",
            str(source),
            "--output",
            str(output),
            *extra_args,
        ],
        converter_factory=recording_factory,
    )

    assert exit_code == 0
    assert received == [expected_table_structure]


@pytest.mark.parametrize("is_batch", [False, True])
def test_convert_passes_local_artifacts_path_to_factory(
    tmp_path: Path,
    is_batch: bool,
) -> None:
    artifacts_path = tmp_path / "local-artifacts"
    artifacts_path.mkdir()
    input_path = tmp_path / ("input" if is_batch else "fixture.pdf")
    output_path = tmp_path / ("output" if is_batch else "result.md")
    if is_batch:
        input_path.mkdir()
        (input_path / "fixture.pdf").write_bytes(b"%PDF-1.4\n")
    else:
        input_path.write_bytes(b"%PDF-1.4\n")
    received: list[tuple[bool, Path]] = []

    def recording_factory(
        do_table_structure: bool,
        local_artifacts_path: Path,
    ) -> FakeConverter:
        received.append((do_table_structure, local_artifacts_path))
        return FakeConverter()

    exit_code = run(
        [
            "convert",
            str(input_path),
            "--output",
            str(output_path),
            "--table-structure",
            "--artifacts-path",
            str(artifacts_path),
        ],
        converter_factory=recording_factory,
    )

    assert exit_code == 0
    assert received == [(True, artifacts_path)]


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_convert_rejects_invalid_local_artifacts_path_without_exposing_it(
    tmp_path: Path,
    capsys: object,
    kind: str,
) -> None:
    source = tmp_path / "fixture.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    artifacts_path = tmp_path / "private-model-location"
    if kind == "file":
        artifacts_path.write_text("not a directory", encoding="utf-8")

    exit_code = run(
        [
            "convert",
            str(source),
            "--output",
            str(tmp_path / "result.md"),
            "--artifacts-path",
            str(artifacts_path),
        ],
        converter_factory=fake_converter_factory,
    )

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert exit_code == 2
    assert "存在するローカルディレクトリ" in captured.err
    assert str(artifacts_path) not in captured.err
    assert captured.out == ""


def test_local_artifacts_path_is_not_added_to_batch_report(
    tmp_path: Path,
) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "fixture.pdf").write_bytes(b"%PDF-1.4\n")
    output_dir = tmp_path / "output"
    artifacts_path = tmp_path / "private-model-location"
    artifacts_path.mkdir()
    report_path = tmp_path / "report.json"

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            "--artifacts-path",
            str(artifacts_path),
            "--report-json",
            str(report_path),
        ],
        converter_factory=lambda do_table_structure, local_artifacts_path: FakeConverter(),
    )

    report_text = report_path.read_text(encoding="utf-8")
    assert exit_code == 0
    assert str(artifacts_path) not in report_text
    assert _read_json_report(report_path) == {
        "schema_version": 1,
        "summary": {"total": 1, "succeeded": 1, "failed": 0, "skipped": 0},
        "exit_code": 0,
        "items": [
            {
                "input": "fixture.pdf",
                "output": "fixture.md",
                "status": "succeeded",
                "error_category": None,
                "message": None,
            }
        ],
    }


def test_missing_input_returns_nonzero(tmp_path: Path, capsys: object) -> None:
    exit_code = run(
        ["convert", str(tmp_path / "missing.pdf"), "--output", str(tmp_path / "out.md")],
        converter_factory=fake_converter_factory,
    )

    assert exit_code != 0
    assert "存在しません" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_non_pdf_returns_nonzero(tmp_path: Path, capsys: object) -> None:
    source = tmp_path / "fixture.txt"
    source.write_text("synthetic", encoding="utf-8")

    exit_code = run(
        ["convert", str(source), "--output", str(tmp_path / "out.md")],
        converter_factory=fake_converter_factory,
    )

    assert exit_code != 0
    assert "PDFではありません" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_directory_converts_direct_pdfs_in_stable_order(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "Beta.pdf").write_bytes(b"%PDF-1.4\n")
    (input_dir / "alpha.PDF").write_bytes(b"%PDF-1.4\n")
    (input_dir / "notes.txt").write_text("ignored", encoding="utf-8")
    nested = input_dir / "nested"
    nested.mkdir()
    (nested / "hidden.pdf").write_bytes(b"%PDF-1.4\n")
    output_dir = tmp_path / "output"
    converter = RecordingConverter()
    table_settings: list[bool] = []

    def factory(do_table_structure: bool) -> RecordingConverter:
        table_settings.append(do_table_structure)
        return converter

    exit_code = run(
        ["convert", str(input_dir), "--output", str(output_dir)],
        converter_factory=factory,
    )

    assert exit_code == 0
    assert table_settings == [False]
    assert [path.name for path in converter.inputs] == ["alpha.PDF", "Beta.pdf"]
    assert (output_dir / "alpha.md").read_text(encoding="utf-8") == "# alpha\n"
    assert (output_dir / "Beta.md").read_text(encoding="utf-8") == "# Beta\n"
    assert not (output_dir / "hidden.md").exists()
    assert not (output_dir / "notes.md").exists()


def test_recursive_directory_preserves_structure_and_relative_order(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    (input_dir / "other").mkdir(parents=True)
    (input_dir / "section" / "deep").mkdir(parents=True)
    for relative_path in (
        Path("root.pdf"),
        Path("section") / "a.pdf",
        Path("section") / "deep" / "b.PDF",
        Path("other") / "a.pdf",
    ):
        (input_dir / relative_path).write_bytes(b"%PDF-1.4\n")
    output_dir = tmp_path / "output"
    converter = RecordingConverter()

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            "--recursive",
        ],
        converter_factory=lambda do_table_structure: converter,
    )

    assert exit_code == 0
    assert [path.relative_to(input_dir).as_posix() for path in converter.inputs] == [
        "other/a.pdf",
        "root.pdf",
        "section/a.pdf",
        "section/deep/b.PDF",
    ]
    assert (output_dir / "root.md").is_file()
    assert (output_dir / "other" / "a.md").is_file()
    assert (output_dir / "section" / "a.md").is_file()
    assert (output_dir / "section" / "deep" / "b.md").is_file()


def test_recursive_directory_skip_and_force_use_nested_output(
    tmp_path: Path,
    capsys: object,
) -> None:
    input_dir = tmp_path / "input"
    source = input_dir / "section" / "item.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"%PDF-1.4\n")
    output_dir = tmp_path / "output"
    existing_output = output_dir / "section" / "item.md"
    existing_output.parent.mkdir(parents=True)
    existing_output.write_text("keep", encoding="utf-8")

    skipped_converter = RecordingConverter()
    skipped_exit = run(
        ["convert", str(input_dir), "--output", str(output_dir), "--recursive"],
        converter_factory=lambda do_table_structure: skipped_converter,
    )

    skipped_output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert skipped_exit == 0
    assert skipped_converter.inputs == []
    assert existing_output.read_text(encoding="utf-8") == "keep"
    assert "成功=0 失敗=0 スキップ=1" in skipped_output

    forced_converter = RecordingConverter()
    forced_exit = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            "--recursive",
            "--force",
        ],
        converter_factory=lambda do_table_structure: forced_converter,
    )

    assert forced_exit == 0
    assert forced_converter.inputs == [source]
    assert existing_output.read_text(encoding="utf-8") == "# item\n"


def test_recursive_directory_excludes_output_subtree_inside_input(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "root.pdf").write_bytes(b"%PDF-1.4\n")
    output_dir = input_dir / "generated"
    output_dir.mkdir()
    (output_dir / "must-not-convert.pdf").write_bytes(b"%PDF-1.4\n")
    converter = RecordingConverter()

    exit_code = run(
        ["convert", str(input_dir), "--output", str(output_dir), "--recursive"],
        converter_factory=lambda do_table_structure: converter,
    )

    assert exit_code == 0
    assert converter.inputs == [input_dir / "root.pdf"]
    assert (output_dir / "root.md").is_file()
    assert not (output_dir / "must-not-convert.md").exists()


def test_recursive_directory_continues_after_nested_output_creation_failure(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir = tmp_path / "input"
    (input_dir / "blocked").mkdir(parents=True)
    (input_dir / "blocked" / "fail.pdf").write_bytes(b"%PDF-1.4\n")
    (input_dir / "ok.pdf").write_bytes(b"%PDF-1.4\n")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    original_mkdir = Path.mkdir

    def selective_mkdir(path: Path, *args: object, **kwargs: object) -> None:
        if path == output_dir / "blocked":
            raise PermissionError("synthetic directory creation failure")
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", selective_mkdir)
    converter = RecordingConverter()

    exit_code = run(
        ["convert", str(input_dir), "--output", str(output_dir), "--recursive"],
        converter_factory=lambda do_table_structure: converter,
    )

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert exit_code == 1
    assert [path.relative_to(input_dir).as_posix() for path in converter.inputs] == [
        "blocked/fail.pdf",
        "ok.pdf",
    ]
    assert "ファイル=blocked/fail.pdf 分類=出力競合・書き込み関連" in captured.err
    assert "成功=1 失敗=1 スキップ=0" in captured.out
    assert (output_dir / "ok.md").is_file()


def test_recursive_directory_does_not_descend_into_linked_directory_logic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir = tmp_path / "input"
    linked_dir = input_dir / "linked"
    linked_dir.mkdir(parents=True)
    (input_dir / "root.pdf").write_bytes(b"%PDF-1.4\n")
    (linked_dir / "hidden.pdf").write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(
        "knowledge_importer.cli._is_linked_directory",
        lambda path: path == linked_dir,
    )

    found = _find_pdf_files(input_dir, recursive=True)

    assert found == [input_dir / "root.pdf"]


def test_recursive_directory_does_not_follow_real_symlink_when_supported(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    (external_dir / "outside.pdf").write_bytes(b"%PDF-1.4\n")
    link = input_dir / "linked"
    try:
        link.symlink_to(external_dir, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not permitted in this environment")

    found = _find_pdf_files(input_dir, recursive=True)

    assert found == []


def test_recursive_failure_uses_relative_paths_without_traceback(
    tmp_path: Path,
    capsys: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    input_dir = tmp_path / "input"
    source = input_dir / "section" / "broken.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"%PDF-1.4\n")
    converter = RecordingConverter(failing_names={"broken.pdf"})

    with caplog.at_level(logging.ERROR, logger="knowledge_importer"):
        exit_code = run(
            [
                "convert",
                str(input_dir),
                "--output",
                str(tmp_path / "output"),
                "--recursive",
            ],
            converter_factory=lambda do_table_structure: converter,
        )

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert exit_code == 1
    assert "ファイル=section/broken.pdf" in captured.err
    assert str(tmp_path) not in captured.err
    assert "Traceback" not in captured.err
    assert str(tmp_path) not in log_text
    assert "Traceback" not in log_text
    assert all(record.exc_info is None for record in caplog.records)


def test_posix_glob_matches_relative_path_segments_case_insensitively() -> None:
    assert _matches_posix_glob(Path("docs/a.pdf"), "docs/**/*.pdf")
    assert _matches_posix_glob(Path("docs/deep/a.PDF"), "DOCS/**/*.pdf")
    assert _matches_posix_glob(Path("manual/guide.PDF"), "manual/*.pdf")
    assert _matches_posix_glob(Path("tmp/a.pdf"), "**/tmp/*")
    assert _matches_posix_glob(Path("docs/tmp/a.pdf"), "**/tmp/*")
    assert not _matches_posix_glob(Path("docs/a.pdf"), "*.pdf")
    assert not _matches_posix_glob(Path("nested/manual/a.pdf"), "manual/*.pdf")


def test_recursive_overlapping_includes_convert_match_once_and_ignore_mismatch(
    tmp_path: Path,
) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("docs/selected.pdf", "manual/ignored.pdf"))
    output_dir = tmp_path / "output"
    converter = RecordingConverter()

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            "--recursive",
            "--include",
            "docs/*.pdf",
            "--include",
            "**/selected.pdf",
        ],
        converter_factory=lambda do_table_structure: converter,
    )

    generated = sorted(path.relative_to(output_dir).as_posix() for path in output_dir.rglob("*.md"))
    assert exit_code == 0
    assert converter.inputs == [input_dir / "docs" / "selected.pdf"]
    assert len(converter.inputs) == 1
    assert generated == ["docs/selected.md"]


def test_recursive_exclude_prevents_converter_call_and_output(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("docs/excluded.pdf", "docs/keep.pdf"))
    output_dir = tmp_path / "output"
    converter = RecordingConverter()

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            "--recursive",
            "--exclude",
            "docs/excluded.pdf",
        ],
        converter_factory=lambda do_table_structure: converter,
    )

    generated = sorted(path.relative_to(output_dir).as_posix() for path in output_dir.rglob("*.md"))
    assert exit_code == 0
    assert converter.inputs == [input_dir / "docs" / "keep.pdf"]
    assert len(converter.inputs) == 1
    assert generated == ["docs/keep.md"]


@pytest.mark.parametrize(
    ("include_patterns", "expected_inputs"),
    [
        pytest.param((), ("a.pdf", "b.PDF", "c.pdf"), id="include-not-specified"),
        pytest.param(("a.pdf",), ("a.pdf",), id="include-match"),
        pytest.param(("missing-*.pdf",), (), id="include-no-match"),
        pytest.param(("a.pdf", "b.pdf"), ("a.pdf", "b.PDF"), id="multiple-includes"),
    ],
)
def test_non_recursive_include_filters(
    tmp_path: Path,
    capsys: object,
    include_patterns: tuple[str, ...],
    expected_inputs: tuple[str, ...],
) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("a.pdf", "b.PDF", "c.pdf"))
    converter = RecordingConverter()
    factory_calls: list[bool] = []
    args = ["convert", str(input_dir), "--output", str(tmp_path / "output")]
    for pattern in include_patterns:
        args.extend(("--include", pattern))

    def factory(do_table_structure: bool) -> RecordingConverter:
        factory_calls.append(do_table_structure)
        return converter

    exit_code = run(
        args,
        converter_factory=factory,
    )

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert exit_code == 0
    assert tuple(path.name for path in converter.inputs) == expected_inputs
    if not expected_inputs:
        assert factory_calls == []
        assert "成功=0 失敗=0 スキップ=0" in captured.out
    else:
        assert factory_calls == [False]


@pytest.mark.parametrize(
    ("exclude_patterns", "expected_inputs"),
    [
        pytest.param(("b.pdf",), ("a.pdf", "c.pdf"), id="exclude-match"),
        pytest.param(("a.pdf", "c.pdf"), ("b.PDF",), id="multiple-excludes"),
    ],
)
def test_non_recursive_exclude_filters(
    tmp_path: Path,
    exclude_patterns: tuple[str, ...],
    expected_inputs: tuple[str, ...],
) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("a.pdf", "b.PDF", "c.pdf"))
    converter = RecordingConverter()
    args = ["convert", str(input_dir), "--output", str(tmp_path / "output")]
    for pattern in exclude_patterns:
        args.extend(("--exclude", pattern))

    exit_code = run(
        args,
        converter_factory=lambda do_table_structure: converter,
    )

    assert exit_code == 0
    assert tuple(path.name for path in converter.inputs) == expected_inputs


def test_exclude_takes_priority_over_include(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("a.pdf", "b.pdf", "c.pdf"))
    converter = RecordingConverter()

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--include",
            "*.pdf",
            "--exclude",
            "b.pdf",
        ],
        converter_factory=lambda do_table_structure: converter,
    )

    assert exit_code == 0
    assert [path.name for path in converter.inputs] == ["a.pdf", "c.pdf"]


def test_recursive_include_uses_input_root_relative_posix_path(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(
        input_dir,
        ("root.pdf", "docs/a.pdf", "docs/deep/b.PDF", "manual/guide.pdf"),
    )
    converter = RecordingConverter()

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--recursive",
            "--include",
            "docs/**/*.pdf",
        ],
        converter_factory=lambda do_table_structure: converter,
    )

    assert exit_code == 0
    assert [path.relative_to(input_dir).as_posix() for path in converter.inputs] == [
        "docs/a.pdf",
        "docs/deep/b.PDF",
    ]


def test_recursive_multiple_excludes_use_relative_posix_paths(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(
        input_dir,
        (
            "root.pdf",
            "archive/old.pdf",
            "docs/keep.pdf",
            "docs/tmp/drop.pdf",
            "tmp/drop-root.pdf",
        ),
    )
    converter = RecordingConverter()

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--recursive",
            "--exclude",
            "archive/**",
            "--exclude",
            "**/tmp/*",
        ],
        converter_factory=lambda do_table_structure: converter,
    )

    assert exit_code == 0
    assert [path.relative_to(input_dir).as_posix() for path in converter.inputs] == [
        "docs/keep.pdf",
        "root.pdf",
    ]


def test_filtered_nested_output_skip_and_force(tmp_path: Path, capsys: object) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("docs/selected.pdf", "docs/ignored.pdf"))
    output_dir = tmp_path / "output"
    selected_output = output_dir / "docs" / "selected.md"
    selected_output.parent.mkdir(parents=True)
    selected_output.write_text("keep", encoding="utf-8")

    skipped_converter = RecordingConverter()
    skipped_exit = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            "--recursive",
            "--include",
            "docs/selected.pdf",
        ],
        converter_factory=lambda do_table_structure: skipped_converter,
    )

    skipped_stdout = capsys.readouterr().out  # type: ignore[attr-defined]
    assert skipped_exit == 0
    assert skipped_converter.inputs == []
    assert selected_output.read_text(encoding="utf-8") == "keep"
    assert "成功=0 失敗=0 スキップ=1" in skipped_stdout

    forced_converter = RecordingConverter()
    forced_exit = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            "--recursive",
            "--include",
            "docs/selected.pdf",
            "--force",
        ],
        converter_factory=lambda do_table_structure: forced_converter,
    )

    assert forced_exit == 0
    assert forced_converter.inputs == [input_dir / "docs" / "selected.pdf"]
    assert selected_output.read_text(encoding="utf-8") == "# selected\n"
    assert not (output_dir / "docs" / "ignored.md").exists()


def test_empty_directory_returns_nonzero_without_building_converter(
    tmp_path: Path,
    capsys: object,
) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "notes.txt").write_text("ignored", encoding="utf-8")

    def unexpected_factory(do_table_structure: bool) -> FakeConverter:
        raise AssertionError("converter must not be built")

    exit_code = run(
        ["convert", str(input_dir), "--output", str(tmp_path / "output")],
        converter_factory=unexpected_factory,
    )

    assert exit_code != 0
    assert "PDFファイルがありません" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_directory_continues_after_failure_and_returns_nonzero(
    tmp_path: Path,
    capsys: object,
) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    for name in ("a.pdf", "b.pdf", "c.pdf"):
        (input_dir / name).write_bytes(b"%PDF-1.4\n")
    output_dir = tmp_path / "output"
    converter = RecordingConverter(failing_names={"b.pdf"})

    exit_code = run(
        ["convert", str(input_dir), "--output", str(output_dir)],
        converter_factory=lambda do_table_structure: converter,
    )

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert exit_code != 0
    assert [path.name for path in converter.inputs] == ["a.pdf", "b.pdf", "c.pdf"]
    assert (output_dir / "a.md").exists()
    assert not (output_dir / "b.md").exists()
    assert (output_dir / "c.md").exists()
    assert "b.pdf" in captured.err
    assert "synthetic failure" in captured.err
    assert "分類=converter生成・変換処理関連" in captured.err
    assert str(tmp_path) not in captured.err
    assert "成功=2 失敗=1 スキップ=0" in captured.out
    assert "converter生成・変換処理関連=1" in captured.out


def test_table_structure_applies_to_entire_directory(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    for name in ("one.pdf", "two.pdf"):
        (input_dir / name).write_bytes(b"%PDF-1.4\n")
    converter = RecordingConverter()
    table_settings: list[bool] = []

    def factory(do_table_structure: bool) -> RecordingConverter:
        table_settings.append(do_table_structure)
        return converter

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--table-structure",
        ],
        converter_factory=factory,
    )

    assert exit_code == 0
    assert table_settings == [True]
    assert [path.name for path in converter.inputs] == ["one.pdf", "two.pdf"]


def test_existing_outputs_are_skipped_and_force_overwrites_in_directory(
    tmp_path: Path,
    capsys: object,
) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    for name in ("existing.pdf", "new.pdf"):
        (input_dir / name).write_bytes(b"%PDF-1.4\n")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    existing_output = output_dir / "existing.md"
    existing_output.write_text("keep", encoding="utf-8")

    first_converter = RecordingConverter()
    first_exit_code = run(
        ["convert", str(input_dir), "--output", str(output_dir)],
        converter_factory=lambda do_table_structure: first_converter,
    )

    first_output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert first_exit_code == 0
    assert [path.name for path in first_converter.inputs] == ["new.pdf"]
    assert existing_output.read_text(encoding="utf-8") == "keep"
    assert (output_dir / "new.md").exists()
    assert "スキップしました" in first_output
    assert "成功=1 失敗=0 スキップ=1" in first_output

    second_converter = RecordingConverter()
    second_exit_code = run(
        ["convert", str(input_dir), "--output", str(output_dir), "--force"],
        converter_factory=lambda do_table_structure: second_converter,
    )

    assert second_exit_code == 0
    assert [path.name for path in second_converter.inputs] == ["existing.pdf", "new.pdf"]
    assert existing_output.read_text(encoding="utf-8") == "# existing\n"


def test_rerun_with_all_outputs_existing_skips_without_building_converter(
    tmp_path: Path,
    capsys: object,
) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "one.pdf").write_bytes(b"%PDF-1.4\n")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    output = output_dir / "one.md"
    output.write_text("keep", encoding="utf-8")

    def unexpected_factory(do_table_structure: bool) -> FakeConverter:
        raise AssertionError("converter must not be built when every output is skipped")

    exit_code = run(
        ["convert", str(input_dir), "--output", str(output_dir)],
        converter_factory=unexpected_factory,
    )

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert exit_code == 0
    assert output.read_text(encoding="utf-8") == "keep"
    assert "成功=0 失敗=0 スキップ=1" in captured.out


def test_converter_factory_failure_reports_pending_and_skipped_counts(
    tmp_path: Path,
    capsys: object,
) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    for name in ("existing.pdf", "pending.pdf"):
        (input_dir / name).write_bytes(b"%PDF-1.4\n")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "existing.md").write_text("keep", encoding="utf-8")

    def failing_factory(do_table_structure: bool) -> FakeConverter:
        raise RuntimeError("synthetic factory failure")

    exit_code = run(
        ["convert", str(input_dir), "--output", str(output_dir)],
        converter_factory=failing_factory,
    )

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert exit_code != 0
    assert "synthetic factory failure" in captured.err
    assert "ファイル=pending.pdf" in captured.err
    assert "分類=converter生成・変換処理関連" in captured.err
    assert str(tmp_path) not in captured.err
    assert "成功=0 失敗=1 スキップ=1" in captured.out
    assert "converter生成・変換処理関連=1" in captured.out


def test_converter_factory_failure_log_omits_traceback_and_local_paths(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    for name in ("existing.pdf", "pending.pdf"):
        (input_dir / name).write_bytes(b"%PDF-1.4\n")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "existing.md").write_text("keep", encoding="utf-8")

    def failing_factory(do_table_structure: bool) -> FakeConverter:
        raise RuntimeError(f"synthetic factory failure at {tmp_path}")

    with caplog.at_level(logging.ERROR, logger="knowledge_importer"):
        exit_code = run(
            ["convert", str(input_dir), "--output", str(output_dir)],
            converter_factory=failing_factory,
        )

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert exit_code == 1
    assert all(record.exc_info is None for record in caplog.records)
    assert "Traceback" not in log_text
    assert str(tmp_path) not in log_text
    assert "category=CONVERTER" in log_text
    assert "exception_type=RuntimeError" in log_text
    assert "success_count=0" in log_text
    assert "failure_count=1" in log_text
    assert "skipped_count=1" in log_text


def test_directory_reports_multiple_failure_categories_and_continues(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    for name in ("convert.pdf", "ok.pdf", "output.pdf", "unexpected.pdf"):
        (input_dir / name).write_bytes(b"%PDF-1.4\n")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "output.md").mkdir()
    ordered_inputs = [
        input_dir / "convert.pdf",
        input_dir / "missing.pdf",
        input_dir / "ok.pdf",
        input_dir / "output.pdf",
        input_dir / "unexpected.pdf",
    ]
    monkeypatch.setattr(
        "knowledge_importer.cli._find_pdf_files",
        lambda path, **kwargs: ordered_inputs,
    )

    converter = RecordingConverter(failing_names={"convert.pdf"})
    from knowledge_importer import cli as cli_module

    original_convert_file = cli_module.convert_file

    def selective_convert_file(
        request: ConversionRequest,
        wrapped_converter: Converter,
    ) -> None:
        if request.input_path.name == "unexpected.pdf":
            raise TypeError("synthetic unexpected failure")
        original_convert_file(request, wrapped_converter)

    monkeypatch.setattr(cli_module, "convert_file", selective_convert_file)

    exit_code = run(
        ["convert", str(input_dir), "--output", str(output_dir)],
        converter_factory=lambda do_table_structure: converter,
    )

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert exit_code != 0
    assert [path.name for path in converter.inputs] == ["convert.pdf", "ok.pdf"]
    assert (output_dir / "ok.md").exists()
    assert captured.err.count("失敗: ファイル=") == 4
    assert "ファイル=missing.pdf 分類=入力・パス関連" in captured.err
    assert "ファイル=output.pdf 分類=出力競合・書き込み関連" in captured.err
    assert "ファイル=convert.pdf 分類=converter生成・変換処理関連" in captured.err
    assert "ファイル=unexpected.pdf 分類=想定外エラー" in captured.err
    assert str(tmp_path) not in captured.err
    assert "成功=1 失敗=4 スキップ=0" in captured.out
    assert "入力・パス関連=1" in captured.out
    assert "出力競合・書き込み関連=1" in captured.out
    assert "converter生成・変換処理関連=1" in captured.out
    assert "想定外エラー=1" in captured.out


def test_directory_reports_all_files_failed_and_processes_each_one(
    tmp_path: Path,
    capsys: object,
) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    for name in ("a.pdf", "b.pdf"):
        (input_dir / name).write_bytes(b"%PDF-1.4\n")
    converter = RecordingConverter(failing_names={"a.pdf", "b.pdf"})

    exit_code = run(
        ["convert", str(input_dir), "--output", str(tmp_path / "output")],
        converter_factory=lambda do_table_structure: converter,
    )

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert exit_code != 0
    assert [path.name for path in converter.inputs] == ["a.pdf", "b.pdf"]
    assert captured.err.count("分類=converter生成・変換処理関連") == 2
    assert "成功=0 失敗=2 スキップ=0" in captured.out
    assert "converter生成・変換処理関連=2" in captured.out


def test_batch_rejects_conflicting_output_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    monkeypatch.setattr(
        "knowledge_importer.cli._find_pdf_files",
        lambda path, **kwargs: [path / "same.pdf", path / "same.PDF"],
    )

    with pytest.raises(KnowledgeImporterError, match="同じ出力名"):
        _build_batch_requests(input_dir, tmp_path / "output", force=False)


def test_recursive_batch_rejects_unicode_normalized_output_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    monkeypatch.setattr(
        "knowledge_importer.cli._find_pdf_files",
        lambda path, **kwargs: [
            path / "section" / "Café.pdf",
            path / "section" / "Cafe\u0301.PDF",
        ],
    )

    with pytest.raises(KnowledgeImporterError, match="正規化後の出力パス"):
        _build_batch_requests(
            input_dir,
            tmp_path / "output",
            force=False,
            recursive=True,
        )


def test_recursive_batch_rejects_input_outside_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    outside = tmp_path / "outside.pdf"
    monkeypatch.setattr(
        "knowledge_importer.cli._find_pdf_files",
        lambda path, **kwargs: [outside],
    )

    with pytest.raises(KnowledgeImporterError, match="入力ルート外"):
        _build_batch_requests(
            input_dir,
            tmp_path / "output",
            force=False,
            recursive=True,
        )


def test_batch_without_report_option_does_not_create_json(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("one.pdf",))

    exit_code = run(
        ["convert", str(input_dir), "--output", str(tmp_path / "output")],
        converter_factory=fake_converter_factory,
    )

    assert exit_code == 0
    assert list(tmp_path.rglob("*.json")) == []
    assert list(tmp_path.rglob("*.csv")) == []


def test_single_pdf_rejects_report_option_without_creating_report(
    tmp_path: Path,
    capsys: object,
) -> None:
    input_path = tmp_path / "one.pdf"
    input_path.write_bytes(b"%PDF-1.4\n")
    report_path = tmp_path / "report.json"

    exit_code = run(
        [
            "convert",
            str(input_path),
            "--output",
            str(tmp_path / "one.md"),
            "--report-json",
            str(report_path),
        ],
        converter_factory=fake_converter_factory,
    )

    assert exit_code == 2
    assert not report_path.exists()
    assert "ディレクトリ一括変換でのみ使用できます" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_json_report_records_recursive_success_in_deterministic_order(
    tmp_path: Path,
) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("Zulu.PDF", "section/架空資料.pdf"))
    output_dir = tmp_path / "output"
    report_path = tmp_path / "reports" / "結果.json"

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            "--recursive",
            "--report-json",
            str(report_path),
        ],
        converter_factory=fake_converter_factory,
    )

    raw_report = report_path.read_bytes()
    assert exit_code == 0
    assert raw_report.endswith(b"\n")
    assert "架空資料.pdf".encode() in raw_report
    assert b"\\u67b6" not in raw_report
    assert str(tmp_path).encode() not in raw_report
    assert _read_json_report(report_path) == {
        "schema_version": 1,
        "summary": {
            "total": 2,
            "succeeded": 2,
            "failed": 0,
            "skipped": 0,
        },
        "exit_code": 0,
        "items": [
            {
                "input": "section/架空資料.pdf",
                "output": "section/架空資料.md",
                "status": "succeeded",
                "error_category": None,
                "message": None,
            },
            {
                "input": "Zulu.PDF",
                "output": "Zulu.md",
                "status": "succeeded",
                "error_category": None,
                "message": None,
            },
        ],
    }


def test_json_report_records_partial_failure_and_skip(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("a.pdf", "b.pdf", "c.pdf"))
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "b.md").write_text("keep", encoding="utf-8")
    report_path = tmp_path / "report.json"
    converter = RecordingConverter(failing_names={"a.pdf"})

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            "--report-json",
            str(report_path),
        ],
        converter_factory=lambda do_table_structure: converter,
    )

    report = _read_json_report(report_path)
    items = report["items"]
    assert exit_code == report["exit_code"] == 1
    assert str(tmp_path) not in report_path.read_text(encoding="utf-8")
    assert "Traceback" not in report_path.read_text(encoding="utf-8")
    assert report["summary"] == {
        "total": 3,
        "succeeded": 1,
        "failed": 1,
        "skipped": 1,
    }
    assert [item["input"] for item in items] == ["a.pdf", "b.pdf", "c.pdf"]  # type: ignore[index]
    assert items[0]["status"] == "failed"  # type: ignore[index]
    assert items[0]["error_category"] == "converter生成・変換処理関連"  # type: ignore[index]
    assert items[0]["message"] == "RuntimeError: synthetic failure for a.pdf"  # type: ignore[index]
    assert items[1] == {  # type: ignore[index]
        "input": "b.pdf",
        "output": "b.md",
        "status": "skipped",
        "error_category": None,
        "message": "既存の出力を保持しました。",
    }
    assert items[2]["status"] == "succeeded"  # type: ignore[index]


def test_json_report_records_all_factory_failures(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("a.pdf", "b.pdf"))
    report_path = tmp_path / "report.json"

    def failing_factory(do_table_structure: bool) -> FakeConverter:
        raise RuntimeError("synthetic factory failure")

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--report-json",
            str(report_path),
        ],
        converter_factory=failing_factory,
    )

    report = _read_json_report(report_path)
    assert exit_code == report["exit_code"] == 1
    assert report["summary"] == {
        "total": 2,
        "succeeded": 0,
        "failed": 2,
        "skipped": 0,
    }
    assert [item["status"] for item in report["items"]] == [  # type: ignore[index]
        "failed",
        "failed",
    ]


def test_json_report_records_all_skipped_without_converter(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("one.pdf",))
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "one.md").write_text("keep", encoding="utf-8")
    report_path = tmp_path / "report.json"

    def unexpected_factory(do_table_structure: bool) -> FakeConverter:
        raise AssertionError("converter must not be built")

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            "--report-json",
            str(report_path),
        ],
        converter_factory=unexpected_factory,
    )

    report = _read_json_report(report_path)
    assert exit_code == report["exit_code"] == 0
    assert report["summary"] == {
        "total": 1,
        "succeeded": 0,
        "failed": 0,
        "skipped": 1,
    }
    assert report["items"][0]["status"] == "skipped"  # type: ignore[index]


def test_json_report_records_empty_filtered_result(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("ignored.pdf",))
    report_path = input_dir / "reports" / "empty.json"

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(input_dir / "output"),
            "--include",
            "selected/*.pdf",
            "--report-json",
            str(report_path),
        ],
        converter_factory=fake_converter_factory,
    )

    assert exit_code == 0
    assert _read_json_report(report_path) == {
        "schema_version": 1,
        "summary": {
            "total": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
        },
        "exit_code": 0,
        "items": [],
    }


def test_json_report_only_contains_files_selected_by_filters(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(
        input_dir,
        ("docs/keep.pdf", "docs/excluded.pdf", "other/ignored.pdf"),
    )
    report_path = tmp_path / "report.json"

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--recursive",
            "--include",
            "docs/*.pdf",
            "--exclude",
            "**/excluded.pdf",
            "--report-json",
            str(report_path),
        ],
        converter_factory=fake_converter_factory,
    )

    report = _read_json_report(report_path)
    assert exit_code == 0
    assert [item["input"] for item in report["items"]] == ["docs/keep.pdf"]  # type: ignore[index]


def test_json_report_replaces_existing_file(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("one.pdf",))
    report_path = tmp_path / "report.json"
    report_path.write_text("old report", encoding="utf-8")

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--report-json",
            str(report_path),
        ],
        converter_factory=fake_converter_factory,
    )

    assert exit_code == 0
    assert _read_json_report(report_path)["schema_version"] == 1


def test_json_report_directory_target_fails_safely(
    tmp_path: Path,
    capsys: object,
) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("one.pdf",))
    report_path = tmp_path / "report"
    report_path.mkdir()

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--report-json",
            str(report_path),
        ],
        converter_factory=fake_converter_factory,
    )

    stderr = capsys.readouterr().err  # type: ignore[attr-defined]
    assert exit_code == 2
    assert stderr.strip() == "JSONレポートを書き込めませんでした。"
    assert str(tmp_path) not in stderr
    assert "Traceback" not in stderr


def test_json_report_parent_creation_failure_is_safe(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("one.pdf",))
    report_parent = tmp_path / "unwritable"
    report_path = report_parent / "report.json"
    original_mkdir = Path.mkdir

    def selective_mkdir(path: Path, *args: object, **kwargs: object) -> None:
        if path == report_parent:
            raise PermissionError("synthetic denied")
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", selective_mkdir)

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--report-json",
            str(report_path),
        ],
        converter_factory=fake_converter_factory,
    )

    stderr = capsys.readouterr().err  # type: ignore[attr-defined]
    assert exit_code == 2
    assert not report_path.exists()
    assert stderr.strip() == "JSONレポートを書き込めませんでした。"
    assert str(tmp_path) not in stderr


def test_json_report_atomic_replace_failure_preserves_existing_file(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("one.pdf",))
    report_path = tmp_path / "report.json"
    report_path.write_text("original", encoding="utf-8")
    original_replace = Path.replace

    def failing_replace(path: Path, target: Path) -> Path:
        if target == report_path:
            raise OSError("synthetic replace failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", failing_replace)

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--report-json",
            str(report_path),
        ],
        converter_factory=fake_converter_factory,
    )

    stderr = capsys.readouterr().err  # type: ignore[attr-defined]
    assert exit_code == 2
    assert report_path.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.glob(".report.json.*.tmp")) == []
    assert stderr.strip() == "JSONレポートを書き込めませんでした。"
    assert str(tmp_path) not in stderr
    assert "Traceback" not in stderr


def test_csv_report_records_recursive_success_with_bom_and_stable_order(
    tmp_path: Path,
) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("Zulu.PDF", "section/架空資料.pdf"))
    output_dir = tmp_path / "output"
    report_path = tmp_path / "reports" / "結果.csv"

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            "--recursive",
            "--report-csv",
            str(report_path),
        ],
        converter_factory=fake_converter_factory,
    )

    raw_report = report_path.read_bytes()
    assert exit_code == 0
    assert raw_report.startswith(b"\xef\xbb\xbf")
    assert raw_report.endswith(b"\n")
    assert "架空資料.pdf".encode() in raw_report
    assert str(tmp_path).encode() not in raw_report
    assert _read_csv_report(report_path) == [
        {
            "input": "section/架空資料.pdf",
            "output": "section/架空資料.md",
            "status": "succeeded",
            "error_category": "",
            "message": "",
        },
        {
            "input": "Zulu.PDF",
            "output": "Zulu.md",
            "status": "succeeded",
            "error_category": "",
            "message": "",
        },
    ]


def test_csv_report_records_partial_failure_and_skip_safely(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("a.pdf", "b.pdf", "c.pdf"))
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "b.md").write_text("keep", encoding="utf-8")
    report_path = tmp_path / "report.csv"
    converter = RecordingConverter(failing_names={"a.pdf"})

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            "--report-csv",
            str(report_path),
        ],
        converter_factory=lambda do_table_structure: converter,
    )

    rows = _read_csv_report(report_path)
    raw_report = report_path.read_text(encoding="utf-8-sig")
    assert exit_code == 1
    assert [row["input"] for row in rows] == ["a.pdf", "b.pdf", "c.pdf"]
    assert rows[0] == {
        "input": "a.pdf",
        "output": "a.md",
        "status": "failed",
        "error_category": "converter生成・変換処理関連",
        "message": "RuntimeError: synthetic failure for a.pdf",
    }
    assert rows[1] == {
        "input": "b.pdf",
        "output": "b.md",
        "status": "skipped",
        "error_category": "",
        "message": "既存の出力を保持しました。",
    }
    assert rows[2]["status"] == "succeeded"
    assert str(tmp_path) not in raw_report
    assert "Traceback" not in raw_report


def test_csv_report_records_all_factory_failures(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("a.pdf", "b.pdf"))
    report_path = tmp_path / "report.csv"

    def failing_factory(do_table_structure: bool) -> FakeConverter:
        raise RuntimeError("synthetic factory failure")

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--report-csv",
            str(report_path),
        ],
        converter_factory=failing_factory,
    )

    rows = _read_csv_report(report_path)
    assert exit_code == 1
    assert [row["status"] for row in rows] == ["failed", "failed"]
    assert {row["error_category"] for row in rows} == {"converter生成・変換処理関連"}


def test_csv_report_records_all_skipped_without_converter(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("a.pdf", "b.pdf"))
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    for name in ("a.md", "b.md"):
        (output_dir / name).write_text("keep", encoding="utf-8")
    report_path = tmp_path / "report.csv"

    def unexpected_factory(do_table_structure: bool) -> FakeConverter:
        raise AssertionError("converter must not be built")

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            "--report-csv",
            str(report_path),
        ],
        converter_factory=unexpected_factory,
    )

    rows = _read_csv_report(report_path)
    assert exit_code == 0
    assert [row["status"] for row in rows] == ["skipped", "skipped"]
    assert all(row["error_category"] == "" for row in rows)


def test_csv_report_writes_header_only_for_empty_filtered_result(
    tmp_path: Path,
) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("ignored.pdf",))
    report_path = tmp_path / "report.csv"

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--include",
            "selected/*.pdf",
            "--report-csv",
            str(report_path),
        ],
        converter_factory=fake_converter_factory,
    )

    assert exit_code == 0
    assert _read_csv_report(report_path) == []
    assert (
        report_path.read_text(encoding="utf-8-sig")
        == "input,output,status,error_category,message\n"
    )


def test_csv_report_writes_header_for_directory_without_pdfs(
    tmp_path: Path,
    capsys: object,
) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "notes.txt").write_text("ignored", encoding="utf-8")
    report_path = tmp_path / "report.csv"

    def unexpected_factory(do_table_structure: bool) -> FakeConverter:
        raise AssertionError("converter must not be built")

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--report-csv",
            str(report_path),
        ],
        converter_factory=unexpected_factory,
    )

    assert exit_code == 2
    assert _read_csv_report(report_path) == []
    assert "PDFファイルがありません" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_csv_writer_quotes_comma_quote_and_newline(tmp_path: Path) -> None:
    report_path = tmp_path / "report.csv"
    message = 'synthetic, "quoted"\nsecond line'
    result = BatchResult(
        (
            BatchResultItem(
                input_name="section/a.pdf",
                output_name="section/a.md",
                status=BatchItemStatus.FAILED,
                error_category=BatchFailureCategory.CONVERTER,
                message=message,
            ),
        )
    )

    _write_batch_csv(report_path, result)

    assert _read_csv_report(report_path)[0]["message"] == message


@pytest.mark.parametrize(
    "report_args",
    [
        ("--report-csv", "report.csv"),
        ("--report-json", "report.json", "--report-csv", "report.csv"),
    ],
)
def test_single_pdf_rejects_csv_report_options(
    tmp_path: Path,
    capsys: object,
    report_args: tuple[str, ...],
) -> None:
    input_path = tmp_path / "one.pdf"
    input_path.write_bytes(b"%PDF-1.4\n")

    exit_code = run(
        [
            "convert",
            str(input_path),
            "--output",
            str(tmp_path / "one.md"),
            *report_args,
        ],
        converter_factory=fake_converter_factory,
    )

    assert exit_code == 2
    assert "ディレクトリ一括変換でのみ使用できます" in capsys.readouterr().err  # type: ignore[attr-defined]
    assert not (tmp_path / "report.csv").exists()


def test_json_and_csv_reports_share_items_without_reconverting(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("b.pdf", "section/a.PDF"))
    output_dir = tmp_path / "output"
    json_path = tmp_path / "report.json"
    csv_path = tmp_path / "report.csv"
    converter = RecordingConverter()

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            "--recursive",
            "--report-json",
            str(json_path),
            "--report-csv",
            str(csv_path),
        ],
        converter_factory=lambda do_table_structure: converter,
    )

    json_items = _read_json_report(json_path)["items"]
    csv_rows = _read_csv_report(csv_path)
    assert exit_code == 0
    assert len(converter.inputs) == 2
    assert [
        {
            "input": item["input"],
            "output": item["output"],
            "status": item["status"],
            "error_category": item["error_category"] or "",
            "message": item["message"] or "",
        }
        for item in json_items  # type: ignore[union-attr]
    ] == csv_rows


def test_same_json_and_csv_report_path_is_rejected_before_conversion(
    tmp_path: Path,
    capsys: object,
) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("one.pdf",))
    report_path = tmp_path / "report"

    def unexpected_factory(do_table_structure: bool) -> FakeConverter:
        raise AssertionError("converter must not be built")

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--report-json",
            str(report_path),
            "--report-csv",
            str(tmp_path / "." / "report"),
        ],
        converter_factory=unexpected_factory,
    )

    assert exit_code == 2
    assert not report_path.exists()
    assert "異なる出力先" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_csv_report_creates_parent_and_replaces_existing_file(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("one.pdf",))
    report_path = tmp_path / "reports" / "report.csv"
    report_path.parent.mkdir()
    report_path.write_text("old report", encoding="utf-8")

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--report-csv",
            str(report_path),
        ],
        converter_factory=fake_converter_factory,
    )

    assert exit_code == 0
    assert _read_csv_report(report_path)[0]["status"] == "succeeded"


def test_csv_atomic_replace_failure_preserves_existing_file(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("one.pdf",))
    report_path = tmp_path / "report.csv"
    report_path.write_text("original", encoding="utf-8")
    original_replace = Path.replace

    def failing_replace(path: Path, target: Path) -> Path:
        if target == report_path:
            raise OSError("synthetic replace failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", failing_replace)

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--report-csv",
            str(report_path),
        ],
        converter_factory=fake_converter_factory,
    )

    stderr = capsys.readouterr().err  # type: ignore[attr-defined]
    assert exit_code == 2
    assert report_path.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.glob(".report.csv.*.tmp")) == []
    assert stderr.strip() == "CSVレポートを書き込めませんでした。"
    assert str(tmp_path) not in stderr
    assert "Traceback" not in stderr


def test_json_success_is_kept_when_csv_write_fails(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from knowledge_importer import cli as cli_module

    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("one.pdf",))
    json_path = tmp_path / "report.json"
    csv_path = tmp_path / "report.csv"
    csv_path.write_text("existing csv", encoding="utf-8")

    def failing_csv(path: Path, result: BatchResult) -> None:
        raise OSError("synthetic CSV failure")

    monkeypatch.setattr(cli_module, "_write_batch_csv", failing_csv)

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--report-json",
            str(json_path),
            "--report-csv",
            str(csv_path),
        ],
        converter_factory=fake_converter_factory,
    )

    assert exit_code == 2
    assert _read_json_report(json_path)["exit_code"] == 2
    assert csv_path.read_text(encoding="utf-8") == "existing csv"
    assert capsys.readouterr().err.strip() == "CSVレポートを書き込めませんでした。"  # type: ignore[attr-defined]


def test_csv_success_is_kept_when_json_write_fails(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from knowledge_importer import cli as cli_module

    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("one.pdf",))
    json_path = tmp_path / "report.json"
    json_path.write_text("existing json", encoding="utf-8")
    csv_path = tmp_path / "report.csv"

    def failing_json(
        path: Path,
        result: BatchResult,
        *,
        exit_code: int | None = None,
    ) -> None:
        raise OSError("synthetic JSON failure")

    monkeypatch.setattr(cli_module, "_write_batch_report", failing_json)

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--report-json",
            str(json_path),
            "--report-csv",
            str(csv_path),
        ],
        converter_factory=fake_converter_factory,
    )

    assert exit_code == 2
    assert json_path.read_text(encoding="utf-8") == "existing json"
    assert _read_csv_report(csv_path)[0]["status"] == "succeeded"
    assert capsys.readouterr().err.strip() == "JSONレポートを書き込めませんでした。"  # type: ignore[attr-defined]


def test_quality_warnings_are_opt_in(tmp_path: Path, capsys: object) -> None:
    input_path = tmp_path / "one.pdf"
    input_path.write_bytes(b"%PDF-1.4\n")
    converter = ContentConverter({"one.pdf": "Traceback (most recent call last):\n"})

    exit_code = run(
        ["convert", str(input_path), "--output", str(tmp_path / "one.md")],
        converter_factory=lambda do_table_structure: converter,
    )

    assert exit_code == 0
    assert "警告:" not in capsys.readouterr().err  # type: ignore[attr-defined]
    assert converter.inputs == [input_path]


def test_single_quality_warnings_accept_normal_markdown_and_convert_once(
    tmp_path: Path,
    capsys: object,
) -> None:
    input_path = tmp_path / "one.pdf"
    input_path.write_bytes(b"%PDF-1.4\n")
    converter = ContentConverter(
        {"one.pdf": "# Synthetic note\n\nThis fictional output has enough visible content.\n"}
    )

    exit_code = run(
        [
            "convert",
            str(input_path),
            "--output",
            str(tmp_path / "one.md"),
            "--quality-warnings",
        ],
        converter_factory=lambda do_table_structure: converter,
    )

    assert exit_code == 0
    assert "警告:" not in capsys.readouterr().err  # type: ignore[attr-defined]
    assert converter.inputs == [input_path]


def test_force_regenerated_empty_markdown_warns_without_failing(
    tmp_path: Path,
    capsys: object,
) -> None:
    input_path = tmp_path / "one.pdf"
    input_path.write_bytes(b"%PDF-1.4\n")
    output_path = tmp_path / "one.md"
    output_path.write_text("existing", encoding="utf-8")
    converter = ContentConverter({"one.pdf": ""})

    exit_code = run(
        [
            "convert",
            str(input_path),
            "--output",
            str(output_path),
            "--force",
            "--quality-warnings",
        ],
        converter_factory=lambda do_table_structure: converter,
    )

    stderr = capsys.readouterr().err  # type: ignore[attr-defined]
    assert exit_code == 0
    assert output_path.read_text(encoding="utf-8") == ""
    assert "分類=empty-output" in stderr
    assert "分類=short-output" not in stderr


def test_single_quality_warnings_are_ordered_safe_and_non_fatal(
    tmp_path: Path,
    capsys: object,
) -> None:
    input_path = tmp_path / "one.pdf"
    input_path.write_bytes(b"%PDF-1.4\n")
    markdown = (
        "Synthetic content long enough for the runtime threshold.\n"
        "C:\\Users\\private\\source.pdf\n"
        "Traceback (most recent call last): private detail\n"
        "\u202e"
    )
    converter = ContentConverter({"one.pdf": markdown})

    exit_code = run(
        [
            "convert",
            str(input_path),
            "--output",
            str(tmp_path / "one.md"),
            "--quality-warnings",
        ],
        converter_factory=lambda do_table_structure: converter,
    )

    stderr = capsys.readouterr().err  # type: ignore[attr-defined]
    assert exit_code == 0
    assert [line.split(" 分類=")[1].split(" ")[0] for line in stderr.splitlines()] == [
        "absolute-path",
        "traceback",
        "control-character",
    ]
    assert stderr.count("分類=absolute-path") == 1
    assert "ファイル=one.pdf" in stderr
    assert str(tmp_path) not in stderr
    assert "private detail" not in stderr
    assert "most recent call last" not in stderr


def test_recursive_batch_quality_warning_continues_with_relative_posix_path(
    tmp_path: Path,
    capsys: object,
) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("section/short.pdf", "section/normal.pdf", "ignored.pdf"))
    converter = ContentConverter(
        {
            "short.pdf": "tiny",
            "normal.pdf": "Synthetic Markdown output with enough visible content to pass.",
        }
    )

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--recursive",
            "--include",
            "section/*.pdf",
            "--quality-warnings",
        ],
        converter_factory=lambda do_table_structure: converter,
    )

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert exit_code == 0
    assert "成功=2 失敗=0 スキップ=0" in captured.out
    assert "ファイル=section/short.pdf 分類=short-output" in captured.err
    assert "normal.pdf" not in captured.err
    assert "ignored.pdf" not in captured.err
    assert [path.name for path in converter.inputs] == ["normal.pdf", "short.pdf"]


def test_skipped_markdown_is_not_quality_checked(tmp_path: Path, capsys: object) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("new.pdf", "skipped.pdf"))
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "skipped.md").write_text(
        "Traceback (most recent call last):\n\u202e",
        encoding="utf-8",
    )
    converter = ContentConverter(
        {"new.pdf": "Synthetic Markdown output with enough visible content to pass."}
    )

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            "--quality-warnings",
        ],
        converter_factory=lambda do_table_structure: converter,
    )

    assert exit_code == 0
    assert "警告:" not in capsys.readouterr().err  # type: ignore[attr-defined]
    assert [path.name for path in converter.inputs] == ["new.pdf"]


def test_conversion_failure_remains_exit_one_without_quality_warning(
    tmp_path: Path,
    capsys: object,
) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("broken.pdf",))
    converter = RecordingConverter(failing_names={"broken.pdf"})

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--quality-warnings",
        ],
        converter_factory=lambda do_table_structure: converter,
    )

    stderr = capsys.readouterr().err  # type: ignore[attr-defined]
    assert exit_code == 1
    assert "失敗: ファイル=broken.pdf" in stderr
    assert "警告:" not in stderr


def test_quality_warnings_do_not_change_json_or_csv_results(
    tmp_path: Path,
    capsys: object,
) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("short.pdf",))
    output_dir = tmp_path / "output"
    json_path = tmp_path / "report.json"
    csv_path = tmp_path / "report.csv"
    converter = ContentConverter({"short.pdf": "tiny"})

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            "--quality-warnings",
            "--report-json",
            str(json_path),
            "--report-csv",
            str(csv_path),
        ],
        converter_factory=lambda do_table_structure: converter,
    )

    json_report = _read_json_report(json_path)
    csv_rows = _read_csv_report(csv_path)
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert exit_code == 0
    assert json_report == {
        "schema_version": 1,
        "summary": {"total": 1, "succeeded": 1, "failed": 0, "skipped": 0},
        "exit_code": 0,
        "items": [
            {
                "input": "short.pdf",
                "output": "short.md",
                "status": "succeeded",
                "error_category": None,
                "message": None,
            }
        ],
    }
    assert csv_rows == [
        {
            "input": "short.pdf",
            "output": "short.md",
            "status": "succeeded",
            "error_category": "",
            "message": "",
        }
    ]
    assert "warning" not in json_path.read_text(encoding="utf-8").casefold()
    assert "warning" not in csv_path.read_text(encoding="utf-8-sig").casefold()
    assert "成功=1 失敗=0 スキップ=0" in captured.out
    assert "分類=short-output" in captured.err


def test_quality_read_failure_is_safe_warning_without_status_change(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "one.pdf"
    input_path.write_bytes(b"%PDF-1.4\n")
    output_path = tmp_path / "one.md"
    converter = ContentConverter(
        {"one.pdf": "Synthetic Markdown output with enough visible content to pass."}
    )
    original_read_text = Path.read_text

    def failing_read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == output_path:
            raise PermissionError("synthetic private path detail")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", failing_read_text)

    exit_code = run(
        [
            "convert",
            str(input_path),
            "--output",
            str(output_path),
            "--quality-warnings",
        ],
        converter_factory=lambda do_table_structure: converter,
    )

    stderr = capsys.readouterr().err  # type: ignore[attr-defined]
    assert exit_code == 0
    assert stderr.strip() == (
        "警告: ファイル=one.pdf 分類=quality-read-error 理由=Markdown出力を読み取れない"
    )
    assert str(tmp_path) not in stderr
    assert "synthetic private path detail" not in stderr


def test_single_quality_report_records_passed_without_stderr_warning(
    tmp_path: Path,
    capsys: object,
) -> None:
    input_path = tmp_path / "one.pdf"
    input_path.write_bytes(b"%PDF-1.4\n")
    output_path = tmp_path / "one.md"
    report_path = tmp_path / "quality.json"
    converter = ContentConverter(
        {"one.pdf": "# Synthetic note\n\nThis output has enough visible content to pass.\n"}
    )

    exit_code = run(
        [
            "convert",
            str(input_path),
            "--output",
            str(output_path),
            "--quality-report-json",
            str(report_path),
        ],
        converter_factory=lambda do_table_structure: converter,
    )

    assert exit_code == 0
    assert capsys.readouterr().err == ""  # type: ignore[attr-defined]
    assert converter.inputs == [input_path]
    assert _read_json_report(report_path) == {
        "report_type": "markdown-quality",
        "schema_version": 1,
        "summary": {"checked": 1, "passed": 1, "warned": 0},
        "items": [
            {
                "input": "one.pdf",
                "output": "one.md",
                "status": "passed",
                "warnings": [],
            }
        ],
    }


def test_omitted_quality_report_option_does_not_call_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from knowledge_importer import cli as cli_module

    input_path = tmp_path / "one.pdf"
    input_path.write_bytes(b"%PDF-1.4\n")

    def unexpected_writer(path: Path, report: object) -> None:
        raise AssertionError("quality report writer must remain opt-in")

    monkeypatch.setattr(cli_module, "write_quality_report", unexpected_writer)

    exit_code = run(
        ["convert", str(input_path), "--output", str(tmp_path / "one.md")],
        converter_factory=fake_converter_factory,
    )

    assert exit_code == 0


def test_quality_report_and_stderr_share_one_read_and_evaluation(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from knowledge_importer import cli as cli_module

    input_path = tmp_path / "one.pdf"
    input_path.write_bytes(b"%PDF-1.4\n")
    output_path = tmp_path / "one.md"
    report_path = tmp_path / "quality.json"
    converter = ContentConverter({"one.pdf": "tiny"})
    original_read_text = Path.read_text
    original_evaluate = cli_module.evaluate_runtime_quality_warnings
    read_count = 0
    evaluation_count = 0

    def counting_read(path: Path, *args: object, **kwargs: object) -> str:
        nonlocal read_count
        if path == output_path:
            read_count += 1
        return original_read_text(path, *args, **kwargs)

    def counting_evaluate(markdown: str) -> tuple[object, ...]:
        nonlocal evaluation_count
        evaluation_count += 1
        return original_evaluate(markdown)

    monkeypatch.setattr(Path, "read_text", counting_read)
    monkeypatch.setattr(cli_module, "evaluate_runtime_quality_warnings", counting_evaluate)

    exit_code = run(
        [
            "convert",
            str(input_path),
            "--output",
            str(output_path),
            "--quality-warnings",
            "--quality-report-json",
            str(report_path),
        ],
        converter_factory=lambda do_table_structure: converter,
    )

    report = _read_json_report(report_path)
    assert exit_code == 0
    assert read_count == 1
    assert evaluation_count == 1
    assert converter.inputs == [input_path]
    assert report["items"][0]["status"] == "warned"  # type: ignore[index]
    assert report["items"][0]["warnings"] == [  # type: ignore[index]
        {"category": "short-output", "message": "Markdown出力が極端に短い"}
    ]
    assert "ファイル=one.pdf 分類=short-output" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_batch_quality_report_records_checked_files_in_relative_order(
    tmp_path: Path,
    capsys: object,
) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("section/z.pdf", "section/a.pdf", "ignored.pdf"))
    report_path = tmp_path / "quality.json"
    converter = ContentConverter(
        {
            "a.pdf": "Synthetic Markdown output with enough visible content to pass.",
            "z.pdf": "tiny",
        }
    )

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--recursive",
            "--include",
            "section/*.pdf",
            "--quality-report-json",
            str(report_path),
        ],
        converter_factory=lambda do_table_structure: converter,
    )

    report = _read_json_report(report_path)
    assert exit_code == 0
    assert capsys.readouterr().err == ""  # type: ignore[attr-defined]
    assert report["summary"] == {"checked": 2, "passed": 1, "warned": 1}
    assert [item["input"] for item in report["items"]] == [  # type: ignore[index]
        "section/a.pdf",
        "section/z.pdf",
    ]
    assert [item["output"] for item in report["items"]] == [  # type: ignore[index]
        "section/a.md",
        "section/z.md",
    ]
    assert "ignored.pdf" not in report_path.read_text(encoding="utf-8")


def test_quality_report_preserves_fixed_warning_order_without_duplicates(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "one.pdf"
    input_path.write_bytes(b"%PDF-1.4\n")
    report_path = tmp_path / "quality.json"
    markdown = (
        "Synthetic content long enough for the runtime threshold.\n"
        "C:\\Users\\private\\one.pdf and /srv/private/one.pdf\n"
        "Traceback (most recent call last): private detail\n"
        "\u202e"
    )

    exit_code = run(
        [
            "convert",
            str(input_path),
            "--output",
            str(tmp_path / "one.md"),
            "--quality-report-json",
            str(report_path),
        ],
        converter_factory=lambda do_table_structure: ContentConverter({"one.pdf": markdown}),
    )

    report_text = report_path.read_text(encoding="utf-8")
    warnings = _read_json_report(report_path)["items"][0]["warnings"]  # type: ignore[index]
    assert exit_code == 0
    assert [warning["category"] for warning in warnings] == [
        "absolute-path",
        "traceback",
        "control-character",
    ]
    assert sum(warning["category"] == "absolute-path" for warning in warnings) == 1
    assert "private detail" not in report_text
    assert str(tmp_path) not in report_text


def test_quality_report_excludes_skipped_and_failed_items(
    tmp_path: Path,
) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("failed.pdf", "passed.pdf", "skipped.pdf"))
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "skipped.md").write_text("existing", encoding="utf-8")
    report_path = tmp_path / "quality.json"

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            "--quality-report-json",
            str(report_path),
        ],
        converter_factory=lambda do_table_structure: RecordingConverter(
            failing_names={"failed.pdf"}
        ),
    )

    report = _read_json_report(report_path)
    assert exit_code == 1
    assert report["summary"] == {"checked": 1, "passed": 0, "warned": 1}
    assert [item["input"] for item in report["items"]] == ["passed.pdf"]  # type: ignore[index]


@pytest.mark.parametrize("mode", ["filtered", "no-pdf", "skipped", "failed"])
def test_batch_quality_report_writes_empty_document_when_nothing_is_checked(
    tmp_path: Path,
    mode: str,
) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    output_dir = tmp_path / "output"
    report_path = tmp_path / "quality.json"
    extra_args: list[str] = []
    converter_factory = fake_converter_factory
    expected_exit_code = 0

    def failing_converter_factory(do_table_structure: bool = False) -> RecordingConverter:
        return RecordingConverter(failing_names={"one.pdf"})

    if mode == "filtered":
        _create_pdf_tree(input_dir, ("one.pdf",))
        extra_args = ["--include", "missing/*.pdf"]
    elif mode == "no-pdf":
        expected_exit_code = 2
    elif mode == "skipped":
        _create_pdf_tree(input_dir, ("one.pdf",))
        output_dir.mkdir()
        (output_dir / "one.md").write_text("existing", encoding="utf-8")
    else:
        _create_pdf_tree(input_dir, ("one.pdf",))
        converter_factory = failing_converter_factory
        expected_exit_code = 1

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            *extra_args,
            "--quality-report-json",
            str(report_path),
        ],
        converter_factory=converter_factory,
    )

    assert exit_code == expected_exit_code
    assert _read_json_report(report_path) == {
        "report_type": "markdown-quality",
        "schema_version": 1,
        "summary": {"checked": 0, "passed": 0, "warned": 0},
        "items": [],
    }


def test_single_conversion_failure_writes_empty_quality_report(tmp_path: Path) -> None:
    input_path = tmp_path / "one.pdf"
    input_path.write_bytes(b"%PDF-1.4\n")
    report_path = tmp_path / "quality.json"

    exit_code = run(
        [
            "convert",
            str(input_path),
            "--output",
            str(tmp_path / "one.md"),
            "--quality-report-json",
            str(report_path),
        ],
        converter_factory=lambda do_table_structure: RecordingConverter(failing_names={"one.pdf"}),
    )

    assert exit_code == 1
    assert _read_json_report(report_path)["items"] == []


def test_single_input_validation_failure_does_not_write_quality_report(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "quality.json"

    exit_code = run(
        [
            "convert",
            str(tmp_path / "missing.pdf"),
            "--output",
            str(tmp_path / "missing.md"),
            "--quality-report-json",
            str(report_path),
        ],
        converter_factory=fake_converter_factory,
    )

    assert exit_code == 2
    assert not report_path.exists()


def test_quality_read_error_is_safe_in_report_without_stderr_warning(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "one.pdf"
    input_path.write_bytes(b"%PDF-1.4\n")
    output_path = tmp_path / "one.md"
    report_path = tmp_path / "quality.json"
    original_read_text = Path.read_text

    def failing_read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == output_path:
            raise PermissionError("private local detail")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", failing_read_text)

    exit_code = run(
        [
            "convert",
            str(input_path),
            "--output",
            str(output_path),
            "--quality-report-json",
            str(report_path),
        ],
        converter_factory=fake_converter_factory,
    )

    report_text = report_path.read_text(encoding="utf-8")
    report = json.loads(report_text)
    assert exit_code == 0
    assert capsys.readouterr().err == ""  # type: ignore[attr-defined]
    assert report["items"][0]["status"] == "warned"
    assert report["items"][0]["warnings"] == [
        {"category": "quality-read-error", "message": "Markdown出力を読み取れない"}
    ]
    assert str(tmp_path) not in report_text
    assert "private local detail" not in report_text


@pytest.mark.parametrize("existing_option", ["--report-json", "--report-csv"])
def test_quality_report_rejects_existing_report_path_collision(
    tmp_path: Path,
    capsys: object,
    existing_option: str,
) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("one.pdf",))
    report_path = tmp_path / "RÉPORT.json"
    equivalent_path = tmp_path / "re\u0301port.JSON"

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--quality-report-json",
            str(report_path),
            existing_option,
            str(equivalent_path),
        ],
        converter_factory=fake_converter_factory,
    )

    assert exit_code == 2
    assert "品質レポートと変換結果レポート" in capsys.readouterr().err  # type: ignore[attr-defined]
    assert not report_path.exists()


def test_quality_report_rejects_single_and_batch_markdown_path_collision(
    tmp_path: Path,
) -> None:
    single_input = tmp_path / "one.pdf"
    single_input.write_bytes(b"%PDF-1.4\n")
    single_output = tmp_path / "one.md"

    single_exit = run(
        [
            "convert",
            str(single_input),
            "--output",
            str(single_output),
            "--quality-report-json",
            str(single_output),
        ],
        converter_factory=fake_converter_factory,
    )

    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("section/a.pdf",))
    output_dir = tmp_path / "output"
    batch_exit = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            "--recursive",
            "--quality-report-json",
            str(output_dir / "SECTION" / "A.MD"),
        ],
        converter_factory=fake_converter_factory,
    )

    assert single_exit == 2
    assert batch_exit == 2
    assert not single_output.exists()
    assert not (output_dir / "section" / "a.md").exists()


def test_quality_report_coexists_with_unchanged_batch_json_and_csv(
    tmp_path: Path,
) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("one.pdf",))
    output_dir = tmp_path / "output"
    quality_path = tmp_path / "quality.json"
    batch_path = tmp_path / "batch.json"
    csv_path = tmp_path / "batch.csv"
    converter = ContentConverter({"one.pdf": "tiny"})

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            "--quality-report-json",
            str(quality_path),
            "--report-json",
            str(batch_path),
            "--report-csv",
            str(csv_path),
        ],
        converter_factory=lambda do_table_structure: converter,
    )

    batch_report = _read_json_report(batch_path)
    csv_rows = _read_csv_report(csv_path)
    assert exit_code == 0
    assert converter.inputs == [input_dir / "one.pdf"]
    assert batch_report == {
        "schema_version": 1,
        "summary": {"total": 1, "succeeded": 1, "failed": 0, "skipped": 0},
        "exit_code": 0,
        "items": [
            {
                "input": "one.pdf",
                "output": "one.md",
                "status": "succeeded",
                "error_category": None,
                "message": None,
            }
        ],
    }
    assert csv_rows == [
        {
            "input": "one.pdf",
            "output": "one.md",
            "status": "succeeded",
            "error_category": "",
            "message": "",
        }
    ]
    assert "quality" not in batch_path.read_text(encoding="utf-8").casefold()
    assert "quality" not in csv_path.read_text(encoding="utf-8-sig").casefold()


def test_quality_report_write_failure_is_safe_and_returns_two(
    tmp_path: Path,
    capsys: object,
) -> None:
    input_path = tmp_path / "one.pdf"
    input_path.write_bytes(b"%PDF-1.4\n")
    report_path = tmp_path / "quality"
    report_path.mkdir()

    exit_code = run(
        [
            "convert",
            str(input_path),
            "--output",
            str(tmp_path / "one.md"),
            "--quality-report-json",
            str(report_path),
        ],
        converter_factory=fake_converter_factory,
    )

    stderr = capsys.readouterr().err  # type: ignore[attr-defined]
    assert exit_code == 2
    assert stderr.strip() == "品質レポートを書き込めませんでした。"
    assert str(tmp_path) not in stderr
    assert "Traceback" not in stderr


def test_quality_report_failure_still_writes_existing_batch_reports(
    tmp_path: Path,
    capsys: object,
) -> None:
    input_dir = tmp_path / "input"
    _create_pdf_tree(input_dir, ("one.pdf",))
    quality_path = tmp_path / "quality"
    quality_path.mkdir()
    batch_path = tmp_path / "batch.json"
    csv_path = tmp_path / "batch.csv"

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(tmp_path / "output"),
            "--quality-report-json",
            str(quality_path),
            "--report-json",
            str(batch_path),
            "--report-csv",
            str(csv_path),
        ],
        converter_factory=fake_converter_factory,
    )

    assert exit_code == 2
    assert _read_json_report(batch_path)["exit_code"] == 2
    assert _read_csv_report(csv_path)[0]["status"] == "succeeded"
    assert capsys.readouterr().err.strip() == "品質レポートを書き込めませんでした。"  # type: ignore[attr-defined]
