#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Douyin watermark-free video download and text extraction MCP server

Features:
1. Parse douyin share links to get watermark-free video URLs
2. Download video and extract audio
3. Extract text from audio (speech-to-text)
4. Auto cleanup of temp files

Speech-to-text backends (switch via env vars):
- ASR_PROVIDER: siliconflow (default) | dashscope | ark
- API_KEY: universal key (or DASHSCOPE_API_KEY / ARK_API_KEY per backend)
- ASR_MODEL / API_BASE_URL: optional overrides
"""

import os
import re
import json
import shutil
import requests
import tempfile
import asyncio
from pathlib import Path
from typing import Optional
import ffmpeg

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp import Context

from .asr_module import create_asr_instance

# Make shared backends importable (lives in douyin-video/scripts)
import sys
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent / "douyin-video" / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

# Runtime operation log (shared with WebUI): log/operation_logs.txt
from operation_logger import log_operation

# Create MCP server instance
mcp = FastMCP("Douyin MCP Server",
              dependencies=["requests", "ffmpeg-python", "tqdm", "dashscope"])

# Request headers, simulating mobile access
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) EdgiOS/121.0.2277.107 Version/17.0 Mobile/15E148 Safari/604.1'
}

# Default API config
SILICONFLOW_API_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
DEFAULT_SILICONFLOW_MODEL = "FunAudioLLM/SenseVoiceSmall"
DEFAULT_DASHSCOPE_MODEL = "qwen3-asr-flash"
DEFAULT_ARK_MODEL = "doubao-seed-1-6-250615"


def resolve_asr_config(model: Optional[str] = None) -> tuple:
    """
    Resolve speech-to-text backend from environment variables.

    Env vars:
    - ASR_PROVIDER: siliconflow (default) | dashscope | ark
    - API_KEY: universal key (or DASHSCOPE_API_KEY / ARK_API_KEY per backend)

    Returns: (provider, api_key, model)
    """
    provider = (os.getenv('ASR_PROVIDER') or 'siliconflow').lower()

    key = os.getenv('API_KEY')
    if provider == 'dashscope':
        key = key or os.getenv('DASHSCOPE_API_KEY')
    elif provider == 'ark':
        key = key or os.getenv('ARK_API_KEY')

    if not key:
        raise ValueError(
            "API key not set: set API_KEY (universal, works for all backends), "
            "or per-backend DASHSCOPE_API_KEY (Alibaba Bailian) / "
            "ARK_API_KEY (Volcengine Ark), and pick backend via ASR_PROVIDER "
            "(siliconflow|dashscope|ark)"
        )

    default_model = {
        'siliconflow': DEFAULT_SILICONFLOW_MODEL,
        'dashscope': DEFAULT_DASHSCOPE_MODEL,
        'ark': DEFAULT_ARK_MODEL,
    }.get(provider, DEFAULT_SILICONFLOW_MODEL)

    return provider, key, model or default_model


class DouyinProcessor:
    """Douyin video processor"""

    def __init__(self, api_key: str = "", provider: str = "siliconflow", model: Optional[str] = None):
        self.api_key = api_key
        self.provider = provider
        self.model = model
        self.temp_dir = Path(tempfile.mkdtemp())

    def __del__(self):
        """Clean up temp directory"""
        if hasattr(self, 'temp_dir') and self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def parse_share_url(self, share_text: str) -> dict:
        """Extract watermark-free video link from share text"""
        # Extract share link
        urls = re.findall(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', share_text)
        if not urls:
            raise ValueError("No valid share link found")

        share_url = urls[0]
        share_response = requests.get(share_url, headers=HEADERS)
        video_id = share_response.url.split("?")[0].strip("/").split("/")[-1]
        share_url = f'https://www.iesdouyin.com/share/video/{video_id}'

        # Get video page content
        response = requests.get(share_url, headers=HEADERS)
        response.raise_for_status()

        pattern = re.compile(
            pattern=r"window\._ROUTER_DATA\s*=\s*(.*?)</script>",
            flags=re.DOTALL,
        )
        find_res = pattern.search(response.text)

        if not find_res or not find_res.group(1):
            raise ValueError("Failed to parse video info from HTML")

        # Parse JSON data
        json_data = json.loads(find_res.group(1).strip())
        VIDEO_ID_PAGE_KEY = "video_(id)/page"
        NOTE_ID_PAGE_KEY = "note_(id)/page"

        if VIDEO_ID_PAGE_KEY in json_data["loaderData"]:
            original_video_info = json_data["loaderData"][VIDEO_ID_PAGE_KEY]["videoInfoRes"]
        elif NOTE_ID_PAGE_KEY in json_data["loaderData"]:
            original_video_info = json_data["loaderData"][NOTE_ID_PAGE_KEY]["videoInfoRes"]
        else:
            raise Exception("Cannot parse video or gallery info from JSON")

        data = original_video_info["item_list"][0]

        # Get video info
        video_url = data["video"]["play_addr"]["url_list"][0].replace("playwm", "play")
        desc = data.get("desc", "").strip() or f"douyin_{video_id}"

        # Replace illegal characters in filename
        desc = re.sub(r'[\\/:*?"<>|]', '_', desc)

        return {
            "url": video_url,
            "title": desc,
            "video_id": video_id
        }

    def download_video(self, video_info: dict) -> Path:
        """Download video to temp directory"""
        filename = f"{video_info['video_id']}.mp4"
        filepath = self.temp_dir / filename

        response = requests.get(video_info['url'], headers=HEADERS, stream=True)
        response.raise_for_status()

        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        return filepath

    def extract_audio(self, video_path: Path) -> Path:
        """Extract audio from video file"""
        audio_path = video_path.with_suffix('.mp3')

        try:
            (
                ffmpeg
                .input(str(video_path))
                .output(str(audio_path), acodec='libmp3lame', q=0)
                .run(capture_stdout=True, capture_stderr=True, overwrite_output=True)
            )
            return audio_path
        except Exception as e:
            raise Exception(f"Error extracting audio: {str(e)}")

    def transcribe_audio(self, audio_path: Path, context: Optional[str] = None) -> str:
        """Extract text from audio file (unified multi-backend entry)"""
        from asr_backends import transcribe_audio_file

        return transcribe_audio_file(
            audio_path,
            api_key=self.api_key,
            provider=self.provider or None,
            model=self.model or None,
            context=context,
        )

    def cleanup_files(self, *file_paths: Path):
        """Clean up given files"""
        for file_path in file_paths:
            if file_path.exists():
                file_path.unlink()


@mcp.tool()
def get_douyin_download_link(share_link: str) -> str:
    """
    Get watermark-free download link for a douyin video

    Args:
    - share_link: douyin share link or text containing the link

    Returns:
    - JSON string with download link and video info
    """
    try:
        processor = DouyinProcessor()  # no API key needed
        video_info = processor.parse_share_url(share_link)

        log_operation("mcp.get_download_link", video_id=video_info["video_id"],
                      title=video_info["title"], download_url=video_info["url"])
        return json.dumps({
            "status": "success",
            "video_id": video_info["video_id"],
            "title": video_info["title"],
            "download_url": video_info["url"],
            "description": f"Video title: {video_info['title']}",
            "usage_tip": "You can download the watermark-free video via this link"
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        log_operation("mcp.get_download_link", status="error", error=str(e))
        return json.dumps({
            "status": "error",
            "error": f"Failed to get download link: {str(e)}"
        }, ensure_ascii=False, indent=2)


@mcp.tool()
async def extract_douyin_text(
    share_link: str,
    model: Optional[str] = None,
    context: Optional[str] = None,
    ctx: Context = None
) -> str:
    """
    Extract text content from a douyin share link

    Args:
    - share_link: douyin share link or text containing the link
    - model: speech-to-text model (optional; defaults depend on provider)
    - context: context text to improve accuracy (optional, dashscope backend only)

    Returns:
    - Extracted text content

    Note: requires API_KEY (universal) or provider-specific key env var,
    and ASR_PROVIDER to pick backend (siliconflow|dashscope|ark)
    """
    video_path = None
    audio_path = None
    try:
        provider, api_key, model_name = resolve_asr_config(model)
        processor = DouyinProcessor(api_key, provider, model_name)

        # Parse video link
        if ctx:
            await ctx.info("Parsing douyin share link...")
        video_info = await asyncio.to_thread(processor.parse_share_url, share_link)
        log_operation("mcp.extract_text", status="parsing", provider=provider,
                      model=model_name, video_id=video_info["video_id"],
                      title=video_info["title"])

        # Download video and extract audio
        if ctx:
            await ctx.info(f"Downloading video: {video_info['title']}")
        video_path = await asyncio.to_thread(processor.download_video, video_info)

        if ctx:
            await ctx.info("Extracting audio...")
        audio_path = await asyncio.to_thread(processor.extract_audio, video_path)

        # Speech recognition
        if ctx:
            await ctx.info("Extracting text from audio...")
        full_context = f"Video title: {video_info['title']}"
        if context:
            full_context = f"{context}\n{full_context}"
        text_content = await asyncio.to_thread(processor.transcribe_audio, audio_path, full_context)

        if ctx:
            await ctx.info("Text extraction complete!")
        log_operation("mcp.extract_text", provider=provider, model=model_name,
                      video_id=video_info["video_id"], title=video_info["title"],
                      text_length=len(text_content))
        return text_content

    except Exception as e:
        log_operation("mcp.extract_text", status="error", error=str(e))
        raise Exception(f"Failed to extract douyin video text: {str(e)}")
    finally:
        # Clean up temp files
        for path in (video_path, audio_path):
            if path is not None and path.exists():
                path.unlink(missing_ok=True)


@mcp.tool()
def recognize_audio_file(
    file_path: str,
    context: Optional[str] = None,
    language: Optional[str] = None,
    model: Optional[str] = None
) -> str:
    """
    Recognize text in a local audio file

    Args:
    - file_path: local audio file path
    - context: context text to improve accuracy (optional)
    - language: language code like 'zh', 'en' (optional, auto-detected by default)
    - model: speech-to-text model (optional, uses qwen3-asr-flash by default)

    Returns:
    - Recognized text content

    Note: requires DASHSCOPE_API_KEY (Alibaba Bailian)
    """
    try:
        # Get API key from environment
        api_key = os.getenv('DASHSCOPE_API_KEY')
        if not api_key:
            raise ValueError("DASHSCOPE_API_KEY not set, please add the Alibaba Bailian API key to your config")

        # Create ASR instance
        asr = create_asr_instance(api_key, model or DEFAULT_DASHSCOPE_MODEL)

        # Recognize audio file
        result = asr.recognize_file(
            file_path=file_path,
            context=context,
            language=language,
            enable_lid=True,
            enable_itn=False
        )

        if result["success"]:
            log_operation("mcp.recognize_file", file_path=file_path,
                          text_length=len(result["text"]), request_id=result.get("request_id"))
            return json.dumps({
                "status": "success",
                "text": result["text"],
                "language": result.get("language"),
                "usage": result.get("usage"),
                "request_id": result.get("request_id")
            }, ensure_ascii=False, indent=2)
        else:
            log_operation("mcp.recognize_file", status="error", file_path=file_path,
                          error=result["error"])
            return json.dumps({
                "status": "error",
                "error": result["error"]
            }, ensure_ascii=False, indent=2)

    except Exception as e:
        log_operation("mcp.recognize_file", status="error", error=str(e))
        return json.dumps({
            "status": "error",
            "error": f"Failed to recognize audio file: {str(e)}"
        }, ensure_ascii=False, indent=2)


@mcp.tool()
def recognize_audio_url(
    audio_url: str,
    context: Optional[str] = None,
    language: Optional[str] = None,
    model: Optional[str] = None
) -> str:
    """
    Recognize text in an online audio URL

    Args:
    - audio_url: audio URL link
    - context: context text to improve accuracy (optional)
    - language: language code like 'zh', 'en' (optional, auto-detected by default)
    - model: speech-to-text model (optional, uses qwen3-asr-flash by default)

    Returns:
    - Recognized text content

    Note: requires DASHSCOPE_API_KEY (Alibaba Bailian)
    """
    try:
        # Get API key from environment
        api_key = os.getenv('DASHSCOPE_API_KEY')
        if not api_key:
            raise ValueError("DASHSCOPE_API_KEY not set, please add the Alibaba Bailian API key to your config")

        # Create ASR instance
        asr = create_asr_instance(api_key, model or DEFAULT_DASHSCOPE_MODEL)

        # Recognize audio URL
        result = asr.recognize_url(
            audio_url=audio_url,
            context=context,
            language=language,
            enable_lid=True,
            enable_itn=False
        )

        if result["success"]:
            log_operation("mcp.recognize_url", audio_url=audio_url,
                          text_length=len(result["text"]), request_id=result.get("request_id"))
            return json.dumps({
                "status": "success",
                "text": result["text"],
                "language": result.get("language"),
                "usage": result.get("usage"),
                "request_id": result.get("request_id")
            }, ensure_ascii=False, indent=2)
        else:
            log_operation("mcp.recognize_url", status="error", audio_url=audio_url,
                          error=result["error"])
            return json.dumps({
                "status": "error",
                "error": result["error"]
            }, ensure_ascii=False, indent=2)

    except Exception as e:
        log_operation("mcp.recognize_url", status="error", error=str(e))
        return json.dumps({
            "status": "error",
            "error": f"Failed to recognize audio URL: {str(e)}"
        }, ensure_ascii=False, indent=2)


@mcp.tool()
def parse_douyin_video_info(share_link: str) -> str:
    """
    Parse douyin share link, get basic video info

    Args:
    - share_link: douyin share link or text containing the link

    Returns:
    - Video info (JSON format string)
    """
    try:
        processor = DouyinProcessor()  # no API key needed
        video_info = processor.parse_share_url(share_link)

        log_operation("mcp.parse_video_info", video_id=video_info["video_id"],
                      title=video_info["title"], download_url=video_info["url"])
        return json.dumps({
            "video_id": video_info["video_id"],
            "title": video_info["title"],
            "download_url": video_info["url"],
            "status": "success"
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        log_operation("mcp.parse_video_info", status="error", error=str(e))
        return json.dumps({
            "status": "error",
            "error": str(e)
        }, ensure_ascii=False, indent=2)


@mcp.resource("douyin://video/{video_id}")
def get_video_info(video_id: str) -> str:
    """
    Get detailed info of a video by ID

    Args:
    - video_id: douyin video ID

    Returns:
    - Video details
    """
    share_url = f"https://www.iesdouyin.com/share/video/{video_id}"
    try:
        processor = DouyinProcessor()
        video_info = processor.parse_share_url(share_url)
        return json.dumps(video_info, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Failed to get video info: {str(e)}"


@mcp.prompt()
def douyin_text_extraction_guide() -> str:
    """Douyin video text extraction usage guide"""
    return """
