"""Deterministic quality checks for generated Markdown."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MarkdownQualityExpectation:
    """Expected information and structure for one synthetic document."""

    headings: tuple[tuple[int, str], ...] = ()
    body_phrases: tuple[str, ...] = ()
    bullet_items: tuple[str, ...] = ()
    table_rows: tuple[tuple[str, ...], ...] = ()
    page_boundary_phrases: tuple[str, ...] = ()
    key_phrases: tuple[str, ...] = ()
    minimum_characters: int = 120


@dataclass(frozen=True, slots=True)
class QualityMetricResult:
    """Result of one named quality check."""

    name: str
    passed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class MarkdownQualityResult:
    """Aggregate result returned by :func:`evaluate_markdown_quality`."""

    passed: bool
    failed_checks: tuple[str, ...]
    metrics: tuple[QualityMetricResult, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class RuntimeQualityWarning:
    """One safe, document-independent warning for generated Markdown."""

    category: str
    reason: str


# Runtime inputs can legitimately be much shorter than the synthetic fixture used
# by the detailed regression suite. Forty visible characters only flags outputs
# that are likely empty or severely truncated, and never changes conversion status.
RUNTIME_SHORT_OUTPUT_THRESHOLD = 40


_HEADING_PATTERN = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*#*\s*$")
_BULLET_PATTERN = re.compile(r"^\s*[-*+]\s+(.+?)\s*$")
_TABLE_SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")
_WINDOWS_ABSOLUTE_PATH = re.compile(r"(?<![\w])(?:[A-Za-z]:[\\/])")
_POSIX_ABSOLUTE_PATH = re.compile(r"(?<![\w/:])/(?:[^/\s]+/)+[^/\s]*")


def _normalise(value: str) -> str:
    return " ".join(value.split()).casefold()


def _visible_character_count(markdown: str) -> int:
    visible_text = re.sub(r"[#|*_`~>\-\[\]()]", " ", markdown)
    return len(_normalise(visible_text))


def _missing_values(markdown: str, expected: tuple[str, ...]) -> list[str]:
    normalised = _normalise(markdown)
    return [value for value in expected if _normalise(value) not in normalised]


def _metric(name: str, missing: list[str], label: str) -> QualityMetricResult:
    if missing:
        return QualityMetricResult(name, False, f"{label}: {', '.join(missing)}")
    return QualityMetricResult(name, True, f"{label}: none")


def _check_minimum_length(
    markdown: str, expectation: MarkdownQualityExpectation
) -> QualityMetricResult:
    character_count = _visible_character_count(markdown)
    passed = character_count >= expectation.minimum_characters
    return QualityMetricResult(
        "minimum_length",
        passed,
        (f"visible characters={character_count}, minimum={expectation.minimum_characters}"),
    )


def _check_headings(markdown: str, expectation: MarkdownQualityExpectation) -> QualityMetricResult:
    actual = []
    for line in markdown.splitlines():
        if match := _HEADING_PATTERN.match(line):
            actual.append((len(match.group(1)), _normalise(match.group(2))))

    expected = [(level, _normalise(text)) for level, text in expectation.headings]
    missing = [f"H{level} {text}" for level, text in expected if (level, text) not in actual]
    return _metric("headings", missing, "missing headings")


def _check_bullets(markdown: str, expectation: MarkdownQualityExpectation) -> QualityMetricResult:
    actual = {
        _normalise(match.group(1))
        for line in markdown.splitlines()
        if (match := _BULLET_PATTERN.match(line))
    }
    missing = [item for item in expectation.bullet_items if _normalise(item) not in actual]
    return _metric("bullet_list", missing, "missing bullet items")


def _split_table_row(line: str) -> tuple[str, ...]:
    return tuple(_normalise(cell) for cell in line.strip().strip("|").split("|"))


def _markdown_table_rows(markdown: str) -> tuple[tuple[str, ...], ...]:
    lines = markdown.splitlines()
    rows: list[tuple[str, ...]] = []
    for index in range(len(lines) - 1):
        header = _split_table_row(lines[index])
        separator = _split_table_row(lines[index + 1])
        if len(header) < 2 or len(header) != len(separator):
            continue
        if not all(_TABLE_SEPARATOR_CELL.fullmatch(cell) for cell in separator):
            continue

        rows.append(header)
        for line in lines[index + 2 :]:
            if "|" not in line:
                break
            row = _split_table_row(line)
            if len(row) != len(header):
                break
            rows.append(row)
        break
    return tuple(rows)


def _check_table(markdown: str, expectation: MarkdownQualityExpectation) -> QualityMetricResult:
    actual = set(_markdown_table_rows(markdown))
    missing = [
        " | ".join(row)
        for row in expectation.table_rows
        if tuple(_normalise(cell) for cell in row) not in actual
    ]
    return _metric("table_structure", missing, "missing table rows")


def _check_page_boundary(
    markdown: str, expectation: MarkdownQualityExpectation
) -> QualityMetricResult:
    normalised = _normalise(markdown)
    positions = [
        normalised.find(_normalise(phrase)) for phrase in expectation.page_boundary_phrases
    ]
    missing = [
        phrase
        for phrase, position in zip(expectation.page_boundary_phrases, positions, strict=True)
        if position < 0
    ]
    if missing:
        return _metric("page_boundary", missing, "missing boundary phrases")
    if positions != sorted(positions):
        return QualityMetricResult(
            "page_boundary",
            False,
            "page boundary phrases are out of order",
        )
    return QualityMetricResult(
        "page_boundary",
        True,
        "page boundary phrases are present in order",
    )


def _check_contamination(markdown: str) -> QualityMetricResult:
    findings: list[str] = []
    if "traceback (most recent call last)" in markdown.casefold():
        findings.append("traceback")
    if _WINDOWS_ABSOLUTE_PATH.search(markdown) or _POSIX_ABSOLUTE_PATH.search(markdown):
        findings.append("absolute_path")
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"} and character not in "\n\r\t"
        for character in markdown
    ):
        findings.append("control_character")
    if findings:
        return QualityMetricResult(
            "contamination",
            False,
            f"unwanted content: {', '.join(findings)}",
        )
    return QualityMetricResult("contamination", True, "unwanted content: none")


def evaluate_runtime_quality_warnings(
    markdown: str,
    *,
    short_output_threshold: int = RUNTIME_SHORT_OUTPUT_THRESHOLD,
) -> tuple[RuntimeQualityWarning, ...]:
    """Return deterministic warnings that do not require document expectations."""

    character_count = _visible_character_count(markdown)
    warnings: list[RuntimeQualityWarning] = []
    if character_count == 0:
        warnings.append(RuntimeQualityWarning("empty-output", "Markdown出力が空"))
    elif character_count < short_output_threshold:
        warnings.append(RuntimeQualityWarning("short-output", "Markdown出力が極端に短い"))
    if _WINDOWS_ABSOLUTE_PATH.search(markdown) or _POSIX_ABSOLUTE_PATH.search(markdown):
        warnings.append(RuntimeQualityWarning("absolute-path", "絶対パスらしい文字列を検出"))
    if "traceback (most recent call last)" in markdown.casefold():
        warnings.append(RuntimeQualityWarning("traceback", "tracebackらしい文字列を検出"))
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"} and character not in "\n\r\t"
        for character in markdown
    ):
        warnings.append(RuntimeQualityWarning("control-character", "Unicode制御文字を検出"))
    return tuple(warnings)


def evaluate_markdown_quality(
    markdown: str, expectation: MarkdownQualityExpectation
) -> MarkdownQualityResult:
    """Evaluate structural and textual regressions without exact-output matching."""

    metrics = (
        _check_minimum_length(markdown, expectation),
        _check_headings(markdown, expectation),
        _metric(
            "body_text",
            _missing_values(markdown, expectation.body_phrases),
            "missing body phrases",
        ),
        _check_bullets(markdown, expectation),
        _check_table(markdown, expectation),
        _check_page_boundary(markdown, expectation),
        _metric(
            "key_phrases",
            _missing_values(markdown, expectation.key_phrases),
            "missing key phrases",
        ),
        _check_contamination(markdown),
    )
    failed_checks = tuple(metric.name for metric in metrics if not metric.passed)
    passed = not failed_checks
    reason = (
        f"all {len(metrics)} checks passed"
        if passed
        else f"failed checks: {', '.join(failed_checks)}"
    )
    return MarkdownQualityResult(passed, failed_checks, metrics, reason)
