"""Deterministic, opt-in normalization for generated Markdown."""

from __future__ import annotations

import re
import tempfile
from contextlib import suppress
from pathlib import Path

CONSERVATIVE_PROFILE = "conservative"
SUPPORTED_NORMALIZATION_PROFILES = (CONSERVATIVE_PROFILE,)

_OPENING_FENCE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,}).*$")


def _is_closing_fence(line: str, marker: str, minimum_length: int) -> bool:
    stripped = line.lstrip(" ")
    indentation = len(line) - len(stripped)
    if indentation > 3 or not stripped.startswith(marker * minimum_length):
        return False
    fence_length = len(stripped) - len(stripped.lstrip(marker))
    return fence_length >= minimum_length and not stripped[fence_length:].strip(" \t")


def _normalize_non_code_line(line: str) -> str:
    without_tabs = line.rstrip("\t")
    trailing_spaces = len(without_tabs) - len(without_tabs.rstrip(" "))
    if trailing_spaces == 1:
        return without_tabs[:-1]
    return without_tabs


def normalize_markdown(markdown: str, profile: str = CONSERVATIVE_PROFILE) -> str:
    """Normalize Markdown without changing document structure or code block content."""

    if profile != CONSERVATIVE_PROFILE:
        raise ValueError(f"unsupported normalization profile: {profile}")

    normalized = markdown.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    result: list[str] = []
    fence_marker: str | None = None
    fence_length = 0

    for line in lines:
        if fence_marker is not None:
            result.append(line)
            if _is_closing_fence(line, fence_marker, fence_length):
                fence_marker = None
                fence_length = 0
            continue

        opening = _OPENING_FENCE.match(line)
        if opening is not None:
            fence = opening.group("fence")
            fence_marker = fence[0]
            fence_length = len(fence)
            result.append(line)
            continue

        result.append(_normalize_non_code_line(line))

    if fence_marker is not None:
        # An unclosed fence makes trailing blank lines code content. Preserve them and
        # only guarantee that the file has a final LF.
        content = "\n".join(result)
        return content if content.endswith("\n") else f"{content}\n"

    while result and not result[-1].strip(" \t"):
        result.pop()
    return f"{'\n'.join(result)}\n"


def normalize_markdown_file(path: Path, profile: str = CONSERVATIVE_PROFILE) -> None:
    """Normalize a UTF-8 Markdown file and atomically replace it when needed."""

    original = path.read_text(encoding="utf-8")
    normalized = normalize_markdown(original, profile)
    if normalized == original:
        return

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(normalized)
        temporary_path.replace(path)
    except Exception:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)
        raise
