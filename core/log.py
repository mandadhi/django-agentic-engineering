"""Structured logging on top of the standard library.

Library code only calls :func:`get_logger` and passes structured fields through
``extra=``; it never installs handlers. Applications (the CLI, a future runner)
call :func:`configure_logging` once.

    log = get_logger(__name__)
    log.info("stage.started", extra={"execution_id": eid, "stage_id": sid})
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Callable

ROOT_LOGGER = "dae"

# Attributes every LogRecord has; anything else came from `extra=` and is structured data.
_STANDARD_ATTRS = frozenset(
    vars(logging.LogRecord("x", logging.INFO, "x", 0, "x", None, None)).keys()
) | {"message", "asctime", "taskName"}

Redact = Callable[[str], str]


def _structured_fields(record: logging.LogRecord) -> dict[str, object]:
    return {k: v for k, v in vars(record).items() if k not in _STANDARD_ATTRS and not k.startswith("_")}


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, event, plus structured fields."""

    def __init__(self, redact: Redact | None = None) -> None:
        super().__init__()
        self._redact = redact

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": message,
        }
        payload.update(_structured_fields(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        text = json.dumps(payload, default=str, ensure_ascii=False)
        return self._redact(text) if self._redact else text


class TextFormatter(logging.Formatter):
    """Human-readable variant: `LEVEL logger event key=value ...`."""

    def __init__(self, redact: Redact | None = None) -> None:
        super().__init__()
        self._redact = redact

    def format(self, record: logging.LogRecord) -> str:
        fields = " ".join(f"{k}={v}" for k, v in _structured_fields(record).items())
        text = f"{record.levelname:<7} {record.name} {record.getMessage()} {fields}".rstrip()
        if record.exc_info:
            text += "\n" + self.formatException(record.exc_info)
        return self._redact(text) if self._redact else text


def get_logger(name: str) -> logging.Logger:
    """Logger under the harness namespace (``dae.<name>``)."""
    if name == ROOT_LOGGER or name.startswith(ROOT_LOGGER + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT_LOGGER}.{name}")


def configure_logging(level: str = "INFO", fmt: str = "json", redact: Redact | None = None,
                      stream=None) -> logging.Logger:
    """Install a single handler on the harness root logger. Idempotent."""
    root = logging.getLogger(ROOT_LOGGER)
    for handler in list(root.handlers):
        if getattr(handler, "_dae_handler", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter(redact) if fmt == "json" else TextFormatter(redact))
    handler._dae_handler = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level.upper())
    root.propagate = False
    return root


logging.getLogger(ROOT_LOGGER).addHandler(logging.NullHandler())
