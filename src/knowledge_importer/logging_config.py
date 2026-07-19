import logging
from pathlib import Path


def configure_logging(log_path: Path = Path("logs/knowledge-importer.log")) -> None:
    """Configure file logging without sending library logs to the console."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger = logging.getLogger("knowledge_importer")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False
