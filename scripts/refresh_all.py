#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
全 UP 刷新 driver：进程内 import batch_extractor，串行遍历 authors.json
里每个 UP 的 profile_url，增量刷新（force=False），已完成的视频自动跳过。

- 不依赖 WebUI 锁系统（不调 WebUI API）
- API 配置自动从 web_ui_config.json 读取
- Chrome profile dir（.douyin_profile/）独占，避免多进程抢 cookie DB
  —— run_all_refresh.bat 会先停托盘，driver 跑完自动重启

后台跑：
    run_all_refresh.bat
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"
LOG_DIR = OUTPUT / "刷新日志"
LOG_DIR.mkdir(exist_ok=True)
LIVE_LOG = LOG_DIR / "driver_live.log"
try:
    if LIVE_LOG.exists() and LIVE_LOG.stat().st_size > 10 * 1024 * 1024:
        LIVE_LOG.unlink()
except Exception:
    pass
_LIVE_F = open(LIVE_LOG, "a", encoding="utf-8", buffering=1)

# 让 douyin-video/scripts 下的模块能被 import
sys.path.insert(0, str(ROOT / "douyin-video" / "scripts"))


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    try:
        _LIVE_F.write(line + "\n")
        _LIVE_F.flush()
    except Exception:
        pass


def main():
    force = False
    workers = 3
    for a in sys.argv[1:]:
        if a in ("-f", "--force"):
            force = True
        else:
            try:
                workers = int(a)
            except ValueError:
                pass

    log("=== 全 UP 刷新 driver 启动 ===")
    log(f"workers={workers}  force={force}  output={OUTPUT}")
    log("增量刷新 = force=False，已完整解析的视频自动跳过")

    authors_path = ROOT / "authors.json"
    if not authors_path.exists():
        log(f"FATAL: 找不到 {authors_path}")
        sys.exit(1)

    authors = json.loads(authors_path.read_text(encoding="utf-8"))
    log(f"UP 数量: {len(authors)}")

    # 延迟 import：让 sys.path 设置先完成
    from batch_extractor import batch_extract, rename_transcripts_numbered

    # 每个 UP 的进度回调
    def on_progress(evt):
        stage = evt.get("stage", "")
        if stage == "list":
            log(f"    list found={evt.get('found')}")
        elif stage == "extract":
            idx = evt.get("index", "?")
            tot = evt.get("total", "?")
            status = evt.get("status", "")
            aweme_id = evt.get("aweme_id", "")
            title = (evt.get("title") or "")[:28]
            marker = {"ok": "✓", "skip": "-", "fail": "✗"}.get(status, "·")
            # throttle: only print every 5th or on status change
            if status != "skip" or idx == 1 or idx == tot or (isinstance(idx, int) and idx % 5 == 0):
                log(f"    [{idx}/{tot}] {marker} {status}  {aweme_id}  {title}")
        elif stage == "error":
            log(f"    ERROR: {evt.get('error', '')[:300]}")
        elif stage == "done":
            pass  # handled after the call returns

    names = list(authors.keys())
    summaries = []
    t_start = time.time()

    for i, name in enumerate(names, 1):
        entry = authors[name]
        url = entry.get("profile_url", "")
        if not url:
            log(f"[{i}/{len(names)}] SKIP {name}: 无 profile_url")
            summaries.append({"author": name, "skipped": "no url"})
            continue

        log(f"[{i}/{len(names)}] ▶ {name}  workers={workers} force={force}")
        t0 = time.time()
        try:
            summary = batch_extract(
                url,
                output_dir=str(OUTPUT),
                force=force,
                workers=workers,
                headless=True,
                use_cache=False,
                retry_failed=True,
                delay_seconds=3.0,
                on_progress=on_progress,
            )
            log(f"  batch_extract returned, keys={list(summary.keys())}")
            # 重新编号 + 重写目录（幂等）
            author_dir = OUTPUT / name
            if author_dir.exists():
                try:
                    n = rename_transcripts_numbered(author_dir)
                    log(f"  rename_transcripts_numbered -> {n} files")
                except Exception as e2:
                    log(f"  rename_transcripts_numbered ERROR: {e2}")
                    traceback.print_exc()
            duration = int(time.time() - t0)
            ok = summary.get("ok", 0)
            skip = summary.get("skip", 0)
            fail = summary.get("fail", 0)
            resumed = summary.get("resumed", 0)
            log(f"  ✓ {name}  ok={ok} skip={skip} fail={fail} resumed={resumed}  ({duration}s)")
            summary["author"] = name
            summary["duration_s"] = duration
            summaries.append(summary)
        except Exception as e:
            log(f"  ✗ FATAL {name}: {e}")
            traceback.print_exc()
            summaries.append({"author": name, "error": str(e),
                               "duration_s": int(time.time() - t0)})

    total_s = int(time.time() - t_start)
    ok_total = sum(int(s.get("ok") or 0) for s in summaries)
    skip_total = sum(int(s.get("skip") or 0) for s in summaries)
    fail_total = sum(int(s.get("fail") or 0) for s in summaries)
    resumed_total = sum(int(s.get("resumed") or 0) for s in summaries)

    log(f"\n=== 全 UP 刷新完成 ===")
    log(f"耗时 {total_s}s ({total_s // 60}min {total_s % 60}s)")
    log(f"总计 ok={ok_total} skip={skip_total} fail={fail_total} resumed={resumed_total}")
    log(f"UP 数 {len([s for s in summaries if 'error' not in s])}/{len(summaries)}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = LOG_DIR / f"{stamp}_全UP刷新_汇总.log"
    report = {
        "started_at": datetime.fromtimestamp(t_start).strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "duration_s": total_s,
        "workers": workers,
        "force": force,
        "total_ok": ok_total,
        "total_skip": skip_total,
        "total_fail": fail_total,
        "total_resumed": resumed_total,
        "authors": summaries,
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"汇总已保存: {report_path}")


if __name__ == "__main__":
    main()
