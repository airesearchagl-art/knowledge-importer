from pathlib import Path

from knowledge_importer.cli import build_parser, run


class FakeConverter:
    def convert(self, input_path: Path) -> str:
        return "# Converted\n\nSynthetic content.\n"


def fake_converter_factory() -> FakeConverter:
    return FakeConverter()


def test_help_describes_convert_command() -> None:
    help_text = build_parser().format_help()

    assert "convert" in help_text
    assert "knowledge-importer" in help_text


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
