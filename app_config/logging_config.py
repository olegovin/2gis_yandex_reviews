import logging
import logging.handlers
from pathlib import Path


def setup_logging(log_level: str = "INFO") -> None:
    """
    Configure root logger:
    - Rotating file handler: logs/app.log, 10 MB, 5 backups
    - StreamHandler to stdout
    """
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)

    log_format = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # --- Rotating file handler ---
    file_handler = logging.handlers.RotatingFileHandler(
        filename=logs_dir / "app.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(log_format)
    root_logger.addHandler(file_handler)

    # --- stdout handler ---
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(log_format)
    root_logger.addHandler(stream_handler)

    # Silence noisy third-party loggers
    for noisy in ("httpx", "httpcore", "playwright", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
