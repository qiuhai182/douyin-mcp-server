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
import re
import json
import base64
import requests
from collections import Counter
from pathlib import Path
from typing import Optional, List

# Default configurations per provider
SILICONFLOW_API_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
SILICONFLOW_MODEL = "FunAudioLLM/SenseVoiceSmall"
DASHSCOPE_MODEL = "qwen3-asr-flash"
ARK_CHAT_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
ARK_MODEL = "doubao-seed-1-6-250615"

# ASR quality gate (2026-11-03)
DEFAULT_LANGUAGE = os.getenv("ASR_LANGUAGE", "zh")
QUALITY_MIN_CJK_RATIO = float(os.getenv("ASR_MIN_CJK_RATIO", "0.5"))
QUALITY_MAX_REPEAT_RATIO = float(os.getenv("ASR_MAX_REPEAT_RATIO", "0.3"))
# 30：2026-11-06 小样本实测，12-29 字档几乎全是音乐/幻听文本，
# 下限放到 12 会把歌词当成功稿收进来（5/5 误收），故维持 30。
QUALITY_MIN_LENGTH = int(os.getenv("ASR_MIN_TEXT_LENGTH", "30"))
NO_TEXT_MARK = "No text recognized"

# 音乐/幻听过滤（2026-11-06）
# SenseVoice 的富文本会插入事件 emoji：🎼 表示 BGM/音乐段，🎵🎶 为音符。
# 实测存量 10857 条转写里 89% 含 🎼（带 BGM 的正常口播也会标），
# 因此不能「见标签即拒」；情绪 emoji（😊😡 等）与正常口播同现率更高，不参与判死。
# 分两档：
#   1) 文本 < ASR_MUSIC_SHORT_LEN：命中任一音乐标签即拒。这类样本本就过不了
#      30 字长度闸，此档只是把原因码从 too_short_text 换成独立的 music_lyric，
#      不新增任何拒绝（存量 <30 字带标签 89 条，全是音乐/幻听）。
#   2) 文本 >= ASR_MUSIC_SHORT_LEN：仅当「音乐标签数 / 非空白字数 >= 0.08」
#      才判纯音乐/幻听循环（存量 >=60 字 9683 条里仅 28 条命中，均为纯音乐）。
# 想改回见标签即拒，设 ASR_MUSIC_TAG_RATIO=0 即可。
MUSIC_EVENT_TAGS = "🎼🎵🎶"
MUSIC_SHORT_LEN = int(os.getenv("ASR_MUSIC_SHORT_LEN", "30"))
MUSIC_TAG_RATIO = float(os.getenv("ASR_MUSIC_TAG_RATIO", "0.08"))
# 歌词弱判据只做「第二条件」，默认开；设 ASR_LYRIC_HINT=0 可关。
# 只对短文本生效（<= ASR_LYRIC_HINT_MAX 字）：长口播里口头禅/重复句很常见，
# 实测存量 10.1% 的长转写有 8 字片段重复 >=3 次，若全量启用会大面积误杀。
LYRIC_HINT_ON = os.getenv("ASR_LYRIC_HINT", "1").strip().lower() not in (
    "0", "off", "false", "no")
LYRIC_HINT_MAX = int(os.getenv("ASR_LYRIC_HINT_MAX", "120"))


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


class AsrBalanceError(TranscriptionError):
    """ASR 账户余额不足（HTTP 402 / insufficient balance）。

    单独成类，让上层（批量／刷新 driver／WebUI）能与普通转写失败区分：
    命中即停止新任务、页面告警并提示充值，而不是把剩余视频逐条撞墙。
    """


# 402 之外，部分兼容端点把欠费包在 400/403 的响应体里
_BALANCE_HINTS = (
    "insufficient balance",
    "balance is insufficient",
    "insufficient_quota",
    "insufficient quota",
    "余额不足",
    "欠费",
    "arrearage",
)


