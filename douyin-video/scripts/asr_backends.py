#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unified speech-to-text backends for douyin transcript extraction.

Supported providers (switch via ASR_PROVIDER env var or WebUI settings):
1. siliconflow - SiliconFlow / any OpenAI-compatible /v1/audio/transcriptions
                 endpoint (default; also OpenAI, Groq, self-hosted whisper...)
2. dashscope   - Alibaba Bailian qwen3-asr (multimodal chat API)
3. ark         - Volcengine Ark chat completions with audio input
                 (doubao-seed models, base64 audio upload)

Config resolution priority per field:
  explicit arg > config file (web_ui_config.json) > env var > default

Env vars:
- API_KEY          : universal API key (any provider)
- ASR_PROVIDER     : siliconflow (default) | dashscope | ark
- ASR_MODEL        : model name override
- API_BASE_URL     : endpoint override
- DASHSCOPE_API_KEY / ARK_API_KEY : provider-specific key fallbacks
"""

import os
import json
import base64
import requests
from pathlib import Path
from typing import Optional

# Default configurations per provider
SILICONFLOW_API_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
SILICONFLOW_MODEL = "FunAudioLLM/SenseVoiceSmall"
DASHSCOPE_MODEL = "qwen3-asr-flash"
ARK_CHAT_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
ARK_MODEL = "doubao-seed-1-6-250615"

# Ark base64 audio limits
ARK_MAX_AUDIO_SIZE = 25 * 1024 * 1024  # 25MB

# Well-known provider metadata (used by UI and config file).
# Labels use ASCII to avoid source-encoding issues on Windows (GBK) systems.
PROVIDERS = {
    "siliconflow": {
        "label": "SiliconFlow",
        "default_model": SILICONFLOW_MODEL,
        "default_base_url": SILICONFLOW_API_URL,
        "key_env": "API_KEY",
    },
    "dashscope": {
        "label": "DashScope (Bailian)",
        "default_model": DASHSCOPE_MODEL,
        "default_base_url": "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
        "key_env": "DASHSCOPE_API_KEY",
    },
    "ark": {
        "label": "Volcengine Ark (Doubao)",
        "default_model": ARK_MODEL,
        "default_base_url": ARK_CHAT_URL,
        "key_env": "ARK_API_KEY",
    },
}

# Persistent config file: web_ui_config.json at project root. Structure:
# {
#   "active_provider": "siliconflow",
#   "providers": {
#     "siliconflow": {"api_key": "...", "model": "...", "api_base_url": "..."},
#     ...
#   }
# }
CONFIG_FILE = Path(__file__).resolve().parent.parent.parent / "web_ui_config.json"


def load_config_file() -> dict:
    """Load persistent WebUI config file if present."""
    try:
        if CONFIG_FILE.exists():
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_config_file(config: dict):
    """Persist WebUI config file."""
    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


class TranscriptionError(Exception):
    """Raised when a speech-to-text backend fails."""


def resolve_backend(
    api_key: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    api_base_url: Optional[str] = None,
) -> dict:
    """Resolve ASR backend config.

    Priority per field:
      explicit arg > config file (web_ui_config.json) > env var > default

    Returns dict: {provider, api_key, model, api_base_url}
    """
    file_config = load_config_file()
    provider = (
        provider
        or file_config.get('active_provider')
        or os.getenv('ASR_PROVIDER')
        or 'siliconflow'
    ).lower()

    if provider not in PROVIDERS:
        raise ValueError(f"Unknown ASR provider '{provider}'. Valid: {', '.join(PROVIDERS)}")

    file_entry = (file_config.get('providers') or {}).get(provider) or {}
    key_env = PROVIDERS[provider]['key_env']

    resolved_key = (
        api_key
        or file_entry.get('api_key')
        or os.getenv(key_env)
        or os.getenv('API_KEY')
    )

    if not resolved_key:
        raise ValueError(
            f"API key not set for provider '{provider}'. "
            f"Configure it in the WebUI settings, the config file, "
            f"or set {key_env} / API_KEY env var."
        )

    resolved_model = (
        model
        or file_entry.get('model')
        or os.getenv('ASR_MODEL')
        or PROVIDERS[provider]['default_model']
    )

    resolved_base = (
        api_base_url
        or file_entry.get('api_base_url')
        or os.getenv('API_BASE_URL')
        or PROVIDERS[provider]['default_base_url']
    )

    return {
        'provider': provider,
        'api_key': resolved_key,
        'model': resolved_model,
        'api_base_url': resolved_base,
    }


def transcribe_audio_file(
    audio_path: Path,
    api_key: str,
    provider: str = 'siliconflow',
    model: Optional[str] = None,
    api_base_url: Optional[str] = None,
    context: Optional[str] = None,
) -> str:
    """Transcribe a local audio file with the configured backend.

    This is the single entry point used by douyin_downloader / web UI / MCP.
    """
    config = resolve_backend(api_key, provider, model, api_base_url)
    backend = config['provider']
    audio_path = Path(audio_path)

    if backend == 'dashscope':
        return _transcribe_dashscope(audio_path, config['api_key'], config['model'], context)
    if backend == 'ark':
        return _transcribe_ark(audio_path, config['api_key'], config['model'], config['api_base_url'])
    return _transcribe_openai_compatible(audio_path, config['api_key'], config['model'], config['api_base_url'])


def _transcribe_openai_compatible(
    audio_path: Path,
    api_key: str,
    model: str,
    api_base_url: str,
) -> str:
    """SiliconFlow / OpenAI / Groq / any compatible transcriptions endpoint."""
    try:
        with open(audio_path, 'rb') as audio_file:
            files = {
                'file': (audio_path.name, audio_file, 'audio/mpeg'),
                'model': (None, model),
            }
            headers = {"Authorization": f"Bearer {api_key}"}
            response = requests.post(api_base_url, files=files, headers=headers, timeout=300)

        if response.status_code != 200:
            raise TranscriptionError(
                f"Transcription request failed (HTTP {response.status_code}): {response.text[:200]}"
            )
        result = response.json()
        if 'text' not in result:
            raise TranscriptionError(f"Unexpected transcription response: {response.text[:200]}")
        return result['text'] or "No text recognized"
    except TranscriptionError:
        raise
    except Exception as e:
        raise TranscriptionError(f"Error extracting text: {str(e)}")


def _transcribe_dashscope(
    audio_path: Path,
    api_key: str,
    model: str,
    context: Optional[str],
) -> str:
    """Alibaba Bailian qwen3-asr via dashscope SDK."""
    try:
        import dashscope
    except ImportError:
        raise TranscriptionError("dashscope package not installed: pip install dashscope")

    dashscope.api_key = api_key
    messages = [
        {"role": "system", "content": [{"text": context or ""}]},
        {"role": "user", "content": [{"audio": f"file://{audio_path.resolve()}"}]},
    ]
    response = dashscope.MultiModalConversation.call(
        api_key=api_key,
        model=model,
        messages=messages,
        result_format="message",
        asr_options={"enable_lid": True, "enable_itn": False},
    )
    if response.status_code != 200:
        raise TranscriptionError(f"dashscope API call failed: {response.message}")
    try:
        return response.output.choices[0].message.content[0].get("text", "") or "No text recognized"
    except (AttributeError, IndexError, TypeError):
        raise TranscriptionError("Unexpected dashscope response structure")


def _transcribe_ark(
    audio_path: Path,
    api_key: str,
    model: str,
    api_base_url: str,
) -> str:
    """Volcengine Ark chat completions with base64 audio input.

    Requires an Ark model that supports audio understanding
    (e.g. doubao-seed-1.6+ / doubao-seed-asr series).
    Audio must be <= 25MB for base64 upload.
    """
    size = audio_path.stat().st_size
    if size > ARK_MAX_AUDIO_SIZE:
        raise TranscriptionError(
            f"Audio file {size / 1024 / 1024:.1f}MB exceeds Ark base64 limit (25MB). "
            "Use a shorter segment or another provider."
        )

    audio_b64 = base64.b64encode(audio_path.read_bytes()).decode('utf-8')
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": f"data:audio/mpeg;base64,{audio_b64}",
                            "format": "mp3",
                        },
                    },
                    {
                        "type": "text",
                        "text": "Please transcribe the audio content verbatim. "
                                "Output only the transcribed text, no explanations.",
                    },
                ],
            }
        ],
        "stream": False,
    }
    try:
        response = requests.post(
            api_base_url,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=300,
        )
        if response.status_code != 200:
            raise TranscriptionError(
                f"Ark API request failed (HTTP {response.status_code}): {response.text[:200]}"
            )
        result = response.json()
        text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        return (text or "No text recognized").strip()
    except TranscriptionError:
        raise
    except Exception as e:
        raise TranscriptionError(f"Ark transcription error: {str(e)}")
