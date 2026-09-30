"""Logging setup with daily rotation and automatic archiving.

Writes to ``logs/latest.log`` and archives previous days' logs as
``logs/YYYY-MM-DD.tar.gz`` at midnight.
"""
from __future__ import annotations

import logging
import logging.handlers
import tarfile
from datetime import datetime
from pathlib import Path


class DailyArchiveHandler(logging.handlers.TimedRotatingFileHandler):
    """Rotates log files at midnight and archives them as .tar.gz.

    The active log is always ``logs/latest.log``. At midnight the
    previous day's log is compressed to ``logs/YYYY-MM-DD.tar.gz``
    and removed as a plain file.
    """

    def __init__(self, logs_dir: Path, **kwargs):
        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.latest_path = self.logs_dir / "latest.log"
        super().__init__(
            filename=str(self.latest_path),
            when="midnight",
            interval=1,
            backupCount=0,
            encoding="utf-8",
            **kwargs,
        )
        self.suffix = "%Y-%m-%d"
        self.extMatch = r"^\d{4}-\d{2}-\d{2}$"

    def rotation_filename(self, default_name: str) -> str:
        """Return the date-stamped filename for the rotated log."""
        date_str = datetime.now().strftime(self.suffix)
        return str(self.logs_dir / f"{date_str}.log")

    def doRollover(self) -> None:
        """Rotate the log and archive the previous day's file."""
        super().doRollover()
        date_str = datetime.now().strftime(self.suffix)
        date_log = self.logs_dir / f"{date_str}.log"
        if date_log.exists():
            self._archive(date_log)

    def _archive(self, log_path: Path) -> None:
        """Compress a log file into a .tar.gz archive and remove the .log."""
        archive_path = self.logs_dir / f"{log_path.stem}.tar.gz"
        try:
            with tarfile.open(str(archive_path), "w:gz") as tar:
                tar.add(str(log_path), arcname=log_path.name)
            log_path.unlink()
        except Exception:
            pass


class WebSocketLogHandler(logging.Handler):
    """Custom log handler that broadcasts application log lines via WebSocket in real-time.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            import asyncio
            from minamo.admin.ws import ws_manager

            msg = self.format(record)
            loop = asyncio.get_running_loop()
            if loop and loop.is_running():
                loop.create_task(ws_manager.broadcast("app_log", {"line": msg}))
        except Exception:
            pass


def setup_logging(
    logs_dir: Path,
    logger_name: str = "minamo",
    level: int = logging.INFO,
) -> logging.Logger:
    """Configure logging with daily rotation, archiving, and real-time WebSocket broadcasting.

    Returns the configured logger instance.
    """
    logger = logging.getLogger(logger_name)
    logger.setLevel(level)
    logger.handlers.clear()

    handler = DailyArchiveHandler(logs_dir=logs_dir)
    handler.setLevel(level)

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    # Attach WebSocket log handler
    ws_handler = WebSocketLogHandler()
    ws_handler.setLevel(level)
    ws_handler.setFormatter(formatter)
    logger.addHandler(ws_handler)

    if level <= logging.DEBUG:
        console = logging.StreamHandler()
        console.setLevel(level)
        console.setFormatter(formatter)
        logger.addHandler(console)

    return logger
