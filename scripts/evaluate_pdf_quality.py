"""Generate synthetic PDFs and evaluate Knowledge Importer conversion quality."""

from __future__ import annotations

import argparse
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas

from knowledge_importer.converter import Converter, DoclingConverter, convert_file
from knowledge_importer.models import ConversionRequest

PAGE_WIDTH, PAGE_HEIGHT = A4


@dataclass(frozen=True, slots=True)
class QualityCase:
    case_id: str
    title: str
    expected_strings: tuple[str, ...]
    reading_order: tuple[str, ...]
    expected_headings: tuple[tuple[str, int], ...] = ()
    has_table: bool = False
    header_tokens: tuple[str, ...] = ()
    footer_tokens: tuple[str, ...] = ()
    expected_pages: int = 1


@dataclass(frozen=True, slots=True)
class QualityResult:
    case_id: str
    title: str
    success: bool
    error_type: str | None
    error_message: str | None
    duration_seconds: float
    output_characters: int
    expected_strings_found: dict[str, bool]
    all_expected_strings_found: bool
    heading_levels: dict[str, int | None]
    headings_preserved: bool | None
    reading_order_preserved: bool
    bullet_list_preserved: bool | None
    table_reproduction: str
    table_rows: int | None
    table_columns: int | None
    header_footer_contamination: dict[str, bool]
    missing_expected_strings: list[str]
    source_pages: int
    text_layer_verified: bool


class ConverterFactory(Protocol):
    def __call__(self) -> Converter: ...


CASES: tuple[QualityCase, ...] = (
    QualityCase(
        case_id="single_column",
        title="Single-column body",
        expected_strings=("SINGLE START", "SINGLE MIDDLE", "SINGLE END"),
        reading_order=("SINGLE START", "SINGLE MIDDLE", "SINGLE END"),
    ),
    QualityCase(
        case_id="heading_hierarchy",
        title="Heading hierarchy",
        expected_strings=("LEVEL ONE", "LEVEL TWO", "LEVEL THREE", "Hierarchy body"),
        reading_order=("LEVEL ONE", "LEVEL TWO", "LEVEL THREE", "Hierarchy body"),
        expected_headings=(("LEVEL ONE", 1), ("LEVEL TWO", 2), ("LEVEL THREE", 3)),
    ),
    QualityCase(
        case_id="bullet_list",
        title="Bullet list",
        expected_strings=("LIST INTRO", "Alpha item", "Beta item", "Gamma item"),
        reading_order=("LIST INTRO", "Alpha item", "Beta item", "Gamma item"),
    ),
    QualityCase(
        case_id="two_column",
        title="Two-column reading order",
        expected_strings=("LEFT TOP", "LEFT BOTTOM", "RIGHT TOP", "RIGHT BOTTOM"),
        reading_order=("LEFT TOP", "LEFT BOTTOM", "RIGHT TOP", "RIGHT BOTTOM"),
    ),
    QualityCase(
        case_id="simple_table",
        title="Simple table",
        expected_strings=("ROOM", "AREA", "Studio", "42 sqm", "Office", "18 sqm"),
        reading_order=("ROOM", "AREA", "Studio", "42 sqm", "Office", "18 sqm"),
        has_table=True,
    ),
    QualityCase(
        case_id="header_footer",
        title="Header and footer contamination",
        expected_strings=("DOCUMENT HEADER", "HEADER BODY", "DOCUMENT FOOTER"),
        reading_order=("DOCUMENT HEADER", "HEADER BODY", "DOCUMENT FOOTER"),
        header_tokens=("DOCUMENT HEADER",),
        footer_tokens=("DOCUMENT FOOTER",),
    ),
    QualityCase(
        case_id="multi_page",
        title="Multiple pages",
        expected_strings=("PAGE ONE CONTENT", "PAGE TWO CONTENT", "PAGE THREE CONTENT"),
        reading_order=("PAGE ONE CONTENT", "PAGE TWO CONTENT", "PAGE THREE CONTENT"),
        expected_pages=3,
    ),
)


def _new_canvas(path: Path, title: str) -> Canvas:
    canvas = Canvas(str(path), pagesize=A4)
    canvas.setTitle(title)
    return canvas


def _draw_lines(canvas: Canvas, lines: Sequence[str], *, x: float, y: float) -> None:
    canvas.setFont("Helvetica", 12)
    for line in lines:
        canvas.drawString(x, y, line)
        y -= 24


