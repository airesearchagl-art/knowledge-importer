import argparse
import csv
import logging
import re
import sys
import tempfile
import unicodedata
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path

from knowledge_importer.artifact_manifest import (
    ArtifactManifest,
    ArtifactManifestSettings,
    ManifestStatus,
    build_manifest_item,
    write_artifact_manifest,
)
from knowledge_importer.converter import (
    Converter,
    build_docling_converter,
    convert_file,
    validate_request,
)
from knowledge_importer.json_writer import write_json_atomically
from knowledge_importer.markdown_normalization import (
    SUPPORTED_NORMALIZATION_PROFILES,
    normalize_markdown_file,
)
from knowledge_importer.markdown_quality import (
    RuntimeQualityWarning,
    evaluate_runtime_quality_warnings,
)
from knowledge_importer.models import (
    ConversionRequest,
    InputValidationError,
    KnowledgeImporterError,
    OutputExistsError,
)
from knowledge_importer.quality_report import (
    QualityReport,
    QualityReportItem,
    write_quality_report,
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


class BatchItemStatus(Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class BatchResultItem:
    input_name: str
    output_name: str
    status: BatchItemStatus
    error_category: BatchFailureCategory | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class BatchResult:
    items: tuple[BatchResultItem, ...]

    def count(self, status: BatchItemStatus) -> int:
        return sum(item.status is status for item in self.items)

    @property
    def exit_code(self) -> int:
        return 1 if self.count(BatchItemStatus.FAILED) else 0


class _BatchSetupError(KnowledgeImporterError):
    def __init__(self, category: BatchFailureCategory, message: str) -> None:
        super().__init__(message)
        self.category = category


class _BatchNoPdfError(_BatchSetupError):
    pass


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
    convert_parser.add_argument(
        "--artifacts-path",
        type=Path,
        metavar="PATH",
        help="Doclingの事前取得済みlocal model artifactsルート",
    )
    convert_parser.add_argument(
        "--recursive",
        action="store_true",
        help="入力ディレクトリ配下のPDFを再帰的に変換",
    )
    convert_parser.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="GLOB",
        help="対象に含める入力ルート相対glob（複数指定可）",
    )
    convert_parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="対象から除外する入力ルート相対glob（複数指定可）",
    )
    convert_parser.add_argument(
        "--report-json",
        type=Path,
        metavar="PATH",
        help="ディレクトリ一括変換の結果をJSONファイルへ出力",
    )
    convert_parser.add_argument(
        "--report-csv",
        type=Path,
        metavar="PATH",
        help="ディレクトリ一括変換の結果をCSVファイルへ出力",
    )
    convert_parser.add_argument(
        "--quality-warnings",
        action="store_true",
        help="生成Markdownの基礎品質warningをstderrへ表示",
    )
    convert_parser.add_argument(
        "--quality-report-json",
        type=Path,
        metavar="PATH",
        help="生成Markdownの基礎品質検査結果を独立JSONファイルへ出力",
    )
    convert_parser.add_argument(
        "--manifest-json",
        type=Path,
        metavar="PATH",
        help="変換artifactの決定的なManifest JSONを出力",
    )
    convert_parser.add_argument(
        "--normalize-markdown",
        metavar="PROFILE",
        help="生成Markdownへopt-in正規化profileを適用（conservative）",
    )
    return parser


def _path_comparison_key(path: Path) -> str:
    resolved = path.resolve(strict=False)
    return unicodedata.normalize("NFC", str(resolved)).casefold()


def _paths_are_equal(first: Path, second: Path) -> bool:
    return _path_comparison_key(first) == _path_comparison_key(second)