def _raise_http_error(prefix: str, response) -> None:
    """HTTP 非 200：识别余额不足并抛专用异常，其余保持 TranscriptionError。"""
    body = (response.text or "")[:200]
    lowered = body.lower()
    if response.status_code == 402 or any(h in lowered for h in _BALANCE_HINTS):
        raise AsrBalanceError(
            f"ASR 账户余额不足（HTTP {response.status_code}）：{body}"
        )
    raise TranscriptionError(f"{prefix} (HTTP {response.status_code}): {body}")


class AudioTooShort(TranscriptionError):
    """音频过短/过小，不送 ASR（避免静音/音效触发幻觉）。"""


def _lyric_hint(text: str) -> bool:
    """歌词/幻听的弱判据：只作第二条件，单独命中不判死。

    - 8 字片段重复出现 >=3 次（副歌／幻听循环）
    - 语气词占比 >= 0.4（无实义语气串）

    只判短文本（<= LYRIC_HINT_MAX），长口播的重复口头禅不在此列。
    """
    chars = [c for c in text if not c.isspace()]
    if len(chars) < 12 or len(chars) > LYRIC_HINT_MAX:
        return False
    if chars:
        grams = Counter(text[i:i + 8] for i in range(max(0, len(text) - 7)))
        if grams and max(grams.values()) >= 3:
            return True
    fillers = "啊哦嗯呀嘿哈哎唉喔噢啦吧呢嘛"
    fill = sum(1 for c in chars if c in fillers)
    return fill / float(len(chars)) >= 0.4


def assess_transcript(text: str) -> dict:
    """Return a quality verdict for one ASR output.

    Rules (2026-11-06):
      - empty / 'No text recognized' -> no_text
      - 音乐标签：短文本（<30 字）命中即拒；长文本按密度 >= 0.08 拒
        -> music_lyric（独立原因码，不并入 too_short_text / low_cjk）
      - 歌词弱判据 + (音乐标签 或 low_cjk) -> music_lyric（可关，单独不判死）
      - CJK ratio < 0.5 -> low_cjk (foreign lyrics / filler)
      - repeated 4-gram ratio >= 0.3 -> repetitive (hallucination loops)
      - stripped length < 30 -> too_short_text
    """
    raw = (text or "").strip()
    if not raw or raw == NO_TEXT_MARK:
        return {"ok": False, "reason": "no_text", "cjk_ratio": 0.0,
                "repeat_ratio": 0.0, "length": 0, "quality": "no_text",
                "music_ratio": 0.0, "lyric_hint": False}

    chars = [c for c in raw if not c.isspace()]
    cjk = len(re.findall(r"[一-鿿]", raw))
    cjk_ratio = (cjk / len(chars)) if chars else 0.0

    shingles = [raw[i:i + 4] for i in range(max(0, len(raw) - 3))]
    repeat_ratio = 0.0
    if shingles:
        repeat_ratio = 1.0 - (len(set(shingles)) / float(len(shingles)))

    music_tags = sum(1 for c in raw if c in MUSIC_EVENT_TAGS)
    music_ratio = (music_tags / float(len(chars))) if chars else 0.0
    lyric_hint = _lyric_hint(raw) if LYRIC_HINT_ON else False

    reasons = []
    # 音乐标签分两档判（见文件头注释）；歌词弱判据必须与音乐标签或低 CJK 同现
    # 才拒，避免单靠它误杀真人短句。
    if music_tags and (len(raw) < MUSIC_SHORT_LEN or music_ratio >= MUSIC_TAG_RATIO):
        reasons.append("music_lyric")
    elif lyric_hint and (music_tags or cjk_ratio < QUALITY_MIN_CJK_RATIO):
        reasons.append("music_lyric")
    if cjk_ratio < QUALITY_MIN_CJK_RATIO:
        reasons.append("low_cjk")
    if repeat_ratio >= QUALITY_MAX_REPEAT_RATIO:
        reasons.append("repetitive")
    if len(raw) < QUALITY_MIN_LENGTH:
        reasons.append("too_short_text")

    return {
        "ok": not reasons,
        "reason": reasons[0] if reasons else "ok",
        "cjk_ratio": round(cjk_ratio, 4),
        "repeat_ratio": round(repeat_ratio, 4),
        "length": len(raw),
        "quality": "ok" if not reasons else "+".join(reasons),
        "music_ratio": round(music_ratio, 4),
        "lyric_hint": lyric_hint,
    }


