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
import threading
from pathlib import Path
from urllib.parse import quote

# Add project path
sys.path.insert(0, str(Path(__file__).parent.parent / "douyin-video" / "scripts"))

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import uvicorn
import requests

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
        if is_profile_url(req.url):
            return VideoInfoResponse(success=False,
                                     error="This is an author profile link. Use the Batch Extract button.")
        info = await asyncio.to_thread(get_video_info, req.url)
        log_operation("video.info", url=req.url, video_id=info["video_id"],
                      title=info["title"], download_url=info["url"])
        return VideoInfoResponse(
            success=True,
            video_id=info["video_id"],
            title=info["title"],
            download_url=info["url"]
        )
    except Exception as e:
        log_operation("video.info", status="error", url=req.url, error=str(e))
        return VideoInfoResponse(success=False, error=str(e))


class BatchRequest(BaseModel):
    """Author profile batch extraction request"""
    url: str
    provider: str = ""
    max_videos: int = 0  # 0 = all
    force: bool = False
    save_video: bool = False
    use_cache: bool = False  # reuse cached video list (retry failed only)
    workers: int = 3  # concurrent videos


# Global batch job state (one job at a time keeps things simple)
_batch_state = {"running": False}


@app.post("/api/profile/batch")
async def profile_batch(req: BatchRequest):
    """Batch extract author profile transcripts.

    Returns a text/event-stream with JSON progress lines, then a final
    summary line, then closes.
    """
    if _batch_state["running"]:
        raise HTTPException(status_code=409, detail="A batch job is already running")

    async def _run_job(api_key: str):
        q: "queue.Queue" = queue.Queue()
        loop = asyncio.get_event_loop()

        try:
            _batch_state["running"] = True

            log_operation("batch.start", url=req.url, provider=req.provider or "",
                          max_videos=req.max_videos, force=req.force,
                          save_video=req.save_video, use_cache=req.use_cache,
                          workers=req.workers)
            def _worker():
                try:
                    from batch_extractor import batch_extract
                    batch_extract(
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
                        on_progress=lambda info: q.put(info),
                    )
                except Exception as e:
                    log_operation("batch.job", status="error", error=str(e))
                    q.put({"stage": "error", "error": str(e)[:400]})
                finally:
                    q.put(None)

            thread = threading.Thread(target=_worker, daemon=True)
            thread.start()

            while True:
                # Pull from the worker thread without blocking the event loop
                item = await loop.run_in_executor(None, q.get)
                if item is None:
                    break
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        finally:
            _batch_state["running"] = False

    # Resolve key like the single-video endpoint does (config file / env)
    from asr_backends import load_config_file
    file_cfg = load_config_file()
    provider_id = (req.provider or file_cfg.get("active_provider") or "siliconflow").lower()
    entry = (file_cfg.get("providers") or {}).get(provider_id) or {}
    api_key = entry.get("api_key") or os.getenv("API_KEY", "") \
        or os.getenv("DASHSCOPE_API_KEY", "") or os.getenv("ARK_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=400, detail="Please configure API Key first")

    return StreamingResponse(
        _run_job(api_key),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
    log_operation("batch.pause", status="ok")
    return {"status": "paused", "notice": "当前视频完成后暂停"}


@app.post("/api/profile/batch/resume")
async def profile_batch_resume():
    """Resume a paused batch job."""
    from batch_extractor import run_gate
    if run_gate.is_set():
        raise HTTPException(status_code=409, detail="Batch job is not paused")
    run_gate.set()
    log_operation("batch.resume", status="ok")
    return {"status": "resumed"}


@app.get("/api/profile/history")
async def profile_history():
    """Counts of extracted/failed videos recorded in history.json"""
    from batch_extractor import TranscriptHistory
    h = TranscriptHistory()
    counts = h.counts()
    return {"count": counts["ok"], "ok": counts["ok"], "fail": counts["fail"]}


@app.get("/api/douyin/login-status")
async def douyin_login_status():
    """Whether a douyin session exists (backup file / imported session)."""
    import profile_fetcher as pf
    try:
        if pf._has_saved_session():
            return {"logged_in": True, "source": "cookies_backup"}
    except Exception:
        pass
    # Try importing the session from installed browsers (no window)
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

    return StreamingResponse(_gen(), media_type="text/event-stream")


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

    try:
        # Resolve backend (provider/model from request or config/env), then
        # run download -> extract audio -> transcribe in a worker thread.
        def _run():
            from douyin_downloader import DouyinProcessor
            from transcript_library import append_record
            backend = resolve_backend(
                api_key,
                provider=req.provider or None,
                model=req.model or None,
            )
            processor = DouyinProcessor(
                backend['api_key'],
                provider=backend['provider'],
                model=backend['model'],
                api_base_url=backend['api_base_url'] or "",
            )
            video_info = processor.parse_share_url(req.url)
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
            return video_info, text

        video_info, text = await asyncio.to_thread(_run)
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
