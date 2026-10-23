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
import re
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


# ---------------------------------------------------------------------------
# Per-video execution log: one JSON line per extracted video (WebUI batch,
# pending queue, single extract, backend refresh driver) so the WebUI's
# "执行日志" view can show which UP / which video succeeded, skipped or
# failed, newest first. Capped so the file never grows unbounded.
# ---------------------------------------------------------------------------
VIDEO_LOG_FILE = _LOG_DIR / "video_results.jsonl"
_VIDEO_LOG_MAX_BYTES = 3 * 1024 * 1024   # trim when exceeded
_VIDEO_LOG_KEEP_LINES = 10000            # total hard cap after a trim
_VIDEO_LOG_STATUS_QUOTA = 2500           # 按状态配额：每状态各留 N 行（D1＝A）
_vlog_lock = threading.Lock()


def _trim_video_log() -> None:
    """按状态配额截断视频执行日志（D1 拍板 A·F-014 根治）。

    老实现只留「最新 10000 行」：跳过日志刷屏时，成功/失败行会被挤出保留
    窗口（实测状态分布 skip 10811／fail 48／error 11／ok 0＝「成功」分类
    一度恒空）。改为每状态各留最近 N 行（配额内按时间取最新），并保留
    总行数硬上限兜底；只在文件超字节上限时触发（调用方判断）。
    """
    text = VIDEO_LOG_FILE.read_text(encoding="utf-8")
    lines = [l for l in text.splitlines() if l.strip()]
    # 从新往旧按状态配额入选，再倒回原时序写回（读取侧依赖时序）
    kept_rev, per, total = [], {}, 0
    for line in reversed(lines):
        try:
            st = str(json.loads(line).get("status") or "")
        except Exception:
            st = "__unknown__"          # 坏行也按配额保留，不当场丢证
        n = per.get(st, 0)
        if n >= _VIDEO_LOG_STATUS_QUOTA:
            continue
        per[st] = n + 1
        kept_rev.append(line)
        total += 1
        if total >= _VIDEO_LOG_KEEP_LINES:
            break
    kept = list(reversed(kept_rev))
    with open(VIDEO_LOG_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(kept) + "\n")


def log_video_result(status: str, author: str = "", title: str = "",
                     video_id: str = "", trigger: str = "", **fields):
    """Append one per-video result record. Logging never raises."""
    record = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": status or "",
        "author": author or "",
        "video_id": str(video_id or ""),
        "title": (title or "")[:200],
        "trigger": trigger or "",
    }
    for k, v in fields.items():
        if v is None or v == "":
            continue
        record[k] = str(v)[:500]
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        with _vlog_lock:
            with open(VIDEO_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            # Cap file size: 按状态配额截断（不再按总行数一刀切）
            try:
                if VIDEO_LOG_FILE.stat().st_size > _VIDEO_LOG_MAX_BYTES:
                    _trim_video_log()
            except Exception:
                pass
    except Exception:
        pass  # logging must never break the caller


def read_video_logs(limit: int = 300, status: str = "",
                    author: str = "") -> list:
    """Read per-video results, NEWEST FIRST (execution-time descending).

    Optional filters: status (ok/skip/fail/error), author (substring match).
    """
    if not VIDEO_LOG_FILE.exists():
        return []
    try:
        text = VIDEO_LOG_FILE.read_text(encoding="utf-8")
    except Exception:
        return []
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if status and rec.get("status") != status:
            continue
        if author and author not in (rec.get("author") or ""):
            continue
        entries.append(rec)
    entries.reverse()  # append order is chronological -> newest first
    return entries[:limit] if limit > 0 else entries


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


# ---------------------------------------------------------------------------
# Runtime log stream (WebUI「运行日志」view): the driver's live console log
# (logs/driver_live.log, plain "[ts] msg" lines) merged with the structured
# WebUI operation log (operation_logs.txt), NEWEST FIRST, so the user can
# watch the currently running task live and page back through history.
# ---------------------------------------------------------------------------
DRIVER_LOG_FILE = _LOG_DIR.parent / "logs" / "driver_live.log"
_RUNTIME_TAIL_BYTES = 512 * 1024            # live-tail scan window per source
_RUNTIME_MAX_SCAN_BYTES = 32 * 1024 * 1024  # cap when widening for history
_RUNTIME_NOISE_ACTIONS = {"http.request"}   # too chatty for the stream view
_DRIVER_LINE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] (.*)$")


