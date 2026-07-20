"""Compare Docling conversion with table structure inference disabled and enabled."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from importlib.metadata import version
from pathlib import Path

from knowledge_importer.converter import Converter, DoclingConverter
from scripts.evaluate_pdf_quality import (
    CASES,
    QualityResult,
    evaluate_case,
    generate_pdfs,
    render_previews,
    write_reports,
)


@dataclass(frozen=True, slots=True)
class ModeSpec:
    mode_id: str
    do_table_structure: bool


@dataclass(frozen=True, slots=True)
class ModeResult:
    mode_id: str
    settings: dict[str, bool]
    total_duration_seconds: float
    model_cache_bytes_before: int
    model_cache_bytes_after: int
    model_cache_growth_bytes: int
    model_artifact_bytes_on_disk: int
    offline_requested: bool
    all_conversions_succeeded: bool
    results: list[QualityResult]


@dataclass(frozen=True, slots=True)
class ComparisonReport:
    python_version: str
    docling_version: str
    modes: list[ModeResult]
    recommendation: str


MODE_SPECS = (
    ModeSpec(mode_id="baseline", do_table_structure=False),
    ModeSpec(mode_id="table_structure", do_table_structure=True),
)

ConverterBuilder = Callable[[bool], Converter]


def build_docling_converter(do_table_structure: bool) -> Converter:
    return DoclingConverter(do_table_structure=do_table_structure)


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _settings(do_table_structure: bool) -> dict[str, bool]:
    return {
        "do_ocr": False,
        "do_table_structure": do_table_structure,
        "force_backend_text": True,
        "enable_remote_services": False,
    }


def _model_artifact_bytes(model_cache: Path, do_table_structure: bool) -> int:
    repo_name = (
        "models--docling-project--docling-models"
        if do_table_structure
        else "models--docling-project--docling-layout-heron"
    )
    return directory_size(model_cache / "hub" / repo_name / "blobs")


def _recommendation(modes: Sequence[ModeResult]) -> str:
    by_mode = {mode.mode_id: mode for mode in modes}
    baseline_table = next(
        result for result in by_mode["baseline"].results if result.case_id == "simple_table"
    )
    enabled_table = next(
        result for result in by_mode["table_structure"].results if result.case_id == "simple_table"
    )
    baseline_by_case = {result.case_id: result for result in by_mode["baseline"].results}
    regressions = any(
        (
            baseline_by_case[result.case_id].all_expected_strings_found
            and not result.all_expected_strings_found
        )
        or (
            baseline_by_case[result.case_id].reading_order_preserved
            and not result.reading_order_preserved
        )
        for result in by_mode["table_structure"].results
    )
    table_improved = (enabled_table.table_columns or 0) > (baseline_table.table_columns or 0) and (
        enabled_table.table_rows or 0
    ) > (baseline_table.table_rows or 0)
    if table_improved and not regressions:
        return "optionize_table_structure"
    if not table_improved:
        return "keep_baseline_and_compare_other_engine"
    return "keep_baseline_until_regressions_are_understood"


def compare_modes(
    output_dir: Path,
    model_cache: Path,
    *,
    converter_builder: ConverterBuilder = build_docling_converter,
    render: bool = False,
    offline: bool = False,
) -> ComparisonReport:
    pdf_paths = generate_pdfs(output_dir / "pdfs")
    if render:
        render_previews(pdf_paths, output_dir / "previews")

    mode_results: list[ModeResult] = []
    for spec in MODE_SPECS:
        before = directory_size(model_cache)
        converter = converter_builder(spec.do_table_structure)
        mode_dir = output_dir / "modes" / spec.mode_id
        markdown_dir = mode_dir / "markdown"
        markdown_dir.mkdir(parents=True, exist_ok=True)
        results = [
            evaluate_case(
                case,
                pdf_paths[case.case_id],
                markdown_dir / f"{case.case_id}.md",
                converter,
            )
            for case in CASES
        ]
        write_reports(
            results,
            mode_dir,
            do_table_structure=spec.do_table_structure,
        )
        after = directory_size(model_cache)
        mode_results.append(
            ModeResult(
                mode_id=spec.mode_id,
                settings=_settings(spec.do_table_structure),
                total_duration_seconds=round(sum(result.duration_seconds for result in results), 3),
                model_cache_bytes_before=before,
                model_cache_bytes_after=after,
                model_cache_growth_bytes=max(0, after - before),
                model_artifact_bytes_on_disk=_model_artifact_bytes(
                    model_cache, spec.do_table_structure
                ),
                offline_requested=offline,
                all_conversions_succeeded=all(result.success for result in results),
                results=results,
            )
        )

    report = ComparisonReport(
        python_version=sys.version.split()[0],
        docling_version=version("docling"),
        modes=mode_results,
        recommendation=_recommendation(mode_results),
    )
    write_comparison_reports(report, output_dir)
    return report


def write_comparison_reports(report: ComparisonReport, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "comparison-report.json").write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    by_mode = {mode.mode_id: mode for mode in report.modes}
    baseline = {result.case_id: result for result in by_mode["baseline"].results}
    enabled = {result.case_id: result for result in by_mode["table_structure"].results}
    lines = [
        "# Docling Table Structure Mode Comparison",
        "",
        f"Python {report.python_version}; Docling {report.docling_version}.",
        "",
        "| Case | Baseline sec | Table sec | Baseline chars | Table chars | "
        "Baseline order | Table order | Baseline table | Table table |",
        "|---|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for case in CASES:
        left = baseline[case.case_id]
        right = enabled[case.case_id]
        left_table = (
            f"{left.table_reproduction} ({left.table_rows}x{left.table_columns})"
            if left.table_rows is not None
            else left.table_reproduction
        )
        right_table = (
            f"{right.table_reproduction} ({right.table_rows}x{right.table_columns})"
            if right.table_rows is not None
            else right.table_reproduction
        )
        lines.append(
            f"| {case.case_id} | {left.duration_seconds:.3f} | "
            f"{right.duration_seconds:.3f} | {left.output_characters} | "
            f"{right.output_characters} | {left.reading_order_preserved} | "
            f"{right.reading_order_preserved} | {left_table} | {right_table} |"
        )
    lines.extend(("", "## Mode summary", ""))
    for mode in report.modes:
        lines.extend(
            (
                f"- `{mode.mode_id}`: {mode.total_duration_seconds:.3f}s; "
                f"cache growth {mode.model_cache_growth_bytes} bytes; "
                f"model artifacts {mode.model_artifact_bytes_on_disk} bytes; "
                f"offline requested={mode.offline_requested}; "
                f"all conversions succeeded={mode.all_conversions_succeeded}",
            )
        )
    lines.extend(("", f"Recommendation: `{report.recommendation}`", ""))
    (output_dir / "comparison-report.md").write_text("\n".join(lines), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/converter-comparison"),
    )
    parser.add_argument(
        "--model-cache",
        type=Path,
        default=Path("output/converter-comparison/model-cache"),
    )
    parser.add_argument("--render-previews", action="store_true")
    parser.add_argument("--offline", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HOME"] = str(args.model_cache.resolve())

    report = compare_modes(
        args.output_dir,
        args.model_cache,
        render=args.render_previews,
        offline=args.offline,
    )
    failures = [
        f"{mode.mode_id}:{result.case_id}"
        for mode in report.modes
        for result in mode.results
        if not result.success
    ]
    print(f"Compared {len(CASES)} PDFs in {len(report.modes)} modes; failures={len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
