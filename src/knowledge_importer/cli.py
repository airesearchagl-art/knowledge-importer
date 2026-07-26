import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from knowledge_importer.converter import (
    Converter,
    build_docling_converter,
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
    convert_parser.add_argument("input", type=Path, help="入力PDFまたは入力ディレクトリ")
    convert_parser.add_argument(
        "--output",
        "-o",
        required=True,
        type=Path,
        help="出力Markdownまたは出力ディレクトリ",
    )
    convert_parser.add_argument("--force", action="store_true", help="既存の出力を上書き")
    convert_parser.add_argument(
        "--table-structure",
        action="store_true",
        help="Doclingの表構造推論を有効化（追加モデルと処理時間が必要）",
    )
    return parser


def run(
    argv: Sequence[str] | None = None,
    *,
    converter_factory: Callable[[bool], Converter] = build_docling_converter,
) -> int:
    args = build_parser().parse_args(argv)
    if args.input.is_dir():
        return _run_directory(
            args.input,
            args.output,
            force=args.force,
            do_table_structure=args.table_structure,
            converter_factory=converter_factory,
        )

    request = ConversionRequest(
        input_path=args.input,
        output_path=args.output,
        force=args.force,
    )
    return _convert_request(
        request,
        converter_factory=converter_factory,
        do_table_structure=args.table_structure,
    )


def _convert_request(
    request: ConversionRequest,
    converter: Converter | None = None,
    *,
    converter_factory: Callable[[bool], Converter] | None = None,
    do_table_structure: bool = False,
    batch: bool = False,
) -> int:
    LOGGER.info(
        "conversion_start input=%s output=%s",
        request.input_path,
        request.output_path,
    )

    try:
        validate_request(request)
        if converter is None:
            if converter_factory is None:
                raise RuntimeError("converterまたはconverter factoryが必要です")
            converter = converter_factory(do_table_structure)
        convert_file(request, converter)
    except KnowledgeImporterError as exc:
        LOGGER.error(
            "conversion_end success=false input=%s output=%s exception_type=%s",
            request.input_path,
            request.output_path,
            type(exc).__name__,
        )
        prefix = f"{request.input_path}: " if batch else ""
        print(f"エラー: {prefix}{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary must produce a stable exit code.
        LOGGER.exception(
            "conversion_end success=false input=%s output=%s exception_type=%s",
            request.input_path,
            request.output_path,
            type(exc).__name__,
        )
        if batch:
            print(
                f"変換に失敗しました: {request.input_path}: ({type(exc).__name__}): {exc}",
                file=sys.stderr,
            )
        else:
            print(f"変換に失敗しました ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1

    LOGGER.info(
        "conversion_end success=true input=%s output=%s exception_type=none",
        request.input_path,
        request.output_path,
    )
    print(f"変換しました: {request.output_path}")
    return 0


def _find_pdf_files(input_dir: Path) -> list[Path]:
    return sorted(
        (path for path in input_dir.iterdir() if path.is_file() and path.suffix.lower() == ".pdf"),
        key=lambda path: (path.name.casefold(), path.name),
    )


def _build_batch_requests(
    input_dir: Path,
    output_dir: Path,
    *,
    force: bool,
) -> list[ConversionRequest]:
    if output_dir.exists() and not output_dir.is_dir():
        raise KnowledgeImporterError(
            f"ディレクトリ入力時の出力先はディレクトリである必要があります: {output_dir}"
        )

    pdf_files = _find_pdf_files(input_dir)
    if not pdf_files:
        raise KnowledgeImporterError(f"入力ディレクトリ直下にPDFファイルがありません: {input_dir}")

    requests = [
        ConversionRequest(
            input_path=input_path,
            output_path=output_dir / f"{input_path.stem}.md",
            force=force,
        )
        for input_path in pdf_files
    ]
    output_keys: dict[str, list[Path]] = {}
    for request in requests:
        output_keys.setdefault(request.output_path.name.casefold(), []).append(request.input_path)
    collisions = [paths for paths in output_keys.values() if len(paths) > 1]
    if collisions:
        conflicting = ", ".join(str(path) for paths in collisions for path in paths)
        raise KnowledgeImporterError(
            f"同じ出力名になるPDFが複数あります。変換を開始しません: {conflicting}"
        )
    return requests


def _run_directory(
    input_dir: Path,
    output_dir: Path,
    *,
    force: bool,
    do_table_structure: bool,
    converter_factory: Callable[[bool], Converter],
) -> int:
    try:
        requests = _build_batch_requests(input_dir, output_dir, force=force)
    except KnowledgeImporterError as exc:
        LOGGER.error(
            "batch_conversion_end success=false input=%s output=%s exception_type=%s",
            input_dir,
            output_dir,
            type(exc).__name__,
        )
        print(f"エラー: {exc}", file=sys.stderr)
        return 2

    pending_requests: list[ConversionRequest] = []
    skipped_requests: list[ConversionRequest] = []
    for request in requests:
        if not force and request.output_path.is_file():
            skipped_requests.append(request)
        else:
            pending_requests.append(request)

    for request in skipped_requests:
        LOGGER.info(
            "conversion_skipped input=%s output=%s reason=output_exists",
            request.input_path,
            request.output_path,
        )
        print(f"スキップしました（出力済み）: {request.output_path}")

    if pending_requests:
        try:
            converter = converter_factory(do_table_structure)
        except Exception as exc:  # noqa: BLE001 - CLI boundary must produce a stable exit code.
            failure_count = len(pending_requests)
            skipped_count = len(skipped_requests)
            LOGGER.exception(
                "batch_conversion_end success=false input=%s output=%s "
                "exception_type=%s success_count=0 failure_count=%d skipped_count=%d",
                input_dir,
                output_dir,
                type(exc).__name__,
                failure_count,
                skipped_count,
            )
            print(
                f"一括変換を開始できませんでした ({type(exc).__name__}): {exc}",
                file=sys.stderr,
            )
            print(f"一括変換完了: 成功=0 失敗={failure_count} スキップ={skipped_count}")
            return 1
    else:
        converter = None

    success_count = 0
    failure_count = 0
    for request in pending_requests:
        assert converter is not None
        exit_code = _convert_request(request, converter, batch=True)
        if exit_code == 0:
            success_count += 1
        else:
            failure_count += 1

    skipped_count = len(skipped_requests)
    LOGGER.info(
        "batch_conversion_end success=%s input=%s output=%s "
        "success_count=%d failure_count=%d skipped_count=%d",
        str(failure_count == 0).lower(),
        input_dir,
        output_dir,
        success_count,
        failure_count,
        skipped_count,
    )
    print(f"一括変換完了: 成功={success_count} 失敗={failure_count} スキップ={skipped_count}")
    return 0 if failure_count == 0 else 1