def run(
    argv: Sequence[str] | None = None,
    *,
    converter_factory: Callable[..., Converter] = build_docling_converter,
) -> int:
    args = build_parser().parse_args(argv)
    if (
        args.normalize_markdown is not None
        and args.normalize_markdown not in SUPPORTED_NORMALIZATION_PROFILES
    ):
        print(
            "エラー: --normalize-markdownにはconservativeを指定してください",
            file=sys.stderr,
        )
        return 2
    if args.artifacts_path is not None and not args.artifacts_path.is_dir():
        print(
            "エラー: --artifacts-pathには存在するローカルディレクトリを指定してください",
            file=sys.stderr,
        )
        return 2
    if args.input.is_dir():
        if (
            args.report_json is not None
            and args.report_csv is not None
            and _paths_are_equal(args.report_json, args.report_csv)
        ):
            print(
                "エラー: JSONレポートとCSVレポートには異なる出力先を指定してください",
                file=sys.stderr,
            )
            return 2
        if args.quality_report_json is not None and any(
            report_path is not None and _paths_are_equal(args.quality_report_json, report_path)
            for report_path in (args.report_json, args.report_csv)
        ):
            print(
                "エラー: 品質レポートと変換結果レポートには異なる出力先を指定してください",
                file=sys.stderr,
            )
            return 2
        if args.manifest_json is not None and any(
            report_path is not None and _paths_are_equal(args.manifest_json, report_path)
            for report_path in (args.report_json, args.report_csv, args.quality_report_json)
        ):
            print(
                "エラー: Artifact Manifestと他のレポートには異なる出力先を指定してください",
                file=sys.stderr,
            )
            return 2
        return _run_directory(
            args.input,
            args.output,
            force=args.force,
            do_table_structure=args.table_structure,
            recursive=args.recursive,
            include_patterns=args.include,
            exclude_patterns=args.exclude,
            report_json=args.report_json,
            report_csv=args.report_csv,
            quality_warnings=args.quality_warnings,
            quality_report_json=args.quality_report_json,
            manifest_json=args.manifest_json,
            normalization_profile=args.normalize_markdown,
            artifacts_path=args.artifacts_path,
            converter_factory=converter_factory,
        )

    if args.report_json is not None or args.report_csv is not None:
        option_name = (
            "--report-json"
            if args.report_json is not None and args.report_csv is None
            else "--report-csv"
            if args.report_csv is not None and args.report_json is None
            else "レポート出力"
        )
        print(
            f"エラー: {option_name}はディレクトリ一括変換でのみ使用できます",
            file=sys.stderr,
        )
        return 2

    if args.quality_report_json is not None and _paths_are_equal(
        args.quality_report_json, args.output
    ):
        print(
            "エラー: 品質レポートとMarkdown出力には異なる出力先を指定してください",
            file=sys.stderr,
        )
        return 2

    if args.manifest_json is not None and any(
        report_path is not None and _paths_are_equal(args.manifest_json, report_path)
        for report_path in (args.quality_report_json, args.output)
    ):
        print(
            "エラー: Artifact ManifestとMarkdownまたは他のレポートには"
            "異なる出力先を指定してください",
            file=sys.stderr,
        )
        return 2

    request = ConversionRequest(
        input_path=args.input,
        output_path=args.output,
        force=args.force,
    )
    return _convert_request(
        request,
        converter_factory=converter_factory,
        do_table_structure=args.table_structure,
        quality_warnings=args.quality_warnings,
        quality_report_json=args.quality_report_json,
        manifest_json=args.manifest_json,
        normalization_profile=args.normalize_markdown,
        artifacts_path=args.artifacts_path,
    )


def _create_converter(
    converter_factory: Callable[..., Converter],
    *,
    do_table_structure: bool,
    artifacts_path: Path | None,
) -> Converter:
    if artifacts_path is None:
        return converter_factory(do_table_structure)
    return converter_factory(do_table_structure, artifacts_path)


def _manifest_settings(
    *,
    recursive: bool,
    include_patterns: Sequence[str],
    exclude_patterns: Sequence[str],
    force: bool,
    do_table_structure: bool,
    artifacts_path: Path | None,
    normalization_profile: str | None,
) -> ArtifactManifestSettings:
    return ArtifactManifestSettings(
        recursive=recursive,
        include=tuple(include_patterns),
        exclude=tuple(exclude_patterns),
        force=force,
        table_structure=do_table_structure,
        artifacts_path_configured=artifacts_path is not None,
        normalization_profile=normalization_profile,
    )


def _single_artifact_manifest(
    request: ConversionRequest,
    *,
    status: ManifestStatus,
    settings: ArtifactManifestSettings,
    failure: BatchFailure | None = None,
) -> ArtifactManifest:
    return ArtifactManifest(
        settings=settings,
        items=(
            build_manifest_item(
                input_path=request.input_path,
                output_path=request.output_path,
                input_name=request.input_path.name,
                output_name=request.output_path.name,
                status=status,
                error_category=failure.category.value if failure is not None else None,
                message=failure.reason if failure is not None else None,
            ),
        ),
    )


