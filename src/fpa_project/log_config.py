"""Logging for the app and for uvicorn, so both emit the same fields.

LOG_LEVEL, LOG_LEVEL_UVICORN and LOG_HANDLER (console|json) change verbosity and
format without a rebuild.
"""

from __future__ import annotations

import logging.config
import os

FORMAT = "%(asctime)s %(levelname)s %(name)s %(pathname)s %(funcName)s %(lineno)s %(message)s"


def _level(name: str, default: str = "INFO") -> str:
    return (os.getenv(name) or default).upper()


def dict_config() -> dict:
    handler = "json" if (os.getenv("LOG_HANDLER") or "console").lower() == "json" else "console"
    app_level = _level("LOG_LEVEL")
    uvicorn_level = _level("LOG_LEVEL_UVICORN")
    # dictConfig builds every formatter it is given, so declaring only the selected
    # one keeps console logging working without python-json-logger installed.
    if handler == "json":
        formatter = {"()": "pythonjsonlogger.json.JsonFormatter", "format": FORMAT}
    else:
        formatter = {"format": FORMAT}
    return {
        "version": 1,
        # uvicorn configures its loggers first; this replaces that, not silences it.
        "disable_existing_loggers": False,
        "formatters": {handler: formatter},
        "handlers": {
            handler: {"class": "logging.StreamHandler", "stream": "ext://sys.stdout",
                      "formatter": handler},
        },
        "loggers": {
            name: {"handlers": [handler], "level": uvicorn_level, "propagate": False}
            for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
        },
        "root": {"handlers": [handler], "level": app_level},
    }


def configure() -> None:
    logging.config.dictConfig(dict_config())
