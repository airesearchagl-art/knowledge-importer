from __future__ import annotations

import csv
import json
import locale
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from importlib.metadata import version
from pathlib import Path

import pytest

from knowledge_importer.cli import run

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_OPTIONS = (
    "--recursive",
    "--include",
    "--exclude",
    "--report-json",
    "--report-csv",
    "--quality-warnings",
    "--quality-report-json",
)
EXPECTED_PACKAGE_MODULES = {
    "knowledge_importer/__init__.py",
    "knowledge_importer/__main__.py",
    "knowledge_importer/cli.py",
    "knowledge_importer/converter.py",
    "knowledge_importer/json_writer.py",
    "knowledge_importer/logging_config.py",
    "knowledge_importer/main.py",
    "knowledge_importer/markdown_quality.py",
    "knowledge_importer/models.py",
    "knowledge_importer/quality_report.py",
}


class ReleaseSmokeConverter:
    def __init__(self) -> None:
        self.inputs: list[Path] = []

    def convert(self, input_path: Path) -> str:
        self.inputs.append(input_path)
        if input_path.name == "短文.PDF":
            return "短い"
        return (
            "# 合成文書\n\n"
            "外部サービスを使わない架空の本文です。"
            "リリース前の決定的な検証に必要な長さを持ち、実資料は含みません。\n"
        )


def _run_command(command: list[str], *, cwd: Path = REPOSITORY_ROOT) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding=locale.getpreferredencoding(False),
        errors="replace",
    )
    return completed.stdout


def _venv_executable(venv_path: Path, name: str) -> Path:
    if os.name == "nt":
        suffix = ".exe" if name != "python" else ".exe"
        return venv_path / "Scripts" / f"{name}{suffix}"
    return venv_path / "bin" / name


def _create_pdf_tree(root: Path, relative_paths: tuple[str, ...]) -> None:
    for relative_path in relative_paths:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4\n% synthetic release fixture\n")


def test_wheel_build_install_and_entry_points(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.fail("uv is required for the release-readiness smoke test")
    dist_dir = tmp_path / "dist"

    _run_command([uv, "build", "--out-dir", str(dist_dir)])

    wheel_path = next(dist_dir.glob("knowledge_importer-0.1.0-*.whl"))
    sdist_path = dist_dir / "knowledge_importer-0.1.0.tar.gz"
    assert sdist_path.is_file()
    with zipfile.ZipFile(wheel_path) as wheel:
        wheel_names = set(wheel.namelist())
    assert wheel_names >= EXPECTED_PACKAGE_MODULES
    assert not any(
        unwanted in name.casefold()
        for name in wheel_names
        for unwanted in ("tests/", "scripts/", "output/", ".pytest", ".env")
    )
    with tarfile.open(sdist_path) as sdist:
        sdist_names = {name.casefold() for name in sdist.getnames()}
    assert not any(
        unwanted in name
        for name in sdist_names
        for unwanted in ("/tests/", "/scripts/", "/output/", "/.pytest", "/.env")
    )

    venv_path = tmp_path / "clean-venv"
    _run_command([sys.executable, "-m", "venv", str(venv_path)])
    python = _venv_executable(venv_path, "python")
    cli = _venv_executable(venv_path, "knowledge-importer")
    _run_command(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            str(wheel_path),
        ]
    )

    cli_help = _run_command([str(cli), "convert", "--help"])
    module_help = _run_command([str(python), "-m", "knowledge_importer", "--help"])
    for option in EXPECTED_OPTIONS:
        assert option in cli_help
    assert "convert" in module_help
    metadata = _run_command(
        [
            str(python),
            "-c",
            (
                "from importlib.metadata import version; "
                "import knowledge_importer.cli, knowledge_importer.json_writer, "
                "knowledge_importer.markdown_quality, knowledge_importer.quality_report; "
                "print(version('knowledge-importer'))"
            ),
        ]
    )
    assert metadata.strip() == "0.1.0"


def test_readme_documents_every_public_batch_option() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

    for option in EXPECTED_OPTIONS:
        assert option in readme
    assert version("knowledge-importer") == "0.1.0"


