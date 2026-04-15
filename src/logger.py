"""Lightweight CSV interaction logger."""
from __future__ import annotations

import csv
import logging
from datetime import datetime
from pathlib import Path
from threading import Lock

from .config import SETTINGS

_LOG_COLUMNS = [
    "timestamp",
    "question",
    "sql",
    "status",
    "rows",
    "error",
    "duration_ms",
]

_write_lock = Lock()

logging.basicConfig(
    level=SETTINGS.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ai_data_analyst")


def _ensure_header(path: Path) -> None:
    if path.exists() and path.stat().st_size > 0:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(_LOG_COLUMNS)


def log_interaction(
    question: str,
    sql: str,
    status: str,
    rows: int = 0,
    error: str | None = None,
    duration_ms: float | None = None,
) -> None:
    path = SETTINGS.log_file
    with _write_lock:
        _ensure_header(path)
        with path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                [
                    datetime.utcnow().isoformat(timespec="seconds"),
                    question,
                    sql.replace("\n", " ").strip(),
                    status,
                    rows,
                    (error or "").replace("\n", " ").strip(),
                    f"{duration_ms:.1f}" if duration_ms is not None else "",
                ]
            )
    logger.info("interaction logged status=%s rows=%d", status, rows)
