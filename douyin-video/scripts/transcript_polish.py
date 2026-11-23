#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Optional LLM post-processing for raw ASR transcripts.

Fixes homophones, wrong segmentation and missing punctuation by sending
chunks of raw text to any OpenAI-compatible chat endpoint.

Features:
- sentence-aware chunking (avoids splitting mid-sentence)
- automatic retry with exponential backoff for transient failures
- parallel chunk processing (optional, ordered output)
- graceful fallback to the original text on any failure (never breaks extraction)
- strips stray Markdown code fences from model output

Config (web_ui_config.json "polish" section):
{
  "polish": {
    "enabled": true,
    "api_key": "sk-...",
    "api_base_url": "https://api.siliconflow.cn/v1/chat/completions",
    "model": "Qwen/Qwen3-8B"     # any cheap fast chat model
  }
}
Fallbacks: env OPENAI_API_KEY / OPENAI_BASE_URL / POLISH_MODEL
"""

import os
import re
import time
import logging
import requests
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor

from asr_backends import load_config_file

logger = logging.getLogger(__name__)

DEFAULT_CHAT_URL = "https://api.siliconflow.cn/v1/chat/completions"
DEFAULT_CHAT_MODEL = "Qwen/Qwen3-8B"

POLISH_SYSTEM_PROMPT = (
    "你是专业的中文字幕校对与文案整理助手。用户给你一段语音识别(ASR)的原始文稿，"
    "可能来自视频解说，其中包含人名、地名、作品名、术语等专有名词。\n"
    "请严格完成以下任务：\n"
    "1. 修正同音字、形近字和错别字（结合上下文与常见专有名词判断正确用字）；\n"
    "2. 补全缺失的标点符号，并按语义合理分段换行；\n"
    "3. 修正明显的语句切分错误与语序不通之处；\n"
    "4. 保留原文的全部信息与口语风格，不得增删内容、不得总结、不得改写、不得翻译。\n\n"
    "输出要求：\n"
    "- 只输出修正后的文稿本身；\n"
    "- 不要添加任何解释、说明、评论或前后缀；\n"
    "- 不要使用 Markdown 代码块或其他格式包裹输出。"
)

TRANSLATE_SYSTEM_PROMPT = (
    "你是专业的字幕翻译助手。用户给你一段外文口播的语音识别(ASR)文稿，"
    "请把它翻译成简体中文。\n"
    "要求：\n"
    "1. 保留原意、信息量与先后顺序，不得增删、不得总结、不得改写；\n"
    "2. 人名、地名、作品名、术语等专有名词按通行译法处理，无通行译法时保留原文；\n"
    "3. 按时长与语义合理断句分段，输出可直接作为字幕/文案使用的中文；\n"
    "4. 若原文含音乐标签等非语音标记，原样保留这些标记的位置，不要翻译标记本身。\n\n"
    "输出要求：\n"
    "- 只输出译文本身；\n"
    "- 不要添加任何解释、说明、评论或前后缀；\n"
    "- 不要使用 Markdown 代码块或其他格式包裹输出。"
)

# Split long transcripts into chunks that fit comfortably in a request
CHUNK_CHARS = 3000

# Sentence-ending characters used to find safe split points
_SENTENCE_BOUNDARY = "。！？!?；;"

# Matches a whole fenced code block (```...```) possibly with a language tag
_FENCE_RE = re.compile(
    r"^\s*```(?:markdown|md|text)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL
)

# Transient statuses worth retrying
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_DEFAULT_RETRIES = 3


def get_polish_config() -> dict:
    """Resolve polish settings from config file, falling back to env vars."""
    cfg = load_config_file().get("polish") or {}
    api_key = cfg.get("api_key") or os.getenv("OPENAI_API_KEY", "")
    return {
        "enabled": bool(cfg.get("enabled", False)) and bool(api_key),
        "api_key": api_key,
        "api_base_url": cfg.get("api_base_url") or os.getenv("OPENAI_BASE_URL") or DEFAULT_CHAT_URL,
        "model": cfg.get("model") or os.getenv("POLISH_MODEL") or DEFAULT_CHAT_MODEL,
    }


def _derive_chat_url(base_url: str) -> str:
    """Derive an OpenAI-compatible chat endpoint from an ASR endpoint.

    Reuses the same vendor/key already configured for ASR - no new provider:
      .../v1/audio/transcriptions -> .../v1/chat/completions
      .../v1 (or /v3)             -> .../chat/completions
      already a chat endpoint     -> unchanged
    Non-OpenAI-compatible endpoints (e.g. DashScope multimodal) return "".
    """
    base = (base_url or "").strip()
    if not base:
        return ""
    if "/chat/completions" in base:
        return base
    if base.endswith("/audio/transcriptions"):
        return base[:-len("/audio/transcriptions")] + "/chat/completions"
    if base.endswith("/v1") or base.endswith("/v3"):
        return base + "/chat/completions"
    return ""


def get_translate_config() -> dict:
    """Resolve translation settings without introducing a new vendor.

    Priority: config "translate" section -> existing "polish" client ->
    active ASR provider (same key, chat URL derived from its endpoint) ->
    OPENAI_* env vars.
    """
    file_cfg = load_config_file()
    cfg = file_cfg.get("translate") or {}
    api_key = cfg.get("api_key") or ""
    base_url = cfg.get("api_base_url") or ""
    model = cfg.get("model") or os.getenv("TRANSLATE_MODEL", "")

    # 1) reuse the polish client (already OpenAI-compatible)
    polish_cfg = get_polish_config()
    if polish_cfg["api_key"]:
        api_key = api_key or polish_cfg["api_key"]
        base_url = base_url or polish_cfg["api_base_url"]
        model = model or polish_cfg["model"]

    # 2) reuse the active ASR provider's key, derive the chat URL
    if not api_key or not base_url:
        active = file_cfg.get("active_provider") or "siliconflow"
        entry = (file_cfg.get("providers") or {}).get(active) or {}
        api_key = api_key or entry.get("api_key", "")
        base_url = base_url or _derive_chat_url(entry.get("api_base_url", ""))
        model = model or DEFAULT_CHAT_MODEL

    # 3) env fallback
    api_key = api_key or os.getenv("OPENAI_API_KEY", "")
    base_url = base_url or os.getenv("OPENAI_BASE_URL", "")

    return {
        "enabled": bool(cfg.get("enabled", True)) and bool(api_key) and bool(base_url),
        "api_key": api_key,
        "api_base_url": base_url,
        "model": model or DEFAULT_CHAT_MODEL,
    }


def _clean_output(content: str) -> str:
    """Strip stray Markdown code fences and surrounding whitespace."""
    content = (content or "").strip()
    m = _FENCE_RE.match(content)
    if m:
        content = m.group(1).strip()
    return content


def _split_chunks(text: str, max_chars: int) -> List[str]:
    """Split text into chunks, preferring sentence boundaries over hard cuts.

    Falls back to a hard cut only when a single sentence exceeds max_chars.
    """
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: List[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + max_chars, n)
        cut = -1
        if end < n:
            # Prefer the last sentence boundary within the window.
            window = text[start:end]
            for i in range(len(window) - 1, -1, -1):
                if window[i] in _SENTENCE_BOUNDARY or window[i] == "\n":
                    cut = i + 1
                    break
            # Accept a boundary only if it keeps the chunk reasonably full.
            if cut <= 0 or cut < max_chars // 2:
                cut = max_chars
            end = start + cut
        chunks.append(text[start:end])
        start = end
    return chunks


def _call_polish(api_base_url: str, api_key: str, model: str,
                 chunk: str, retries: int,
                 stats: Optional[list] = None,
                 system_prompt: str = POLISH_SYSTEM_PROMPT) -> str:
    """Polish a single chunk with retry + backoff. Returns the raw chunk on failure.

    stats: optional list, appended once with {"attempts": n, "ok": bool} so
    callers can report the REAL retry count (never inferred from duration).
    """
    attempts = max(1, retries)
    used = 0

    def _finish(result: str, ok: bool) -> str:
        if stats is not None:
            stats.append({"attempts": used, "ok": ok})
        return result

    for attempt in range(attempts):
        used = attempt + 1
        try:
            resp = requests.post(
                api_base_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": chunk},
                    ],
                    "temperature": 0,
                    "stream": False,
                },
                timeout=180,
            )
            if resp.status_code == 200:
                try:
                    result = resp.json()
                    content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                except (ValueError, KeyError, IndexError, TypeError):
                    logger.warning("Polish: unparseable 200 response, keeping raw chunk")
                    return _finish(chunk, True)
                cleaned = _clean_output(content)
                return _finish(cleaned or chunk, True)
            if resp.status_code in _RETRYABLE_STATUS:
                logger.warning(
                    "Polish: HTTP %s, attempt %d/%d",
                    resp.status_code, attempt + 1, attempts,
                )
            else:
                logger.warning(
                    "Polish: non-retryable HTTP %s: %s",
                    resp.status_code, resp.text[:200],
                )
                return _finish(chunk, False)
        except requests.exceptions.RequestException as e:
            logger.warning("Polish: request error (attempt %d/%d): %s",
                           attempt + 1, attempts, e)
        except Exception as e:
            logger.warning("Polish: unexpected error (attempt %d/%d): %s",
                           attempt + 1, attempts, e)

        if attempt < attempts - 1:
            time.sleep(min(2 ** attempt, 8))

    return _finish(chunk, False)


def polish_transcript(text: str, api_key: Optional[str] = None,
                      api_base_url: Optional[str] = None,
                      model: Optional[str] = None,
                      max_chars: int = 20000,
                      max_workers: int = 4,
                      retries: int = _DEFAULT_RETRIES,
                      stats: Optional[list] = None) -> str:
    """Polish raw ASR text with an LLM. Long texts are processed in chunks.

    Never raises on failure - returns the original text so extraction
    never breaks because of polishing.

    stats: optional list collecting per-chunk {"attempts", "ok"} readings
    (thread-safe append) for real retry/timing instrumentation.
    """
    cfg = get_polish_config()
    api_key = api_key or cfg["api_key"]
    api_base_url = api_base_url or cfg["api_base_url"]
    model = model or cfg["model"]

    if not api_key or not text:
        return text

    text = text[:max_chars] if len(text) > max_chars else text
    chunks = _split_chunks(text, CHUNK_CHARS)

    def _run(chunk: str) -> str:
        return _call_polish(api_base_url, api_key, model, chunk, retries, stats)

    if max_workers and max_workers > 1 and len(chunks) > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            polished_parts = list(executor.map(_run, chunks))
    else:
        polished_parts = [_run(chunk) for chunk in chunks]

    return "".join(polished_parts)


def translate_transcript(text: str, api_key: Optional[str] = None,
                         api_base_url: Optional[str] = None,
                         model: Optional[str] = None,
                         max_chars: int = 20000,
                         max_workers: int = 4,
                         retries: int = _DEFAULT_RETRIES,
                         stats: Optional[list] = None) -> str:
    """Translate a foreign-language transcript into Simplified Chinese.

    Reuses the same OpenAI-compatible client / chunking / retry policy as
    polishing. Returns "" when translation is unavailable or any chunk fails:
    the caller must then keep the original text untouched (never overwrite it
    with a half-translated mix).
    """
    cfg = get_translate_config()
    api_key = api_key or cfg["api_key"]
    api_base_url = api_base_url or cfg["api_base_url"]
    model = model or cfg["model"]

    if not api_key or not api_base_url or not text:
        return ""

    text = text[:max_chars] if len(text) > max_chars else text
    chunks = _split_chunks(text, CHUNK_CHARS)

    def _run(chunk: str) -> str:
        chunk_stats: list = []
        out = _call_polish(
            api_base_url, api_key, model, chunk, retries, chunk_stats,
            system_prompt=TRANSLATE_SYSTEM_PROMPT,
        )
        if stats is not None:
            stats.extend(chunk_stats)
        # 任一分块失败即整体判失败：混着原文的“译文”不能落盘
        if not chunk_stats or not chunk_stats[0].get("ok"):
            return ""
        return out

    if max_workers and max_workers > 1 and len(chunks) > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            parts = list(executor.map(_run, chunks))
    else:
        parts = [_run(chunk) for chunk in chunks]

    if not parts or any(p == "" for p in parts):
        return ""

    translated = "".join(parts).strip()
    # 模型原样回吐原文＝没翻译，按失败处理，避免把原文当译文落盘
    return "" if translated == text.strip() else translated