def _generate_single_column(path: Path) -> None:
    canvas = _new_canvas(path, "Single-column body")
    canvas.setFont("Helvetica-Bold", 18)
    canvas.drawString(72, 780, "Synthetic Single Column")
    _draw_lines(
        canvas,
        (
            "SINGLE START describes a fictional building envelope.",
            "SINGLE MIDDLE records a synthetic coordination note.",
            "SINGLE END closes the generated evaluation document.",
        ),
        x=72,
        y=740,
    )
    canvas.save()


def _generate_heading_hierarchy(path: Path) -> None:
    canvas = _new_canvas(path, "Heading hierarchy")
    for text, size, y in (
        ("LEVEL ONE", 22, 790),
        ("LEVEL TWO", 17, 735),
        ("LEVEL THREE", 14, 680),
    ):
        canvas.setFont("Helvetica-Bold", size)
        canvas.drawString(72, y, text)
    canvas.setFont("Helvetica", 11)
    canvas.drawString(72, 645, "Hierarchy body contains fictional evaluation content.")
    canvas.save()


def _generate_bullet_list(path: Path) -> None:
    canvas = _new_canvas(path, "Bullet list")
    canvas.setFont("Helvetica-Bold", 18)
    canvas.drawString(72, 780, "LIST INTRO")
    _draw_lines(
        canvas,
        ("- Alpha item", "- Beta item", "- Gamma item"),
        x=90,
        y=735,
    )
    canvas.save()


def _generate_two_column(path: Path) -> None:
    canvas = _new_canvas(path, "Two-column reading order")
    canvas.setFont("Helvetica-Bold", 18)
    canvas.drawString(72, 790, "Synthetic Two Column")
    _draw_lines(canvas, ("LEFT TOP", "LEFT BOTTOM"), x=72, y=735)
    _draw_lines(canvas, ("RIGHT TOP", "RIGHT BOTTOM"), x=330, y=735)
    canvas.save()


def _generate_simple_table(path: Path) -> None:
    canvas = _new_canvas(path, "Simple table")
    x_positions = (72, 270, 450)
    y_positions = (760, 720, 680, 640)
    for x in x_positions:
        canvas.line(x, y_positions[-1], x, y_positions[0])
    for y in y_positions:
        canvas.line(x_positions[0], y, x_positions[-1], y)
    canvas.setFont("Helvetica-Bold", 12)
    canvas.drawString(84, 735, "ROOM")
    canvas.drawString(282, 735, "AREA")
    canvas.setFont("Helvetica", 12)
    canvas.drawString(84, 695, "Studio")
    canvas.drawString(282, 695, "42 sqm")
    canvas.drawString(84, 655, "Office")
    canvas.drawString(282, 655, "18 sqm")
    canvas.save()


def _generate_header_footer(path: Path) -> None:
    canvas = _new_canvas(path, "Header and footer")
    canvas.setFont("Helvetica", 9)
    canvas.drawString(72, 810, "DOCUMENT HEADER")
    canvas.line(72, 802, PAGE_WIDTH - 72, 802)
    canvas.setFont("Helvetica", 12)
    canvas.drawString(72, 720, "HEADER BODY contains fictional project-neutral text.")
    canvas.line(72, 50, PAGE_WIDTH - 72, 50)
    canvas.setFont("Helvetica", 9)
    canvas.drawString(72, 32, "DOCUMENT FOOTER")
    canvas.save()


def _generate_multi_page(path: Path) -> None:
    canvas = _new_canvas(path, "Multiple pages")
    for page_number, marker in enumerate(
        ("PAGE ONE CONTENT", "PAGE TWO CONTENT", "PAGE THREE CONTENT"), start=1
    ):
        canvas.setFont("Helvetica-Bold", 18)
        canvas.drawString(72, 780, marker)
        canvas.setFont("Helvetica", 11)
        canvas.drawString(72, 740, f"Synthetic page number {page_number}.")
        canvas.showPage()
    canvas.save()


GENERATORS: dict[str, Callable[[Path], None]] = {
    "single_column": _generate_single_column,
    "heading_hierarchy": _generate_heading_hierarchy,
    "bullet_list": _generate_bullet_list,
    "two_column": _generate_two_column,
    "simple_table": _generate_simple_table,
    "header_footer": _generate_header_footer,
    "multi_page": _generate_multi_page,
}


