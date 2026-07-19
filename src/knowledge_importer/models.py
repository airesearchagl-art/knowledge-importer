from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ConversionRequest:
    input_path: Path
    output_path: Path
    force: bool = False


class KnowledgeImporterError(Exception):
    """Base class for expected user-facing errors."""


class InputValidationError(KnowledgeImporterError):
    """Raised when an input path is not a readable PDF file."""


class OutputExistsError(KnowledgeImporterError):
    """Raised when output exists and overwrite was not requested."""
