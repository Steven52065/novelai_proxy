from __future__ import annotations

import logging
from pathlib import Path

from app.config import LoggingConfig
from app.logging_utils import configure_logging


class CloseTrackingFileHandler(logging.FileHandler):
    def __init__(self, filename: Path):
        super().__init__(filename, encoding="utf-8")
        self.close_called = False

    def close(self) -> None:
        self.close_called = True
        super().close()


def test_configure_logging_closes_replaced_handlers(tmp_path: Path):
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    for handler in original_handlers:
        root_logger.removeHandler(handler)

    old_handler = CloseTrackingFileHandler(tmp_path / "old.log")
    root_logger.addHandler(old_handler)

    try:
        configure_logging(
            LoggingConfig(
                directory=str(tmp_path / "logs"),
                request_log_file="new.log",
            )
        )

        assert old_handler.close_called
        assert old_handler.stream is None
    finally:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
            handler.close()
        for handler in original_handlers:
            root_logger.addHandler(handler)
        root_logger.setLevel(original_level)
