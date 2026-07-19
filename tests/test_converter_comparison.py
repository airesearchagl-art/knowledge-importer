from pathlib import Path

from scripts.compare_docling_modes import compare_modes


class ModeConverter:
    def __init__(self, do_table_structure: bool) -> None:
        self.do_table_structure = do_table_structure

    def convert(self, input_path: Path) -> str:
        outputs = {
            "single_column": "SINGLE START\nSINGLE MIDDLE\nSINGLE END\n",
            "heading_hierarchy": ("# LEVEL ONE\n## LEVEL TWO\n### LEVEL THREE\nHierarchy body\n"),
            "bullet_list": "LIST INTRO\n- Alpha item\n- Beta item\n- Gamma item\n",
            "two_column": "LEFT TOP\nLEFT BOTTOM\nRIGHT TOP\nRIGHT BOTTOM\n",
            "header_footer": "DOCUMENT HEADER\nHEADER BODY\nDOCUMENT FOOTER\n",
            "multi_page": "PAGE ONE CONTENT\nPAGE TWO CONTENT\nPAGE THREE CONTENT\n",
        }
        if input_path.stem == "simple_table":
            if self.do_table_structure:
                return "| ROOM | AREA |\n|---|---|\n| Studio | 42 sqm |\n| Office | 18 sqm |\n"
            return "| ROOM AREA Studio 42 sqm Office 18 sqm |\n|---|\n"
        return outputs[input_path.stem]


def test_compare_modes_uses_fake_converters_without_docling_inference(tmp_path: Path) -> None:
    report = compare_modes(
        tmp_path / "results",
        tmp_path / "model-cache",
        converter_builder=ModeConverter,
        offline=True,
    )

    assert [mode.mode_id for mode in report.modes] == ["baseline", "table_structure"]
    assert all(mode.offline_success for mode in report.modes)
    assert report.modes[0].settings["do_table_structure"] is False
    assert report.modes[1].settings["do_table_structure"] is True
    assert report.recommendation == "optionize_table_structure"

    baseline_table = next(
        result for result in report.modes[0].results if result.case_id == "simple_table"
    )
    enabled_table = next(
        result for result in report.modes[1].results if result.case_id == "simple_table"
    )
    assert (baseline_table.table_rows, baseline_table.table_columns) == (1, 1)
    assert (enabled_table.table_rows, enabled_table.table_columns) == (3, 2)
    assert (tmp_path / "results" / "comparison-report.json").is_file()
    assert (tmp_path / "results" / "comparison-report.md").is_file()
    enabled_report = (
        tmp_path / "results" / "modes" / "table_structure" / "quality-report.md"
    ).read_text(encoding="utf-8")
    assert "do_table_structure=True" in enabled_report
