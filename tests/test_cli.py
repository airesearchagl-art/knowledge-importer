import logging
from pathlib import Path

import pytest

from knowledge_importer.cli import _build_batch_requests, _find_pdf_files, build_parser, run
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


def fake_converter_factory(do_table_structure: bool = False) -> FakeConverter:
    return FakeConverter()


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
    assert "--recursive" in help_text


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
