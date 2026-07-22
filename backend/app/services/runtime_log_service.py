import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re


LOGGER_NAME = "review_agent.execution"
MAX_LOG_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3
SUPPORTED_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_SENSITIVE_VALUE = re.compile(
    r"(?i)\b(?:api[_-]?key|authorization)\b\s*[:=]\s*(?:bearer\s+)?\S+"
    r"|\bbearer\s+\S+"
)


def configure_runtime_logging(log_file: str, level: str = "INFO") -> logging.Logger:
    resolved_level = str(level or "INFO").upper()
    if resolved_level not in SUPPORTED_LEVELS:
        raise ValueError(
            "REVIEW_AGENT_RUNTIME_LOG_LEVEL must be one of "
            + ", ".join(sorted(SUPPORTED_LEVELS))
        )
    resolved_path = Path(log_file).expanduser().resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, resolved_level))
    logger.propagate = False
    for existing_handler in list(logger.handlers):
        if not isinstance(existing_handler, RotatingFileHandler):
            existing_handler.close()
            logger.removeHandler(existing_handler)
            continue
        if Path(existing_handler.baseFilename).resolve() != resolved_path:
            existing_handler.flush()
            existing_handler.close()
            logger.removeHandler(existing_handler)
    if not any(
        isinstance(handler, RotatingFileHandler)
        and Path(handler.baseFilename).resolve() == resolved_path
        for handler in logger.handlers
    ):
        handler = RotatingFileHandler(
            resolved_path,
            maxBytes=MAX_LOG_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def write_runtime_log(event: str, **fields) -> None:
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        return
    payload = {
        "event": _safe_text(event),
        **{key: _safe_value(value) for key, value in fields.items()},
    }
    logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def close_runtime_logging() -> None:
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        handler.flush()
        handler.close()
        logger.removeHandler(handler)


def _safe_value(value):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _safe_text(value)


def _safe_text(value) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    text = _SENSITIVE_VALUE.sub("[redacted]", text)
    return text[:500]
