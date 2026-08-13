"""Read-only validation for Knowledge Package artifacts."""

from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

from knowledge_importer.artifact_manifest import digest_file
from knowledge_importer.json_writer import write_json_atomically

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


class ValidationSeverity(Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    path: str
    severity: ValidationSeverity
    category: str
    message: str

    def payload(self) -> dict[str, str]:
        return {
            "path": self.path,
            "severity": self.severity.value,
            "category": self.category,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class PackageValidationResult:
    checked_paths: tuple[str, ...]
    issues: tuple[ValidationIssue, ...]

    @property
    def failed(self) -> int:
        return len(
            {issue.path for issue in self.issues if issue.severity is ValidationSeverity.ERROR}
        )

    @property
    def warnings(self) -> int:
        return len(
            {issue.path for issue in self.issues if issue.severity is ValidationSeverity.WARNING}
        )

    @property
    def passed(self) -> int:
        return max(len(self.checked_paths) - self.failed, 0)

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0

    def payload(self) -> dict[str, object]:
        return {
            "report_type": "knowledge-package-validation",
            "schema_version": 1,
            "summary": {
                "checked": len(self.checked_paths),
                "passed": self.passed,
                "failed": self.failed,
                "warnings": self.warnings,
            },
            "issues": [issue.payload() for issue in self.issues],
        }


@dataclass(frozen=True, slots=True)
class _SidecarRecord:
    sidecar_path: str
    input_path: str
    output_path: str
    status: str
    source_bytes: int
    source_sha256: str
    artifact_bytes: int
    artifact_sha256: str
    engine: dict[str, str]
    settings: dict[str, object]


@dataclass(frozen=True, slots=True)
class ManifestRecord:
    input_path: str
    output_path: str
    status: str
    input_bytes: int | None
    input_sha256: str | None
    output_bytes: int | None
    output_sha256: str | None


@dataclass(frozen=True, slots=True)
class ManifestState:
    records: tuple[ManifestRecord, ...]
    engine: dict[str, str]
    settings: dict[str, object]


def _sort_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _issue_sort_key(issue: ValidationIssue) -> tuple[str, str, str, str]:
    return (_sort_key(issue.path), issue.category, issue.severity.value, issue.message)


def _is_linked_directory(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _discover(root: Path, suffix: str) -> tuple[Path, ...]:
    found: list[Path] = []
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = [
            name for name in directories if not _is_linked_directory(current_path / name)
        ]
        for name in files:
            path = current_path / name
            if name.casefold().endswith(suffix.casefold()):
                found.append(path)
    return tuple(sorted(found, key=lambda path: _sort_key(path.relative_to(root).as_posix())))


def _safe_relative_path(value: object) -> str | None:
    if not isinstance(value, str) or not value or "\\" in value or _WINDOWS_DRIVE.match(value):
        return None
    pure = PurePosixPath(value)
    if (
        pure.as_posix() == "."
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        return None
    return pure.as_posix()


def _resolve_inside(root: Path, relative_path: str) -> Path | None:
    root_resolved = root.resolve()
    candidate = root.joinpath(*PurePosixPath(relative_path).parts)
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        return None
    return resolved if resolved.is_relative_to(root_resolved) else None


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _read_json(path: Path) -> object:
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def _invalid_schema(path: str, message: str) -> ValidationIssue:
    return ValidationIssue(path, ValidationSeverity.ERROR, "invalid-schema", message)


def _parse_sidecar(
    path: Path,
    root: Path,
) -> tuple[_SidecarRecord | None, list[ValidationIssue]]:
    relative = path.relative_to(root).as_posix()
    resolved_sidecar = _resolve_inside(root, relative)
    if resolved_sidecar is None:
        return None, [
            ValidationIssue(
                relative,
                ValidationSeverity.ERROR,
                "outside-package-root",
                "Metadata sidecarがpackage root外を参照しています",
            )
        ]
    try:
        payload = _read_json(resolved_sidecar)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, [
            ValidationIssue(
                relative,
                ValidationSeverity.ERROR,
                "invalid-json",
                "Metadata sidecarをJSONとして読み取れません",
            )
        ]
    if not isinstance(payload, dict):
        return None, [_invalid_schema(relative, "Metadata sidecarのrootはobjectが必要です")]
    if payload.get("schema_version") != 1:
        return None, [
            ValidationIssue(
                relative,
                ValidationSeverity.ERROR,
                "unsupported-schema",
                "Metadata sidecarのschema versionをサポートしていません",
            )
        ]
    if payload.get("report_type") != "knowledge-document-metadata":
        return None, [_invalid_schema(relative, "Metadata sidecarのreport_typeが不正です")]

    engine = payload.get("engine")
    document = payload.get("document")
    artifact = payload.get("artifact")
    source = payload.get("source")
    settings = payload.get("settings")
    if not all(isinstance(value, dict) for value in (engine, document, artifact, source, settings)):
        return None, [_invalid_schema(relative, "Metadata sidecarの必須objectが不足しています")]
    assert isinstance(engine, dict)
    assert isinstance(document, dict)
    assert isinstance(artifact, dict)
    assert isinstance(source, dict)
    assert isinstance(settings, dict)

    raw_input_path = document.get("input_path")
    raw_output_path = document.get("output_path")
    input_path = _safe_relative_path(raw_input_path)
    output_path = _safe_relative_path(raw_output_path)
    if (
        isinstance(raw_input_path, str)
        and input_path is None
        or isinstance(raw_output_path, str)
        and output_path is None
    ):
        return None, [
            ValidationIssue(
                relative,
                ValidationSeverity.ERROR,
                "outside-package-root",
                "Metadata sidecarに安全でないrelative pathがあります",
            )
        ]
    status = document.get("status")
    normalization = settings.get("normalization_profile")
    valid = (
        engine.get("name") == "knowledge-importer"
        and isinstance(engine.get("version"), str)
        and bool(engine.get("version"))
        and input_path is not None
        and output_path is not None
        and status in {"succeeded", "skipped"}
        and _is_nonnegative_int(artifact.get("bytes"))
        and _is_digest(artifact.get("sha256"))
        and _is_nonnegative_int(source.get("bytes"))
        and _is_digest(source.get("sha256"))
        and isinstance(settings.get("table_structure"), bool)
        and (normalization is None or isinstance(normalization, str))
        and isinstance(settings.get("artifacts_path_configured"), bool)
    )
    if not valid:
        return None, [_invalid_schema(relative, "Metadata sidecarの必須fieldが不正です")]

    expected_sidecar = PurePosixPath(output_path).with_suffix(".metadata.json").as_posix()
    issues: list[ValidationIssue] = []
    if _sort_key(relative) != _sort_key(expected_sidecar):
        issues.append(
            ValidationIssue(
                relative,
                ValidationSeverity.ERROR,
                "path-mismatch",
                "Metadata sidecar pathがdocument.output_pathと一致しません",
            )
        )

    artifact_path = _resolve_inside(root, output_path)
    if artifact_path is None:
        issues.append(
            ValidationIssue(
                relative,
                ValidationSeverity.ERROR,
                "outside-package-root",
                "Markdown pathがpackage root外を参照しています",
            )
        )
    elif not artifact_path.is_file():
        issues.append(
            ValidationIssue(
                relative,
                ValidationSeverity.ERROR,
                "missing-artifact",
                "対応するMarkdownが存在しません",
            )
        )
    else:
        actual = digest_file(artifact_path)
        if actual.bytes != artifact.get("bytes"):
            issues.append(
                ValidationIssue(
                    relative,
                    ValidationSeverity.ERROR,
                    "artifact-size-mismatch",
                    "Markdown sizeがsidecarと一致しません",
                )
            )
        if actual.sha256 != artifact.get("sha256"):
            issues.append(
                ValidationIssue(
                    relative,
                    ValidationSeverity.ERROR,
                    "artifact-digest-mismatch",
                    "Markdown digestがsidecarと一致しません",
                )
            )

    record = _SidecarRecord(
        sidecar_path=relative,
        input_path=input_path,
        output_path=output_path,
        status=status,
        source_bytes=source["bytes"],
        source_sha256=source["sha256"],
        artifact_bytes=artifact["bytes"],
        artifact_sha256=artifact["sha256"],
        engine={"name": engine["name"], "version": engine["version"]},
        settings={
            "table_structure": settings["table_structure"],
            "normalization_profile": normalization,
            "artifacts_path_configured": settings["artifacts_path_configured"],
        },
    )
    return record, issues


def _parse_manifest(
    path: Path,
) -> tuple[
    tuple[ManifestRecord, ...] | None,
    dict[str, str] | None,
    dict[str, object] | None,
    list[ValidationIssue],
]:
    display_path = path.name
    try:
        payload = _read_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return (
            None,
            None,
            None,
            [
                ValidationIssue(
                    display_path,
                    ValidationSeverity.ERROR,
                    "invalid-json",
                    "Artifact ManifestをJSONとして読み取れません",
                )
            ],
        )
    if not isinstance(payload, dict):
        return None, None, None, [_invalid_schema(display_path, "Manifest rootが不正です")]
    if payload.get("schema_version") != 1:
        return (
            None,
            None,
            None,
            [
                ValidationIssue(
                    display_path,
                    ValidationSeverity.ERROR,
                    "unsupported-schema",
                    "Artifact Manifestのschema versionをサポートしていません",
                )
            ],
        )
    if payload.get("report_type") != "knowledge-artifact-manifest":
        return None, None, None, [_invalid_schema(display_path, "Manifest report_typeが不正です")]
    engine = payload.get("engine")
    settings = payload.get("settings")
    items = payload.get("items")
    if (
        not isinstance(engine, dict)
        or not isinstance(settings, dict)
        or not isinstance(items, list)
    ):
        return (
            None,
            None,
            None,
            [_invalid_schema(display_path, "Manifest必須fieldが不足しています")],
        )
    selected_settings = {
        key: settings.get(key)
        for key in ("table_structure", "normalization_profile", "artifacts_path_configured")
    }
    if not (
        engine.get("name") == "knowledge-importer"
        and isinstance(engine.get("version"), str)
        and isinstance(selected_settings["table_structure"], bool)
        and (
            selected_settings["normalization_profile"] is None
            or isinstance(selected_settings["normalization_profile"], str)
        )
        and isinstance(selected_settings["artifacts_path_configured"], bool)
    ):
        return None, None, None, [_invalid_schema(display_path, "Manifest設定が不正です")]

    records: list[ManifestRecord] = []
    for raw_item in items:
        if not isinstance(raw_item, dict):
            return None, None, None, [_invalid_schema(display_path, "Manifest itemが不正です")]
        input_data = raw_item.get("input")
        output_data = raw_item.get("output")
        status = raw_item.get("status")
        if not isinstance(input_data, dict) or not isinstance(output_data, dict):
            return None, None, None, [_invalid_schema(display_path, "Manifest digestが不正です")]
        raw_input_path = input_data.get("path")
        raw_output_path = output_data.get("path")
        input_path = _safe_relative_path(raw_input_path)
        output_path = _safe_relative_path(raw_output_path)
        if (
            isinstance(raw_input_path, str)
            and input_path is None
            or isinstance(raw_output_path, str)
            and output_path is None
        ):
            return (
                None,
                None,
                None,
                [
                    ValidationIssue(
                        display_path,
                        ValidationSeverity.ERROR,
                        "outside-package-root",
                        "Manifestに安全でないrelative pathがあります",
                    )
                ],
            )
        if (
            input_path is None
            or output_path is None
            or status
            not in {
                "succeeded",
                "skipped",
                "failed",
            }
        ):
            return (
                None,
                None,
                None,
                [_invalid_schema(display_path, "Manifest item fieldが不正です")],
            )
        input_bytes = input_data.get("bytes")
        input_sha = input_data.get("sha256")
        output_bytes = output_data.get("bytes")
        output_sha = output_data.get("sha256")
        if status in {"succeeded", "skipped"} and not (
            _is_nonnegative_int(input_bytes)
            and _is_digest(input_sha)
            and _is_nonnegative_int(output_bytes)
            and _is_digest(output_sha)
        ):
            return None, None, None, [_invalid_schema(display_path, "Manifest digestが不正です")]
        records.append(
            ManifestRecord(
                input_path,
                output_path,
                status,
                input_bytes,
                input_sha,
                output_bytes,
                output_sha,
            )
        )
    return (
        tuple(records),
        {"name": engine["name"], "version": engine["version"]},
        selected_settings,
        [],
    )


def _compare_manifest(
    records: tuple[ManifestRecord, ...],
    manifest_engine: dict[str, str],
    manifest_settings: dict[str, object],
    sidecars: dict[str, _SidecarRecord],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    matched: set[str] = set()
    for item in records:
        expected = PurePosixPath(item.output_path).with_suffix(".metadata.json").as_posix()
        key = _sort_key(expected)
        sidecar = sidecars.get(key)
        if item.status == "failed":
            if sidecar is not None:
                matched.add(key)
                issues.append(
                    ValidationIssue(
                        sidecar.sidecar_path,
                        ValidationSeverity.ERROR,
                        "stale-sidecar",
                        "failed Manifest itemにsidecarが存在します",
                    )
                )
            continue
        if sidecar is None:
            issues.append(
                ValidationIssue(
                    expected,
                    ValidationSeverity.ERROR,
                    "missing-sidecar",
                    "Manifest itemに対応するsidecarが存在しません",
                )
            )
            continue
        matched.add(key)
        if _sort_key(sidecar.input_path) != _sort_key(item.input_path) or _sort_key(
            sidecar.output_path
        ) != _sort_key(item.output_path):
            issues.append(
                ValidationIssue(
                    sidecar.sidecar_path,
                    ValidationSeverity.ERROR,
                    "path-mismatch",
                    "Manifestとsidecarのdocument pathが一致しません",
                )
            )
        if sidecar.status != item.status or sidecar.engine != manifest_engine:
            issues.append(
                ValidationIssue(
                    sidecar.sidecar_path,
                    ValidationSeverity.ERROR,
                    "manifest-sidecar-mismatch",
                    "Manifestとsidecarのstatusまたはengineが一致しません",
                )
            )
        if sidecar.settings != manifest_settings:
            issues.append(
                ValidationIssue(
                    sidecar.sidecar_path,
                    ValidationSeverity.ERROR,
                    "settings-mismatch",
                    "Manifestとsidecarのsettingsが一致しません",
                )
            )
        if (
            sidecar.source_bytes != item.input_bytes
            or sidecar.source_sha256 != item.input_sha256
            or sidecar.artifact_bytes != item.output_bytes
            or sidecar.artifact_sha256 != item.output_sha256
        ):
            issues.append(
                ValidationIssue(
                    sidecar.sidecar_path,
                    ValidationSeverity.ERROR,
                    "manifest-sidecar-mismatch",
                    "Manifestとsidecarのdigestが一致しません",
                )
            )

    for key, sidecar in sidecars.items():
        if key not in matched:
            issues.append(
                ValidationIssue(
                    sidecar.sidecar_path,
                    ValidationSeverity.ERROR,
                    "orphan-sidecar",
                    "Manifestに対応itemがないsidecarです",
                )
            )
    return issues


def read_manifest_records(path: Path) -> tuple[ManifestRecord, ...]:
    """Read Artifact Manifest v1 records through the validation parser."""

    return read_manifest_state(path).records


def read_manifest_state(path: Path) -> ManifestState:
    """Read the Manifest v1 records and settings through the validation parser."""

    records, engine, settings, issues = _parse_manifest(path)
    if records is None or engine is None or settings is None or issues:
        raise ValueError("invalid Artifact Manifest")
    return ManifestState(records, engine, settings)


def validate_package(
    package_root: Path,
    *,
    manifest_path: Path | None = None,
    strict: bool = False,
) -> PackageValidationResult:
    """Validate package artifacts without modifying them."""

    sidecar_paths = _discover(package_root, ".metadata.json")
    issues: list[ValidationIssue] = []
    records: dict[str, _SidecarRecord] = {}
    checked = {path.relative_to(package_root).as_posix() for path in sidecar_paths}
    for path in sidecar_paths:
        record, record_issues = _parse_sidecar(path, package_root)
        issues.extend(record_issues)
        if record is not None:
            records[_sort_key(record.sidecar_path)] = record

    if manifest_path is not None:
        manifest_records, engine, settings, manifest_issues = _parse_manifest(manifest_path)
        issues.extend(manifest_issues)
        if manifest_records is not None and engine is not None and settings is not None:
            for item in manifest_records:
                expected_sidecar = (
                    PurePosixPath(item.output_path).with_suffix(".metadata.json").as_posix()
                )
                checked.add(expected_sidecar)
                if _resolve_inside(package_root, item.output_path) is None:
                    issues.append(
                        ValidationIssue(
                            expected_sidecar,
                            ValidationSeverity.ERROR,
                            "outside-package-root",
                            "Manifest output pathがpackage root外へ解決されます",
                        )
                    )
            issues.extend(_compare_manifest(manifest_records, engine, settings, records))
            manifest_outputs = {_sort_key(item.output_path) for item in manifest_records}
            severity = ValidationSeverity.ERROR if strict else ValidationSeverity.WARNING
            for markdown_path in _discover(package_root, ".md"):
                relative = markdown_path.relative_to(package_root).as_posix()
                if _sort_key(relative) not in manifest_outputs:
                    checked.add(relative)
                    issues.append(
                        ValidationIssue(
                            relative,
                            severity,
                            "extra-artifact",
                            "Manifestに含まれないMarkdownです",
                        )
                    )

    sorted_issues = tuple(sorted(issues, key=_issue_sort_key))
    return PackageValidationResult(
        tuple(sorted(checked, key=_sort_key)),
        sorted_issues,
    )


def write_validation_report(path: Path, result: PackageValidationResult) -> None:
    """Write a deterministic validation report atomically."""

    write_json_atomically(path, result.payload())
