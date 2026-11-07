#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真数据阶段耗时统计：从 profile_cache.json 取真实抖音视频，逐条跑
parse -> download -> ffmpeg -> ASR ->(polish) -> 写 md，统计五段分布。

用法（在 douyin-mcp-server 目录下）：
    .venv\\Scripts\\python.exe -B scripts\\measure_stage_timing.py --count 20
    .venv\\Scripts\\python.exe -B scripts\\measure_stage_timing.py --count 20 --author 卢诗翰

说明：
  * 只从缓存里挑 history 状态为 ok 的视频（已知有口播、能过质量闸），
    避免 20 条里有大量图集/音乐视频拉高失败率。
  * 每条都走真实的 download_video / extract_audio / extract_text_from_audio，
    产物写到临时目录，不覆盖生产 output/、不改 history。
  * 串行执行（不并发），避免 CPU/带宽争抢把阶段耗时搅混。
  * 结果同时写入 --json 指定的文件（默认临时目录）。
"""
import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "douyin-video" / "scripts"))

import operation_logger as op                       # noqa: E402
from operation_logger import (                       # noqa: E402
    StageTrace, new_batch_id, stage_fields, log_video_result,
)
from asr_backends import resolve_backend, load_config_file   # noqa: E402
from batch_extractor import TranscriptHistory, _write_transcript_md, _safe_file_size  # noqa: E402
from douyin_downloader import DouyinProcessor        # noqa: E402

STAGES = ["resolve", "download", "audio", "asr", "parse", "kb_write"]


def _pick(count: int, author: str):
    data = json.loads((ROOT / "profile_cache.json").read_text(encoding="utf-8"))
    profiles = data.get("profiles") or data
    hist = TranscriptHistory()
    picked, seen = [], set()
    for key, prof in profiles.items():
        if author and prof.get("nickname") != author:
            continue
        for v in prof.get("videos") or []:
            aid = str(v.get("aweme_id") or "")
            if not aid or aid in seen:
                continue
            if hist.status_of(aid) != "ok":
                continue
            seen.add(aid)
            picked.append((aid, prof.get("nickname") or "", v.get("url") or ""))
            if len(picked) >= count:
                return picked
    return picked


def _pick_mixed(count: int):
    """按已知音频时长分层抽样（短/中/长），避免整批全是超长视频。

    duration 来自 output/transcripts_library.jsonl 的历史记录（不是猜的）。
    各桶配额 = 8 / 7 / 5 / 0 条（>8 分钟的长视频 ASR 需几十次分段请求，
    单条十几分钟以上，本批不做，避免整批跑不完）。
    """
    lib = ROOT / "output" / "transcripts_library.jsonl"
    durations = {}
    if lib.exists():
        for line in lib.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("duration") and rec.get("video_id"):
                durations[str(rec["video_id"])] = float(rec["duration"])

    data = json.loads((ROOT / "profile_cache.json").read_text(encoding="utf-8"))
    profiles = data.get("profiles") or data
    hist = TranscriptHistory()
    buckets = {"<60": [], "60-180": [], "180-480": [], ">480": []}
    seen = set()
    for key, prof in profiles.items():
        for v in prof.get("videos") or []:
            aid = str(v.get("aweme_id") or "")
            if not aid or aid in seen:
                continue
            seen.add(aid)
            if hist.status_of(aid) != "ok" or aid not in durations:
                continue
            d = durations[aid]
            name = ("<60" if d < 60 else "60-180" if d < 180
                    else "180-480" if d < 480 else ">480")
            buckets[name].append((aid, prof.get("nickname") or "", v.get("url") or ""))
    quota = {"<60": 8, "60-180": 7, "180-480": 5, ">480": 0}
    out = []
    for name, need in quota.items():
        out.extend(buckets[name][:need])
    # 补齐到目标条数（某桶不足时从其它桶借）
    if len(out) < count:
        used = {x[0] for x in out}
        for name in ("60-180", "<60", "180-480", ">480"):
            for item in buckets[name]:
                if item[0] not in used:
                    out.append(item)
                    used.add(item[0])
                    if len(out) >= count:
                        break
            if len(out) >= count:
                break
    return out[:count]


def _event(trace, action):
    for ev in trace._events:
        if ev["action"] == action:
            return ev["fields"]
    return {}


def _pct(values, q):
    if not values:
        return None
    values = sorted(values)
    idx = min(len(values) - 1, max(0, int(round((len(values) - 1) * q))))
    return values[idx]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--author", default="")
    ap.add_argument("--mode", default="mixed", choices=["mixed", "first"],
                    help="mixed=按已知时长分层抽样（默认）；first=按缓存顺序取前 N 条")
    ap.add_argument("--json", default="")
    ap.add_argument("--resolve-only", action="store_true",
                    help="只测 parse_share_url（取播放直链），不下载/不转写")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="stage_measure_"))
    op._LOG_DIR = tmp / "log"
    op.LOG_FILE = op._LOG_DIR / "operation_logs.txt"
    op.VIDEO_LOG_FILE = op._LOG_DIR / "video_results.jsonl"

    backend = resolve_backend("")
    polish_cfg = (load_config_file().get("polish") or {})
    polish_on = bool(polish_cfg.get("enabled"))
    batch_id = new_batch_id()
    targets = (_pick_mixed(args.count) if args.mode == "mixed"
               else _pick(args.count, args.author))
    print(f"[target] {len(targets)} 条 | batch_id={batch_id} | "
          f"asr={backend['provider']}/{backend['model']} | polish={polish_on}",
          flush=True)

    rows = []
    for n, (aid, author, share_url) in enumerate(targets, 1):
        trace = StageTrace(video_id=aid, batch_id=batch_id, worker_id="measure")
        proc = DouyinProcessor(backend["api_key"], provider=backend["provider"],
                               model=backend["model"],
                               api_base_url=backend["api_base_url"] or "")
        proc.trace = trace
        row = {"video_id": aid, "author": author, "status": "ok", "stage_fail": ""}
        t_all = time.perf_counter()
        try:
            trace.start("resolve")
            info = proc.parse_share_url(share_url)
            trace.video_id = str(info.get("video_id") or aid)
            trace.emit("video.resolve", duration_ms=trace.end("resolve"))
            if args.resolve_only:
                row["status"] = "ok"
                row.update({"resolve_ms": trace.durations.get("resolve"),
                            "total_ms": round((time.perf_counter() - t_all) * 1000.0, 1)})
                trace.flush()
                rows.append(row)
                print(f"[{n}/{len(targets)}] {aid} resolve={row['resolve_ms']}ms",
                      flush=True)
                continue
            row["audio_seconds_expected"] = None
            video_path = proc.download_video(info, show_progress=False)
            audio_path = proc.extract_audio(video_path, show_progress=False)
            try:
                text = proc.extract_text_from_audio(audio_path, show_progress=False)
            finally:
                proc.cleanup_files(video_path, audio_path)
            if polish_on:
                from transcript_polish import polish_transcript
                _in = len(text)
                trace.start("parse")
                _px = []
                text = polish_transcript(text, stats=_px)
                _tries = sum(s["attempts"] for s in _px) or 1
                trace.emit("parse.extract",
                           status="ok" if all(s["ok"] for s in _px) else "error",
                           mode="llm", model=(polish_cfg.get("model") or ""),
                           input_chars=_in, output_chars=len(text),
                           retry=max(0, _tries - (len(_px) or 1)),
                           duration_ms=trace.end("parse"))
            md = tmp / f"{aid}.md"
            trace.start("kb_write")
            _write_transcript_md(md, info.get("title") or "", aid, author, text)
            trace.emit("kb.write", path=str(md), bytes=_safe_file_size(md),
                       duration_ms=trace.end("kb_write"))
            asr_ev = _event(trace, "asr.request")
            row["audio_seconds"] = asr_ev.get("audio_seconds")
            row["text_chars"] = len(text)
        except Exception as e:
            row["status"] = "fail"
            row["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            done = set(trace.durations)
            row["stage_fail"] = ";".join(s for s in STAGES if s not in done) or "unknown"
        dl = _event(trace, "video.download.end")
        row.update({
            "download_ms": trace.durations.get("download"),
            "audio_ms": trace.durations.get("audio"),
            "asr_ms": trace.durations.get("asr"),
            "parse_ms": trace.durations.get("parse"),
            "kb_write_ms": trace.durations.get("kb_write"),
            "download_retry": dl.get("retry"),
            "http_status": dl.get("http_status"),
            "bytes": dl.get("bytes"),
            "total_ms": round((time.perf_counter() - t_all) * 1000.0, 1),
        })
        trace.flush()
        log_video_result(status=row["status"], author=author, video_id=aid,
                         title="", trigger="stage-measure",
                         error=row.get("error", ""),
                         **stage_fields(trace.result_fields()))
        rows.append(row)
        print(f"[{n}/{len(targets)}] {aid} {row['status']} "
              + " ".join(f"{s}={row.get(s + '_ms', '-')}" for s in STAGES)
              + f" retry={row.get('download_retry')}", flush=True)

    # ---- 统计 ----
    ok = [r for r in rows if r["status"] == "ok"]
    summary = {"count": len(rows), "ok": len(ok),
               "fail": len(rows) - len(ok),
               "fail_rate": round((len(rows) - len(ok)) / max(1, len(rows)), 3),
               "polish_enabled": polish_on, "batch_id": batch_id,
               "stages": {}, "ratio": {}}
    sums = {s: 0.0 for s in STAGES}
    for s in STAGES:
        vals = [r[s + "_ms"] for r in ok if r.get(s + "_ms") is not None]
        if vals:
            summary["stages"][s] = {
                "n": len(vals), "median": _pct(vals, 0.5),
                "p90": _pct(vals, 0.9), "max": max(vals),
                "mean": round(statistics.mean(vals), 1),
                "sum": round(sum(vals), 1),
            }
            sums[s] = sum(vals)
    grand = sum(sums.values())
    if grand:
        summary["ratio"] = {s: round(sums[s] / grand, 3) for s in STAGES}
    retries = [r.get("download_retry") or 0 for r in ok]
    summary["download_retry_gt0"] = sum(1 for x in retries if x > 0)
    summary["download_retry_max"] = max(retries) if retries else 0
    summary["cache_hit_lt_20ms"] = sum(
        1 for r in ok if (r.get("download_ms") or 1e9) < 20)

    # 按时长分桶看占比：判断“哪段最耗时”是否随视频长度变化
    buckets = {"<60s": [], "60-180s": [], "180-480s": [], ">480s": []}
    for r in ok:
        sec = float(r.get("audio_seconds") or 0)
        name = ("<60s" if sec < 60 else "60-180s" if sec < 180
                else "180-480s" if sec < 480 else ">480s")
        buckets[name].append(r)
    summary["by_duration"] = {}
    for name, group in buckets.items():
        if not group:
            continue
        sub = {}
        tot = 0.0
        for s in STAGES:
            vals = [g[s + "_ms"] for g in group if g.get(s + "_ms") is not None]
            sub[s] = round(statistics.mean(vals), 1) if vals else None
            tot += sum(vals)
        summary["by_duration"][name] = {
            "n": len(group), "mean_ms": sub,
            "ratio": {s: round(sum(g.get(s + "_ms") or 0 for g in group) / tot, 3)
                      for s in STAGES} if tot else {},
        }

    # 未被四/五段覆盖的残差：parse_share_url（取播放地址）+ 质检/去重判断
    residuals = []
    for r in ok:
        used = sum(r.get(s + "_ms") or 0 for s in STAGES)
        r["residual_ms"] = round((r.get("total_ms") or 0) - used, 1)
        residuals.append(r["residual_ms"])
    if residuals:
        summary["residual_ms"] = {
            "median": _pct(residuals, 0.5), "p90": _pct(residuals, 0.9),
            "max": max(residuals), "mean": round(statistics.mean(residuals), 1),
            "ratio": round(sum(residuals) / max(1, grand + sum(residuals)), 3),
        }

    print("\n=== summary ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    out = Path(args.json) if args.json else (tmp / "summary.json")
    out.write_text(json.dumps({"summary": summary, "rows": rows},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[json] {out}")
    print(f"[log] {op.LOG_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