# Douyin Video Text Extraction Guide

## Features
This MCP server extracts text from douyin video share links,
and fetches watermark-free download links.

## Environment Variables
Speech-to-text supports three backends, configure one:
- `API_KEY` + `ASR_PROVIDER=siliconflow`: SiliconFlow (default, https://cloud.siliconflow.cn)
- `DASHSCOPE_API_KEY` + `ASR_PROVIDER=dashscope`: Alibaba Bailian qwen3-asr
- `ARK_API_KEY` + `ASR_PROVIDER=ark`: Volcengine Ark doubao models

## Usage Steps
1. Copy the douyin video share link
2. Set env vars in the Claude Desktop config
3. Use the appropriate tools

## Tools
- `extract_douyin_text`: full text extraction flow (API key required)
- `get_douyin_download_link`: watermark-free download link (no API key)
- `parse_douyin_video_info`: parse basic video info only
- `recognize_audio_file`: recognize local audio file (DASHSCOPE_API_KEY)
- `recognize_audio_url`: recognize online audio URL (DASHSCOPE_API_KEY)
- `douyin://video/{video_id}`: get video details

## Claude Desktop Config Example
```json
{
  "mcpServers": {
    "douyin-mcp": {
      "command": "uvx",
      "args": ["douyin-mcp-server"],
      "env": {
        "ASR_PROVIDER": "siliconflow",
        "API_KEY": "your-api-key"
      }
    }
  }
}
```

## Notes
- A valid API key is required (via environment variables)
- Most douyin video formats are supported
- Getting download links needs no API key
"""


def main():
    """Start MCP server"""
    mcp.run()


if __name__ == "__main__":
    main()
