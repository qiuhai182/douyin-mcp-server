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
import time
import uuid
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional

# All runtime logs live in a dedicated "log" directory under the project root.
_LOG_DIR = Path(__file__).resolve().parent.parent.parent / "log"
LOG_FILE = _LOG_DIR / "operation_logs.txt"
MAX_FIELD_LEN = 500

_lock = threading.Lock()


def _now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _build_record(action: str, status: str = "ok", ts: str = None,
                  fields: Optional[dict] = None) -> dict:
    """One operation-log record. Existing keys keep their meaning; callers may
    pass an explicit `ts` (stage events timestamped when they happened, not
    when the buffered write lands)."""
    record = {
        "ts": ts or _now_ts(),
        "action": action,
        "status": status,
    }
    for k, v in (fields or {}).items():
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
    return record


def log_operation(action: str, status: str = "ok", ts: str = None, **fields):
    """Append one operation record to the runtime log file.

    Non-serializable values are str()'d; long strings are truncated so each
    line stays bounded. Logging failures never propagate to the caller.
    """
    record = _build_record(action, status, ts, fields)
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        with _lock:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # logging must never break the app


def append_operations(items) -> None:
    """Append several buffered records in ONE open/write (no fsync).

    items: iterable of {"ts", "action", "status", "fields"} dicts, produced by
    StageTrace. Same line schema as log_operation, so existing readers are
    unaffected.
    """
    lines = []
    for it in items or []:
        rec = _build_record(it.get("action", ""), it.get("status", "ok"),
                            it.get("ts"), it.get("fields"))
        lines.append(json.dumps(rec, ensure_ascii=False))
    if not lines:
        return
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        with _lock:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
    except Exception:
        pass


def log_file_path() -> Path:
    """Absolute path of the runtime log file."""
    return LOG_FILE


# ---------------------------------------------------------------------------
# Per-stage timing (download / audio extract / ASR / kb write)
#
# Why a buffer instead of a log call per event: the pipeline must not pay a
# file open+write (or a fsync) on every stage boundary. StageTrace only takes
# one perf_counter() per boundary and one clock read per event, then flush()
# appends all of the video's events with a single buffered write.
#
# Every stage event carries batch_id / worker_id / video_id so one video's
# download and ASR can be told apart from the next one's in the flat log.
# Fields that a stage cannot actually measure are left EMPTY (None -> omitted)
# - never back-filled from the total duration.
# ---------------------------------------------------------------------------
STAGE_EVENT_FIELDS = ("batch_id", "worker_id",
                      "resolve_ms", "download_ms", "audio_ms", "asr_ms",
                      "parse_ms", "kb_write_ms")


def new_batch_id() -> str:
    """Short unique id for one batch/single-video run (log correlation)."""
    return "%s_%s" % (datetime.now().strftime("%Y%m%d_%H%M%S"),
                      uuid.uuid4().hex[:6])


def stage_fields(payload: Optional[dict]) -> dict:
    """Pick the stage-timing keys out of an event/trace payload.

    Missing/empty values are dropped, so a video processed by an older code
    path yields exactly the old record (no empty columns added).
    """
    payload = payload or {}
    return {k: payload[k] for k in STAGE_EVENT_FIELDS
            if payload.get(k) not in (None, "")}


class StageTrace:
    """Timing + events for ONE video's processing stages.

    Usage:
        trace = StageTrace(video_id=..., batch_id=..., worker_id=...)
        trace.start("download"); ...; ms = trace.end("download")
        trace.emit("video.download.end", bytes=n, http_status=200,
                   retry=0, duration_ms=ms)
        ...
        trace.flush()   # one append for all events of this video
    """

    # resolve = 取播放直链（parse_share_url，抓取的前半段）
    STAGES = ("resolve", "download", "audio", "asr", "parse", "kb_write")

    def __init__(self, video_id: str = "", batch_id: str = "",
                 worker_id: str = ""):
        self.video_id = str(video_id or "")
        self.batch_id = batch_id or ""
        self.worker_id = worker_id or ""
        self.durations = {}
        self._starts = {}
        self._events = []

    # -- timing -----------------------------------------------------------
    def start(self, stage: str) -> None:
        self._starts[stage] = time.perf_counter()

    def end(self, stage: str) -> Optional[float]:
        """Close one stage, remember the ms reading, and return it."""
        t0 = self._starts.pop(stage, None)
        if t0 is None:
            return None
        ms = round((time.perf_counter() - t0) * 1000.0, 1)
        self.durations[stage] = ms
        return ms

    # -- events -----------------------------------------------------------
    def emit(self, action: str, status: str = "ok", **fields) -> None:
        enriched = {"batch_id": self.batch_id,
                    "worker_id": self.worker_id,
                    "video_id": self.video_id}
        enriched.update(fields)
        self._events.append({"ts": _now_ts(), "action": action,
                             "status": status, "fields": enriched})

    def result_fields(self) -> dict:
        """Stage durations in video_results.jsonl column form."""
        out = {"batch_id": self.batch_id, "worker_id": self.worker_id}
        for stage in self.STAGES:
            if stage in self.durations:
                out[stage + "_ms"] = self.durations[stage]
        return out

    def flush(self) -> None:
        """Write all buffered events in one go. Never raises."""
        if not self._events:
            return
        pending, self._events = self._events, []
        try:
            append_operations(pending)
        except Exception:
            pass


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


