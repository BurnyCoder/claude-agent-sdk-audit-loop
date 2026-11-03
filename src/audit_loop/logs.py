"""Timestamped logging to the terminal and to one full, untruncated log file per session."""

import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("audit_loop")
_FORMAT = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")


def setup_console() -> None:
    """Log INFO and up to stdout; UTF-8 so model output with any characters prints on Windows."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    if not log.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_FORMAT)
        log.addHandler(handler)
        log.setLevel(logging.INFO)


@contextmanager
def session_log(log_dir: Path, name: str) -> Iterator[Path]:
    """Also copy every log line to ``log_dir/<UTC time>_<name>.log`` while the block runs."""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_{name}.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(_FORMAT)
    log.addHandler(handler)
    try:
        yield path
    finally:
        log.removeHandler(handler)
        handler.close()
