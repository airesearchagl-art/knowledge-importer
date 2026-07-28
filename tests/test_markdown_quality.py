from pathlib import Path

import pytest
from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas

from knowledge_importer.markdown_quality import (
    MarkdownQualityExpectation,
    MarkdownQualityResult,
    evaluate_markdown_quality,
)

VALID_MARKDOWN = """\
# Synthetic Building Guide

## Envelope Planning

This fictional guide describes a repeatable envelope review.
The second paragraph records neutral coordination notes for testing.

### Review Items

- Confirm alpha boundary
- Record beta opening
- Check gamma junction

| Zone | Rating |
| --- | --- |
| North | A1 |
| South | B2 |

PAGE ONE END: the envelope discussion continues.

PAGE TWO START: the envelope discussion resumes.
The closing paragraph confirms the synthetic evaluation is complete.
"""

EXPECTATION = MarkdownQualityExpectation(
    headings=(
        (1, "Synthetic Building Guide"),
        (2, "Envelope Planning"),
        (3, "Review Items"),
    ),
    body_phrases=(
        "repeatable envelope review",
        "neutral coordination notes",
        "synthetic evaluation is complete",
    ),
    bullet_items=(
        "Confirm alpha boundary",
        "Record beta opening",
        "Check gamma junction",
    ),
    table_rows=(
        ("Zone", "Rating"),
        ("North", "A1"),
        ("South", "B2"),
    ),
    page_boundary_phrases=(
        "PAGE ONE END",
        "PAGE TWO START",
    ),
    key_phrases=(
        "Synthetic Building Guide",
        "South",
        "PAGE TWO START",
    ),
    # The complete fixture has more than 400 visible characters. A 120-character
    # floor catches empty or heavily truncated output without relying on exact text.
    minimum_characters=120,
)


def _generate_synthetic_pdf(path: Path) -> None:
    canvas = Canvas(str(path), pagesize=A4, invariant=1)
    canvas.setTitle("Synthetic Markdown Quality Fixture")

    canvas.setFont("Helvetica-Bold", 20)
    canvas.drawString(72, 790, "Synthetic Building Guide")
    canvas.setFont("Helvetica-Bold", 16)
    canvas.drawString(72, 750, "Envelope Planning")
    canvas.setFont("Helvetica", 11)
    canvas.drawString(72, 715, "This fictional guide describes a repeatable envelope review.")
    canvas.drawString(72, 690, "The second paragraph records neutral coordination notes.")
    canvas.setFont("Helvetica-Bold", 13)
    canvas.drawString(72, 650, "Review Items")
    canvas.setFont("Helvetica", 11)
    canvas.drawString(90, 625, "- Confirm alpha boundary")
    canvas.drawString(90, 602, "- Record beta opening")
    canvas.drawString(90, 579, "- Check gamma junction")
    canvas.drawString(72, 80, "PAGE ONE END: the envelope discussion continues.")
    canvas.showPage()

    canvas.setFont("Helvetica", 11)
    canvas.drawString(72, 790, "PAGE TWO START: the envelope discussion resumes.")
    canvas.drawString(72, 760, "The closing paragraph confirms the synthetic evaluation.")
    canvas.setFont("Helvetica-Bold", 11)
    canvas.drawString(72, 700, "Zone")
    canvas.drawString(250, 700, "Rating")
    canvas.setFont("Helvetica", 11)
    canvas.drawString(72, 675, "North")
    canvas.drawString(250, 675, "A1")
    canvas.drawString(72, 650, "South")
    canvas.drawString(250, 650, "B2")
    canvas.save()


def _failure_message(result: MarkdownQualityResult) -> str:
    failed_metrics = [metric for metric in result.metrics if not metric.passed]
    details = "\n".join(f"- {metric.name}: {metric.reason}" for metric in failed_metrics)
    return f"{result.reason}\n{details}"


def test_synthetic_pdf_has_two_page_text_layer(tmp_path: Path) -> None:
    pdf_path = tmp_path / "synthetic-quality.pdf"
    duplicate_path = tmp_path / "synthetic-quality-copy.pdf"
    _generate_synthetic_pdf(pdf_path)
    _generate_synthetic_pdf(duplicate_path)

    reader = PdfReader(pdf_path)
    extracted_pages = [page.extract_text() or "" for page in reader.pages]

    assert pdf_path.read_bytes() == duplicate_path.read_bytes()
    assert len(extracted_pages) == 2
    assert "PAGE ONE END" in extracted_pages[0]
    assert "PAGE TWO START" in extracted_pages[1]
    assert "Confirm alpha boundary" in extracted_pages[0]
    assert "South" in extracted_pages[1]


def test_complete_markdown_passes_all_quality_metrics() -> None:
    result = evaluate_markdown_quality(VALID_MARKDOWN, EXPECTATION)

    assert result.passed is True, _failure_message(result)
    assert result.failed_checks == ()
    assert all(metric.passed for metric in result.metrics)
    assert result.reason == "all 8 checks passed"


@pytest.mark.parametrize(
    ("broken_markdown", "expected_failure"),
    [
        pytest.param("", "minimum_length", id="empty-output"),
        pytest.param(
            VALID_MARKDOWN.replace("## Envelope Planning", "Envelope Planning"),
            "headings",
            id="missing-heading",
        ),
        pytest.param(
            VALID_MARKDOWN.replace(
                "| Zone | Rating |\n| --- | --- |\n| North | A1 |\n| South | B2 |",
                "Zone Rating North A1 South B2",
            ),
            "table_structure",
            id="flattened-table",
        ),
        pytest.param(
            VALID_MARKDOWN.replace("PAGE TWO START", "PAGE TWO OMITTED"),
            "page_boundary",
            id="missing-page-boundary",
        ),
        pytest.param(
            VALID_MARKDOWN
            + "\nC:\\Users\\sample\\private.pdf"
            + "\nTraceback (most recent call last):\n"
            + "\u202e",
            "contamination",
            id="unsafe-content",
        ),
    ],
)
def test_broken_markdown_reports_specific_failed_check(
    broken_markdown: str, expected_failure: str
) -> None:
    result = evaluate_markdown_quality(broken_markdown, EXPECTATION)

    assert result.passed is False
    assert expected_failure in result.failed_checks
    metric = next(metric for metric in result.metrics if metric.name == expected_failure)
    assert metric.passed is False
    assert metric.reason


def test_minor_whitespace_and_markdown_style_changes_do_not_fail() -> None:
    restyled = (
        VALID_MARKDOWN.replace("# Synthetic Building Guide", "#   Synthetic Building Guide   #")
        .replace("- Confirm alpha boundary", "*  Confirm alpha boundary")
        .replace("| --- | --- |", "| :--- | ---: |")
    )

    result = evaluate_markdown_quality(restyled, EXPECTATION)

    assert result.passed is True, _failure_message(result)