def _parse_runtime_web_line(line: str) -> Optional[dict]:
    if '"http.request"' in line:  # cheap pre-filter (~95% of the file)
        return None
    try:
        rec = json.loads(line)
    except Exception:
        return None
    action = str(rec.get("action") or "")
    if action in _RUNTIME_NOISE_ACTIONS:
        return None
    status = str(rec.get("status") or "")
    # 动作日志展示位：msg/动作 字段直接当正文（不打 key= 前缀），其余字段仍
    # key=value；全空则回落成「动作｜状态」——保证每一行都带可读的动作信息，
    # 源侧无信息日志已统一补 msg，此处为历史行兜底。
    parts = []
    for k, v in rec.items():
        if k in ("ts", "action", "status"):
            continue
        parts.append(str(v) if k in ("msg", "动作") else f"{k}={v}")
    body = " ".join(parts) or f"{action or '-'}｜{status or '-'}"
    return {"ts": str(rec.get("ts") or ""), "kind": "web",
            "action": action or "-", "status": status,
            "msg": body[:400]}


def _parse_runtime_driver_line(line: str) -> Optional[dict]:
    m = _DRIVER_LINE_RE.match(line)
    if not m:
        return None
    return {"ts": m.group(1), "kind": "driver", "action": "driver",
            "status": "", "msg": m.group(2)[:400]}


def _parse_runtime_video_line(line: str) -> Optional[dict]:
    try:
        rec = json.loads(line)
    except Exception:
        return None
    if not isinstance(rec, dict) or not rec.get("ts"):
        return None
    extra = [f"{k}={v}" for k, v in rec.items()
             if k not in ("ts", "status", "author", "video_id", "title",
                          "trigger", "error", "output") and v not in ("", None)]
    return {"ts": str(rec.get("ts") or ""), "kind": "video",
            "action": "video", "status": str(rec.get("status") or ""),
            "author": str(rec.get("author") or ""),
            "video_id": str(rec.get("video_id") or ""),
            "title": str(rec.get("title") or "")[:200],
            "trigger": str(rec.get("trigger") or ""),
            "error": str(rec.get("error") or "")[:400],
            "output": str(rec.get("output") or "")[:200],
            "msg": " ".join(extra)[:400]}


def _scan_runtime_file(path: Path, parse_fn, before: str,
                       need: int, status: str = "") -> tuple:
    """Read one log file from its END and return (entries, exhausted).

    entries: parsed records older than `before` (ts anchor), newest first,
    capped at `need` (0 = unlimited). The scan starts with the last 512KB
    and widens x4 (up to 32MB) so「加载更多」reaches deep history cheaply.
    `status` filters entries INSIDE the scan so a requested status is
    collected from deep history even when recent pages are dominated by
    other statuses. exhausted=True means the beginning of the file has been
    reached (or the scan cap hit) - no older bytes remain.
    """
    try:
        if not path.exists():
            return [], True
        size = path.stat().st_size
        window = _RUNTIME_TAIL_BYTES
        while True:
            start = max(0, size - window)
            with open(path, "rb") as f:
                f.seek(start)
                blob = f.read()
            if start > 0:  # drop the (possibly partial) first line
                nl = blob.find(b"\n")
                if nl == -1:
                    return [], start <= 0
                blob = blob[nl + 1:]
            lines = blob.decode("utf-8", errors="replace").splitlines()
            entries = [e for line in lines if (e := parse_fn(line.strip()))]
            if status:
                entries = [e for e in entries if e.get("status") == status]
            if before:
                entries = [e for e in entries if e["ts"] < before]
            entries.reverse()  # append order is chronological -> newest first
            done = start <= 0 or window >= _RUNTIME_MAX_SCAN_BYTES
            if 0 < need <= len(entries) or done:
                return entries[:need] if need > 0 else entries, done
            window *= 4
    except Exception:
        return [], True


def read_runtime_logs(limit: int = 200, before: str = "",
                      status: str = "") -> dict:
    """Unified runtime stream (driver live log + WebUI operation log +
    per-video results), newest first. `before` ("YYYY-MM-DD HH:MM:SS")
    pages older history: only entries strictly older than the anchor are
    returned.

    `status`（成功/跳过/失败/错误 分类）只作用于「任务/文案」的每视频执行
    结果：状态分类下不合并 driver 实时输出与 WebUI 动作日志——它们是程序
    运行日志，只在【全部】里看（status 为空时三源照常合并）。

    Returns {"logs": [...], "has_more": bool} - has_more tells the UI
    whether an even older「加载更多」page may exist.
    """
    if status:
        # 状态分类＝任务/文案结果面：只扫视频结果文件（动作日志不参与分类）
        web, web_done = [], True
        drv, drv_done = [], True
    else:
        web, web_done = _scan_runtime_file(
            LOG_FILE, _parse_runtime_web_line, before, limit, status)
        drv, drv_done = _scan_runtime_file(
            DRIVER_LOG_FILE, _parse_runtime_driver_line, before, limit, status)
    vid, vid_done = _scan_runtime_file(
        VIDEO_LOG_FILE, _parse_runtime_video_line, before, limit, status)
    merged = sorted(web + drv + vid, key=lambda e: e["ts"], reverse=True)
    page = merged[:limit] if limit > 0 else merged
    has_more = len(merged) > len(page) or not (web_done and drv_done
                                               and vid_done)
    return {"logs": page, "has_more": has_more}
