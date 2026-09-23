#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Douyin video transcript extractor WebUI

Usage:
    cd douyin-mcp-server
    export API_KEY="sk-xxx"
    python web/app.py
    # visit http://localhost:8080
"""

import os
import re
import sys
import json
import time
import queue
import asyncio
import subprocess
import threading
from pathlib import Path
from urllib.parse import quote

# Add project path
sys.path.insert(0, str(Path(__file__).parent.parent / "douyin-video" / "scripts"))

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, PlainTextResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import uvicorn
import requests

ROOT = Path(__file__).resolve().parent.parent


def _silence_child_consoles() -> None:
    r"""Keep the service fully silent: no child console app (ffmpeg, ffprobe,
    powershell...) may pop a console window while the server runs windowless
    (pythonw / tray).

    ffmpeg-python offers no way to pass creationflags, so the only hook that
    covers every child process is the Popen constructor itself. Console
    windows are only ever suppressed - GUI apps (e.g. explorer.exe) are
    unaffected.
    """
    global _NO_WINDOW_PATCHED
    if _NO_WINDOW_PATCHED or os.name != "nt":
        return
    _NO_WINDOW_PATCHED = True
    original_init = subprocess.Popen.__init__

    def init_without_window(self, *args, **kwargs):
        kwargs["creationflags"] = (
            kwargs.get("creationflags") or 0) | subprocess.CREATE_NO_WINDOW
        original_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = init_without_window


_NO_WINDOW_PATCHED = False
_silence_child_consoles()


# Import douyin processing module
from douyin_downloader import get_video_info, HEADERS
from asr_backends import (
    resolve_backend, transcribe_audio_file, PROVIDERS,
    load_config_file, save_config_file,
)
from operation_logger import log_operation, read_logs, log_file_path

HEADERS_UA = HEADERS['User-Agent']

app = FastAPI(title="Douyin Transcript Extractor", version="1.0.0")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@app.middleware("http")
async def log_http_requests(request: Request, call_next):
    """Persist every HTTP request (method, path, status, duration) to the
    runtime operation log."""
    start = time.time()
    try:
        response = await call_next(request)
        log_operation(
            "http.request",
            status="ok" if response.status_code < 400 else "error",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=int((time.time() - start) * 1000),
        )
        return response
    except Exception as e:
        log_operation(
            "http.request",
            status="error",
            method=request.method,
            path=request.url.path,
            error=str(e),
            duration_ms=int((time.time() - start) * 1000),
        )
        raise


class VideoRequest(BaseModel):
    """Video request model"""
    url: str
    api_key: str = ""  # optional, passed from frontend
    provider: str = ""  # optional: siliconflow (default) | dashscope | ark
    model: str = ""  # optional model name override


class VideoInfoResponse(BaseModel):
    """Video info response"""
    success: bool
    video_id: str = ""
    title: str = ""
    download_url: str = ""
    error: str = ""


class ExtractResponse(BaseModel):
    """Transcript extraction response"""
    success: bool
    video_id: str = ""
    title: str = ""
    text: str = ""
    download_url: str = ""
    error: str = ""


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Main page"""
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/health")
async def health_check():
    """Health check"""
    api_key = os.getenv("API_KEY", "")
    return {
        "status": "ok",
        "api_key_configured": bool(api_key)
    }


class ProviderSettings(BaseModel):
    """Per-provider settings"""
    api_key: str = ""
    model: str = ""  # empty = provider default
    api_base_url: str = ""  # empty = provider default


class PolishSettings(BaseModel):
    """LLM transcript polish settings"""
    enabled: bool = False
    api_key: str = ""  # "-" clears
    api_base_url: str = ""
    model: str = ""


class ConfigPayload(BaseModel):
    """WebUI config payload: active provider + per-provider settings"""
    active_provider: str = "siliconflow"
    providers: dict = {}  # {provider: {api_key, model, api_base_url}}
    polish: dict = {}  # optional {enabled, api_key, api_base_url, model}


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


@app.get("/api/config")
async def get_config():
    """Get current ASR provider config (keys masked)"""
    file_config = load_config_file()
    providers_out = {}
    for pid, meta in PROVIDERS.items():
        entry = (file_config.get("providers") or {}).get(pid) or {}
        env_key = os.getenv(meta["key_env"], "") or os.getenv("API_KEY", "")
        stored_key = entry.get("api_key", "") or env_key
        providers_out[pid] = {
            "label": meta["label"],
            "default_model": meta["default_model"],
            "model": entry.get("model", "") or meta["default_model"],
            "api_base_url": entry.get("api_base_url", "") or meta["default_base_url"],
            "has_key": bool(stored_key),
            "api_key_masked": _mask_key(stored_key),
        }
    return {
        "active_provider": file_config.get("active_provider", "siliconflow"),
        "providers": providers_out,
        "polish": file_config.get("polish", {}),
    }


@app.post("/api/config")
async def set_config(payload: ConfigPayload):
    """Save ASR provider config to web_ui_config.json (server-side persistence).

    Empty api_key/model/api_base_url fields keep existing stored values.
    Set api_key to "-" to clear a stored key.
    """
    if payload.active_provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {payload.active_provider}")

    file_config = load_config_file()
    existing = file_config.get("providers") or {}
    merged = {}
    for pid in PROVIDERS:
        incoming = (payload.providers or {}).get(pid) or {}
        prev = existing.get(pid) or {}
        new_key = incoming.get("api_key", "").strip()
        if new_key == "-":
            stored_key = ""
        elif new_key:
            stored_key = new_key
        else:
            stored_key = prev.get("api_key", "")
        merged[pid] = {
            "api_key": stored_key,
            "model": incoming.get("model", "").strip() or PROVIDERS[pid]["default_model"],
            "api_base_url": incoming.get("api_base_url", "").strip() or PROVIDERS[pid]["default_base_url"],
        }
    file_config["active_provider"] = payload.active_provider
    file_config["providers"] = merged

    # Optional polish settings merge
    if payload.polish:
        prev_polish = file_config.get("polish") or {}
        new_pkey = (payload.polish.get("api_key") or "").strip()
        if new_pkey == "-":
            stored_pkey = ""
        elif new_pkey:
            stored_pkey = new_pkey
        else:
            stored_pkey = prev_polish.get("api_key", "")
        file_config["polish"] = {
            "enabled": bool(payload.polish.get("enabled", False)),
            "api_key": stored_pkey,
            "api_base_url": (payload.polish.get("api_base_url") or "").strip() or prev_polish.get("api_base_url", ""),
            "model": (payload.polish.get("model") or "").strip() or prev_polish.get("model", ""),
        }

    save_config_file(file_config)
    log_operation("config.save", active_provider=payload.active_provider,
                  providers=list(PROVIDERS.keys()),
                  polish_enabled=bool((file_config.get("polish") or {}).get("enabled")))
    return {"status": "ok", "active_provider": payload.active_provider}


