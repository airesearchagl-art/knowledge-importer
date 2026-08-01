"""Independent report model for runtime Markdown quality checks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from knowledge_importer.json_writer import write_json_atomically
from knowledge_importer.markdown_quality import RuntimeQualityWarning


@dataclass(frozen=True, slots=True)
class QualityReportItem:
    """Quality result for one Markdown file checked during this run."""

    input_name: str
    output_name: str
    warnings: tuple[RuntimeQualityWarning, ...]

    @property
    def status(self) -> str:
        return "warned" if self.warnings else "passed"


@dataclass(frozen=True, slots=True)
class QualityReport:
    """Deterministically ordered collection of checked Markdown files."""

    items: tuple[QualityReportItem, ...]

    def payload(self) -> dict[str, object]:
        passed = sum(item.status == "passed" for item in self.items)
        warned = len(self.items) - passed
        return {
            "report_type": "markdown-quality",
            "schema_version": 1,
            "summary": {
                "checked": len(self.items),
                "passed": passed,
                "warned": warned,
            },
            "items": [
                {
                    "input": item.input_name,
                    "output": item.output_name,
                    "status": item.status,
                    "warnings": [
                        {"category": warning.category, "message": warning.reason}
                        for warning in item.warnings
                    ],
                }
                for item in self.items
            ],
        }


def write_quality_report(report_path: Path, report: QualityReport) -> None:
    """Write a quality report atomically."""

    write_json_atomically(report_path, report.payload())