def test_public_release_gate_documents_keep_human_decisions_explicit() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    checklist = (REPOSITORY_ROOT / "RELEASE_CHECKLIST.md").read_text(encoding="utf-8")
    license_review = (REPOSITORY_ROOT / "THIRD_PARTY_LICENSES_REVIEW.md").read_text(
        encoding="utf-8"
    )

    assert "[v0.1.0 Public Release Gate](RELEASE_CHECKLIST.md)" in readme
    assert "[Third-party License Metadata Review](THIRD_PARTY_LICENSES_REVIEW.md)" in readme
    assert "判定: **公開準備不可**" in checklist
    assert "project license" in checklist
    assert "real Docling" in checklist
    assert "法的判断を行いません" in license_review
    assert "docling" in license_review
    assert "unknown" in license_review


def test_integrated_batch_reports_and_quality_warning_smoke(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    _create_pdf_tree(
        input_dir,
        (
            "資料/既存.pdf",
            "資料/正常.pdf",
            "資料/短文.PDF",
            "除外/ignore.pdf",
        ),
    )
    existing_output = output_dir / "資料" / "既存.md"
    existing_output.parent.mkdir(parents=True)
    existing_output.write_text("既存の合成出力", encoding="utf-8")
    batch_json_path = tmp_path / "reports" / "batch.json"
    csv_path = tmp_path / "reports" / "batch.csv"
    quality_path = tmp_path / "reports" / "quality.json"
    converter = ReleaseSmokeConverter()

    exit_code = run(
        [
            "convert",
            str(input_dir),
            "--output",
            str(output_dir),
            "--recursive",
            "--include",
            "資料/**/*.pdf",
            "--exclude",
            "**/除外/*",
            "--report-json",
            str(batch_json_path),
            "--report-csv",
            str(csv_path),
            "--quality-warnings",
            "--quality-report-json",
            str(quality_path),
        ],
        converter_factory=lambda do_table_structure: converter,
    )

    captured = capsys.readouterr()
    batch_report = json.loads(batch_json_path.read_text(encoding="utf-8"))
    quality_report = json.loads(quality_path.read_text(encoding="utf-8"))
    with csv_path.open(encoding="utf-8-sig", newline="") as csv_file:
        csv_items = list(csv.DictReader(csv_file))

    assert exit_code == 0
    assert "成功=2 失敗=0 スキップ=1" in captured.out
    assert "ファイル=資料/短文.PDF 分類=short-output" in captured.err
    assert str(tmp_path) not in captured.err
    assert [path.relative_to(input_dir).as_posix() for path in converter.inputs] == [
        "資料/正常.pdf",
        "資料/短文.PDF",
    ]
    assert (output_dir / "資料" / "正常.md").is_file()
    assert (output_dir / "資料" / "短文.md").is_file()
    assert existing_output.read_text(encoding="utf-8") == "既存の合成出力"

    assert batch_report["summary"] == {
        "total": 3,
        "succeeded": 2,
        "failed": 0,
        "skipped": 1,
    }
    assert [item["input"] for item in batch_report["items"]] == [
        "資料/既存.pdf",
        "資料/正常.pdf",
        "資料/短文.PDF",
    ]
    assert [item["status"] for item in batch_report["items"]] == [
        "skipped",
        "succeeded",
        "succeeded",
    ]
    assert [row["input"] for row in csv_items] == [item["input"] for item in batch_report["items"]]
    assert [row["status"] for row in csv_items] == [
        item["status"] for item in batch_report["items"]
    ]

    assert quality_report["summary"] == {"checked": 2, "passed": 1, "warned": 1}
    assert [item["input"] for item in quality_report["items"]] == [
        "資料/正常.pdf",
        "資料/短文.PDF",
    ]
    assert [item["status"] for item in quality_report["items"]] == [
        "passed",
        "warned",
    ]
    assert quality_report["items"][1]["warnings"] == [
        {"category": "short-output", "message": "Markdown出力が極端に短い"}
    ]
    for report_path in (batch_json_path, csv_path, quality_path):
        report_text = report_path.read_text(
            encoding="utf-8-sig" if report_path.suffix == ".csv" else "utf-8"
        )
        assert str(tmp_path) not in report_text
        assert "Traceback" not in report_text