_READ_CHUNK_BYTES = 256 * 1024   # reverse-reader step size


def _iter_lines_reverse(path: Path, chunk: int = _READ_CHUNK_BYTES):
    """Yield the file's non-empty lines from the END backwards.

    Only the bytes actually needed are read: the reader starts at EOF and
    pulls one `chunk` more only when the buffer runs out of lines, so a
    90MB log costs a few hundred KB for the newest 200 records instead of
    87MB of text + 800k json.loads (measured 3.5s -> ~10ms).
    """
    try:
        f = open(path, "rb")
    except Exception:
        return
    with f:
        f.seek(0, 2)
        pos = f.tell()
        buf = b""
        while True:
            if pos > 0:
                step = min(chunk, pos)
                pos -= step
                f.seek(pos)
                buf = f.read(step) + buf
                parts = buf.split(b"\n")
                if pos > 0:
                    # parts[0] may be a partial line: keep it for the next read
                    buf = parts[0]
                    complete = parts[1:]
                else:
                    buf = b""
                    complete = parts
            else:
                complete = [buf] if buf else []
                buf = b""
            for raw in reversed(complete):
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    yield line
            if pos <= 0:
                return


def read_video_logs(limit: int = 300, status: str = "",
                    author: str = "") -> list:
    """Read per-video results, NEWEST FIRST (execution-time descending).

    Optional filters: status (ok/skip/fail/error), author (substring match).
    Scans from EOF and stops once `limit` matches are collected.
    """
    if not VIDEO_LOG_FILE.exists():
        return []
    entries = []
    for line in _iter_lines_reverse(VIDEO_LOG_FILE):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if status and rec.get("status") != status:
            continue
        if author and author not in (rec.get("author") or ""):
            continue
        entries.append(rec)
        if limit > 0 and len(entries) >= limit:
            break
    return entries


def read_logs(limit: int = 200,
              action: Optional[str] = None,
              status: Optional[str] = None) -> list:
    """Read the most recent log entries (newest last), optionally filtered.

    Reverse-scans from EOF, so cost tracks `limit`, not file size.
    """
    if not LOG_FILE.exists():
        return []
    entries = []
    # Cheap pre-filter for an action query: a serialized record always carries
    # the quoted action value, so the ~560k batch.progress lines skip json
    # entirely (2.1s -> ~50ms for action=video.extract). Only trivially safe
    # action names use it; anything exotic falls back to parsing every line.
    needle = '"%s"' % action if action and re.fullmatch(r"[A-Za-z0-9_.:\-]+", action) else None
    for line in _iter_lines_reverse(LOG_FILE):
        if needle and needle not in line:
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
        if limit > 0 and len(entries) >= limit:
            break
    entries.reverse()   # oldest first, matching the previous contract
    return entries


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

