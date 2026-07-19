from knowledge_importer.cli import run
from knowledge_importer.logging_config import configure_logging


def main() -> int:
    """Run the command-line application."""
    configure_logging()
    return run()
