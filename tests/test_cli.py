from pathlib import Path

import pytest

from knowledge_importer.cli import _build_batch_requests, build_parser, run
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


def test_convert_help_describes_table_structure_option(capsys: object) -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["convert", "--help"])

    assert exc_info.value.code == 0
    assert "--table-structure" in capsys.readouterr().out  # type: ignore[attr-defined]


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
        lambda path: ordered_inputs,
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
        lambda path: [path / "same.pdf", path / "same.PDF"],
    )

    with pytest.raises(KnowledgeImporterError, match="同じ出力名"):
        _build_batch_requests(input_dir, tmp_path / "output", force=False)
