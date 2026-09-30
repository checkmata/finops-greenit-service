"""Logging setup driven by LOG_LEVEL / LOG_FORMAT (V1: these settings were unused)."""

from __future__ import annotations

import logging

from app.config import Settings


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler()
    if settings.LOG_FORMAT == "json":
        from pythonjsonlogger import jsonlogger  # dependency already pinned in requirements.txt

        handler.setFormatter(jsonlogger.JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(settings.LOG_LEVEL.upper())
    # httpx logs every Prometheus query at INFO (dozens per cycle) — keep it quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
