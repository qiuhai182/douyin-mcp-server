#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Global runtime operation log: every operation (HTTP request, video info query,
transcript extraction, batch progress, config change, MCP tool call) is
appended to ONE file - log/operation_logs.txt (one JSON object per line).

Line schema:
{
  "ts":     "2026-09-03 12:00:00",
  "action": "video.extract | http.request | batch.progress | config.save | mcp.tool ...",
  "status": "ok | error | skip | ...",
  ...operation-specific fields (strings truncated to MAX_FIELD_LEN)
}
"""

import json
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional

# All runtime logs live in a dedicated "log" directory under the project root.
_LOG_DIR = Path(__file__).resolve().parent.parent.parent / "log"
LOG_FILE = _LOG_DIR / "operation_logs.txt"
MAX_FIELD_LEN = 500

_lock = threading.Lock()


def log_operation(action: str, status: str = "ok", **fields):
    """Append one operation record to the runtime log file.

    Non-serializable values are str()'d; long strings are truncated so each
    line stays bounded. Logging failures never propagate to the caller.
    """
    record = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "status": status,
    }
    for k, v in fields.items():
        if v is None:
            continue
        if not isinstance(v, (str, int, float, bool)):
            try:
                v = json.dumps(v, ensure_ascii=False)
            except Exception:
                v = str(v)
        if isinstance(v, str) and len(v) > MAX_FIELD_LEN:
            v = v[:MAX_FIELD_LEN] + "..."
        record[k] = v
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        with _lock:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # logging must never break the app


def log_file_path() -> Path:
    """Absolute path of the runtime log file."""
    return LOG_FILE


def read_logs(limit: int = 200,
              action: Optional[str] = None,
              status: Optional[str] = None) -> list:
    """Read the most recent log entries (newest last), optionally filtered."""
    if not LOG_FILE.exists():
        return []
    entries = []
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if action and rec.get("action") != action:
            continue
        if status and rec.get("status") != status:
            continue
        entries.append(rec)
    return entries[-limit:] if limit > 0 else entries
