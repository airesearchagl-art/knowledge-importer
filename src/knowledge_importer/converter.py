from pathlib import Path
from typing import Any, Protocol

from knowledge_importer.models import (
    ConversionRequest,
    InputValidationError,
    OutputExistsError,
)


class Converter(Protocol):
    """Interface implemented by PDF-to-Markdown conversion engines."""

    def convert(self, input_path: Path) -> str:
        """Convert one PDF and return Markdown text."""
        ...


class DocumentConverterBackend(Protocol):
    """Small adapter surface used to isolate Docling in unit tests."""

    def convert(self, source: Path) -> Any: ...


class DoclingConverter:
    """Docling engine configured for PDFs that already have a text layer."""

    def __init__(self, backend: DocumentConverterBackend | None = None) -> None:
        self._backend = backend or self._build_backend()

    @staticmethod
    def _build_backend() -> DocumentConverterBackend:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        pipeline_options = PdfPipelineOptions(
            do_ocr=False,
            force_backend_text=True,
            enable_remote_services=False,
        )
        return DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
            }
        )

    def convert(self, input_path: Path) -> str:
        result = self._backend.convert(input_path)
        return result.document.export_to_markdown()


def validate_request(request: ConversionRequest) -> None:
    input_path = request.input_path
    if not input_path.exists():
        raise InputValidationError(f"入力ファイルが存在しません: {input_path}")
    if not input_path.is_file():
        raise InputValidationError(f"入力パスはファイルではありません: {input_path}")
    if input_path.suffix.lower() != ".pdf":
        raise InputValidationError(f"入力ファイルはPDFではありません: {input_path}")
    if request.output_path.exists() and not request.force:
        raise OutputExistsError(
            f"出力ファイルは既に存在します。上書きする場合は --force を指定してください: "
            f"{request.output_path}"
        )


def convert_file(request: ConversionRequest, converter: Converter) -> None:
    """Validate, convert, then save Markdown as UTF-8."""
    validate_request(request)
    markdown = converter.convert(request.input_path)
    request.output_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if request.force else "x"
    try:
        with request.output_path.open(mode, encoding="utf-8", newline="\n") as output_file:
            output_file.write(markdown)
    except FileExistsError as exc:
        raise OutputExistsError(
            f"出力ファイルは既に存在します。上書きする場合は --force を指定してください: "
            f"{request.output_path}"
        ) from exc