def _check_text_or_raise(text: str, provider: str) -> str:
    verdict = assess_transcript(text)
    if not verdict["ok"]:
        raise TranscriptionError(
            "asr_suspect[%s]: %s (cjk=%.2f repeat=%.2f len=%d music=%.3f hint=%s)" % (
                provider, verdict["quality"], verdict["cjk_ratio"],
                verdict["repeat_ratio"], verdict["length"],
                verdict.get("music_ratio", 0.0), verdict.get("lyric_hint", False))
        )
    return text.strip()


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


def _default_fallbacks(primary: str) -> List[str]:
    order = {
        "dashscope": ["ark", "siliconflow"],
        "ark": ["dashscope", "siliconflow"],
        "siliconflow": ["dashscope", "ark"],
    }
    env = os.getenv("ASR_FALLBACKS", "").strip()
    if env:
        return [p.strip().lower() for p in env.split(",") if p.strip().lower() != primary]
    return order.get(primary, [])


def transcribe_audio_file(
    audio_path: Path,
    api_key: str,
    provider: str = 'siliconflow',
    model: Optional[str] = None,
    api_base_url: Optional[str] = None,
    context: Optional[str] = None,
    language: Optional[str] = DEFAULT_LANGUAGE,
    fallbacks: Optional[List[str]] = None,
) -> str:
    """Transcribe a local audio file with provider fallback + quality gate.

    Order: primary provider -> ASR_FALLBACKS (default dashscope/ark/siliconflow
    order). Providers without a configured key are skipped. A backend that
    returns empty / foreign / repetitive text is treated as failure, so the
    caller never writes a "successful" transcript from a song or silence.
    """
    audio_path = Path(audio_path)
    configs = [resolve_backend(api_key, provider, model, api_base_url)]
    for name in (fallbacks if fallbacks is not None else _default_fallbacks(configs[0]['provider'])):
        try:
            configs.append(resolve_backend(None, name, None, None))
        except Exception:
            continue

    errors = []
    for cfg in configs:
        backend = cfg['provider']
        try:
            if backend == 'dashscope':
                text = _transcribe_dashscope(audio_path, cfg['api_key'], cfg['model'], context, language)
            elif backend == 'ark':
                text = _transcribe_ark(audio_path, cfg['api_key'], cfg['model'], cfg['api_base_url'], language, context)
            else:
                text = _transcribe_openai_compatible(
                    audio_path, cfg['api_key'], cfg['model'], cfg['api_base_url'], language, context)
            return _check_text_or_raise(text, backend)
        except AsrBalanceError:
            raise  # 欠费立即停批，不回落掩盖
        except Exception as e:
            errors.append("%s: %s" % (backend, str(e)[:160]))
    raise TranscriptionError("All ASR backends failed or produced suspect text -> " + " | ".join(errors))


def _transcribe_openai_compatible(
    audio_path: Path,
    api_key: str,
    model: str,
    api_base_url: str,
    language: Optional[str] = None,
    context: Optional[str] = None,
) -> str:
    """SiliconFlow / OpenAI / Groq / any compatible transcriptions endpoint."""
    try:
        with open(audio_path, 'rb') as audio_file:
            files = {
                'file': (audio_path.name, audio_file, 'audio/mpeg'),
                'model': (None, model),
            }
            if language:
                files['language'] = (None, language)
            if context:
                files['prompt'] = (None, context[:200])
            headers = {"Authorization": f"Bearer {api_key}"}
            response = requests.post(api_base_url, files=files, headers=headers, timeout=300)

        if response.status_code != 200:
            _raise_http_error("Transcription request failed", response)
        result = response.json()
        if 'text' not in result:
            raise TranscriptionError(f"Unexpected transcription response: {response.text[:200]}")
        if not (result['text'] or "").strip():
            raise TranscriptionError("No text recognized")
        return result['text']
    except TranscriptionError:
        raise
    except Exception as e:
        raise TranscriptionError(f"Error extracting text: {str(e)}")


