from pathlib import Path

import pytest

from knowledge_importer.cli import build_parser, run


class FakeConverter:
    def convert(self, input_path: Path) -> str:
        return "# Converted\n\nSynthetic content.\n"


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
