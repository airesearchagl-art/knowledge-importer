"""Shared atomic JSON report writing."""

from __future__ import annotations

import json
import tempfile
from contextlib import suppress
from pathlib import Path


def write_json_atomically(report_path: Path, payload: dict[str, object]) -> None:
    """Write a deterministic UTF-8 JSON document without corrupting an existing file."""

    if report_path.is_dir():
        raise IsADirectoryError

    report_path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{report_path.name}.",
            suffix=".tmp",
            dir=report_path.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(f"{content}\n")
        temporary_path.replace(report_path)
    except Exception:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)
        raise
