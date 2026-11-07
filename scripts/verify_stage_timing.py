#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段耗时埋点验证（本地 fixture，不依赖抖音/外网）。

跑 N 条本地假视频，走真实的 download_video / extract_audio /
extract_text_from_audio / _write_transcript_md，然后把其中一条视频的四个
阶段耗时打印出来，并输出它落到 operation_logs.txt / video_results.jsonl
的原始行。

用法（在 douyin-mcp-server 目录下）：
    .venv\\Scripts\\python.exe -B scripts\\verify_stage_timing.py
    .venv\\Scripts\\python.exe -B scripts\\verify_stage_timing.py --count 5
    .venv\\Scripts\\python.exe -B scripts\\verify_stage_timing.py --live-asr

真实 / 替代说明：
  download_ms   真实：本地 HTTP 服务，真实字节数与 http_status
  audio_ms      真实：bundled ffmpeg 抽音
  asr_ms        真实墙钟；默认请求体是本地替身（sleep 0.3s）；
                --live-asr 且已配置 Key 时才真调外部 ASR
  kb_write_ms   真实：写 md

日志写到临时目录，不污染生产 log/。
"""
import argparse
import json
import os
import sys
import tempfile
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "douyin-video" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import operation_logger as op                      # noqa: E402
from operation_logger import (                      # noqa: E402
    StageTrace, new_batch_id, stage_fields, log_video_result,
)

STAGES = ("download_ms", "audio_ms", "asr_ms", "parse_ms", "kb_write_ms")


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def _make_fixture(ffmpeg_exe: Path, out: Path, seconds: int = 10) -> None:
    import subprocess
    subprocess.run(
        [str(ffmpeg_exe), "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=size=640x360:rate=25:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "libx264", "-preset", "veryfast", "-b:v", "600k",
         "-c:a", "aac", "-shortest", str(out)],
        check=True, capture_output=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=3, help="跑几条（默认 3）")
    ap.add_argument("--live-asr", action="store_true",
                    help="真实调用外部 ASR（需要已配置 API Key）")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="stage_timing_"))
    # 把日志重定向到临时目录，避免测试数据混进生产 log/
    op._LOG_DIR = tmp / "log"
    op.LOG_FILE = op._LOG_DIR / "operation_logs.txt"
    op.VIDEO_LOG_FILE = op._LOG_DIR / "video_results.jsonl"

    ffmpeg_exe = next((ROOT / "ffmpeg").rglob("ffmpeg.exe"), None)
    if ffmpeg_exe is None:
        print("找不到 bundled ffmpeg.exe")
        return 2

    fixtures = tmp / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    print(f"[fixture] 生成 {args.count} 条本地视频 -> {fixtures}")
    for i in range(args.count):
        _make_fixture(ffmpeg_exe, fixtures / f"v{i + 1}.mp4")

    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0), lambda *a, **kw: _QuietHandler(*a, directory=str(fixtures), **kw))
    port = httpd.server_address[1]
    httpd_thread = __import__("threading").Thread(target=httpd.serve_forever, daemon=True)
    httpd_thread.start()

    from douyin_downloader import DouyinProcessor
    from batch_extractor import _write_transcript_md, _safe_file_size
    from asr_backends import resolve_backend

    # 真实凭据（只从配置读取；--live-asr 时才会真的发给外部 ASR）
    backend = resolve_backend(os.getenv("STAGE_VERIFY_KEY", "") or "",
                              provider=os.getenv("STAGE_VERIFY_PROVIDER") or None,
                              model=os.getenv("STAGE_VERIFY_MODEL") or None)
    print(f"[asr] provider={backend['provider']} model={backend['model']} "
          f"key_len={len(backend.get('api_key') or '')}")

    batch_id = new_batch_id()
    rows = []
    for i in range(args.count):
        vid = f"verify{i + 1:02d}"
        trace = StageTrace(video_id=vid, batch_id=batch_id,
                           worker_id="verify-script")
        proc = DouyinProcessor(backend["api_key"],
                               provider=backend["provider"],
                               model=backend["model"],
                               api_base_url=backend["api_base_url"] or "")
        proc.trace = trace
        if not args.live_asr:
            # 本地替身：只替换“请求体”，阶段墙钟仍是真实测量
            proc.transcribe_single_audio = lambda p: (
                time.sleep(0.30), "本地替身转写文本。")[1]

        video_info = {
            "video_id": vid,
            "title": f"验证视频 {i + 1}",
            "url": f"http://127.0.0.1:{port}/v{i + 1}.mp4",
        }
        started = time.perf_counter()
        video_path = proc.download_video(video_info, show_progress=False)
        audio_path = proc.extract_audio(video_path, show_progress=False)
        text = proc.extract_text_from_audio(audio_path, show_progress=False)

        # 第五段：文案解析/清洗（存在则是 LLM 清洗链，见报告说明）
        from asr_backends import load_config_file as _load_cfg
        pcfg = (_load_cfg().get("polish") or {})
        if pcfg.get("enabled"):
            from transcript_polish import polish_transcript
            _in_chars = len(text)
            trace.start("parse")
            _px = []
            text = polish_transcript(text, stats=_px)
            _tries = sum(s["attempts"] for s in _px) or 1
            trace.emit("parse.extract",
                       status="ok" if all(s["ok"] for s in _px) else "error",
                       mode="llm", model=(pcfg.get("model") or ""),
                       input_chars=_in_chars, output_chars=len(text),
                       retry=max(0, _tries - (len(_px) or 1)),
                       duration_ms=trace.end("parse"))

        md_path = tmp / f"{vid}.md"
        trace.start("kb_write")
        _write_transcript_md(md_path, video_info["title"], vid,
                             "本地fixture", text)
        trace.emit("kb.write", path=str(md_path),
                   bytes=_safe_file_size(md_path),
                   duration_ms=trace.end("kb_write"))
        total_ms = round((time.perf_counter() - started) * 1000.0, 1)

        trace.flush()
        log_video_result(status="ok", author="本地fixture", video_id=vid,
                         title=video_info["title"], trigger="stage-timing-verify",
                         **stage_fields(trace.result_fields()))
        proc.cleanup_files(video_path, audio_path)
        rows.append((vid, trace.durations, total_ms))
        print(f"[{i + 1}/{args.count}] {vid} " + " ".join(
            f"{k}={trace.durations.get(k[:-3], 'N/A')}ms" for k in STAGES))

    httpd.shutdown()

    print("\n=== 逐条阶段耗时（ms） ===")
    header = f"{'video_id':<10}" + "".join(f"{s:>14}" for s in STAGES) + f"{'sum':>12}"
    print(header)
    for vid, durations, _total in rows:
        line = f"{vid:<10}" + "".join(
            f"{str(durations.get(s[:-3], '-')):>14}" for s in STAGES)
        line += f"{sum(durations.values()):>12.1f}"
        print(line)

    print(f"\n=== {rows[0][0]} 的埋点事件（operation_logs.txt 原文）===")
    log_lines = []
    if op.LOG_FILE.exists():
        log_lines = [ln for ln in op.LOG_FILE.read_text(encoding="utf-8").splitlines()
                     if ln.strip()]
    matched = [ln for ln in log_lines if f'"video_id": "{rows[0][0]}"' in ln]
    print("\n".join(matched) if matched else "(没有匹配事件)")

    print(f"\n=== video_results.jsonl 中 {rows[0][0]} 的记录 ===")
    vlog = op.VIDEO_LOG_FILE
    if vlog.exists():
        recs = [json.loads(ln) for ln in vlog.read_text(encoding="utf-8").splitlines()
                if ln.strip()]
        for rec in recs:
            if rec.get("video_id") == rows[0][0]:
                print(json.dumps(rec, ensure_ascii=False, indent=2))
    print(f"\n(临时日志目录: {op._LOG_DIR})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