@app.post("/api/video/info", response_model=VideoInfoResponse)
async def get_info(req: VideoRequest):
    """Get video info (no API_KEY required)"""
    try:
        # Profile links are handled by the batch flow instead
        sys.path.insert(0, str(Path(__file__).parent.parent / "douyin-video" / "scripts"))
        from profile_fetcher import is_profile_url
        if is_profile_url(req.url) and "modal_id=" not in req.url:
            return VideoInfoResponse(success=False,
                                     error="This is an author profile link. Use the Batch Extract button.")
        if _driver_info():
            raise HTTPException(status_code=409, detail=_driver_busy_detail())
        if not _claim_extract_slot():
            raise HTTPException(
                status_code=409,
                detail="已有解析任务在运行；请等它结束后再试，或使用「排队解析」")
        try:
            info = await asyncio.to_thread(get_video_info, req.url)
        finally:
            _release_extract_slot()
        log_operation("video.info", url=req.url, video_id=info["video_id"],
                      title=info["title"], download_url=info["url"])
        return VideoInfoResponse(
            success=True,
            video_id=info["video_id"],
            title=info["title"],
            download_url=info["url"]
        )
    except HTTPException:
        raise
    except Exception as e:
        log_operation("video.info", status="error", url=req.url, error=str(e))
        return VideoInfoResponse(success=False, error=str(e))


class BatchRequest(BaseModel):
    """Author profile batch extraction request"""
    url: str
    provider: str = ""
    author: str = ""  # display name of the UP, kept so a reloaded page can
                      # tell which UP the background job belongs to
    max_videos: int = 0  # 0 = all
    force: bool = False
    save_video: bool = False
    use_cache: bool = False  # reuse cached video list (retry failed only)
    workers: int = 3  # concurrent videos


# Global batch job state (one job at a time keeps things simple)
_batch_state = {"running": False}

# Live view of the running batch job. A page reload (or a closed and reopened
# tab) drops the SSE stream while the worker thread keeps going, so without
# this the UI has no way back into a job it started:
#   label   - which UP the job belongs to ("" for a one-off manual batch)
#   history - every event published so far, replayed to a late subscriber
#   subs    - queues of the progress streams currently attached
#   seq     - increasing event id, used to skip replayed duplicates
_batch_job = {"label": "", "history": [], "subs": [], "seq": 0}
_BATCH_HISTORY_MAX = 2000

# Increments on every new job (manual batch or next queue item). The seq
# counter restarts from 0 per job, so clients need the epoch to tell
# "replayed event of the same job" apart from "event of a NEW job whose
# seq started over" - without it a reattached page filters away every
# event of the follow-up job and freezes on the previous job's state.
# Seeded from the clock so it stays monotonic across service restarts
# (a page holding a pre-restart epoch must never see it reused).
_batch_epoch = int(time.time())


def _batch_new_job(label: str, run_ctx: dict = None) -> None:
    """Start a new progress job on the shared SSE channel."""
    global _batch_epoch, _run_ctx
    _batch_epoch += 1
    _batch_job["label"] = label
    _batch_job["seq"] = 0
    del _batch_job["history"][:]
    # Per-run refresh-log context (written to output/刷新日志/ on finish)
    _run_ctx = run_ctx if run_ctx is not None else {
        "trigger": "", "url": "", "params": "", "note": "",
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "t0": time.time(), "logged": False,
    }


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _batch_publish(event: dict) -> None:
    """Record a batch event and fan it out to every attached progress stream."""
    _batch_job["seq"] += 1
    event["seq"] = _batch_job["seq"]
    event["epoch"] = _batch_epoch
    history = _batch_job["history"]
    history.append(event)
    if len(history) > _BATCH_HISTORY_MAX:
        del history[: len(history) - _BATCH_HISTORY_MAX]
    for sub in list(_batch_job["subs"]):
        sub.put(event)


def _batch_finish() -> None:
    """Tells every attached progress stream that the job is over."""
    for sub in list(_batch_job["subs"]):
        sub.put(None)


# ---------------------------------------------------------------------------
# 刷新日志: every finished run (manual batch, queued UP refresh, queued single
# video) writes its OWN standalone log file under output/刷新日志/, so each
# incremental refresh leaves a permanent self-contained record: trigger,
# timing, stats and a per-video detail table. output/ is git-ignored.
# ---------------------------------------------------------------------------
REFRESH_LOG_DIR = ROOT / "output" / "刷新日志"
_run_ctx: dict = {}

# ---------------------------------------------------------------------------
# 后端脚本任务（driver：scripts/refresh_all.py 全 UP 刷新）检测。
# driver 进程会写心跳文件 driver_state.json（pid + 进度），WebUI 由此：
#   - 在前端显示"后端任务运行中"横幅和进度（页面刷新也不丢）
#   - 拦截所有会占用 Chrome profile 的新任务（409 互斥）
#   - 提供终止按钮（taskkill 进程树，连带 Chrome 子进程）
# ---------------------------------------------------------------------------
DRIVER_STATE_FILE = REFRESH_LOG_DIR / "driver_state.json"


def _pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return exit_code.value == STILL_ACTIVE
        return False
    finally:
        kernel32.CloseHandle(handle)