def generate_pdfs(pdf_dir: Path) -> dict[str, Path]:
    """Generate all synthetic PDFs and verify their text layers."""
    pdf_dir.mkdir(parents=True, exist_ok=True)
    generated: dict[str, Path] = {}
    for case in CASES:
        pdf_path = pdf_dir / f"{case.case_id}.pdf"
        GENERATORS[case.case_id](pdf_path)
        reader = PdfReader(pdf_path)
        extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
        if len(reader.pages) != case.expected_pages:
            raise RuntimeError(f"Unexpected page count for {case.case_id}")
        if not all(expected in extracted for expected in case.expected_strings):
            raise RuntimeError(f"Text-layer verification failed for {case.case_id}")
        generated[case.case_id] = pdf_path
    return generated


def render_previews(pdf_paths: dict[str, Path], preview_dir: Path) -> None:
    """Render every PDF page to PNG for visual inspection."""
    import pypdfium2 as pdfium

    preview_dir.mkdir(parents=True, exist_ok=True)
    for case_id, pdf_path in pdf_paths.items():
        document = pdfium.PdfDocument(pdf_path)
        for page_number, page in enumerate(document, start=1):
            image = page.render(scale=1.5).to_pil()
            image.save(preview_dir / f"{case_id}-page-{page_number}.png")


def _heading_levels(markdown: str, expected: Sequence[tuple[str, int]]) -> dict[str, int | None]:
    levels: dict[str, int | None] = {}
    lines = markdown.splitlines()
    for heading, _ in expected:
        pattern = re.compile(rf"^(#{{1,6}})\s+{re.escape(heading)}\s*$", re.IGNORECASE)
        match = next(
            (pattern.match(line.strip()) for line in lines if pattern.match(line.strip())), None
        )
        levels[heading] = len(match.group(1)) if match else None
    return levels


def _is_reading_order_preserved(markdown: str, markers: Sequence[str]) -> bool:
    positions = [markdown.find(marker) for marker in markers]
    return all(position >= 0 for position in positions) and positions == sorted(positions)


def _table_reproduction(markdown: str, has_table: bool) -> str:
    if not has_table:
        return "not_applicable"
    table_lines = [line for line in markdown.splitlines() if line.count("|") >= 2]
    separator_found = any(re.fullmatch(r"[| :\-]+", line.strip()) for line in table_lines)
    if len(table_lines) < 2 or not separator_found:
        return "plain_text"
    data_rows = [line for line in table_lines if not re.fullmatch(r"[| :\-]+", line.strip())]
    max_columns = max(
        (
            len([cell for cell in line.strip().strip("|").split("|") if cell.strip()])
            for line in data_rows
        ),
        default=0,
    )
    return "structured_markdown_table" if max_columns >= 2 else "flattened_markdown_table"


def _table_dimensions(markdown: str, has_table: bool) -> tuple[int | None, int | None]:
    if not has_table:
        return None, None
    table_lines = [line for line in markdown.splitlines() if line.count("|") >= 2]
    data_rows = [line for line in table_lines if not re.fullmatch(r"[| :\-]+", line.strip())]
    columns = max(
        (
            len([cell for cell in line.strip().strip("|").split("|") if cell.strip()])
            for line in data_rows
        ),
        default=0,
    )
    return len(data_rows), columns


def _bullet_list_preserved(case: QualityCase, markdown: str) -> bool | None:
    if case.case_id != "bullet_list":
        return None
    item_tokens = ("Alpha item", "Beta item", "Gamma item")
    bullet_lines = [line for line in markdown.splitlines() if re.match(r"^\s*[-*+]\s+", line)]
    return all(any(token in line for line in bullet_lines) for token in item_tokens)