def _write_artifact_manifest_safely(
    report_path: Path,
    manifest_factory: Callable[[], ArtifactManifest],
) -> bool:
    try:
        write_artifact_manifest(report_path, manifest_factory())
    except Exception as exc:  # noqa: BLE001 - report failures map to exit code 2.
        LOGGER.error(
            "artifact_manifest_write_failed exception_type=%s",
            type(exc).__name__,
        )
        print("Artifact Manifestを書き込めませんでした。", file=sys.stderr)
        return False
    return True


def _convert_request(
    request: ConversionRequest,
    converter: Converter | None = None,
    *,
    converter_factory: Callable[..., Converter] | None = None,
    do_table_structure: bool = False,
    artifacts_path: Path | None = None,
    batch: bool = False,
    quality_warnings: bool = False,
    quality_report_json: Path | None = None,
    manifest_json: Path | None = None,
    normalization_profile: str | None = None,
) -> int:
    LOGGER.info(
        "conversion_start input=%s output=%s",
        request.input_path,
        request.output_path,
    )

    settings = _manifest_settings(
        recursive=False,
        include_patterns=(),
        exclude_patterns=(),
        force=request.force,
        do_table_structure=do_table_structure,
        artifacts_path=artifacts_path,
        normalization_profile=normalization_profile,
    )
    validation_succeeded = False
    try:
        validate_request(request)
        validation_succeeded = True
        if converter is None:
            if converter_factory is None:
                raise RuntimeError("converterまたはconverter factoryが必要です")
            converter = _create_converter(
                converter_factory,
                do_table_structure=do_table_structure,
                artifacts_path=artifacts_path,
            )
        convert_file(request, converter)
        if normalization_profile is not None:
            normalize_markdown_file(request.output_path, normalization_profile)
    except KnowledgeImporterError as exc:
        LOGGER.error(
            "conversion_end success=false input=%s output=%s exception_type=%s",
            request.input_path,
            request.output_path,
            type(exc).__name__,
        )
        prefix = f"{request.input_path}: " if batch else ""
        print(f"エラー: {prefix}{exc}", file=sys.stderr)
        report_failed = False
        if validation_succeeded and quality_report_json is not None:
            report_failed = not _write_quality_report_safely(quality_report_json, QualityReport(()))
        if validation_succeeded and manifest_json is not None:
            failure = _classify_batch_failure(exc, request)
            report_failed = (
                not _write_artifact_manifest_safely(
                    manifest_json,
                    lambda: _single_artifact_manifest(
                        request,
                        status=ManifestStatus.FAILED,
                        settings=settings,
                        failure=failure,
                    ),
                )
                or report_failed
            )
        if report_failed:
            return 2
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
        report_failed = False
        if validation_succeeded and quality_report_json is not None:
            report_failed = not _write_quality_report_safely(quality_report_json, QualityReport(()))
        if validation_succeeded and manifest_json is not None:
            failure = _classify_batch_failure(exc, request)
            report_failed = (
                not _write_artifact_manifest_safely(
                    manifest_json,
                    lambda: _single_artifact_manifest(
                        request,
                        status=ManifestStatus.FAILED,
                        settings=settings,
                        failure=failure,
                    ),
                )
                or report_failed
            )
        if report_failed:
            return 2
        return 1

    quality_result: tuple[RuntimeQualityWarning, ...] = ()
    if quality_warnings or quality_report_json is not None:
        quality_result = _read_markdown_quality(request.output_path)
        if quality_warnings:
            _print_quality_warnings(request.input_path.name, quality_result)

    LOGGER.info(
        "conversion_end success=true input=%s output=%s exception_type=none",
        request.input_path,
        request.output_path,
    )
    print(f"変換しました: {request.output_path}")
    report_failed = False
    if quality_report_json is not None:
        report = QualityReport(
            (
                QualityReportItem(
                    input_name=request.input_path.name,
                    output_name=request.output_path.name,
                    warnings=quality_result,
                ),
            )
        )
        report_failed = not _write_quality_report_safely(quality_report_json, report)
    if manifest_json is not None:
        report_failed = (
            not _write_artifact_manifest_safely(
                manifest_json,
                lambda: _single_artifact_manifest(
                    request,
                    status=ManifestStatus.SUCCEEDED,
                    settings=settings,
                ),
            )
            or report_failed
        )
    return 2 if report_failed else 0