# 动作中文名：日志行左侧的来源徽标读这个，而不是把 raw action 直接甩给用户
# （video.download.end 这类键值对正文对人不友好，属「动作被藏起来」的一半）。
_ACTION_LABELS = {
    "video.resolve": "取直链",
    "video.download.start": "开始下载",
    "video.download.end": "下载完成",
    "audio.extract": "提取音频",
    "asr.request": "语音转写",
    "parse": "解析文案",
    "kb.write": "写入知识库",
    "translate": "翻译",
    "batch.start": "批量开始",
    "batch.progress": "批量进度",
    "batch.pause": "批量暂停",
    "batch.abort": "批量中止",
    "batch.done": "批量完成",
    "config.save": "保存配置",
    "authors.nickname": "解析昵称",
    "queue.add": "入队",
    "queue.runner": "队列调度",
    "queue.item": "队列执行",
    "queue.extract_now": "插队解析",
    "service.restart": "服务重启",
    "service.start": "服务启动",
    "service.stop": "服务退出",
    "scheduler.trigger": "定时触发",
    "scheduler.skip": "定时跳过",
    "scheduler.retry": "定时重试",
    "scheduler.step": "定时步骤",
    "scheduler.finish": "定时完成",
    "driver.start": "启动驱动",
    "driver.terminate": "停止驱动",
    "up.start": "UP 开始",
    "up.finish": "UP 完成",
    "task.persist": "保存续跑任务",
    "task.clear": "清除续跑任务",
    "stats.reset": "清空统计",
    "stats.seed": "恢复统计",
    "authors.open_dir": "打开目录",
    "video.download.proxy": "代理下载",
    "douyin.login": "抖音登录",
    "video.extract": "提取文案",
    "http.request": "HTTP 请求",
    "test.write": "写入测试",
}


def action_label(action: str) -> str:
    """Readable Chinese label for an action id (falls back to the raw id)."""
    return _ACTION_LABELS.get(action or "", action or "-")


# ---------------------------------------------------------------------------
# 来源/类型分类表（kinds）。新增一类只改这张表：解析侧用 action_kind() 归类，
# 查询侧按 RUNTIME_KINDS 生成过滤器，不必再改 read_runtime_logs 的分支。
# ---------------------------------------------------------------------------
RUNTIME_KIND_RULES = (
    ("scheduler", lambda a: a.startswith("scheduler.")),
    ("progress", lambda a: a == "batch.progress"),
    ("action", lambda a: True),          # 兜底：其余 Web/程序动作
)
WEB_KINDS = tuple(k for k, _ in RUNTIME_KIND_RULES)
RUNTIME_KINDS = ("video", "driver") + WEB_KINDS


def action_kind(action: str) -> str:
    """ClassName one Web/action record belongs to (see RUNTIME_KIND_RULES)."""
    a = action or ""
    for kind, rule in RUNTIME_KIND_RULES:
        if rule(a):
            return kind
    return "action"


def log_scheduler(task: str, event: str, status: str = "ok",
                  run_id: str = "", **fields) -> None:
    """One scheduler lifecycle record (trigger/skip/retry/step/finish).

    `run_id` correlates the trigger, every skip/retry and the finish line of
    ONE scheduled run - the scheduler equivalent of batch_id.
    """
    log_operation("scheduler." + event, status=status or "ok",
                  task=task, run_id=run_id or "", **fields)


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
        if k in ("ts", "action", "status", "batch_id", "worker_id"):
            # batch_id/worker_id 只用于文件内关联，展示位去掉（数据仍在行里）
            continue
        parts.append(str(v) if k in ("msg", "动作") else f"{k}={v}")
    body = " ".join(parts) or f"{action or '-'}｜{status or '-'}"
    return {"ts": str(rec.get("ts") or ""), "kind": action_kind(action),
            "action": action or "-", "label": action_label(action),
            "status": status,
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
                       need: int, status: str = "", keep=None) -> tuple:
    """Read one log file from its END and return (entries, exhausted).

    entries: parsed records older than `before` (ts anchor), newest first,
    capped at `need` (0 = unlimited). The scan starts with the last 512KB
    and widens x4 (up to 32MB) so「加载更多」reaches deep history cheaply.
    `status` filters entries INSIDE the scan so a requested status is
    collected from deep history even when recent pages are dominated by
    other statuses. exhausted=True means the beginning of the file has been
    reached (or the scan cap hit) - no older bytes remain.

    Each byte is parsed ONCE: widening a window only parses the newly
    exposed prefix (the previous implementation re-read and re-parsed the
    whole window on every widening, ~46MB of work to reach the 32MB cap).
    """
    try:
        if not path.exists():
            return [], True
        size = path.stat().st_size
        window = _RUNTIME_TAIL_BYTES
        entries = []      # accumulated matches, newest first
        covered = 0       # line-aligned bytes from EOF already parsed
        while True:
            # Append-only growth is fine (we read the captured `size` snapshot;
            # new bytes arrive on the next poll). A SHRINK means the file was
            # rewritten (video-log trim) and invalidates the byte bookkeeping:
            # restart once from the new EOF.
            now = path.stat().st_size
            if now < size:
                size, window, entries, covered = now, _RUNTIME_TAIL_BYTES, [], 0
                continue
            start = max(0, size - window)
            with open(path, "rb") as f:
                f.seek(start)
                blob = f.read(size - start)
            if start > 0:  # drop the (possibly partial) first line
                nl = blob.find(b"\n")
                blob = blob[nl + 1:] if nl != -1 else b""
            fresh = blob if not covered else blob[:max(0, len(blob) - covered)]
            covered = len(blob)
            if fresh:
                parsed = [e for line in fresh.decode("utf-8", errors="replace").splitlines()
                          if (e := parse_fn(line.strip()))]
                if status:
                    parsed = [e for e in parsed if e.get("status") == status]
                if keep:
                    parsed = [e for e in parsed if keep(e)]
                if before:
                    parsed = [e for e in parsed if e["ts"] < before]
                parsed.reverse()  # append order is chronological -> newest first
                # `parsed` is the newly exposed OLDER prefix: it goes after the
                # already-collected newer entries (newest-first order).
                entries = entries + parsed
            done = start <= 0 or window >= _RUNTIME_MAX_SCAN_BYTES
            if 0 < need <= len(entries) or done:
                return (entries[:need] if need > 0 else entries), done
            window *= 4
    except Exception:
        return [], True