def evaluate_case(
    case: QualityCase, pdf_path: Path, markdown_path: Path, converter: Converter
) -> QualityResult:
    start = time.perf_counter()
    try:
        convert_file(ConversionRequest(pdf_path, markdown_path, force=True), converter)
        markdown = markdown_path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - report each case without aborting the suite.
        return QualityResult(
            case_id=case.case_id,
            title=case.title,
            success=False,
            error_type=type(exc).__name__,
            error_message=str(exc),
            duration_seconds=round(time.perf_counter() - start, 3),
            output_characters=0,
            expected_strings_found={item: False for item in case.expected_strings},
            all_expected_strings_found=False,
            heading_levels={item: None for item, _ in case.expected_headings},
            headings_preserved=False if case.expected_headings else None,
            reading_order_preserved=False,
            bullet_list_preserved=False if case.case_id == "bullet_list" else None,
            table_reproduction="not_evaluated",
            table_rows=None,
            table_columns=None,
            header_footer_contamination={},
            missing_expected_strings=list(case.expected_strings),
            source_pages=case.expected_pages,
            text_layer_verified=True,
        )

    found = {item: item in markdown for item in case.expected_strings}
    heading_levels = _heading_levels(markdown, case.expected_headings)
    headings_preserved = (
        all(heading_levels[text] == level for text, level in case.expected_headings)
        if case.expected_headings
        else None
    )
    contamination = {
        token: token in markdown for token in (*case.header_tokens, *case.footer_tokens)
    }
    table_rows, table_columns = _table_dimensions(markdown, case.has_table)
    return QualityResult(
        case_id=case.case_id,
        title=case.title,
        success=True,
        error_type=None,
        error_message=None,
        duration_seconds=round(time.perf_counter() - start, 3),
        output_characters=len(markdown),
        expected_strings_found=found,
        all_expected_strings_found=all(found.values()),
        heading_levels=heading_levels,
        headings_preserved=headings_preserved,
        reading_order_preserved=_is_reading_order_preserved(markdown, case.reading_order),
        bullet_list_preserved=_bullet_list_preserved(case, markdown),
        table_reproduction=_table_reproduction(markdown, case.has_table),
        table_rows=table_rows,
        table_columns=table_columns,
        header_footer_contamination=contamination,
        missing_expected_strings=[item for item, present in found.items() if not present],
        source_pages=case.expected_pages,
        text_layer_verified=True,
    )


def evaluate_quality(
    output_dir: Path,
    *,
    converter_factory: ConverterFactory = DoclingConverter,
    render: bool = False,
) -> list[QualityResult]:
    pdf_paths = generate_pdfs(output_dir / "pdfs")
    if render:
        render_previews(pdf_paths, output_dir / "previews")
    markdown_dir = output_dir / "markdown"
    markdown_dir.mkdir(parents=True, exist_ok=True)
    converter = converter_factory()
    results = [
        evaluate_case(
            case,
            pdf_paths[case.case_id],
            markdown_dir / f"{case.case_id}.md",
            converter,
        )
        for case in CASES
    ]
    write_reports(results, output_dir)
    return results


def write_reports(
    results: Sequence[QualityResult],
    output_dir: Path,
    *,
    do_table_structure: bool = False,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "quality-report.json"
    json_path.write_text(
        json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# Synthetic PDF Quality Report",
        "",
        "| Case | Success | Seconds | Chars | Expected | Headings | "
        "Order | Table | Header/footer |",
        "|---|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for result in results:
        contamination = (
            ", ".join(key for key, present in result.header_footer_contamination.items() if present)
            or "none"
        )
        lines.append(
            f"| {result.case_id} | {result.success} | {result.duration_seconds:.3f} | "
            f"{result.output_characters} | {result.all_expected_strings_found} | "
            f"{result.headings_preserved} | {result.reading_order_preserved} | "
            f"{result.table_reproduction} | {contamination} |"
        )
    table_note = (
        "Table structure inference is enabled (`do_table_structure=True`)."
        if do_table_structure
        else "Table structure inference is disabled (`do_table_structure=False`). "
        "Table cells may be emitted as plain text rather than Markdown tables."
    )
    lines.extend(("", table_note, ""))
    (output_dir / "quality-report.md").write_text("\n".join(lines), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/pdf-quality-evaluation"),
        help="Generated PDFs, Markdown, previews, and reports",
    )
    parser.add_argument(
        "--generate-only",
        action="store_true",
        help="Generate and verify PDFs without running Docling",
    )
    parser.add_argument(
        "--render-previews",
        action="store_true",
        help="Render generated PDF pages to PNG",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.generate_only:
        pdf_paths = generate_pdfs(args.output_dir / "pdfs")
        if args.render_previews:
            render_previews(pdf_paths, args.output_dir / "previews")
        print(f"Generated {len(pdf_paths)} synthetic PDFs in {args.output_dir}")
        return 0

    results = evaluate_quality(args.output_dir, render=args.render_previews)
    failures = [result.case_id for result in results if not result.success]
    print(f"Evaluated {len(results)} synthetic PDFs; failures={len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
