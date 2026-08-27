from __future__ import annotations

import os
import time
from pathlib import Path

from config import LOG_DIR
from models import now_iso
from core.runtime_primitives import india_now

LOG_MAX_BYTES = int(os.environ.get("PROJECT_LADDU_LOG_MAX_BYTES", "10485760"))
LOG_RETENTION_DAYS = int(os.environ.get("PROJECT_LADDU_LOG_RETENTION_DAYS", "21"))

def _daily_log_path(name: str = "backend") -> Path:
    day = india_now().strftime("%Y-%m-%d")
    day_dir = LOG_DIR / day
    day_dir.mkdir(parents=True, exist_ok=True)
    return day_dir / f"{name}.log"

def _rotate_if_needed(path: Path, max_bytes: int = LOG_MAX_BYTES, keep: int = 5) -> None:
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return
        for i in range(keep - 1, 0, -1):
            src = path.with_name(path.name + f".{i}")
            dst = path.with_name(path.name + f".{i+1}")
            if src.exists():
                try:
                    if dst.exists():
                        dst.unlink()
                    src.rename(dst)
                except Exception:
                    pass
        first = path.with_name(path.name + ".1")
        try:
            if first.exists():
                first.unlink()
            path.rename(first)
        except Exception:
            pass
    except Exception:
        pass

def _cleanup_old_logs() -> None:
    try:
        cutoff = time.time() - (LOG_RETENTION_DAYS * 86400)
        if not LOG_DIR.exists():
            return
        for child in LOG_DIR.iterdir():
            try:
                if child.is_dir() and child.stat().st_mtime < cutoff:
                    for f in child.glob("*"):
                        try:
                            f.unlink()
                        except Exception:
                            pass
                    child.rmdir()
            except Exception:
                pass
    except Exception:
        pass

def log_line(msg: str, name: str = "backend") -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{now_iso()} {msg}\n"
    path = _daily_log_path(name)
    _rotate_if_needed(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
    # Compatibility pointer for old tools that still open logs\backend.log.
    compat = LOG_DIR / f"{name}.log"
    _rotate_if_needed(compat, max_bytes=max(1048576, LOG_MAX_BYTES // 4), keep=2)
    with open(compat, "a", encoding="utf-8") as f:
        f.write(line)

cleanup_old_logs = _cleanup_old_logs
