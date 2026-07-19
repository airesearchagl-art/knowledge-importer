from pathlib import Path
from types import SimpleNamespace

import pytest

from knowledge_importer.converter import DoclingConverter, convert_file, validate_request
from knowledge_importer.models import (
    ConversionRequest,
    InputValidationError,
    OutputExistsError,
)


class FakeDocument:
    def export_to_markdown(self) -> str:
        return "# Sample\n"


class FakeBackend:
    def __init__(self) -> None:
        self.source: Path | None = None

    def convert(self, source: Path) -> SimpleNamespace:
        self.source = source
        return SimpleNamespace(document=FakeDocument())


class StubConverter:
    def convert(self, input_path: Path) -> str:
        return "# Local fixture\n"


def test_docling_converter_delegates_and_exports_markdown(tmp_path: Path) -> None:
    source = tmp_path / "sample.pdf"
    backend = FakeBackend()

    markdown = DoclingConverter(backend=backend).convert(source)

    assert backend.source == source
    assert markdown == "# Sample\n"


def test_docling_backend_disables_remote_ocr_and_table_models() -> None:
    from docling.datamodel.base_models import InputFormat

    backend = DoclingConverter._build_backend()
    options = backend.format_to_options[InputFormat.PDF].pipeline_options

    assert options.do_ocr is False
    assert options.do_table_structure is False
    assert options.force_backend_text is True
    assert options.enable_remote_services is False


def test_docling_backend_can_enable_only_table_structure_model() -> None:
    from docling.datamodel.base_models import InputFormat

    backend = DoclingConverter._build_backend(do_table_structure=True)
    options = backend.format_to_options[InputFormat.PDF].pipeline_options

    assert options.do_ocr is False
    assert options.do_table_structure is True
    assert options.force_backend_text is True
    assert options.enable_remote_services is False


@pytest.mark.parametrize("name", ["missing.pdf", "sample.txt"])
def test_validate_request_rejects_invalid_input(tmp_path: Path, name: str) -> None:
    source = tmp_path / name
    if name.endswith(".txt"):
        source.write_text("synthetic", encoding="utf-8")

    with pytest.raises(InputValidationError):
        validate_request(ConversionRequest(source, tmp_path / "result.md"))


def test_validate_request_rejects_directory_named_pdf(tmp_path: Path) -> None:
    source = tmp_path / "directory.pdf"
    source.mkdir()

    with pytest.raises(InputValidationError, match="ファイルではありません"):
        validate_request(ConversionRequest(source, tmp_path / "result.md"))


def test_convert_file_creates_parent_and_writes_utf8(tmp_path: Path) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-1.4\n% synthetic fixture\n")
    output = tmp_path / "nested" / "result.md"

    convert_file(ConversionRequest(source, output), StubConverter())

    assert output.read_text(encoding="utf-8") == "# Local fixture\n"


def test_convert_file_does_not_overwrite_without_force(tmp_path: Path) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    output = tmp_path / "result.md"
    output.write_text("keep", encoding="utf-8")

    with pytest.raises(OutputExistsError):
        convert_file(ConversionRequest(source, output), StubConverter())

    assert output.read_text(encoding="utf-8") == "keep"


def test_convert_file_overwrites_with_force(tmp_path: Path) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    output = tmp_path / "result.md"
    output.write_text("old", encoding="utf-8")

    convert_file(ConversionRequest(source, output, force=True), StubConverter())

    assert output.read_text(encoding="utf-8") == "# Local fixture\n"
