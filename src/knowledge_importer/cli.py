import argparse
import logging
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from knowledge_importer.converter import (
    Converter,
    build_docling_converter,
    convert_file,
    validate_request,
)
from knowledge_importer.models import (
    ConversionRequest,
    InputValidationError,
    KnowledgeImporterError,
    OutputExistsError,
)

LOGGER = logging.getLogger("knowledge_importer")
_WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)(?:[a-z]:[\\/]|\\\\)[^\s]+")
_POSIX_ABSOLUTE_PATH = re.compile(r"(?<!\w)/(?:[^/\s]+/)+[^\s]+")


class BatchFailureCategory(Enum):
    INPUT_PATH = "入力・パス関連"
    OUTPUT = "出力競合・書き込み関連"
    CONVERTER = "converter生成・変換処理関連"
    UNEXPECTED = "想定外エラー"


@dataclass(frozen=True, slots=True)
class BatchFailure:
    file_name: str
    category: BatchFailureCategory
    reason: str


class _BatchSetupError(KnowledgeImporterError):
    def __init__(self, category: BatchFailureCategory, message: str) -> None:
        super().__init__(message)
        self.category = category


class _BatchConverterError(Exception):
    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


class _BatchConverterAdapter:
    def __init__(self, converter: Converter) -> None:
        self._converter = converter

    def convert(self, input_path: Path) -> str:
        try:
            return self._converter.convert(input_path)
        except Exception as exc:
            raise _BatchConverterError(exc) from exc


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
        raise _BatchSetupError(
            BatchFailureCategory.OUTPUT,
            f"出力先はディレクトリである必要があります: {output_dir.name}",
        )

    pdf_files = _find_pdf_files(input_dir)
    if not pdf_files:
        raise _BatchSetupError(
            BatchFailureCategory.INPUT_PATH,
            f"入力ディレクトリ直下にPDFファイルがありません: {input_dir.name}",
        )

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
        conflicting = ", ".join(path.name for paths in collisions for path in paths)
        raise _BatchSetupError(
            BatchFailureCategory.OUTPUT,
            f"同じ出力名になるPDFが複数あります。変換を開始しません: {conflicting}",
        )
    return requests


def _safe_error_reason(exc: Exception, request: ConversionRequest) -> str:
    message = " ".join(str(exc).split())
    for path in (request.input_path, request.output_path):
        replacements = {str(path), str(path.absolute())}
        for replacement in sorted(replacements, key=len, reverse=True):
            if replacement:
                message = message.replace(replacement, path.name)
    message = _WINDOWS_ABSOLUTE_PATH.sub("<local-path>", message)
    message = _POSIX_ABSOLUTE_PATH.sub("<local-path>", message)
    if len(message) > 160:
        message = f"{message[:157]}..."
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _classify_batch_failure(exc: Exception, request: ConversionRequest) -> BatchFailure:
    source = exc.cause if isinstance(exc, _BatchConverterError) else exc
    if isinstance(exc, InputValidationError):
        category = BatchFailureCategory.INPUT_PATH
    elif isinstance(exc, OutputExistsError | OSError):
        category = BatchFailureCategory.OUTPUT
    elif isinstance(exc, _BatchConverterError):
        category = BatchFailureCategory.CONVERTER
    else:
        category = BatchFailureCategory.UNEXPECTED
    return BatchFailure(
        file_name=request.input_path.name,
        category=category,
        reason=_safe_error_reason(source, request),
    )


def _print_batch_failure(failure: BatchFailure) -> None:
    print(
        f"失敗: ファイル={failure.file_name} 分類={failure.category.value} 理由={failure.reason}",
        file=sys.stderr,
    )


def _format_batch_summary(
    success_count: int,
    failures: Sequence[BatchFailure],
    skipped_count: int,
) -> str:
    summary = f"一括変換完了: 成功={success_count} 失敗={len(failures)} スキップ={skipped_count}"
    if not failures:
        return summary
    counts = {category: 0 for category in BatchFailureCategory}
    for failure in failures:
        counts[failure.category] += 1
    category_summary = " ".join(
        f"{category.value}={counts[category]}" for category in BatchFailureCategory
    )
    return f"{summary} 分類別: {category_summary}"


def _convert_batch_request(
    request: ConversionRequest,
    converter: Converter,
) -> BatchFailure | None:
    LOGGER.info(
        "conversion_start input=%s output=%s",
        request.input_path,
        request.output_path,
    )
    try:
        convert_file(request, _BatchConverterAdapter(converter))
    except Exception as exc:  # noqa: BLE001 - batch boundary classifies every file failure.
        failure = _classify_batch_failure(exc, request)
        LOGGER.error(
            "conversion_end success=false input=%s output=%s category=%s exception_type=%s",
            request.input_path,
            request.output_path,
            failure.category.name,
            type(exc.cause if isinstance(exc, _BatchConverterError) else exc).__name__,
        )
        _print_batch_failure(failure)
        return failure

    LOGGER.info(
        "conversion_end success=true input=%s output=%s exception_type=none",
        request.input_path,
        request.output_path,
    )
    print(f"変換しました: {request.output_path}")
    return None


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
    except _BatchSetupError as exc:
        LOGGER.error(
            "batch_conversion_end success=false input=%s output=%s category=%s exception_type=%s",
            input_dir,
            output_dir,
            exc.category.name,
            type(exc).__name__,
        )
        print(f"エラー: 分類={exc.category.value} 理由={exc}", file=sys.stderr)
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
            failures = [
                BatchFailure(
                    file_name=request.input_path.name,
                    category=BatchFailureCategory.CONVERTER,
                    reason=_safe_error_reason(exc, request),
                )
                for request in pending_requests
            ]
            LOGGER.error(
                "batch_conversion_end success=false category=%s "
                "exception_type=%s success_count=0 failure_count=%d skipped_count=%d",
                BatchFailureCategory.CONVERTER.name,
                type(exc).__name__,
                len(failures),
                len(skipped_requests),
            )
            for failure in failures:
                _print_batch_failure(failure)
            print(_format_batch_summary(0, failures, len(skipped_requests)))
            return 1
    else:
        converter = None

    success_count = 0
    failures: list[BatchFailure] = []
    for request in pending_requests:
        assert converter is not None
        failure = _convert_batch_request(request, converter)
        if failure is None:
            success_count += 1
        else:
            failures.append(failure)

    skipped_count = len(skipped_requests)
    LOGGER.info(
        "batch_conversion_end success=%s input=%s output=%s "
        "success_count=%d failure_count=%d skipped_count=%d",
        str(not failures).lower(),
        input_dir,
        output_dir,
        success_count,
        len(failures),
        skipped_count,
    )
    print(_format_batch_summary(success_count, failures, skipped_count))
    return 0 if not failures else 1
