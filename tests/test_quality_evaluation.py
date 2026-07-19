from pathlib import Path

from pypdf import PdfReader

from scripts.evaluate_pdf_quality import (
    CASES,
    _table_reproduction,
    evaluate_quality,
    generate_pdfs,
)


class SyntheticConverter:
    def convert(self, input_path: Path) -> str:
        case_id = input_path.stem
        outputs = {
            "single_column": "SINGLE START\nSINGLE MIDDLE\nSINGLE END\n",
            "heading_hierarchy": ("# LEVEL ONE\n## LEVEL TWO\n### LEVEL THREE\nHierarchy body\n"),
            "bullet_list": "LIST INTRO\n- Alpha item\n- Beta item\n- Gamma item\n",
            "two_column": "LEFT TOP\nLEFT BOTTOM\nRIGHT TOP\nRIGHT BOTTOM\n",
            "simple_table": "ROOM AREA\nStudio 42 sqm\nOffice 18 sqm\n",
            "header_footer": "DOCUMENT HEADER\nHEADER BODY\nDOCUMENT FOOTER\n",
            "multi_page": "PAGE ONE CONTENT\nPAGE TWO CONTENT\nPAGE THREE CONTENT\n",
        }
        return outputs[case_id]


def test_generate_pdfs_creates_all_text_layer_cases(tmp_path: Path) -> None:
    generated = generate_pdfs(tmp_path)

    assert set(generated) == {case.case_id for case in CASES}
    for case in CASES:
        reader = PdfReader(generated[case.case_id])
        extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
        assert len(reader.pages) == case.expected_pages
        assert all(expected in extracted for expected in case.expected_strings)


def test_evaluation_uses_injected_converter_and_writes_reports(tmp_path: Path) -> None:
    results = evaluate_quality(tmp_path, converter_factory=SyntheticConverter)

    assert len(results) == len(CASES)
    assert all(result.success for result in results)
    assert all(result.all_expected_strings_found for result in results)
    assert all(result.reading_order_preserved for result in results)
    assert (tmp_path / "quality-report.json").is_file()
    assert (tmp_path / "quality-report.md").is_file()

    heading_result = next(result for result in results if result.case_id == "heading_hierarchy")
    assert heading_result.headings_preserved is True
    bullet_result = next(result for result in results if result.case_id == "bullet_list")
    assert bullet_result.bullet_list_preserved is True
    table_result = next(result for result in results if result.case_id == "simple_table")
    assert table_result.table_reproduction == "plain_text"
    assert table_result.table_rows == 0
    assert table_result.table_columns == 0
    assert all(not result.missing_expected_strings for result in results)


def test_table_reproduction_distinguishes_structure_from_flattening() -> None:
    structured = "| ROOM | AREA |\n|---|---|\n| Studio | 42 sqm |\n"
    flattened = "| ROOM AREA Studio 42 sqm |\n|---|\n"

    assert _table_reproduction(structured, has_table=True) == "structured_markdown_table"
    assert _table_reproduction(flattened, has_table=True) == "flattened_markdown_table"