def _driver_info() -> dict:
    """Running driver task info, or {} when no backend script task is alive.

    pid-alive is the authoritative check; the heartbeat timestamp is a
    secondary guard against a recycled pid showing a long-dead run.
    """
    try:
        if not DRIVER_STATE_FILE.exists():
            return {}
        data = json.loads(DRIVER_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not _pid_alive(int(data.get("pid") or 0)):
            return {}
        try:
            age = time.time() - DRIVER_STATE_FILE.stat().st_mtime
            if age > 3600:  # stale heartbeat from a recycled pid
                return {}
        except OSError:
            return {}
        return data
    except Exception:
        return {}


def _terminate_driver() -> bool:
    """Kill the driver process tree (pythonw + its Chrome children)."""
    info = _driver_info()
    pid = int(info.get("pid") or 0)
    if not pid:
        return False
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, text=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        log_operation("driver.terminate", pid=pid,
                      author=info.get("current_author", ""))
        return True
    except Exception as e:
        log_operation("driver.terminate", status="error", pid=pid, error=str(e))
        return False


def _driver_busy_detail() -> str:
    info = _driver_info()
    who = info.get("current_author") or ""
    idx, tot = info.get("author_index"), info.get("author_total")
    pos = f"（第 {idx}/{tot} 位：{who}）" if idx and tot else (f"（{who}）" if who else "")
    return f"后端全 UP 刷新脚本任务正在运行{pos}：不能开始新任务。可在前端进度区终止它，或等它自动完成。"


def _refresh_log_ctx(trigger: str, url: str = "", params: str = "",
                     note: str = "") -> dict:
    return {"trigger": trigger, "url": url, "params": params, "note": note,
            "started": time.strftime("%Y-%m-%d %H:%M:%S"),
            "t0": time.time(), "logged": False}


def _sanitize_log_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", (name or "").strip())
    return name[:60] or "未命名"


def _write_refresh_log() -> None:
    """Dump the current job's event history into its own log file.

    Runs in the worker thread right before the job is declared over, while
    _batch_job["history"] still holds this run's events (the next
    _batch_new_job clears them). Never raises: logging must not break the
    batch pipeline.
    """
    ctx = _run_ctx
    if not ctx or ctx.get("logged"):
        return
    ctx["logged"] = True
    try:
        events = [e for e in _batch_job["history"] if isinstance(e, dict)]
        if not events:
            return
        done = next((e for e in reversed(events) if e.get("stage") == "done"), {})
        extracts = [e for e in events if e.get("stage") == "extract"]
        errors = [e for e in events if e.get("stage") == "error"]

        secs = max(0, int(time.time() - float(ctx.get("t0") or 0)))
        dur = f"{secs // 60}分{secs % 60:02d}秒" if secs >= 60 else f"{secs}秒"
        author = (done.get("author") or _batch_job["label"] or "").strip()

        lines = [
            f"# 刷新日志：{_sanitize_log_name(author)}",
            "",
            f"- 开始时间：{ctx.get('started', '')}",
            f"- 结束时间：{time.strftime('%Y-%m-%d %H:%M:%S')}（耗时 {dur}）",
            f"- 触发方式：{ctx.get('trigger') or '手动批量'}",
        ]
        if ctx.get("url"):
            lines.append(f"- 链接：{ctx['url']}")
        if ctx.get("params"):
            lines.append(f"- 参数：{ctx['params']}")
        if ctx.get("note"):
            lines.append(f"- 备注：{ctx['note']}")
        lines += [
            "",
            "## 结果统计",
            "",
            f"- 新增：{done.get('ok', sum(1 for e in extracts if e.get('status') == 'ok'))}"
            f"（其中断点续传 {done.get('resumed', 0)}）",
            f"- 跳过（已有文案）：{done.get('skip', sum(1 for e in extracts if e.get('status') == 'skip'))}",
            f"- 失败：{done.get('fail', sum(1 for e in extracts if e.get('status') == 'fail'))}",
        ]
        if done.get("output_dir"):
            lines.append(f"- 输出目录：{done['output_dir']}")
        if errors:
            lines += ["", "## 运行错误", ""]
            for e in errors:
                lines.append(f"- {str(e.get('error', '')).replace('|', chr(92) + '|')}")
        if extracts:
            lines += [
                "",
                "## 视频明细",
                "",
                "| # | 标题 | 视频ID | 状态 | 备注 |",
                "|---|------|--------|------|------|",
            ]
            for e in extracts:
                status = {"ok": "新增", "skip": "跳过",
                          "fail": "失败"}.get(e.get("status"), str(e.get("status", "")))
                if e.get("resumed"):
                    status += "（续传）"
                remark = e.get("error") or e.get("output") or ""
                title = str(e.get("title", "")).replace("|", "\\|").replace("\n", " ")
                lines.append(
                    f"| {e.get('index', '')} | {title} | {e.get('aweme_id', '')} "
                    f"| {status} | {str(remark).replace('|', chr(92) + '|')} |")

        REFRESH_LOG_DIR.mkdir(parents=True, exist_ok=True)
        base = f"{time.strftime('%Y%m%d_%H%M%S')}_{_sanitize_log_name(author)}"
        path = REFRESH_LOG_DIR / f"{base}.md"
        n = 2
        while path.exists():
            path = REFRESH_LOG_DIR / f"{base}-{n}.md"
            n += 1
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


async def _batch_stream():
    """Replay the running job's events, then follow it live until it ends.

    The replay is what lets a reloaded page restore the progress it lost; the
    live tail then keeps it in sync. Events are deduped by seq, so an event
    published between the replay snapshot and the subscription is not doubled.
    """
    sub: "queue.Queue" = queue.Queue()
    _batch_job["subs"].append(sub)
    loop = asyncio.get_event_loop()
    last = 0
    try:
        for evt in list(_batch_job["history"]):
            if evt.get("seq", 0) > last:
                last = evt["seq"]
                yield _sse(evt)
        if not _batch_state["running"]:
            yield _sse({"stage": "closed"})
            return
        while True:
            item = await loop.run_in_executor(None, sub.get)
            if item is None:
                break
            if item.get("seq", 0) <= last:
                continue
            last = item["seq"]
            yield _sse(item)
    finally:
        if sub in _batch_job["subs"]:
            _batch_job["subs"].remove(sub)

# A restart requested while a batch job runs is deferred until that job
# finishes, so an in-flight task is never interrupted. "restarting" latches
# once the deferred restart is actually on its way, so the pending-links
# queue runner knows not to start new work in those final seconds.
_restart_state = {"pending": False, "restarting": False}


def _restart_now(delay: float = 1.0) -> None:
    """Reload this service with the latest code, silently and in place.

    os.execv replaces the process image (same PID, same window mode), so no
    console window is spawned and no launcher script is needed. The current
    listening socket is released as the old image goes away, which is why
    tray_server waits for the port when DOUYIN_RESTART is set.
    """
    def _run():
        time.sleep(delay)
        # Prefer the windowless interpreter: execv keeps the current session,
        # so re-imaging with python.exe would attach a console (or fail when
        # launched from an already windowless tray process).
        pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
        exe = str(pythonw) if pythonw.exists() else sys.executable
        os.environ["DOUYIN_RESTART"] = "1"
        try:
            os.execv(exe, [exe, str(ROOT / "tray_server.py")])
        except Exception as e:
            log_operation("service.restart", status="error", error=str(e))
            os._exit(1)

    threading.Thread(target=_run, daemon=True).start()


def _restart_after_batch():
    """Run a restart that was deferred because a batch job was running."""
    if _restart_state["pending"]:
        _restart_state["pending"] = False
        _restart_state["restarting"] = True
        log_operation("service.restart", status="run_deferred")
        _restart_now(delay=3.0)


# ------------------------------------------------------------------
# 待解析链接队列 (pending links): users can queue single-video and UP-profile
# links while a batch job is running; each queued link runs automatically,
# one at a time, as soon as the global batch lock frees up. The queue is
# persisted to pending_links.json so it survives page reloads and service
# restarts (a startup hook re-arms the runner when items are waiting).
# ------------------------------------------------------------------
PENDING_LINKS_FILE = ROOT / "pending_links.json"
_pending_lock = threading.Lock()      # guards the JSON file + worker/timer state
_pending_worker_alive = False
_pending_timer = None

# The original check-then-set of _batch_state["running"] inside the async
# endpoint was race-free only because no await sat between them. The queue
# runner claims the lock from a plain thread, so claiming is now atomic.
_batch_claim_mutex = threading.Lock()

# Single-video extractions (info/extract endpoints) can fall back to the
# Playwright browser when the share page is risk-controlled, so they must be
# serialized against batch jobs and the pending queue just like a batch.
_extract_busy = False

# Set by /api/profile/batch/abort: the queue runner stops after the aborted
# item instead of auto-starting the next one (items stay queued on disk).
_queue_halt_requested = False


def _claim_batch_lock() -> bool:
    """Atomically claim the global single-job lock."""
    global _extract_busy
    with _batch_claim_mutex:
        if _batch_state["running"] or _extract_busy:
            return False
        _batch_state["running"] = True
        return True


def _claim_extract_slot() -> bool:
    """Claim the single-extraction slot (mutually exclusive with batch jobs)."""
    global _extract_busy
    with _batch_claim_mutex:
        if _batch_state["running"] or _extract_busy:
            return False
        _extract_busy = True
        return True


def _release_extract_slot() -> None:
    global _extract_busy
    with _batch_claim_mutex:
        _extract_busy = False


def classify_link(url: str) -> str:
    """'up' for author profile links, 'video' for everything else.

    /user/ links carrying modal_id are video-permalink pages (e.g. a video
    opened in a modal from one's own profile / favorites), not author
    profiles — parse_share_url already extracts the modal_id video."""
    u = (url or "").strip().lower()
    if "/user/" in u and "modal_id=" not in u:
        return "up"
    return "video"


def _load_pending() -> dict:
    try:
        if PENDING_LINKS_FILE.exists():
            data = json.loads(PENDING_LINKS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                data.setdefault("next_id", 1)
                return data
    except Exception:
        pass
    return {"next_id": 1, "items": []}


def _save_pending(data: dict) -> None:
    PENDING_LINKS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _batch_pause_flag() -> bool:
    with _pending_lock:
        return bool(_load_pending().get("batch_paused"))


def _set_batch_pause_flag(value: bool) -> None:
    """Persist the user's pause intent so a service restart (which rebuilds
    the batch from the queue) can restore it instead of silently resuming."""
    with _pending_lock:
        data = _load_pending()
        if bool(data.get("batch_paused")) != value:
            data["batch_paused"] = value
            _save_pending(data)


def _pending_items() -> list:
    with _pending_lock:
        return _load_pending()["items"]


def _pending_add(urls: list, titles: list = None) -> tuple:
    """Append links, deduped against still-queued ones. Returns (added, duplicates).
    Optional parallel `titles` list labels items (e.g. a known UP's name)."""
    from datetime import datetime
    added = dup = 0
    titles = titles or []
    with _pending_lock:
        data = _load_pending()
        queued = {it["url"] for it in data["items"] if it["status"] in ("pending", "running")}
        for idx, raw in enumerate(urls):
            url = (raw or "").strip()
            if not url or not re.match(r"^https?://", url, re.I):
                continue
            if url in queued:
                dup += 1
                continue
            data["items"].append({
                "id": data["next_id"], "url": url,
                "kind": classify_link(url), "status": "pending",
                "title": (titles[idx].strip() if idx < len(titles) and titles[idx] else ""),
                "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "started_at": "", "finished_at": "", "result": "",
            })
            data["next_id"] += 1
            queued.add(url)
            added += 1
        if added:
            _save_pending(data)
    return added, dup


def _pending_remove(item_id: int) -> bool:
    with _pending_lock:
        data = _load_pending()
        before = len(data["items"])
        data["items"] = [it for it in data["items"]
                         if not (it["id"] == item_id and it["status"] != "running")]
        if len(data["items"]) != before:
            _save_pending(data)
            return True
        return False


def _pending_clear_finished() -> int:
    with _pending_lock:
        data = _load_pending()
        before = len(data["items"])
        data["items"] = [it for it in data["items"] if it["status"] not in ("done", "fail")]
        removed = before - len(data["items"])
        if removed:
            _save_pending(data)
        return removed


def _pending_update(item_id: int, url: str) -> bool:
    """Edit a queued link's URL (pending items only; kind is re-detected)."""
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        return False
    with _pending_lock:
        data = _load_pending()
        for it in data["items"]:
            if it["id"] == item_id and it["status"] == "pending":
                it["url"] = url
                it["kind"] = classify_link(url)
                it["title"] = ""  # old label may no longer match the new URL
                _save_pending(data)
                return True
        return False


def _pending_reorder(ids: list) -> bool:
    """Apply a full item order (the id sequence the UI shows). id order only
    decides execution priority among pending items; done/fail items just keep
    their displayed position."""
    with _pending_lock:
        data = _load_pending()
        if sorted(ids) != sorted(it["id"] for it in data["items"]):
            return False  # must be a permutation of current ids
        by_id = {it["id"]: it for it in data["items"]}
        data["items"] = [by_id[i] for i in ids]
        _save_pending(data)
        return True


def _pending_mark(item_id: int, status: str, result: str = "") -> None:
    from datetime import datetime
    with _pending_lock:
        data = _load_pending()
        for it in data["items"]:
            if it["id"] == item_id:
                it["status"] = status
                it["result"] = (result or "")[:300]
                if status in ("done", "fail"):
                    it["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                break
        _save_pending(data)


def _pending_pop_next() -> dict:
    """Mark and return the next pending item (caller must hold the batch lock)."""
    from datetime import datetime
    with _pending_lock:
        data = _load_pending()
        for it in data["items"]:
            if it["status"] == "pending":
                it["status"] = "running"
                it["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                _save_pending(data)
                return it
    return None


def _maybe_start_pending(delay: float = 2.0) -> None:
    """Arm the delayed queue runner unless one is already alive or armed."""
    global _pending_timer
    with _pending_lock:
        if _pending_worker_alive or _pending_timer is not None:
            return
        if not any(it["status"] == "pending" for it in _load_pending()["items"]):
            return
        _pending_timer = threading.Timer(delay, _pending_worker_loop)
        _pending_timer.daemon = True
        _pending_timer.start()


def _pending_worker_loop() -> None:
    """Run queued links serially. Every item executes under the global batch
    lock, so a queue item and a manual/refresh-all batch can never overlap on
    the browser profile. A manual batch always gets the lock first: this loop
    simply retries until the lock stays free."""
    global _pending_timer, _pending_worker_alive, _queue_halt_requested
    _pending_timer = None
    _pending_worker_alive = True
    log_operation("queue.runner", status="start")
    try:
        while not (_restart_state["pending"] or _restart_state["restarting"]):
            if _queue_halt_requested:
                _queue_halt_requested = False
                log_operation("queue.runner", status="halted_by_abort")
                break
            if not any(it["status"] == "pending" for it in _pending_items()):
                break
            if _driver_info():
                # 后端脚本任务独占 Chrome profile：排队项等它结束再跑
                time.sleep(15)
                continue
            if not _claim_batch_lock():
                time.sleep(5)  # another job holds the lock; wait for it
                continue
            item = _pending_pop_next()
            if item is None:
                _batch_state["running"] = False  # claim released, nothing to do
                break
            try:
                _pending_run_item(item)
            except Exception as e:
                log_operation("queue.item", status="error",
                              url=item.get("url", ""), error=str(e)[:300])
            finally:
                _write_refresh_log()  # one standalone log per queued run
                _batch_state["running"] = False
                _set_batch_pause_flag(False)  # item ended; don't pause the next one
                _batch_finish()
                _restart_after_batch()
    finally:
        with _pending_lock:
            _pending_worker_alive = False
            _pending_timer = None
    log_operation("queue.runner", status="stop")


def _pending_api_key() -> str:
    from asr_backends import load_config_file
    file_cfg = load_config_file()
    provider_id = (file_cfg.get("active_provider") or "siliconflow").lower()
    entry = (file_cfg.get("providers") or {}).get(provider_id) or {}
    return entry.get("api_key") or os.getenv("API_KEY", "") \
        or os.getenv("DASHSCOPE_API_KEY", "") or os.getenv("ARK_API_KEY", "")


def _pending_run_item(item: dict) -> None:
    """Run one queued link (batch lock already held). Publishes progress on
    the same SSE channel as manual batches, so the WebUI shows it the same way."""
    _batch_new_job("待解析队列", _refresh_log_ctx(
        trigger="待解析队列", url=item.get("url", ""),
        note="类型：UP 主页" if item["kind"] == "up" else "类型：单个视频"))
    _batch_publish({"stage": "queue", "kind": item["kind"], "url": item["url"]})
    try:
        if item["kind"] == "up":
            _pending_run_up(item)
        else:
            _pending_run_video(item)
    except Exception as e:
        _pending_mark(item["id"], "fail", result=str(e))
        _batch_publish({"stage": "error", "error": str(e)[:400]})


def _pending_run_up(item: dict) -> None:
    api_key = _pending_api_key()
    if not api_key:
        raise RuntimeError("未配置 API Key")
    from batch_extractor import batch_extract, run_gate
    # Restore a pause that was active when the service stopped (e.g. the
    # deferred hot-reload restart): start the batch paused instead of
    # silently resuming. batch_extract re-sets run_gate on entry, so the
    # clear is applied shortly after it starts (workers check the gate
    # before every video, well past this point).
    if _batch_pause_flag():
        def _apply_restored_pause():
            try:
                if _batch_state["running"] and _batch_pause_flag():
                    run_gate.clear()
            except Exception:
                pass
        threading.Timer(2.0, _apply_restored_pause).start()
    from batch_extractor import batch_extract, BatchCancelled
    try:
        summary = batch_extract(
            item["url"],
            output_dir=str(Path(__file__).parent.parent / "output"),
            api_key=api_key,
            provider=None,
            max_videos=0,
            force=False,
            headless=True,
            save_video=False,
            use_cache=False,
            workers=3,
            on_progress=_batch_publish,
        )
        if summary.get("aborted"):
            _batch_publish({"stage": "aborted",
                            "ok": summary.get("ok", 0),
                            "skip": summary.get("skip", 0),
                            "fail": summary.get("fail", 0)})
            _pending_mark(item["id"], "pending",
                          result="批量任务被终止，重新排队")
            return
    except BatchCancelled:
        _batch_publish({"stage": "aborted"})
        _pending_mark(item["id"], "pending", result="批量任务被终止，重新排队")
        return
    _pending_mark(item["id"], "done",
                  result=f"新增 {summary.get('ok', 0)}，跳过 {summary.get('skip', 0)}，失败 {summary.get('fail', 0)}")


def _pending_run_video(item: dict) -> None:
    api_key = _pending_api_key()
    if not api_key:
        raise RuntimeError("未配置 API Key")
    video_info, _text = _extract_single_video(item["url"], api_key)
    _batch_publish({"stage": "extract", "index": 1, "total": 1,
                    "aweme_id": video_info["video_id"], "title": video_info["title"],
                    "status": "ok"})
    _pending_mark(item["id"], "done", result=video_info.get("title", ""))
    _batch_publish({"stage": "done", "ok": 1, "skip": 0, "fail": 0,
                    "resumed": 0, "output_dir": ""})


class PendingLinksRequest(BaseModel):
    urls: list = []
    titles: list = []  # optional display labels, parallel to urls


@app.get("/api/pending-links")
async def pending_links_list():
    """The 待解析 queue: links waiting to run after the current job."""
    return {"items": _pending_items()}


@app.post("/api/pending-links")
async def pending_links_add(req: PendingLinksRequest):
    """Queue links (single video / UP profile, auto-detected). Kicks the
    runner if nothing is running; otherwise it starts when the lock frees."""
    if _driver_info():
        raise HTTPException(status_code=409, detail=_driver_busy_detail())
    added, dup = _pending_add(req.urls, req.titles)
    if added:
        log_operation("queue.add", count=added, duplicates=dup)
        _maybe_start_pending(delay=2.0)
    return {"added": added, "duplicates": dup, "items": _pending_items()}


@app.post("/api/pending-links/clear-finished")
async def pending_links_clear_finished():
    removed = _pending_clear_finished()
    return {"removed": removed, "items": _pending_items()}


@app.post("/api/pending-links/{item_id}/extract-now", response_model=ExtractResponse)
async def pending_links_extract_now(item_id: int):
    """Manually run a queued single-video link ahead of the queue (提前解析).

    Allowed while idle OR while a batch is PAUSED (workers idle, browser
    untouched); refused while a batch is actively running. The item is marked
    running so the queue runner skips it, then done/fail with the outcome.
    """
    from datetime import datetime
    from batch_extractor import run_gate

    with _pending_lock:
        data = _load_pending()
        item = next((it for it in data["items"] if it["id"] == item_id), None)
        if item is None:
            raise HTTPException(status_code=404, detail="任务列表中不存在该条目")
        if item["status"] != "pending":
            raise HTTPException(status_code=409, detail="该条目已在处理中或已完成")
        if item["kind"] != "video":
            raise HTTPException(status_code=409, detail="UP 主页链接请在队列中等待自动批量")

    if _driver_info():
        raise HTTPException(status_code=409, detail=_driver_busy_detail())

    if _batch_state["running"]:
        try:
            paused = not run_gate.is_set()
        except Exception:
            paused = False
        if not paused:
            raise HTTPException(
                status_code=409,
                detail="批量任务正在运行：请先暂停批量，或等待其完成（该链接已在队列中排队）")

    global _extract_busy
    with _batch_claim_mutex:
        if _extract_busy:
            raise HTTPException(status_code=409, detail="已有解析任务在运行")
        _extract_busy = True

    try:
        with _pending_lock:
            data = _load_pending()
            for it in data["items"]:
                if it["id"] == item_id and it["status"] == "pending":
                    it["status"] = "running"
                    it["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    _save_pending(data)
                    break

        api_key = _pending_api_key()
        if not api_key:
            _pending_mark(item_id, "fail", result="未配置 API Key")
            return ExtractResponse(success=False, error="请先配置 API Key")

        try:
            video_info, text = await asyncio.to_thread(
                _extract_single_video, item["url"], api_key)
            _pending_mark(item_id, "done", result=video_info.get("title", ""))
            log_operation("queue.extract_now", video_id=video_info["video_id"],
                          title=video_info["title"], ok=True)
            return ExtractResponse(
                success=True,
                video_id=video_info["video_id"],
                title=video_info["title"],
                text=text,
                download_url=video_info["url"]
            )
        except Exception as e:
            _pending_mark(item_id, "fail", result=str(e))
            log_operation("queue.extract_now", url=item["url"], ok=False, error=str(e))
            return ExtractResponse(success=False, error=str(e))
    finally:
        with _batch_claim_mutex:
            _extract_busy = False


class PendingLinkUpdateRequest(BaseModel):
    url: str = ""


class PendingReorderRequest(BaseModel):
    ids: list = []


@app.put("/api/pending-links/{item_id}")
async def pending_links_update(item_id: int, req: PendingLinkUpdateRequest):
    """Edit a pending link's URL (re-detects UP-profile vs single video)."""
    if not _pending_update(item_id, req.url):
        raise HTTPException(
            status_code=409,
            detail="条目不存在、已在处理中，或链接格式无效")
    return {"ok": True, "items": _pending_items()}


@app.post("/api/pending-links/reorder")
async def pending_links_reorder(req: PendingReorderRequest):
    """Apply the UI's item order (drag & drop)."""
    if not _pending_reorder(req.ids):
        raise HTTPException(status_code=409, detail="排序数据与当前列表不一致")
    return {"ok": True, "items": _pending_items()}


@app.delete("/api/pending-links/{item_id}")
async def pending_links_remove(item_id: int):
    if not _pending_remove(item_id):
        raise HTTPException(status_code=409, detail="条目不存在或正在运行")
    return {"ok": True, "items": _pending_items()}


@app.on_event("startup")
async def _resume_pending_queue():
    """After a crash/restart, requeue an item that died mid-run and resume
    the queue - links the user queued must not be lost to a service restart."""
    with _pending_lock:
        data = _load_pending()
        changed = False
        for it in data["items"]:
            if it["status"] == "running":
                it["status"] = "pending"
                changed = True
        if changed:
            _save_pending(data)
    _maybe_start_pending(delay=8.0)


@app.post("/api/service/restart")
async def service_restart():
    """Reload the service with the latest code.

    Returns 200 when the restart starts immediately, or 202 when a batch job
    is running and the restart has been queued for when it finishes.
    """
    if _batch_state["running"]:
        _restart_state["pending"] = True
        log_operation("service.restart", status="deferred")
        return JSONResponse(
            {"status": "deferred", "restart_pending": True},
            status_code=202,
        )
    log_operation("service.restart", status="now")
    _restart_now()
    return {"status": "restarting"}


@app.get("/api/service/status")
async def service_status():
    """Whether a batch job is running and whether a restart is queued.

    A page that was reloaded mid-job asks this to learn that a job is still
    running in the background (and which UP it belongs to), which is what
    makes the "continue" button appear.
    """
    running = bool(_batch_state["running"])
    paused = False
    if running:
        try:
            from batch_extractor import run_gate
            paused = not run_gate.is_set()
        except Exception:
            paused = False
    driver = _driver_info()
    return {
        "batch_running": running,
        "batch_paused": paused,
        "batch_label": _batch_job["label"] if running else "",
        "restart_pending": bool(_restart_state["pending"]),
        "driver_running": bool(driver),
        "driver": driver,
    }


@app.post("/api/profile/batch/abort")
async def profile_batch_abort():
    """Terminate the current task.

    - WebUI batch job: cancel_flag stops new videos at the next boundary
      (in-flight videos finish, transcripts kept); a paused job is woken so
      the cancel is always observed; the pending queue runner halts instead
      of starting the next item.
    - Backend script task (driver 全 UP 刷新): kill the pythonw process
      tree (its Chrome children die with it); the guardian notices and
      restarts the tray.
    """
    global _queue_halt_requested
    if _driver_info():
        if _terminate_driver():
            # force-kill means the driver cannot clean up its own heartbeat
            try:
                DRIVER_STATE_FILE.unlink(missing_ok=True)
            except OSError:
                pass
            return {"status": "terminating", "target": "driver",
                    "notice": "已发送终止指令：后端刷新任务即将退出"}
        raise HTTPException(status_code=500, detail="终止后端任务失败（进程已退出？）")
    if _batch_state["running"]:
        from batch_extractor import cancel_flag, run_gate
        _queue_halt_requested = True
        _set_batch_pause_flag(False)
        run_gate.set()      # wake paused workers so they see the cancel
        cancel_flag.set()
        log_operation("batch.abort", status="requested")
        return {"status": "terminating", "target": "batch",
                "notice": "已请求终止：当前视频完成后停止（已完成的文案会保留）"}
    raise HTTPException(status_code=409, detail="当前没有正在运行的任务")


class RefreshAllRequest(BaseModel):
    workers: int = 3
    force: bool = False


@app.post("/api/refresh-all/start")
async def refresh_all_start(req: RefreshAllRequest):
    """Launch the backend full-UP refresh script as a detached background
    task (hidden window, survives page reloads and browser restarts).

    The driver runs refresh_guarded.ps1 (schtasks-guarded), writes a
    heartbeat state file the WebUI polls, and refuses to start while a
    WebUI batch holds the Chrome profile.
    """
    detail = _driver_busy_detail() if _driver_info() else ""
    if detail:
        raise HTTPException(status_code=409, detail=detail)
    if _batch_state["running"] or _extract_busy:
        raise HTTPException(
            status_code=409,
            detail="WebUI 批量/解析任务正在运行（Chrome profile 独占），请等它结束后再启动全量刷新")
    ps1 = ROOT / "scripts" / "refresh_guarded.ps1"
    if not ps1.exists():
        raise HTTPException(status_code=500, detail="找不到 scripts/refresh_guarded.ps1")
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-WindowStyle", "Hidden", "-File", str(ps1),
            "-Workers", str(max(1, min(req.workers, 8)))]
    if req.force:
        args.append("-Force")
    try:
        subprocess.Popen(
            args, cwd=str(ROOT), close_fds=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"启动失败: {e}")
    log_operation("refreshall.start", workers=req.workers, force=req.force)
    return {"status": "starting",
            "notice": "全 UP 刷新已在后台启动（关闭浏览器不影响）；进度会显示在页面上"}


@app.post("/api/profile/batch")
async def profile_batch(req: BatchRequest):
    """Batch extract author profile transcripts.

    Returns a text/event-stream with JSON progress lines, then a final
    summary line, then closes.
    """
    if _driver_info():
        raise HTTPException(status_code=409, detail=_driver_busy_detail())
    if not _claim_batch_lock():
        if _extract_busy:
            raise HTTPException(
                status_code=409,
                detail="插队解析进行中：请等它完成后再发起批量（插队任务优先）")
        raise HTTPException(
            status_code=409,
            detail="已有批量任务在运行（若界面看不到进度，可能是页面刷新后任务仍在后台继续，"
                   "请等它结束后再试）")
    _set_batch_pause_flag(False)  # a manual batch always starts unpaused

    # Resolve key like the single-video endpoint does (config file / env)
    from asr_backends import load_config_file
    file_cfg = load_config_file()
    provider_id = (req.provider or file_cfg.get("active_provider") or "siliconflow").lower()
    entry = (file_cfg.get("providers") or {}).get(provider_id) or {}
    api_key = entry.get("api_key") or os.getenv("API_KEY", "") \
        or os.getenv("DASHSCOPE_API_KEY", "") or os.getenv("ARK_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=400, detail="Please configure API Key first")

    # Claim the global single-job lock here, and let the WORKER thread release
    # it in its own finally. Releasing it from the SSE generator would be wrong:
    # a client disconnect (page reload) cancels the generator while the worker
    # keeps running, so an early release lets a second batch start and open a
    # second browser on the same persistent profile dir - two Chromium
    # instances on one user-data-dir lock each other's cookie DB, which freezes
    # the page renderer and hangs the run forever.
    # Start a fresh job view: subscribing clients must never see the previous
    # job's events replayed as if they belonged to this one.
    _batch_new_job(req.author or "", _refresh_log_ctx(
        trigger="手动批量", url=req.url,
        params=f"max_videos={req.max_videos}, force={req.force}, "
               f"save_video={req.save_video}, use_cache={req.use_cache}, "
               f"workers={req.workers}"))

    def _worker():
        try:
            log_operation("batch.start", url=req.url, provider=req.provider or "",
                          max_videos=req.max_videos, force=req.force,
                          save_video=req.save_video, use_cache=req.use_cache,
                          workers=req.workers)

            from batch_extractor import batch_extract, BatchCancelled
            try:
                summary = batch_extract(
                    req.url,
                    output_dir=str(Path(__file__).parent.parent / "output"),
                    api_key=api_key,
                    provider=req.provider or None,
                    max_videos=req.max_videos,
                    force=req.force,
                    headless=True,
                    save_video=req.save_video,
                    use_cache=req.use_cache,
                    workers=req.workers,
                    on_progress=_batch_publish,
                )
                if summary.get("aborted"):
                    _batch_publish({"stage": "aborted",
                                    "ok": summary.get("ok", 0),
                                    "skip": summary.get("skip", 0),
                                    "fail": summary.get("fail", 0)})
            except BatchCancelled:
                _batch_publish({"stage": "aborted"})
        except Exception as e:
            log_operation("batch.job", status="error", error=str(e))
            _batch_publish({"stage": "error", "error": str(e)[:400]})
        finally:
            # Order matters: a late subscriber treats "not running" as "the job
            # is over", so the flag must be down before the streams are closed.
            _write_refresh_log()  # needs history before any next job clears it
            _batch_state["running"] = False
            _batch_finish()
            _maybe_start_pending(delay=2.0)  # queued links take over next
            _restart_after_batch()

    threading.Thread(target=_worker, daemon=True).start()

    return _batch_response()


@app.get("/api/profile/batch/stream")
async def profile_batch_stream():
    """Reattach to the running batch job's progress stream.

    A reloaded page (or a dropped connection) has lost its event stream while
    the worker thread kept going. This replays what it missed and follows the
    job live; when the job is already over it just replays the tail and says
    so, which is how the client knows it can move on.
    """
    return _batch_response()


def _batch_response() -> StreamingResponse:
    return StreamingResponse(
        _batch_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# Durable copy of the last "refresh all" queue. The WebUI drives that queue, so
# it lives in the browser - and dies with the tab. Mirroring it here lets
# 「继续上次刷新任务」 pick the run back up after a reload, a closed tab, a
# restarted browser or a service restart. It only records where the queue
# stopped: videos already extracted ok are skipped by history.json dedup, so
# re-running the interrupted UP processes just what is still missing.
REFRESH_TASK_FILE = ROOT / "refresh_task.json"


class RefreshTaskRequest(BaseModel):
    pending: list = []   # UPs not started yet
    current: str = ""    # UP that was being refreshed
    done: list = []      # UPs already finished in that run
    total: int = 0
    run_new: int = 0     # transcripts added so far this run (completed UPs)
    run_skipped: int = 0
    run_fail: int = 0


@app.get("/api/refresh-task")
async def refresh_task_get():
    """The last refresh-all queue, for 「继续上次刷新任务」."""
    try:
        if REFRESH_TASK_FILE.exists():
            return {"task": json.loads(REFRESH_TASK_FILE.read_text(encoding="utf-8"))}
    except Exception:
        pass
    return {"task": None}


@app.post("/api/refresh-task")
async def refresh_task_put(req: RefreshTaskRequest):
    """Save the refresh-all queue as it advances."""
    data = req.model_dump()
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    REFRESH_TASK_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True}


@app.delete("/api/refresh-task")
async def refresh_task_delete():
    """Drop the saved queue (the run finished, or there is nothing left)."""
    try:
        REFRESH_TASK_FILE.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return {"ok": True}


@app.post("/api/profile/batch/pause")
async def profile_batch_pause():
    """Pause the running batch: in-flight video finishes, then workers wait.

    The batch stays alive (SSE stream open); POST /api/profile/batch/resume
    continues with the remaining videos.
    """
    if not _batch_state["running"]:
        raise HTTPException(status_code=409, detail="No batch job is running")
    from batch_extractor import run_gate
    run_gate.clear()
    _set_batch_pause_flag(True)
    log_operation("batch.pause", status="ok")
    return {"status": "paused", "notice": "当前视频完成后暂停"}


@app.post("/api/profile/batch/resume")
async def profile_batch_resume():
    """Resume a paused batch job.

    A running jump-queue extraction (插队解析) always has priority: instead of
    refusing, the resume is deferred until the extraction slot frees up, then
    applied automatically - the user's 继续 click is remembered, never lost,
    and never interrupts the extraction.
    """
    global _resume_waiting
    from batch_extractor import run_gate
    if run_gate.is_set():
        raise HTTPException(status_code=409, detail="Batch job is not paused")
    if _extract_busy:
        with _batch_claim_mutex:
            if not _resume_waiting:
                _resume_waiting = True
                threading.Thread(
                    target=_deferred_resume_when_extract_done,
                    daemon=True).start()
        return {"status": "deferred",
                "notice": "插队解析进行中：完成后批量将自动继续（插队任务优先）"}
    run_gate.set()
    _set_batch_pause_flag(False)
    log_operation("batch.resume", status="ok")
    return {"status": "resumed"}


# One deferred-resume watcher at a time (guarded by _batch_claim_mutex).
_resume_waiting = False


def _deferred_resume_when_extract_done():
    """Wait for the jump-queue extraction to finish, then resume the batch.

    The paused batch keeps waiting on run_gate the whole time, so resuming is
    just set() - the extraction is never disturbed. If the service is about to
    restart, bail out: the persisted pause flag makes the next process restore
    the pause instead.
    """
    global _resume_waiting
    from batch_extractor import run_gate
    try:
        while True:
            if _restart_state["pending"] or _restart_state["restarting"]:
                return  # keep paused; next process restores it via the flag
            with _batch_claim_mutex:
                busy = _extract_busy
            if not busy:
                run_gate.set()
                _set_batch_pause_flag(False)
                log_operation("batch.resume", status="ok", deferred=True)
                _batch_publish({"stage": "notice",
                                "message": "插队解析完成，批量已自动继续"})
                return
            time.sleep(1.0)
    finally:
        with _batch_claim_mutex:
            _resume_waiting = False


@app.get("/api/profile/history")
async def profile_history():
    """Counts of extracted/failed videos recorded in history.json"""
    from batch_extractor import TranscriptHistory
    h = TranscriptHistory()
    counts = h.counts()
    return {"count": counts["ok"], "ok": counts["ok"], "fail": counts["fail"]}


@app.get("/api/authors")
async def authors_list():
    """Known UPs (batch-extracted before): name, profile URL, counts."""
    from batch_extractor import author_registry
    return {"authors": author_registry.list()}


@app.delete("/api/authors")
async def authors_remove(name: str):
    """Remove one author from the registry (does not delete output files)."""
    from batch_extractor import author_registry
    existed = author_registry.get(name) is not None
    if existed:
        author_registry._data.pop(name, None)
        author_registry.path.write_text(
            json.dumps(author_registry._data, ensure_ascii=False, indent=2),
            encoding="utf-8")
        log_operation("authors.remove", name=name)
    return {"ok": True, "removed": existed}


class AuthorNameRequest(BaseModel):
    name: str


@app.post("/api/authors/open-dir")
async def authors_open_dir(req: AuthorNameRequest):
    """Open the author's output folder in Explorer."""
    import subprocess
    from batch_extractor import author_registry
    entry = author_registry.get(req.name)
    if not entry:
        raise HTTPException(status_code=404, detail="Author not found")
    d = Path(__file__).parent.parent / "output" / req.name
    if not d.is_dir():
        raise HTTPException(status_code=404, detail="Output folder not found")
    subprocess.Popen(["explorer", str(d)])
    return {"ok": True}


@app.get("/api/douyin/login-status")
async def douyin_login_status():
    """Whether a douyin session exists (backup file / imported session)."""
    import profile_fetcher as pf
    try:
        if pf._has_saved_session():
            return {"logged_in": True, "source": "cookies_backup"}
    except Exception:
        pass
    # Try importing the session from installed browsers (no window).
    # Skipped while a backend script task runs: the import would open a
    # second browser on the profile the driver is using.
    if _driver_info():
        return {"logged_in": False, "source": "driver_busy"}
    try:
        if pf._import_browser_session_to_backup():
            return {"logged_in": True, "source": "system_browser"}
    except Exception:
        pass
    return {"logged_in": False, "source": "none"}


@app.post("/api/douyin/login")
async def douyin_login():
    """Open a visible browser once so the user can log in (any method).

    The session persists in the .douyin_profile cache; later batch runs
    reuse it silently. Runs in a background thread; progress via SSE.
    """
    if _driver_info():
        raise HTTPException(status_code=409, detail=_driver_busy_detail())
    if _batch_state["running"]:
        raise HTTPException(status_code=409, detail="A batch job is already running")

    async def _gen():
        _batch_state["running"] = True
        loop = asyncio.get_event_loop()
        notices: "queue.Queue" = queue.Queue()
        try:
            def _worker():
                from profile_fetcher import interactive_login
                return interactive_login(on_notice=lambda m: notices.put(m))

            # Run in executor; stream status + notices via SSE
            yield f"data: {json.dumps({'stage': 'opening'}, ensure_ascii=False)}\n\n"
            log_operation("douyin.login", status="opening")
            task = loop.run_in_executor(None, _worker)
            while not task.done() or not notices.empty():
                try:
                    msg = await asyncio.wait_for(loop.run_in_executor(None, notices.get), timeout=0.5)
                    yield f"data: {json.dumps({'stage': 'notice', 'message': msg}, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    pass
            ok = task.result()
            log_operation("douyin.login", status="ok" if ok else "fail", ok=bool(ok))
            yield f"data: {json.dumps({'stage': 'done', 'ok': ok}, ensure_ascii=False)}\n\n"
        except Exception as e:
            log_operation("douyin.login", status="error", error=str(e))
            yield f"data: {json.dumps({'stage': 'error', 'error': str(e)[:300]}, ensure_ascii=False)}\n\n"
        finally:
            _batch_state["running"] = False
            _restart_after_batch()

    return StreamingResponse(_gen(), media_type="text/event-stream")


def _extract_single_video(url: str, api_key: str, provider: str = None,
                          model: str = None) -> tuple:
    """Single-video pipeline: share-url -> download -> audio -> ASR (+polish)
    -> record to the global library -> save md into the per-author output dir.

    Raises on failure. Shared by the /api/video/extract endpoint and the
    pending-links queue runner so both behave identically.
    """
    from douyin_downloader import DouyinProcessor
    from transcript_library import append_record
    backend = resolve_backend(api_key, provider=provider, model=model)
    processor = DouyinProcessor(
        backend['api_key'],
        provider=backend['provider'],
        model=backend['model'],
        api_base_url=backend['api_base_url'] or "",
    )
    video_info = processor.parse_share_url(url)
    video_path = processor.download_video(video_info, show_progress=False)
    audio_path = processor.extract_audio(video_path, show_progress=False)
    try:
        text = processor.extract_text_from_audio(audio_path, show_progress=False)
    finally:
        processor.cleanup_files(video_path, audio_path)
    # Optional LLM polish (fixes homophones/punctuation)
    polish_cfg = (load_config_file().get("polish") or {})
    if polish_cfg.get("enabled"):
        try:
            from transcript_polish import polish_transcript
            text = polish_transcript(text)
        except Exception:
            pass  # never break extraction due to polish failure
    # Record to the global library file
    append_record(
        video_id=video_info["video_id"],
        title=video_info["title"],
        text=text,
        source="single",
        provider=backend['provider'],
        model=backend['model'],
    )
    # Save into the per-author directory (one dir per UP) and
    # refresh that author's catalog; never fail the request on it.
    try:
        from batch_extractor import save_transcript
        author = (video_info.get("author") or "").strip() or "未知作者"
        safe_author = re.sub(r'[\\/:*?"<>|]', '_', author).strip() or "未知作者"
        author_dir = (Path(__file__).resolve().parent.parent
                      / "output" / safe_author)
        save_transcript(author_dir, video_info["video_id"],
                        video_info["title"], author, text)
    except Exception as e:
        log_operation("video.extract.save_md_failed",
                      video_id=video_info["video_id"],
                      error=str(e)[:200])
    return video_info, text


@app.post("/api/video/extract", response_model=ExtractResponse)
async def extract_transcript(req: VideoRequest):
    """Extract video transcript (API_KEY required)"""
    # Resolve key from config file first, then env vars
    file_cfg = load_config_file()
    provider_id = (req.provider or file_cfg.get("active_provider") or "siliconflow").lower()
    entry = (file_cfg.get("providers") or {}).get(provider_id) or {}
    api_key = req.api_key or entry.get("api_key", "") or os.getenv("API_KEY", "") \
        or os.getenv("DASHSCOPE_API_KEY", "") or os.getenv("ARK_API_KEY", "")
    if not api_key:
        return ExtractResponse(
            success=False,
            error="Please configure API Key first"
        )

    if _driver_info():
        raise HTTPException(status_code=409, detail=_driver_busy_detail())
    if not _claim_extract_slot():
        raise HTTPException(
            status_code=409,
            detail="已有解析任务在运行；请等它结束后再试，或使用「排队解析」加入待解析队列")

    try:
        # Resolve backend (provider/model from request or config/env), then
        # run download -> extract audio -> transcribe in a worker thread.
        video_info, text = await asyncio.to_thread(
            _extract_single_video, req.url, api_key,
            req.provider or None, req.model or None)
        log_operation("video.extract", video_id=video_info["video_id"],
                      title=video_info["title"], text_length=len(text),
                      download_url=video_info["url"])
        return ExtractResponse(
            success=True,
            video_id=video_info["video_id"],
            title=video_info["title"],
            text=text,
            download_url=video_info["url"]
        )
    except Exception as e:
        log_operation("video.extract", status="error", url=req.url, error=str(e))
        return ExtractResponse(success=False, error=str(e))
    finally:
        _release_extract_slot()


@app.get("/api/logs")
async def get_operation_logs(limit: int = 200, action: str = "", status: str = ""):
    """Query recent runtime operation logs (newest last).

    Optional filters: action (e.g. video.extract), status (ok/error).
    """
    return {
        "count": limit,
        "file": str(log_file_path()),
        "logs": read_logs(limit=limit, action=action or None, status=status or None),
    }


def _content_disposition(filename: str) -> str:
    """Build safe Content-Disposition header (ASCII fallback + RFC 5987 encoding)"""
    ascii_name = re.sub(r'[^A-Za-z0-9._-]', '_', filename) or "video.mp4"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


@app.get("/api/video/download")
async def download_video(video_id: str, filename: str = "video.mp4"):
    """Proxy video download (solves CORS and header issues)

    Only accepts douyin video IDs; the server resolves the CDN link itself,
    avoiding proxying arbitrary URLs.
    """
    if not re.fullmatch(r'\d+', video_id):
        raise HTTPException(status_code=400, detail="Invalid video ID")

    try:
        share_url = f"https://www.iesdouyin.com/share/video/{video_id}"
        info = await asyncio.to_thread(get_video_info, share_url)

        # Full request headers, simulating browser access
        download_headers = {
            'User-Agent': HEADERS_UA,
            'Referer': 'https://www.douyin.com/',
            'Accept': '*/*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'identity',
            'Connection': 'keep-alive',
        }

        response = await asyncio.to_thread(
            requests.get, info["url"], headers=download_headers, stream=True, allow_redirects=True
        )
        response.raise_for_status()

        content_length = response.headers.get("content-length", "")

        def iter_content():
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk

        headers = {
            "Content-Disposition": _content_disposition(filename),
        }
        if content_length:
            headers["Content-Length"] = content_length

        return StreamingResponse(
            iter_content(),
            media_type="video/mp4",
            headers=headers
        )
    except requests.exceptions.HTTPError as e:
        raise HTTPException(status_code=e.response.status_code, detail=f"Download failed: {e.response.status_code}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def main():
    """Start service"""
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8080"))
    log_operation("server.start", host=host, port=port)
    print(f"[WebUI] http://localhost:{port}")
    print(f"[API_KEY] {'configured' if os.getenv('API_KEY') else 'not configured'}")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
