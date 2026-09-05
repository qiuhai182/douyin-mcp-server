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
                 chunk: str, retries: int) -> str:
    """Polish a single chunk with retry + backoff. Returns the raw chunk on failure."""
    attempts = max(1, retries)
    for attempt in range(attempts):
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
                        {"role": "system", "content": POLISH_SYSTEM_PROMPT},
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
                    return chunk
                cleaned = _clean_output(content)
                return cleaned or chunk
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
                return chunk
        except requests.exceptions.RequestException as e:
            logger.warning("Polish: request error (attempt %d/%d): %s",
                           attempt + 1, attempts, e)
        except Exception as e:
            logger.warning("Polish: unexpected error (attempt %d/%d): %s",
                           attempt + 1, attempts, e)

        if attempt < attempts - 1:
            time.sleep(min(2 ** attempt, 8))

    return chunk


def polish_transcript(text: str, api_key: Optional[str] = None,
                      api_base_url: Optional[str] = None,
                      model: Optional[str] = None,
                      max_chars: int = 20000,
                      max_workers: int = 4,
                      retries: int = _DEFAULT_RETRIES) -> str:
    """Polish raw ASR text with an LLM. Long texts are processed in chunks.

    Never raises on failure - returns the original text so extraction
    never breaks because of polishing.
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
        return _call_polish(api_base_url, api_key, model, chunk, retries)

    if max_workers and max_workers > 1 and len(chunks) > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            polished_parts = list(executor.map(_run, chunks))
    else:
        polished_parts = [_run(chunk) for chunk in chunks]

    return "".join(polished_parts)
