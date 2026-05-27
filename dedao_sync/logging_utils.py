from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from .security import redact


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return redact(rendered)


def setup_logging(root_dir: Path) -> Path:
    logs_dir = root_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{datetime.now().strftime('%Y-%m-%d')}.log"
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(RedactingFormatter("%(levelname)s %(message)s"))
    logger.addHandler(console)
    return log_path