def read_runtime_logs(limit: int = 200, before: str = "",
                      status: str = "", kinds: str = "") -> dict:
    """Unified runtime stream (driver live log + WebUI operation log +
    per-video results), newest first. `before` ("YYYY-MM-DD HH:MM:SS")
    pages older history: only entries strictly older than the anchor are
    returned.

    `status`（成功/跳过/失败/错误 分类）只作用于「任务/文案」的每视频执行
    结果：状态分类下不合并 driver 实时输出与 WebUI 动作日志——它们是程序
    运行日志，只在【全部】里看（status 为空时三源照常合并）。

    `kinds`（来源/类型分类，逗号分隔，新增可选参数）:
      video    - 每视频执行结果
      driver   - 驱动实时输出
      action   - Web/程序动作（含下载、转写、写库等逐动作行）
      progress - 批量进度（batch.progress）
      scheduler- 定时任务生命周期（触发/跳过/重试/完成/失败）
    省略＝不过滤（等价于旧行为）；只扫被选中的来源，切换筛选不读无关文件。
    来源清单见 RUNTIME_KINDS（配置表），加类不用改本函数。

    Returns {"logs": [...], "has_more": bool} - has_more tells the UI
    whether an even older「加载更多」page may exist.
    """
    wanted = {k.strip() for k in str(kinds or "").split(",") if k.strip()}
    want_video = not wanted or "video" in wanted
    want_driver = not wanted or "driver" in wanted
    web_kinds = {k for k in WEB_KINDS if not wanted or k in wanted}
    if status:
        # 状态分类＝任务/文案结果面：只扫视频结果文件（动作日志不参与分类）
        web, web_done = [], True
        drv, drv_done = [], True
        vid, vid_done = _scan_runtime_file(
            VIDEO_LOG_FILE, _parse_runtime_video_line, before, limit, status)
    else:
        if web_kinds:
            keep = None if len(web_kinds) == len(WEB_KINDS) else (
                lambda e: e.get("kind") in web_kinds)
            web, web_done = _scan_runtime_file(
                LOG_FILE, _parse_runtime_web_line, before, limit, "", keep)
        else:
            web, web_done = [], True
        drv, drv_done = (_scan_runtime_file(
            DRIVER_LOG_FILE, _parse_runtime_driver_line, before, limit) if want_driver
            else ([], True))
        vid, vid_done = (_scan_runtime_file(
            VIDEO_LOG_FILE, _parse_runtime_video_line, before, limit) if want_video
            else ([], True))
    merged = sorted(web + drv + vid, key=lambda e: e["ts"], reverse=True)
    page = merged[:limit] if limit > 0 else merged
    has_more = len(merged) > len(page) or not (web_done and drv_done
                                               and vid_done)
    return {"logs": page, "has_more": has_more}
