from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
import threading
import time


LOGGER_ROOT_NAME = "busapi"
LOG_RETENTION_DAYS = 7
_SETUP_LOCK = threading.Lock()
_CONFIGURED_LOG_DIR: Path | None = None


def _cleanup_old_logs(log_dir: Path, *, retention_days: int = LOG_RETENTION_DAYS) -> None:
    cutoff = time.time() - (retention_days * 86400)
    for path in log_dir.glob("*.log*"):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def setup_logging(project_dir: str | Path) -> Path:
    global _CONFIGURED_LOG_DIR

    log_dir = Path(project_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    _cleanup_old_logs(log_dir)

    with _SETUP_LOCK:
        app_logger = logging.getLogger(LOGGER_ROOT_NAME)
        if _CONFIGURED_LOG_DIR == log_dir and app_logger.handlers:
            return log_dir

        for handler in list(app_logger.handlers):
            app_logger.removeHandler(handler)
            handler.close()

        formatter = logging.Formatter(
            fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)

        file_handler = TimedRotatingFileHandler(
            filename=log_dir / "app.log",
            when="midnight",
            interval=1,
            backupCount=LOG_RETENTION_DAYS,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)

        app_logger.setLevel(logging.INFO)
        app_logger.propagate = False
        app_logger.addHandler(stream_handler)
        app_logger.addHandler(file_handler)

        _CONFIGURED_LOG_DIR = log_dir
        return log_dir


def shutdown_logging() -> None:
    global _CONFIGURED_LOG_DIR

    with _SETUP_LOCK:
        app_logger = logging.getLogger(LOGGER_ROOT_NAME)
        for handler in list(app_logger.handlers):
            app_logger.removeHandler(handler)
            handler.close()
        _CONFIGURED_LOG_DIR = None


def get_logger(name: str | None = None) -> logging.Logger:
    if not name:
        return logging.getLogger(LOGGER_ROOT_NAME)
    return logging.getLogger(f"{LOGGER_ROOT_NAME}.{name}")