def _is_linked_directory(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _print_quality_warning(file_name: str, warning: RuntimeQualityWarning) -> None:
    LOGGER.warning(
        "quality_warning file=%s category=%s",
        file_name,
        warning.category,
    )
    print(
        f"警告: ファイル={file_name} 分類={warning.category} 理由={warning.reason}",
        file=sys.stderr,
    )


def _print_quality_warnings(file_name: str, warnings: tuple[RuntimeQualityWarning, ...]) -> None:
    for warning in warnings:
        _print_quality_warning(file_name, warning)


def _read_markdown_quality(markdown_path: Path) -> tuple[RuntimeQualityWarning, ...]:
    try:
        markdown = markdown_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return (
            RuntimeQualityWarning(
                "quality-read-error",
                "Markdown出力を読み取れない",
            ),
        )
    return evaluate_runtime_quality_warnings(markdown)


def _write_quality_report_safely(report_path: Path, report: QualityReport) -> bool:
    try:
        write_quality_report(report_path, report)
    except Exception as exc:  # noqa: BLE001 - report failures map to exit code 2.
        LOGGER.error(
            "quality_report_write_failed exception_type=%s",
            type(exc).__name__,
        )
        print("品質レポートを書き込めませんでした。", file=sys.stderr)
        return False
    return True


def _relative_sort_key(path: Path, input_dir: Path) -> tuple[str, str]:
    relative = path.relative_to(input_dir).as_posix()
    return (relative.casefold(), relative)


def _matches_posix_glob(relative_path: Path, pattern: str) -> bool:
    path_parts = tuple(part.casefold() for part in relative_path.parts)
    pattern_parts = tuple(part.casefold() for part in pattern.split("/"))

    @lru_cache
    def matches(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        pattern_part = pattern_parts[pattern_index]
        if pattern_part == "**":
            return matches(path_index, pattern_index + 1) or (
                path_index < len(path_parts) and matches(path_index + 1, pattern_index)
            )
        return (
            path_index < len(path_parts)
            and fnmatchcase(path_parts[path_index], pattern_part)
            and matches(path_index + 1, pattern_index + 1)
        )

    return matches(0, 0)


def _matches_batch_filters(
    path: Path,
    input_dir: Path,
    *,
    include_patterns: Sequence[str],
    exclude_patterns: Sequence[str],
) -> bool:
    relative_path = path.relative_to(input_dir)
    if include_patterns and not any(
        _matches_posix_glob(relative_path, pattern) for pattern in include_patterns
    ):
        return False
    return not any(_matches_posix_glob(relative_path, pattern) for pattern in exclude_patterns)


def _is_selected_pdf(
    path: Path,
    input_dir: Path,
    *,
    include_patterns: Sequence[str],
    exclude_patterns: Sequence[str],
) -> bool:
    return (
        not path.is_symlink()
        and path.is_file()
        and path.suffix.lower() == ".pdf"
        and _matches_batch_filters(
            path,
            input_dir,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
        )
    )


def _find_pdf_files(
    input_dir: Path,
    *,
    recursive: bool = False,
    excluded_dir: Path | None = None,
    include_patterns: Sequence[str] = (),
    exclude_patterns: Sequence[str] = (),
) -> list[Path]:
    if not recursive:
        return sorted(
            (
                path
                for path in input_dir.iterdir()
                if _is_selected_pdf(
                    path,
                    input_dir,
                    include_patterns=include_patterns,
                    exclude_patterns=exclude_patterns,
                )
            ),
            key=lambda path: _relative_sort_key(path, input_dir),
        )

    input_root = input_dir.resolve(strict=False)
    excluded_root = excluded_dir.resolve(strict=False) if excluded_dir is not None else None
    if excluded_root == input_root or (
        excluded_root is not None and not excluded_root.is_relative_to(input_root)
    ):
        excluded_root = None

    pdf_files: list[Path] = []
    pending_directories = [input_dir]
    while pending_directories:
        directory = pending_directories.pop()
        child_directories: list[Path] = []
        for path in directory.iterdir():
            if _is_selected_pdf(
                path,
                input_dir,
                include_patterns=include_patterns,
                exclude_patterns=exclude_patterns,
            ):
                pdf_files.append(path)
                continue
            if path.is_symlink() or path.is_file():
                continue
            if not path.is_dir() or _is_linked_directory(path):
                continue
            if excluded_root is not None and path.resolve(strict=False).is_relative_to(
                excluded_root
            ):
                continue
            child_directories.append(path)
        pending_directories.extend(
            sorted(
                child_directories,
                key=lambda path: _relative_sort_key(path, input_dir),
                reverse=True,
            )
        )
    return sorted(pdf_files, key=lambda path: _relative_sort_key(path, input_dir))


def _build_batch_requests(
    input_dir: Path,
    output_dir: Path,
    *,
    force: bool,
    recursive: bool = False,
    include_patterns: Sequence[str] = (),
    exclude_patterns: Sequence[str] = (),
) -> list[ConversionRequest]:
    if output_dir.exists() and not output_dir.is_dir():
        raise _BatchSetupError(
            BatchFailureCategory.OUTPUT,
            f"出力先はディレクトリである必要があります: {output_dir.name}",
        )

    try:
        pdf_files = _find_pdf_files(
            input_dir,
            recursive=recursive,
            excluded_dir=output_dir,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
        )
    except OSError as exc:
        raise _BatchSetupError(
            BatchFailureCategory.INPUT_PATH,
            f"入力ディレクトリを探索できません: {input_dir.name} ({type(exc).__name__})",
        ) from exc
    if not pdf_files:
        if include_patterns or exclude_patterns:
            return []
        scope = "配下" if recursive else "直下"
        raise _BatchNoPdfError(
            BatchFailureCategory.INPUT_PATH,
            f"入力ディレクトリ{scope}にPDFファイルがありません: {input_dir.name}",
        )

    output_root = output_dir.resolve(strict=False)
    requests: list[ConversionRequest] = []
    for input_path in pdf_files:
        try:
            relative_input = input_path.relative_to(input_dir)
        except ValueError as exc:
            raise _BatchSetupError(
                BatchFailureCategory.INPUT_PATH,
                f"入力ルート外のPDFは処理できません: {input_path.name}",
            ) from exc
        relative_output = relative_input.with_suffix(".md")
        output_path = output_dir / relative_output
        if not output_path.resolve(strict=False).is_relative_to(output_root):
            raise _BatchSetupError(
                BatchFailureCategory.OUTPUT,
                f"出力先が出力ディレクトリ外になります: {relative_output.as_posix()}",
            )
        requests.append(
            ConversionRequest(
                input_path=input_path,
                output_path=output_path,
                force=force,
            )
        )

    output_keys: dict[str, list[Path]] = {}
    for request in requests:
        relative_output = request.output_path.relative_to(output_dir).as_posix()
        output_key = unicodedata.normalize("NFC", relative_output).casefold()
        output_keys.setdefault(output_key, []).append(request.input_path)
    collisions = [paths for paths in output_keys.values() if len(paths) > 1]
    if collisions:
        conflicting = ", ".join(
            path.relative_to(input_dir).as_posix() for paths in collisions for path in paths
        )
        raise _BatchSetupError(
            BatchFailureCategory.OUTPUT,
            "同じ出力名または正規化後の出力パスになるPDFが複数あります。"
            f"変換を開始しません: {conflicting}",
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


def _format_batch_summary(result: BatchResult) -> str:
    success_count = result.count(BatchItemStatus.SUCCEEDED)
    failure_count = result.count(BatchItemStatus.FAILED)
    skipped_count = result.count(BatchItemStatus.SKIPPED)
    summary = f"一括変換完了: 成功={success_count} 失敗={failure_count} スキップ={skipped_count}"
    if not failure_count:
        return summary
    counts = {category: 0 for category in BatchFailureCategory}
    for item in result.items:
        if item.error_category is not None:
            counts[item.error_category] += 1
    category_summary = " ".join(
        f"{category.value}={counts[category]}" for category in BatchFailureCategory
    )
    return f"{summary} 分類別: {category_summary}"


def _batch_result_payload(
    result: BatchResult,
    *,
    exit_code: int | None = None,
) -> dict[str, object]:
    succeeded = result.count(BatchItemStatus.SUCCEEDED)
    failed = result.count(BatchItemStatus.FAILED)
    skipped = result.count(BatchItemStatus.SKIPPED)
    return {
        "schema_version": 1,
        "summary": {
            "total": succeeded + failed + skipped,
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
        },
        "exit_code": result.exit_code if exit_code is None else exit_code,
        "items": [
            {
                "input": item.input_name,
                "output": item.output_name,
                "status": item.status.value,
                "error_category": (
                    item.error_category.value if item.error_category is not None else None
                ),
                "message": item.message,
            }
            for item in result.items
        ],
    }


def _write_batch_report(
    report_path: Path,
    result: BatchResult,
    *,
    exit_code: int | None = None,
) -> None:
    write_json_atomically(
        report_path,
        _batch_result_payload(result, exit_code=exit_code),
    )


def _write_batch_csv(report_path: Path, result: BatchResult) -> None:
    if report_path.is_dir():
        raise IsADirectoryError

    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8-sig",
            newline="",
            prefix=f".{report_path.name}.",
            suffix=".tmp",
            dir=report_path.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            writer = csv.DictWriter(
                temporary_file,
                fieldnames=(
                    "input",
                    "output",
                    "status",
                    "error_category",
                    "message",
                ),
            )
            writer.writeheader()
            for item in result.items:
                writer.writerow(
                    {
                        "input": item.input_name,
                        "output": item.output_name,
                        "status": item.status.value,
                        "error_category": (
                            item.error_category.value if item.error_category is not None else ""
                        ),
                        "message": item.message or "",
                    }
                )
        temporary_path.replace(report_path)
    except Exception:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)
        raise


def _batch_artifact_manifest(
    result: BatchResult,
    requests: Sequence[ConversionRequest],
    *,
    settings: ArtifactManifestSettings,
) -> ArtifactManifest:
    status_map = {
        BatchItemStatus.SUCCEEDED: ManifestStatus.SUCCEEDED,
        BatchItemStatus.SKIPPED: ManifestStatus.SKIPPED,
        BatchItemStatus.FAILED: ManifestStatus.FAILED,
    }
    items = tuple(
        build_manifest_item(
            input_path=request.input_path,
            output_path=request.output_path,
            input_name=result_item.input_name,
            output_name=result_item.output_name,
            status=status_map[result_item.status],
            error_category=(
                result_item.error_category.value if result_item.error_category is not None else None
            ),
            message=result_item.message,
        )
        for request, result_item in zip(requests, result.items, strict=True)
    )
    return ArtifactManifest(settings=settings, items=items)


def _write_requested_reports(
    result: BatchResult,
    *,
    report_json: Path | None,
    report_csv: Path | None,
    quality_report_json: Path | None = None,
    quality_report: QualityReport | None = None,
    manifest_json: Path | None = None,
    manifest_factory: Callable[[], ArtifactManifest] | None = None,
) -> bool:
    report_failed = False
    if quality_report_json is not None and not _write_quality_report_safely(
        quality_report_json,
        quality_report if quality_report is not None else QualityReport(()),
    ):
        report_failed = True
    if report_csv is not None:
        try:
            _write_batch_csv(report_csv, result)
        except Exception as exc:  # noqa: BLE001 - report failures map to exit code 2.
            report_failed = True
            LOGGER.error(
                "batch_report_write_failed report_type=%s exception_type=%s",
                "CSV",
                type(exc).__name__,
            )
            print("CSVレポートを書き込めませんでした。", file=sys.stderr)
    if manifest_json is not None:
        if manifest_factory is None:
            raise RuntimeError("Artifact Manifest factoryが必要です")
        if not _write_artifact_manifest_safely(manifest_json, manifest_factory):
            report_failed = True
    if report_json is not None:
        try:
            _write_batch_report(
                report_json,
                result,
                exit_code=2 if report_failed else result.exit_code,
            )
        except Exception as exc:  # noqa: BLE001 - report failures map to exit code 2.
            report_failed = True
            LOGGER.error(
                "batch_report_write_failed report_type=%s exception_type=%s",
                "JSON",
                type(exc).__name__,
            )
            print("JSONレポートを書き込めませんでした。", file=sys.stderr)
    return report_failed


def _finish_batch(
    result: BatchResult,
    report_json: Path | None,
    report_csv: Path | None,
    quality_report_json: Path | None,
    quality_report: QualityReport,
    manifest_json: Path | None,
    manifest_factory: Callable[[], ArtifactManifest] | None,
) -> int:
    print(_format_batch_summary(result))
    report_failed = _write_requested_reports(
        result,
        report_json=report_json,
        report_csv=report_csv,
        quality_report_json=quality_report_json,
        quality_report=quality_report,
        manifest_json=manifest_json,
        manifest_factory=manifest_factory,
    )
    return 2 if report_failed else result.exit_code


def _batch_result_item(
    request: ConversionRequest,
    input_dir: Path,
    output_dir: Path,
    status: BatchItemStatus,
    *,
    failure: BatchFailure | None = None,
    message: str | None = None,
) -> BatchResultItem:
    return BatchResultItem(
        input_name=request.input_path.relative_to(input_dir).as_posix(),
        output_name=request.output_path.relative_to(output_dir).as_posix(),
        status=status,
        error_category=failure.category if failure is not None else None,
        message=failure.reason if failure is not None else message,
    )


def _convert_batch_request(
    request: ConversionRequest,
    converter: Converter,
    *,
    input_name: str,
    output_name: str,
    quality_warnings: bool,
    quality_report_enabled: bool,
    normalization_profile: str | None,
) -> tuple[BatchFailure | None, tuple[RuntimeQualityWarning, ...]]:
    LOGGER.info(
        "conversion_start input=%s output=%s",
        input_name,
        output_name,
    )
    try:
        convert_file(request, _BatchConverterAdapter(converter))
        if normalization_profile is not None:
            normalize_markdown_file(request.output_path, normalization_profile)
    except Exception as exc:  # noqa: BLE001 - batch boundary classifies every file failure.
        failure = _classify_batch_failure(exc, request)
        failure = BatchFailure(input_name, failure.category, failure.reason)
        LOGGER.error(
            "conversion_end success=false input=%s output=%s category=%s exception_type=%s",
            input_name,
            output_name,
            failure.category.name,
            type(exc.cause if isinstance(exc, _BatchConverterError) else exc).__name__,
        )
        _print_batch_failure(failure)
        return failure, ()

    quality_result: tuple[RuntimeQualityWarning, ...] = ()
    if quality_warnings or quality_report_enabled:
        quality_result = _read_markdown_quality(request.output_path)
        if quality_warnings:
            _print_quality_warnings(input_name, quality_result)

    LOGGER.info(
        "conversion_end success=true input=%s output=%s exception_type=none",
        input_name,
        output_name,
    )
    print(f"変換しました: {output_name}")
    return None, quality_result


def _run_directory(
    input_dir: Path,
    output_dir: Path,
    *,
    force: bool,
    do_table_structure: bool,
    recursive: bool,
    include_patterns: Sequence[str],
    exclude_patterns: Sequence[str],
    report_json: Path | None,
    report_csv: Path | None,
    quality_warnings: bool,
    quality_report_json: Path | None,
    manifest_json: Path | None,
    normalization_profile: str | None,
    artifacts_path: Path | None,
    converter_factory: Callable[..., Converter],
) -> int:
    manifest_settings = _manifest_settings(
        recursive=recursive,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
        force=force,
        do_table_structure=do_table_structure,
        artifacts_path=artifacts_path,
        normalization_profile=normalization_profile,
    )
    try:
        requests = _build_batch_requests(
            input_dir,
            output_dir,
            force=force,
            recursive=recursive,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
        )
    except _BatchNoPdfError as exc:
        LOGGER.error(
            "batch_conversion_end success=false input=%s output=%s category=%s exception_type=%s",
            input_dir.name,
            output_dir.name,
            exc.category.name,
            type(exc).__name__,
        )
        print(f"エラー: 分類={exc.category.value} 理由={exc}", file=sys.stderr)
        if report_csv is not None or quality_report_json is not None or manifest_json is not None:
            _write_requested_reports(
                BatchResult(()),
                report_json=None,
                report_csv=report_csv,
                quality_report_json=quality_report_json,
                quality_report=QualityReport(()),
                manifest_json=manifest_json,
                manifest_factory=lambda: ArtifactManifest(manifest_settings, ()),
            )
        return 2
    except _BatchSetupError as exc:
        LOGGER.error(
            "batch_conversion_end success=false input=%s output=%s category=%s exception_type=%s",
            input_dir.name,
            output_dir.name,
            exc.category.name,
            type(exc).__name__,
        )
        print(f"エラー: 分類={exc.category.value} 理由={exc}", file=sys.stderr)
        return 2

    if quality_report_json is not None and any(
        _paths_are_equal(quality_report_json, request.output_path) for request in requests
    ):
        print(
            "エラー: 品質レポートとMarkdown出力には異なる出力先を指定してください",
            file=sys.stderr,
        )
        return 2

    if manifest_json is not None and any(
        _paths_are_equal(manifest_json, request.output_path) for request in requests
    ):
        print(
            "エラー: Artifact ManifestとMarkdown出力には異なる出力先を指定してください",
            file=sys.stderr,
        )
        return 2

    pending_requests: list[ConversionRequest] = []
    skipped_requests: list[ConversionRequest] = []
    result_items: dict[Path, BatchResultItem] = {}
    quality_items: list[QualityReportItem] = []
    for request in requests:
        if not force and request.output_path.is_file():
            skipped_requests.append(request)
            result_items[request.input_path] = _batch_result_item(
                request,
                input_dir,
                output_dir,
                BatchItemStatus.SKIPPED,
                message="既存の出力を保持しました。",
            )
        else:
            pending_requests.append(request)

    for request in skipped_requests:
        input_name = request.input_path.relative_to(input_dir).as_posix()
        output_name = request.output_path.relative_to(output_dir).as_posix()
        LOGGER.info(
            "conversion_skipped input=%s output=%s reason=output_exists",
            input_name,
            output_name,
        )
        print(f"スキップしました（出力済み）: {output_name}")

    if pending_requests:
        try:
            converter = _create_converter(
                converter_factory,
                do_table_structure=do_table_structure,
                artifacts_path=artifacts_path,
            )
        except Exception as exc:  # noqa: BLE001 - CLI boundary must produce a stable exit code.
            failures = [
                BatchFailure(
                    file_name=request.input_path.relative_to(input_dir).as_posix(),
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
            for request, failure in zip(pending_requests, failures, strict=True):
                result_items[request.input_path] = _batch_result_item(
                    request,
                    input_dir,
                    output_dir,
                    BatchItemStatus.FAILED,
                    failure=failure,
                )
            result = BatchResult(tuple(result_items[request.input_path] for request in requests))
            return _finish_batch(
                result,
                report_json,
                report_csv,
                quality_report_json,
                QualityReport(()),
                manifest_json,
                lambda: _batch_artifact_manifest(
                    result,
                    requests,
                    settings=manifest_settings,
                ),
            )
    else:
        converter = None

    success_count = 0
    failures: list[BatchFailure] = []
    for request in pending_requests:
        assert converter is not None
        input_name = request.input_path.relative_to(input_dir).as_posix()
        output_name = request.output_path.relative_to(output_dir).as_posix()
        failure, quality_result = _convert_batch_request(
            request,
            converter,
            input_name=input_name,
            output_name=output_name,
            quality_warnings=quality_warnings,
            quality_report_enabled=quality_report_json is not None,
            normalization_profile=normalization_profile,
        )
        if failure is None:
            success_count += 1
            result_items[request.input_path] = _batch_result_item(
                request,
                input_dir,
                output_dir,
                BatchItemStatus.SUCCEEDED,
            )
            if quality_report_json is not None:
                quality_items.append(
                    QualityReportItem(
                        input_name=input_name,
                        output_name=output_name,
                        warnings=quality_result,
                    )
                )
        else:
            failures.append(failure)
            result_items[request.input_path] = _batch_result_item(
                request,
                input_dir,
                output_dir,
                BatchItemStatus.FAILED,
                failure=failure,
            )

    skipped_count = len(skipped_requests)
    LOGGER.info(
        "batch_conversion_end success=%s input=%s output=%s "
        "success_count=%d failure_count=%d skipped_count=%d",
        str(not failures).lower(),
        input_dir.name,
        output_dir.name,
        success_count,
        len(failures),
        skipped_count,
    )
    result = BatchResult(tuple(result_items[request.input_path] for request in requests))
    return _finish_batch(
        result,
        report_json,
        report_csv,
        quality_report_json,
        QualityReport(tuple(quality_items)),
        manifest_json,
        lambda: _batch_artifact_manifest(
            result,
            requests,
            settings=manifest_settings,
        ),
    )
