#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Global transcript ledger: every extracted video (single or batch) is appended
to ONE file - transcripts_library.jsonl (one JSON object per line).

Line schema:
{
  "video_id":  "7656...",
  "title":     "video title",
  "author":    "author nickname (batch) or '' (single)",
  "source":    "single" | "batch",
  "text":      "full transcript text",
  "output":    "path of the per-video md file (if saved)",
  "provider":  "siliconflow | dashscope | ark",
  "model":     "asr model used",
  "extracted_at": "2026-09-02 23:30:00"
}
"""

import json
from pathlib import Path
from datetime import datetime
from typing import Optional

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "output"
LIBRARY_FILE = OUTPUT_DIR / "transcripts_library.jsonl"


def append_record(
    video_id: str,
    title: str,
    text: str,
    author: str = "",
    source: str = "single",
    output: str = "",
    provider: str = "",
    model: str = "",
    library_path: Optional[Path] = None,
):
    """Append one extraction record to the global library file."""
    record = {
        "video_id": str(video_id),
        "title": title,
        "author": author,
        "source": source,
        "text": text,
        "output": output,
        "provider": provider,
        "model": model,
        "extracted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    path = Path(library_path) if library_path else LIBRARY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_records(library_path: Optional[Path] = None) -> list:
    """Read all records (newest last)."""
    path = Path(library_path) if library_path else LIBRARY_FILE
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except Exception:
            continue
    return records


def has_video(video_id: str, library_path: Optional[Path] = None) -> bool:
    """True if this video was extracted before (any source)."""
    return any(r.get("video_id") == str(video_id) for r in read_records(library_path))
