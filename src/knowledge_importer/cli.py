import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from knowledge_importer.converter import (
    Converter,
    DoclingConverter,
    convert_file,
    validate_request,
)
from knowledge_importer.models import ConversionRequest, KnowledgeImporterError

LOGGER = logging.getLogger("knowledge_importer")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="knowledge-importer",
        description="OCR済みPDFをローカルでMarkdownへ変換します。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    convert_parser = subparsers.add_parser("convert", help="PDFをMarkdownへ変換")
    convert_parser.add_argument("input", type=Path, help="入力PDF")
    convert_parser.add_argument("--output", "-o", required=True, type=Path, help="出力Markdown")
    convert_parser.add_argument("--force", action="store_true", help="既存の出力を上書き")
    return parser


def run(
    argv: Sequence[str] | None = None,
    *,
    converter_factory: Callable[[], Converter] = DoclingConverter,
) -> int:
    args = build_parser().parse_args(argv)
    request = ConversionRequest(
        input_path=args.input,
        output_path=args.output,
        force=args.force,
    )
    LOGGER.info(
        "conversion_start input=%s output=%s",
        request.input_path,
        request.output_path,
    )

    try:
        validate_request(request)
        convert_file(request, converter_factory())
    except KnowledgeImporterError as exc:
        LOGGER.error(
            "conversion_end success=false input=%s output=%s exception_type=%s",
            request.input_path,
            request.output_path,
            type(exc).__name__,
        )
        print(f"エラー: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary must produce a stable exit code.
        LOGGER.exception(
            "conversion_end success=false input=%s output=%s exception_type=%s",
            request.input_path,
            request.output_path,
            type(exc).__name__,
        )
        print(f"変換に失敗しました ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1

    LOGGER.info(
        "conversion_end success=true input=%s output=%s exception_type=none",
        request.input_path,
        request.output_path,
    )
    print(f"変換しました: {request.output_path}")
    return 0
