import json
from pathlib import Path

import pytest

from knowledge_importer.markdown_quality import RuntimeQualityWarning
from knowledge_importer.quality_report import (
    QualityReport,
    QualityReportItem,
    write_quality_report,
)


def test_quality_report_payload_has_fixed_schema_and_counts() -> None:
    report = QualityReport(
        (
            QualityReportItem("a.pdf", "a.md", ()),
            QualityReportItem(
                "section/b.pdf",
                "section/b.md",
                (RuntimeQualityWarning("short-output", "Markdown出力が極端に短い"),),
            ),
        )
    )

    assert report.payload() == {
        "report_type": "markdown-quality",
        "schema_version": 1,
        "summary": {"checked": 2, "passed": 1, "warned": 1},
        "items": [
            {
                "input": "a.pdf",
                "output": "a.md",
                "status": "passed",
                "warnings": [],
            },
            {
                "input": "section/b.pdf",
                "output": "section/b.md",
                "status": "warned",
                "warnings": [
                    {
                        "category": "short-output",
                        "message": "Markdown出力が極端に短い",
                    }
                ],
            },
        ],
    }


def test_quality_report_writes_utf8_with_trailing_newline_and_replaces(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "reports" / "quality.json"
    report_path.parent.mkdir()
    report_path.write_text("old report", encoding="utf-8")
    report = QualityReport((QualityReportItem("資料.pdf", "資料.md", ()),))

    write_quality_report(report_path, report)

    content = report_path.read_text(encoding="utf-8")
    assert content.endswith("\n")
    assert json.loads(content)["items"][0]["input"] == "資料.pdf"


def test_quality_report_creates_missing_parent_directories(tmp_path: Path) -> None:
    report_path = tmp_path / "nested" / "reports" / "quality.json"

    write_quality_report(report_path, QualityReport(()))

    assert json.loads(report_path.read_text(encoding="utf-8"))["items"] == []


def test_quality_report_atomic_failure_preserves_existing_file_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path = tmp_path / "quality.json"
    report_path.write_text("original", encoding="utf-8")
    original_replace = Path.replace

    def failing_replace(path: Path, target: Path) -> Path:
        if target == report_path:
            raise OSError("synthetic replace failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", failing_replace)

    with pytest.raises(OSError, match="synthetic replace failure"):
        write_quality_report(report_path, QualityReport(()))

    assert report_path.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.glob(".quality.json.*.tmp")) == []
