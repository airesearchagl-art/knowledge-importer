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
    ArtifactManifestItem,
    ArtifactManifestSettings,
    ManifestStatus,
    build_manifest_item,
    write_artifact_manifest,
)
from knowledge_importer.backup_cleanup_approval import (
    build_backup_cleanup_approval,
    is_backup_cleanup_approval_report,
    write_backup_cleanup_approval,
)
from knowledge_importer.backup_cleanup_execution import (
    BackupCleanupExecutionInputError,
    capture_backup_cleanup_audit_output,
    execute_backup_cleanup,
    write_backup_cleanup_audit,
)
from knowledge_importer.backup_cleanup_plan import (
    build_backup_cleanup_plan,
    is_backup_cleanup_plan_report,
    write_backup_cleanup_plan,
)
from knowledge_importer.backup_inventory import (
    BackupInventoryInputError,
    build_backup_inventory,
    is_backup_inventory_report,
    path_is_within,
    path_uses_link_or_reparse,
    repository_roots,
    write_backup_inventory,
)
from knowledge_importer.converter import (
    Converter,
    build_docling_converter,
    convert_file,
    validate_request,
)
from knowledge_importer.document_metadata import (
    DocumentMetadataSettings,
    build_document_metadata,
    metadata_sidecar_path,
    write_document_metadata,
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
from knowledge_importer.package_validation import (
    ValidationSeverity,
    validate_package,
    write_validation_report,
)
from knowledge_importer.quality_report import (
    QualityReport,
    QualityReportItem,
    write_quality_report,
)
from knowledge_importer.repair_approval import (
    build_repair_approval,
    is_repair_approval_report,
    write_repair_approval,
)
from knowledge_importer.repair_execution import (
    RepairExecutionInputError,
    execute_repair,
    is_execution_report,
    write_execution_report,
)
from knowledge_importer.repair_plan import (
    build_repair_plan,
    is_repair_plan_report,
    write_repair_plan,
)
from knowledge_importer.repair_preflight import (
    build_repair_preflight,
    is_repair_preflight_report,
    write_repair_preflight,
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
    convert_parser.add_argument(
        "--metadata-sidecar",
        action="store_true",
        help="成功・スキップしたMarkdownの隣へmetadata JSONを生成",
    )
    validate_parser = subparsers.add_parser(
        "validate",
        help="既存Knowledge Packageをread-only検証",
    )
    validate_parser.add_argument("package_root", type=Path, help="検証するpackage root")
    validate_parser.add_argument(
        "--manifest",
        type=Path,
        metavar="PATH",
        help="整合確認するArtifact Manifest v1",
    )
    validate_parser.add_argument(
        "--report-json",
        type=Path,
        metavar="PATH",
        help="決定的なvalidation JSON reportを出力",
    )
    validate_parser.add_argument(
        "--strict",
        action="store_true",
        help="Manifest外のextra Markdownもfailureとして扱う",
    )
    repair_plan_parser = subparsers.add_parser(
        "repair-plan",
        help="Knowledge Packageのread-only修復計画を生成",
    )
    repair_plan_parser.add_argument("package_root", type=Path, help="検証するpackage root")
    repair_plan_parser.add_argument(
        "--manifest",
        type=Path,
        metavar="PATH",
        help="整合確認するArtifact Manifest v1",
    )
    repair_plan_parser.add_argument(
        "--report-json",
        type=Path,
        metavar="PATH",
        help="決定的なRepair Plan JSONを出力",
    )
    repair_plan_parser.add_argument(
        "--strict",
        action="store_true",
        help="Manifest外のextra Markdownも修復候補として扱う",
    )
    approve_repair_parser = subparsers.add_parser(
        "approve-repair",
        help="Repair Planのsafe actionを明示承認",
    )
    approve_repair_parser.add_argument("plan_json", type=Path, help="承認するRepair Plan v1")
    approve_repair_parser.add_argument(
        "--all-safe",
        action="store_true",
        required=True,
        help="safe=trueの全actionだけを承認",
    )
    approve_repair_parser.add_argument(
        "--report-json",
        type=Path,
        metavar="PATH",
        required=True,
        help="決定的なApproval JSONの出力先",
    )
    repair_preflight_parser = subparsers.add_parser(
        "repair-preflight",
        help="承認済みrepair actionの実行前提をread-only検証",
    )
    repair_preflight_parser.add_argument("package_root", type=Path, help="検証するpackage root")
    repair_preflight_parser.add_argument(
        "--manifest",
        type=Path,
        metavar="PATH",
        help="ready判定の基準となるArtifact Manifest v1",
    )
    repair_preflight_parser.add_argument(
        "--plan",
        type=Path,
        metavar="PLAN_JSON",
        required=True,
        help="検証するRepair Plan v1",
    )
    repair_preflight_parser.add_argument(
        "--approval",
        type=Path,
        metavar="APPROVAL_JSON",
        required=True,
        help="検証するRepair Approval v1",
    )
    repair_preflight_parser.add_argument(
        "--report-json",
        type=Path,
        metavar="PATH",
        help="決定的なPreflight JSONの出力先",
    )
    repair_execute_parser = subparsers.add_parser(
        "repair-execute",
        help="承認・事前検証済みsafe actionを実行",
    )
    repair_execute_parser.add_argument("package_root", type=Path, help="修復するpackage root")
    for option, metavar, help_text in (
        ("--manifest", "MANIFEST_JSON", "Artifact Manifest v1"),
        ("--plan", "PLAN_JSON", "Repair Plan v1"),
        ("--approval", "APPROVAL_JSON", "Repair Approval v1"),
        ("--preflight", "PREFLIGHT_JSON", "ready状態のRepair Preflight v1"),
    ):
        repair_execute_parser.add_argument(
            option,
            type=Path,
            metavar=metavar,
            required=True,
            help=help_text,
        )
    repair_execute_parser.add_argument(
        "--report-json",
        type=Path,
        metavar="PATH",
        help="決定的なExecution Report JSONの出力先",
    )
    repair_execute_parser.add_argument(
        "--backup-dir",
        type=Path,
        metavar="BACKUP_DIR",
        help="package・repository外のbackup保存先",
    )
    backup_inventory_parser = subparsers.add_parser(
        "backup-inventory",
        help="Repair Execution backup rootをread-only検査",
    )
    backup_inventory_parser.add_argument(
        "backup_root",
        type=Path,
        metavar="BACKUP_ROOT",
        help="検査するbackup root",
    )
    backup_inventory_parser.add_argument(
        "--package-root",
        type=Path,
        metavar="PACKAGE_ROOT",
        required=True,
        help="backup領域外であることを検証するKnowledge Package root",
    )
    backup_inventory_parser.add_argument(
        "--report-json",
        type=Path,
        metavar="PATH",
        help="決定的なBackup Inventory JSONの出力先",
    )
    backup_cleanup_plan_parser = subparsers.add_parser(
        "backup-cleanup-plan",
        help="Backup Inventoryからread-only cleanup計画を生成",
    )
    backup_cleanup_plan_parser.add_argument(
        "inventory_json",
        type=Path,
        metavar="INVENTORY_JSON",
        help="入力となるBackup Inventory v1",
    )
    backup_cleanup_plan_parser.add_argument(
        "--backup-root",
        type=Path,
        metavar="BACKUP_ROOT",
        required=True,
        help="report領域外であることを検証するbackup root",
    )
    backup_cleanup_plan_parser.add_argument(
        "--session",
        action="append",
        required=True,
        metavar="SESSION",
        help="cleanup候補にするsession（複数指定可）",
    )
    backup_cleanup_plan_parser.add_argument(
        "--report-json",
        type=Path,
        metavar="PATH",
        required=True,
        help="決定的なBackup Cleanup Plan v1の出力先",
    )
    backup_cleanup_approval_parser = subparsers.add_parser(
        "approve-backup-cleanup",
        help="Cleanup Planのplanned actionを明示承認",
    )
    backup_cleanup_approval_parser.add_argument(
        "plan_json",
        type=Path,
        metavar="PLAN_JSON",
        help="承認するBackup Cleanup Plan v1",
    )
    backup_cleanup_approval_parser.add_argument(
        "--backup-root",
        type=Path,
        metavar="BACKUP_ROOT",
        required=True,
        help="report領域外であることを検証するbackup root",
    )
    backup_cleanup_approval_parser.add_argument(
        "--all-planned",
        action="store_true",
        required=True,
        help="eligible=trueの全actionだけを承認",
    )
    backup_cleanup_approval_parser.add_argument(
        "--report-json",
        type=Path,
        metavar="PATH",
        required=True,
        help="決定的なBackup Cleanup Approval v1の出力先",
    )
    backup_cleanup_execute_parser = subparsers.add_parser(
        "backup-cleanup-execute",
        help="明示承認済みbackup sessionを不可逆削除",
    )
    backup_cleanup_execute_parser.add_argument(
        "backup_root",
        type=Path,
        metavar="BACKUP_ROOT",
        help="cleanupするmanaged backup root",
    )
    backup_cleanup_execute_parser.add_argument(
        "--package-root",
        type=Path,
        metavar="PACKAGE_ROOT",
        required=True,
        help="変更禁止のKnowledge Package root",
    )
    for option, metavar, help_text in (
        ("--inventory", "INVENTORY_JSON", "binding済みBackup Inventory v1"),
        ("--plan", "PLAN_JSON", "explicit session Cleanup Plan v1"),
        ("--approval", "APPROVAL_JSON", "all-planned Cleanup Approval v1"),
    ):
        backup_cleanup_execute_parser.add_argument(
            option,
            type=Path,
            metavar=metavar,
            required=True,
            help=help_text,
        )
    backup_cleanup_execute_parser.add_argument(
        "--report-json",
        type=Path,
        metavar="AUDIT_JSON",
        required=True,
        help="決定的なCleanup Audit v1の出力先",
    )
    return parser


def _path_comparison_key(path: Path) -> str:
    resolved = path.resolve(strict=False)
    return unicodedata.normalize("NFC", str(resolved)).casefold()


def _paths_are_equal(first: Path, second: Path) -> bool:
    return _path_comparison_key(first) == _path_comparison_key(second)


def _metadata_sidecars_conflict(
    requests: Sequence[ConversionRequest],
    report_paths: Sequence[Path | None],
) -> bool:
    reserved = {
        _path_comparison_key(path)
        for request in requests
        for path in (request.input_path, request.output_path)
    }
    reserved.update(_path_comparison_key(path) for path in report_paths if path is not None)
    sidecars: set[str] = set()
    for request in requests:
        key = _path_comparison_key(metadata_sidecar_path(request.output_path))
        if key in reserved or key in sidecars:
            return True
        sidecars.add(key)
    return False


def _run_package_validation(
    package_root: Path,
    *,
    manifest_path: Path | None,
    report_json: Path | None,
    strict: bool,
) -> int:
    if not package_root.is_dir() or _is_linked_directory(package_root):
        print("エラー: 存在する通常のpackage rootディレクトリを指定してください", file=sys.stderr)
        return 2
    if manifest_path is not None and not manifest_path.is_file():
        print("エラー: --manifestには存在するファイルを指定してください", file=sys.stderr)
        return 2
    if report_json is not None and (
        report_json.name.casefold().endswith(".metadata.json")
        or report_json.suffix.casefold() == ".md"
        or (manifest_path is not None and _paths_are_equal(report_json, manifest_path))
    ):
        print("エラー: validation reportの出力先が検証対象と競合します", file=sys.stderr)
        return 2

    result = validate_package(
        package_root,
        manifest_path=manifest_path,
        strict=strict,
    )
    for issue in result.issues:
        prefix = "検証失敗" if issue.severity is ValidationSeverity.ERROR else "検証警告"
        print(
            f"{prefix}: ファイル={issue.path} 分類={issue.category} 理由={issue.message}",
            file=sys.stderr,
        )
    print(
        "Knowledge Package検証完了: "
        f"対象={len(result.checked_paths)} 成功={result.passed} "
        f"失敗={result.failed} 警告={result.warnings}"
    )
    if report_json is not None:
        try:
            write_validation_report(report_json, result)
        except Exception as exc:  # noqa: BLE001 - report failures map to exit code 2.
            LOGGER.error("validation_report_write_failed exception_type=%s", type(exc).__name__)
            print("Validation reportを書き込めませんでした。", file=sys.stderr)
            return 2
    return result.exit_code


def _run_repair_plan(
    package_root: Path,
    *,
    manifest_path: Path | None,
    report_json: Path | None,
    strict: bool,
) -> int:
    if not package_root.is_dir() or _is_linked_directory(package_root):
        print("エラー: 存在する通常のpackage rootディレクトリを指定してください", file=sys.stderr)
        return 2
    if manifest_path is not None and not manifest_path.is_file():
        print("エラー: --manifestには存在するファイルを指定してください", file=sys.stderr)
        return 2
    if report_json is not None and (
        report_json.name.casefold().endswith(".metadata.json")
        or report_json.suffix.casefold() == ".md"
        or (manifest_path is not None and _paths_are_equal(report_json, manifest_path))
    ):
        print("エラー: Repair Planの出力先が検証対象と競合します", file=sys.stderr)
        return 2
    if (
        report_json is not None
        and (report_json.is_symlink() or report_json.exists())
        and not is_repair_plan_report(report_json)
    ):
        print("エラー: 既存のRepair Plan以外は上書きできません", file=sys.stderr)
        return 2

    validation_result = validate_package(
        package_root,
        manifest_path=manifest_path,
        strict=strict,
    )
    plan = build_repair_plan(
        validation_result,
        manifest_name=manifest_path.name if manifest_path is not None else None,
    )
    print(
        "Knowledge Package修復計画: "
        f"問題={plan.issues} 修復候補={len(plan.actions)} 手動確認={plan.manual_review}"
    )
    for action in plan.actions:
        print(
            f"修復候補: ファイル={action.path} 操作={action.action.value} "
            f"理由分類={action.reason_category} safe={str(action.safe).lower()}"
        )
    if report_json is not None:
        try:
            write_repair_plan(report_json, plan)
        except Exception as exc:  # noqa: BLE001 - report failures map to exit code 2.
            LOGGER.error("repair_plan_write_failed exception_type=%s", type(exc).__name__)
            print("Repair Planを書き込めませんでした。", file=sys.stderr)
            return 2
    return 0


def _run_repair_approval(plan_path: Path, *, report_json: Path) -> int:
    if plan_path.is_symlink() or not plan_path.is_file():
        print("エラー: 存在する通常のRepair Plan fileを指定してください", file=sys.stderr)
        return 2
    if _paths_are_equal(plan_path, report_json):
        print("エラー: Approvalの出力先がRepair Planと競合します", file=sys.stderr)
        return 2
    if (report_json.is_symlink() or report_json.exists()) and not is_repair_approval_report(
        report_json
    ):
        print("エラー: 既存のRepair Approval以外は上書きできません", file=sys.stderr)
        return 2
    try:
        approval = build_repair_approval(plan_path)
    except Exception as exc:  # noqa: BLE001 - invalid plans map to exit code 2.
        LOGGER.error("repair_approval_plan_invalid exception_type=%s", type(exc).__name__)
        print("Repair Planを検証できませんでした。", file=sys.stderr)
        return 2
    try:
        write_repair_approval(report_json, approval)
    except Exception as exc:  # noqa: BLE001 - report failures map to exit code 2.
        LOGGER.error("repair_approval_write_failed exception_type=%s", type(exc).__name__)
        print("Repair Approvalを書き込めませんでした。", file=sys.stderr)
        return 2
    print(f"Repair承認を生成しました: 承認action={len(approval.approved_actions)}")
    return 0


def _run_repair_preflight(
    package_root: Path,
    *,
    manifest_path: Path | None,
    plan_path: Path,
    approval_path: Path,
    report_json: Path | None,
) -> int:
    if not package_root.is_dir() or _is_linked_directory(package_root):
        print("エラー: 存在する通常のpackage rootディレクトリを指定してください", file=sys.stderr)
        return 2
    inputs = (plan_path, approval_path) + ((manifest_path,) if manifest_path is not None else ())
    if any(path.is_symlink() or not path.is_file() for path in inputs):
        print(
            "エラー: Manifest、Repair Plan、Approvalには通常fileを指定してください", file=sys.stderr
        )
        return 2
    if report_json is not None and (
        report_json.name.casefold().endswith(".metadata.json")
        or report_json.suffix.casefold() in {".md", ".csv"}
        or any(_paths_are_equal(report_json, path) for path in inputs)
    ):
        print("エラー: Preflight reportの出力先が検証対象と競合します", file=sys.stderr)
        return 2
    if (
        report_json is not None
        and (report_json.is_symlink() or report_json.exists())
        and not is_repair_preflight_report(report_json)
    ):
        print("エラー: 既存のRepair Preflight以外は上書きできません", file=sys.stderr)
        return 2
    try:
        preflight = build_repair_preflight(
            package_root,
            manifest_path=manifest_path,
            plan_path=plan_path,
            approval_path=approval_path,
        )
    except Exception as exc:  # noqa: BLE001 - invalid bindings map to exit code 2.
        LOGGER.error("repair_preflight_invalid exception_type=%s", type(exc).__name__)
        print("Repair Preflightの入力またはbindingを検証できませんでした。", file=sys.stderr)
        return 2
    for action in preflight.actions:
        print(
            f"実行前提: ファイル={action.repair_action.path} "
            f"操作={action.repair_action.action.value} status={action.status}"
        )
    print(
        "Repair実行前検証: "
        f"action={len(preflight.actions)} ready={preflight.ready} blocked={preflight.blocked}"
    )
    if report_json is not None:
        try:
            write_repair_preflight(report_json, preflight)
        except Exception as exc:  # noqa: BLE001 - report failures map to exit code 2.
            LOGGER.error("repair_preflight_write_failed exception_type=%s", type(exc).__name__)
            print("Repair Preflight reportを書き込めませんでした。", file=sys.stderr)
            return 2
    return preflight.exit_code


def _run_repair_execution(
    package_root: Path,
    *,
    manifest_path: Path,
    plan_path: Path,
    approval_path: Path,
    preflight_path: Path,
    report_json: Path | None,
    backup_dir: Path | None,
) -> int:
    if not package_root.is_dir() or _is_linked_directory(package_root):
        print("エラー: 存在する通常のpackage rootディレクトリを指定してください", file=sys.stderr)
        return 2
    inputs = (manifest_path, plan_path, approval_path, preflight_path)
    if any(path.is_symlink() or not path.is_file() for path in inputs):
        print("エラー: Manifest、Plan、Approval、Preflightを検証できません", file=sys.stderr)
        return 2
    if report_json is not None and (
        report_json.name.casefold().endswith(".metadata.json")
        or report_json.suffix.casefold() in {".md", ".csv"}
        or any(_paths_are_equal(report_json, path) for path in inputs)
    ):
        print("エラー: Execution Reportの出力先が実行対象と競合します", file=sys.stderr)
        return 2
    if (
        report_json is not None
        and (report_json.is_symlink() or report_json.exists())
        and not is_execution_report(report_json)
    ):
        print("エラー: 既存のExecution Report以外は上書きできません", file=sys.stderr)
        return 2
    try:
        report = execute_repair(
            package_root,
            manifest_path=manifest_path,
            plan_path=plan_path,
            approval_path=approval_path,
            preflight_path=preflight_path,
            backup_dir=backup_dir,
        )
    except RepairExecutionInputError as exc:
        LOGGER.error("repair_execution_input_invalid exception_type=%s", type(exc).__name__)
        print("Repair Executionの入力またはbindingを検証できませんでした。", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - unexpected setup failures remain sanitized.
        LOGGER.error("repair_execution_failed exception_type=%s", type(exc).__name__)
        print("Repair Executionを開始できませんでした。", file=sys.stderr)
        return 2

    for action in report.actions:
        print(
            f"修復実行: ファイル={action.repair_action.path} "
            f"操作={action.repair_action.action.value} status={action.status}"
        )
    summary = report.payload()["summary"]
    assert isinstance(summary, dict)
    print(
        "Repair実行完了: "
        f"計画={summary['planned']} 成功={summary['succeeded']} "
        f"失敗={summary['failed']} rollback={summary['rolled_back']} "
        f"未実行={summary['not_run']}"
    )
    if report_json is not None:
        try:
            write_execution_report(report_json, report)
        except Exception as exc:  # noqa: BLE001 - report failure must not rollback mutations.
            LOGGER.error(
                "repair_execution_report_write_failed exception_type=%s", type(exc).__name__
            )
            print("Execution Reportを書き込めませんでした。", file=sys.stderr)
            return 2
    return report.exit_code


def _run_backup_inventory(
    backup_root: Path,
    *,
    package_root: Path,
    report_json: Path | None,
) -> int:
    if report_json is not None and (
        path_is_within(report_json, backup_root) or path_uses_link_or_reparse(report_json)
    ):
        print("エラー: Inventory Reportはbackup root外へ出力してください", file=sys.stderr)
        return 2
    if (
        report_json is not None
        and (report_json.exists() or report_json.is_symlink())
        and not is_backup_inventory_report(report_json)
    ):
        print("エラー: 既存のBackup Inventory以外は上書きできません", file=sys.stderr)
        return 2
    try:
        inventory = build_backup_inventory(package_root, backup_root)
    except BackupInventoryInputError as exc:
        LOGGER.error("backup_inventory_invalid exception_type=%s", type(exc).__name__)
        print("Backup Inventoryの入力を安全に検証できませんでした。", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - local paths remain sanitized.
        LOGGER.error("backup_inventory_failed exception_type=%s", type(exc).__name__)
        print("Backup Inventoryを生成できませんでした。", file=sys.stderr)
        return 2

    for session in inventory.sessions:
        print(
            f"Backup session: session={session.session} "
            f"分類={session.classification.value} "
            f"planning_eligible={str(session.planning_eligible).lower()}"
        )
    summary = inventory.payload()["summary"]
    assert isinstance(summary, dict)
    print(
        "Backup Inventory: "
        f"session={summary['sessions']} managed={summary['managed']} "
        f"orphan={summary['orphaned']} legacy={summary['legacy_unmanaged']} "
        f"planning_eligible={summary['planning_eligible']}"
    )
    if report_json is not None:
        try:
            write_backup_inventory(report_json, inventory)
        except Exception as exc:  # noqa: BLE001 - report failures map to exit code 2.
            LOGGER.error("backup_inventory_write_failed exception_type=%s", type(exc).__name__)
            print("Backup Inventory Reportを書き込めませんでした。", file=sys.stderr)
            return 2
    return inventory.exit_code


def _cleanup_lifecycle_paths_are_safe(
    backup_root: Path,
    input_path: Path,
    report_path: Path,
) -> bool:
    try:
        backup_resolved = backup_root.resolve(strict=False)
        backup_overlaps_repository = any(
            backup_resolved == root or backup_resolved.is_relative_to(root)
            for root in repository_roots(backup_root)
        )
        return (
            backup_root.is_dir()
            and not path_uses_link_or_reparse(backup_root)
            and not backup_overlaps_repository
            and not path_is_within(input_path, backup_root)
            and not path_is_within(report_path, backup_root)
            and not path_uses_link_or_reparse(input_path)
            and not path_uses_link_or_reparse(report_path)
            and not _paths_are_equal(input_path, report_path)
        )
    except OSError:
        return False


def _run_backup_cleanup_plan(
    inventory_path: Path,
    *,
    backup_root: Path,
    sessions: Sequence[str],
    report_json: Path,
) -> int:
    if not _cleanup_lifecycle_paths_are_safe(backup_root, inventory_path, report_json):
        print("エラー: Cleanup Planの入力または出力先を安全に検証できません", file=sys.stderr)
        return 2
    if (report_json.exists() or report_json.is_symlink()) and not is_backup_cleanup_plan_report(
        report_json
    ):
        print("エラー: 既存のBackup Cleanup Plan以外は上書きできません", file=sys.stderr)
        return 2
    try:
        plan = build_backup_cleanup_plan(inventory_path, tuple(sessions))
    except Exception as exc:  # noqa: BLE001 - input details remain sanitized.
        LOGGER.error("backup_cleanup_plan_invalid exception_type=%s", type(exc).__name__)
        print("Backup Cleanup Planの入力を検証できませんでした。", file=sys.stderr)
        return 2
    for action in plan.actions:
        print(f"Cleanup候補: session={action.session} eligible={str(action.eligible).lower()}")
    try:
        write_backup_cleanup_plan(report_json, plan)
    except Exception as exc:  # noqa: BLE001 - output details remain sanitized.
        LOGGER.error("backup_cleanup_plan_write_failed exception_type=%s", type(exc).__name__)
        print("Backup Cleanup Planを書き込めませんでした。", file=sys.stderr)
        return 2
    print(
        f"Backup Cleanup Plan: requested={len(plan.actions)} "
        f"planned={plan.planned} blocked={len(plan.actions) - plan.planned}"
    )
    return 0


def _run_backup_cleanup_approval(
    plan_path: Path,
    *,
    backup_root: Path,
    report_json: Path,
) -> int:
    if not _cleanup_lifecycle_paths_are_safe(backup_root, plan_path, report_json):
        print("エラー: Cleanup Approvalの入力または出力先を安全に検証できません", file=sys.stderr)
        return 2
    if (report_json.exists() or report_json.is_symlink()) and not is_backup_cleanup_approval_report(
        report_json
    ):
        print("エラー: 既存のBackup Cleanup Approval以外は上書きできません", file=sys.stderr)
        return 2
    try:
        approval = build_backup_cleanup_approval(plan_path)
    except Exception as exc:  # noqa: BLE001 - input details remain sanitized.
        LOGGER.error("backup_cleanup_approval_invalid exception_type=%s", type(exc).__name__)
        print("Backup Cleanup Planを検証できませんでした。", file=sys.stderr)
        return 2
    try:
        write_backup_cleanup_approval(report_json, approval)
    except Exception as exc:  # noqa: BLE001 - output details remain sanitized.
        LOGGER.error("backup_cleanup_approval_write_failed exception_type=%s", type(exc).__name__)
        print("Backup Cleanup Approvalを書き込めませんでした。", file=sys.stderr)
        return 2
    print(f"Backup Cleanup承認: approved={len(approval.approved_actions)}")
    return 0


def _run_backup_cleanup_execution(
    backup_root: Path,
    *,
    package_root: Path,
    inventory_path: Path,
    plan_path: Path,
    approval_path: Path,
    report_json: Path,
) -> int:
    inputs = (inventory_path, plan_path, approval_path)
    if not all(
        _cleanup_lifecycle_paths_are_safe(backup_root, input_path, report_json)
        for input_path in inputs
    ) or (
        len({_path_comparison_key(path) for path in inputs}) != len(inputs)
        or path_is_within(report_json, package_root)
    ):
        print("エラー: Cleanup Executionの入力または出力先を安全に検証できません", file=sys.stderr)
        return 2
    try:
        audit_output_state = capture_backup_cleanup_audit_output(report_json)
    except (OSError, ValueError):
        print("エラー: 既存のBackup Cleanup Audit以外は上書きできません", file=sys.stderr)
        return 2
    try:
        audit = execute_backup_cleanup(
            package_root,
            backup_root,
            inventory_path=inventory_path,
            plan_path=plan_path,
            approval_path=approval_path,
        )
    except BackupCleanupExecutionInputError as exc:
        LOGGER.error("backup_cleanup_execution_invalid exception_type=%s", type(exc).__name__)
        print("Cleanup Executionのschemaまたはbindingを検証できませんでした。", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - setup details remain sanitized.
        LOGGER.error("backup_cleanup_execution_failed exception_type=%s", type(exc).__name__)
        print("Cleanup Executionを開始できませんでした。", file=sys.stderr)
        return 2
    for action in audit.actions:
        print(f"Cleanup実行: session={action.session} status={action.status.value}")
    try:
        write_backup_cleanup_audit(
            report_json,
            audit,
            expected_output=audit_output_state,
        )
    except Exception as exc:  # noqa: BLE001 - deletion is never rolled back for report failure.
        LOGGER.error("backup_cleanup_audit_write_failed exception_type=%s", type(exc).__name__)
        print("Cleanup Auditを書き込めませんでした。", file=sys.stderr)
        return 2
    summary = audit.payload()["summary"]
    assert isinstance(summary, dict)
    print(
        "Backup Cleanup: "
        f"planned={summary['planned']} deleted={summary['deleted']} "
        f"failed={summary['failed']} not_run={summary['not_run']}"
    )
    return audit.exit_code


def run(
    argv: Sequence[str] | None = None,
    *,
    converter_factory: Callable[..., Converter] = build_docling_converter,
) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        return _run_package_validation(
            args.package_root,
            manifest_path=args.manifest,
            report_json=args.report_json,
            strict=args.strict,
        )
    if args.command == "repair-plan":
        return _run_repair_plan(
            args.package_root,
            manifest_path=args.manifest,
            report_json=args.report_json,
            strict=args.strict,
        )
    if args.command == "approve-repair":
        return _run_repair_approval(args.plan_json, report_json=args.report_json)
    if args.command == "repair-preflight":
        return _run_repair_preflight(
            args.package_root,
            manifest_path=args.manifest,
            plan_path=args.plan,
            approval_path=args.approval,
            report_json=args.report_json,
        )
    if args.command == "repair-execute":
        return _run_repair_execution(
            args.package_root,
            manifest_path=args.manifest,
            plan_path=args.plan,
            approval_path=args.approval,
            preflight_path=args.preflight,
            report_json=args.report_json,
            backup_dir=args.backup_dir,
        )
    if args.command == "backup-inventory":
        return _run_backup_inventory(
            args.backup_root,
            package_root=args.package_root,
            report_json=args.report_json,
        )
    if args.command == "backup-cleanup-plan":
        return _run_backup_cleanup_plan(
            args.inventory_json,
            backup_root=args.backup_root,
            sessions=args.session,
            report_json=args.report_json,
        )
    if args.command == "approve-backup-cleanup":
        return _run_backup_cleanup_approval(
            args.plan_json,
            backup_root=args.backup_root,
            report_json=args.report_json,
        )
    if args.command == "backup-cleanup-execute":
        return _run_backup_cleanup_execution(
            args.backup_root,
            package_root=args.package_root,
            inventory_path=args.inventory,
            plan_path=args.plan,
            approval_path=args.approval,
            report_json=args.report_json,
        )
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
            metadata_sidecar=args.metadata_sidecar,
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

    if args.metadata_sidecar:
        sidecar_path = metadata_sidecar_path(args.output)
        conflict_paths = (
            args.input,
            args.output,
            args.quality_report_json,
            args.manifest_json,
        )
        if any(
            path is not None and _paths_are_equal(sidecar_path, path) for path in conflict_paths
        ):
            print(
                "エラー: Metadata sidecarの出力先が入力または他の出力先と競合します",
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
        metadata_sidecar=args.metadata_sidecar,
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
    item: ArtifactManifestItem | None = None,
) -> ArtifactManifest:
    if item is not None:
        return ArtifactManifest(settings=settings, items=(item,))
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


def _document_metadata_settings(
    settings: ArtifactManifestSettings,
) -> DocumentMetadataSettings:
    return DocumentMetadataSettings(
        table_structure=settings.table_structure,
        normalization_profile=settings.normalization_profile,
        artifacts_path_configured=settings.artifacts_path_configured,
    )


def _write_document_metadata_safely(
    sidecar_path: Path,
    item: ArtifactManifestItem,
    settings: ArtifactManifestSettings,
) -> bool:
    try:
        write_document_metadata(
            sidecar_path,
            build_document_metadata(item, _document_metadata_settings(settings)),
        )
    except Exception as exc:  # noqa: BLE001 - sidecar failures map to exit code 2.
        LOGGER.error("metadata_sidecar_write_failed exception_type=%s", type(exc).__name__)
        print("Metadata sidecarを書き込めませんでした。", file=sys.stderr)
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
    metadata_sidecar: bool = False,
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

    artifact_item: ArtifactManifestItem | None = None
    if metadata_sidecar or manifest_json is not None:
        try:
            artifact_item = build_manifest_item(
                input_path=request.input_path,
                output_path=request.output_path,
                input_name=request.input_path.name,
                output_name=request.output_path.name,
                status=ManifestStatus.SUCCEEDED,
            )
        except Exception as exc:  # noqa: BLE001 - checksum failures map to exit code 2.
            LOGGER.error("artifact_digest_failed exception_type=%s", type(exc).__name__)
            if metadata_sidecar:
                print("Metadata sidecarを書き込めませんでした。", file=sys.stderr)
            if manifest_json is not None:
                print("Artifact Manifestを書き込めませんでした。", file=sys.stderr)
            report_failed = True

    if metadata_sidecar and artifact_item is not None:
        report_failed = (
            not _write_document_metadata_safely(
                metadata_sidecar_path(request.output_path),
                artifact_item,
                settings,
            )
            or report_failed
        )
    if manifest_json is not None and artifact_item is not None:
        report_failed = (
            not _write_artifact_manifest_safely(
                manifest_json,
                lambda: _single_artifact_manifest(
                    request,
                    status=ManifestStatus.SUCCEEDED,
                    settings=settings,
                    item=artifact_item,
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
    prepared_items: tuple[ArtifactManifestItem, ...] | None = None,
) -> ArtifactManifest:
    if prepared_items is not None:
        return ArtifactManifest(settings=settings, items=prepared_items)
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


def _prepare_batch_artifacts(
    result: BatchResult,
    requests: Sequence[ConversionRequest],
    *,
    settings: ArtifactManifestSettings,
    metadata_sidecar: bool,
    manifest_requested: bool,
) -> tuple[ArtifactManifest | None, bool]:
    status_map = {
        BatchItemStatus.SUCCEEDED: ManifestStatus.SUCCEEDED,
        BatchItemStatus.SKIPPED: ManifestStatus.SKIPPED,
        BatchItemStatus.FAILED: ManifestStatus.FAILED,
    }
    manifest_items: list[ArtifactManifestItem] = []
    manifest_complete = True
    report_failed = False

    for request, result_item in zip(requests, result.items, strict=True):
        if result_item.status is BatchItemStatus.FAILED and not manifest_requested:
            continue
        try:
            item = build_manifest_item(
                input_path=request.input_path,
                output_path=request.output_path,
                input_name=result_item.input_name,
                output_name=result_item.output_name,
                status=status_map[result_item.status],
                error_category=(
                    result_item.error_category.value
                    if result_item.error_category is not None
                    else None
                ),
                message=result_item.message,
            )
        except Exception as exc:  # noqa: BLE001 - checksum failures map to exit code 2.
            LOGGER.error("artifact_digest_failed exception_type=%s", type(exc).__name__)
            if metadata_sidecar and result_item.status is not BatchItemStatus.FAILED:
                print("Metadata sidecarを書き込めませんでした。", file=sys.stderr)
            manifest_complete = False
            report_failed = True
            continue

        if manifest_requested:
            manifest_items.append(item)
        if (
            metadata_sidecar
            and result_item.status is not BatchItemStatus.FAILED
            and not _write_document_metadata_safely(
                metadata_sidecar_path(request.output_path),
                item,
                settings,
            )
        ):
            report_failed = True

    manifest: ArtifactManifest | None = None
    if manifest_requested:
        if manifest_complete and len(manifest_items) == len(result.items):
            manifest = _batch_artifact_manifest(
                result,
                requests,
                settings=settings,
                prepared_items=tuple(manifest_items),
            )
        else:
            print("Artifact Manifestを書き込めませんでした。", file=sys.stderr)
    return manifest, report_failed


def _write_requested_reports(
    result: BatchResult,
    *,
    report_json: Path | None,
    report_csv: Path | None,
    quality_report_json: Path | None = None,
    quality_report: QualityReport | None = None,
    manifest_json: Path | None = None,
    manifest_factory: Callable[[], ArtifactManifest] | None = None,
    initial_report_failed: bool = False,
) -> bool:
    report_failed = initial_report_failed
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
    if manifest_json is not None and (
        manifest_factory is None
        or not _write_artifact_manifest_safely(manifest_json, manifest_factory)
    ):
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
    requests: Sequence[ConversionRequest],
    settings: ArtifactManifestSettings,
    report_json: Path | None,
    report_csv: Path | None,
    quality_report_json: Path | None,
    quality_report: QualityReport,
    manifest_json: Path | None,
    metadata_sidecar: bool,
) -> int:
    print(_format_batch_summary(result))
    prepared_manifest: ArtifactManifest | None = None
    artifact_failed = False
    if metadata_sidecar or manifest_json is not None:
        prepared_manifest, artifact_failed = _prepare_batch_artifacts(
            result,
            requests,
            settings=settings,
            metadata_sidecar=metadata_sidecar,
            manifest_requested=manifest_json is not None,
        )
    report_failed = _write_requested_reports(
        result,
        report_json=report_json,
        report_csv=report_csv,
        quality_report_json=quality_report_json,
        quality_report=quality_report,
        manifest_json=manifest_json,
        manifest_factory=((lambda: prepared_manifest) if prepared_manifest is not None else None),
        initial_report_failed=artifact_failed,
    )
    return 2 if report_failed or artifact_failed else result.exit_code


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
    metadata_sidecar: bool,
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

    if metadata_sidecar and _metadata_sidecars_conflict(
        requests,
        (report_json, report_csv, quality_report_json, manifest_json),
    ):
        print(
            "エラー: Metadata sidecarの出力先が入力または他の出力先と競合します",
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
                requests,
                manifest_settings,
                report_json,
                report_csv,
                quality_report_json,
                QualityReport(()),
                manifest_json,
                metadata_sidecar,
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
        requests,
        manifest_settings,
        report_json,
        report_csv,
        quality_report_json,
        QualityReport(tuple(quality_items)),
        manifest_json,
        metadata_sidecar,
    )