def _transcribe_dashscope(
    audio_path: Path,
    api_key: str,
    model: str,
    context: Optional[str],
    language: Optional[str] = DEFAULT_LANGUAGE,
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
    asr_options = {"enable_lid": False, "enable_itn": True}
    if language:
        asr_options["language"] = language
    else:
        asr_options["enable_lid"] = True
    response = dashscope.MultiModalConversation.call(
        api_key=api_key,
        model=model,
        messages=messages,
        result_format="message",
        asr_options=asr_options,
    )
    if response.status_code != 200:
        raise TranscriptionError(f"dashscope API call failed: {response.message}")
    try:
        text = response.output.choices[0].message.content[0].get("text", "")
    except (AttributeError, IndexError, TypeError):
        raise TranscriptionError("Unexpected dashscope response structure")
    if not (text or "").strip():
        raise TranscriptionError("No text recognized")
    return text


def _transcribe_ark(
    audio_path: Path,
    api_key: str,
    model: str,
    api_base_url: str,
    language: Optional[str] = DEFAULT_LANGUAGE,
    context: Optional[str] = None,
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
                        "text": ("音频为中文，请逐字转写中文；若音频是外语歌曲/纯音乐，"
                                 "只输出语言标记或空，不要编造中文。"
                                 + ("\n上下文：" + context[:300] if context else "")
                                 + "\nOutput only the transcribed text, no explanations."),
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
            _raise_http_error("Ark API request failed", response)
        result = response.json()
        text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not (text or "").strip():
            raise TranscriptionError("No text recognized")
        return text.strip()
    except TranscriptionError:
        raise
    except Exception as e:
        raise TranscriptionError(f"Ark transcription error: {str(e)}")


def selftest() -> int:
    """离线自测：不联网、不读凭据、不依赖任何后端。

    real_short_sentence 断言的是「不被音乐判据误杀」；12-29 字仍会被既有的
    30 字长度闸判 too_short_text，这是既有策略，本过滤器不改它。
    """
    cases = [
        ("music_tag_short", "🎼是谁的？あ。🎼不能分.", "music_lyric"),
        ("music_tag_loop", "🎼你是我的小呀小苹果，怎么爱你都不嫌多。"
                           "🎼你是我的小呀小苹果，怎么爱你都不嫌多。"
                           "🎼你是我的小呀小苹果，怎么爱你都不嫌多。", "music_lyric"),
        ("music_tag_dense_long", "🎼 Yeah.🎼 Yeah.🎼 Yeah.🎼 Yeah.🎼 Yeah."
                                 "🎼 Yeah.🎼 Yeah.🎼 Yeah.🎼 Yeah.", "music_lyric"),
        ("normal_speech_with_bgm", "🎼今天跟大家聊一个特别有意思的话题，就是我们在写作的时候，"
                                   "到底应该先想清楚人物，还是先把情节推下去。", "ok"),
        ("real_short_sentence", "这次真的下定决心了，明天就开始动手写第一本书。", "not_music_lyric"),
        ("lyric_hint_without_tag", "哈哈哈哈哈哈哈哈哈哈哈哈，太好玩了吧，我真的笑死了。", "not_music_lyric"),
        ("empty", "", "no_text"),
    ]
    failed = 0
    for name, text, expect in cases:
        v = assess_transcript(text)
        quality = v["quality"]
        if expect == "ok":
            good = v["ok"]
        elif expect == "not_music_lyric":
            good = "music_lyric" not in quality
        else:
            good = v["reason"] == expect
        failed += 0 if good else 1
        print("%s %-24s len=%-3d cjk=%.2f music=%.3f hint=%-5s -> %s (%s)" % (
            "PASS" if good else "FAIL", name, v["length"], v["cjk_ratio"],
            v["music_ratio"], v["lyric_hint"], quality, expect))
    print("SELFTEST %s (%d/%d)" % ("OK" if not failed else "FAILED",
                                   len(cases) - failed, len(cases)))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(selftest())
