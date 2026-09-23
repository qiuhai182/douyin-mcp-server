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
import os
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"
LOG_DIR = OUTPUT / "刷新日志"
LOG_DIR.mkdir(exist_ok=True)
LIVE_LOG = LOG_DIR / "driver_live.log"
# Heartbeat state file: the WebUI polls it to detect a running backend
# script task, show its progress and offer a terminate button. Atomic
# writes (tmp + replace) so a concurrent read never sees partial JSON.
STATE_FILE = LOG_DIR / "driver_state.json"
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


_state_lock = threading.Lock()


def write_state(**fields):
    """Atomically update the heartbeat state file (WebUI-facing progress)."""
    with _state_lock:
        try:
            data = {"pid": os.getpid(), "stage": "running"}
            if STATE_FILE.exists():
                try:
                    data.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
                except Exception:
                    pass
            data.update(fields)
            data["updated_at"] = now()
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, STATE_FILE)
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

    # 互斥：WebUI 批量任务运行中（占用 Chrome profile）时不启动
    try:
        import urllib.request
        with urllib.request.urlopen(
                "http://127.0.0.1:8080/api/service/status", timeout=3) as r:
            st = json.loads(r.read().decode("utf-8"))
        if st.get("batch_running"):
            log("FATAL: WebUI 批量任务正在运行（Chrome profile 独占），请等它结束后再刷新")
            sys.exit(1)
    except SystemExit:
        raise
    except Exception:
        pass  # WebUI 未运行时直接继续

    # 延迟 import：让 sys.path 设置先完成
    from batch_extractor import batch_extract, rename_transcripts_numbered

    # 每个 UP 的进度回调
    def on_progress(evt):
        stage = evt.get("stage", "")
        if stage == "list":
            log(f"    list found={evt.get('found')}")
            write_state(last_event={"stage": "list", "found": evt.get("found")})
        elif stage == "extract":
            idx = evt.get("index", "?")
            tot = evt.get("total", "?")
            status = evt.get("status", "")
            aweme_id = evt.get("aweme_id", "")
            title = (evt.get("title") or "")[:28]
            marker = {"ok": "✓", "skip": "-", "fail": "✗"}.get(status, "·")
            write_state(last_event={"stage": "extract", "index": idx,
                                    "total": tot, "status": status,
                                    "aweme_id": aweme_id,
                                    "title": (evt.get("title") or "")[:60]})
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
    write_state(started_at=now(), author_total=len(names),
                author_index=0, current_author="", stage="running",
                totals={"ok": 0, "skip": 0, "fail": 0})

    aborted = False
    try:
        for i, name in enumerate(names, 1):
            entry = authors[name]
            url = entry.get("profile_url", "")
            if not url:
                log(f"[{i}/{len(names)}] SKIP {name}: 无 profile_url")
                summaries.append({"author": name, "skipped": "no url"})
                continue

            write_state(author_index=i, current_author=name, current_url=url,
                        author_total=len(names))
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
                if summary.get("aborted"):
                    aborted = True
                    log(f"  ■ {name}: 任务已被用户终止，停止后续 UP")
                    summaries.append({**summary, "author": name, "aborted": True,
                                      "duration_s": int(time.time() - t0)})
                    break
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
                write_state(totals={
                    "ok": sum(int(s.get("ok") or 0) for s in summaries),
                    "skip": sum(int(s.get("skip") or 0) for s in summaries),
                    "fail": sum(int(s.get("fail") or 0) for s in summaries),
                })
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

        if aborted:
            log(f"\n=== 全 UP 刷新已终止（用户操作） ===")
        else:
            log(f"\n=== 全 UP 刷新完成 ===")
        log(f"耗时 {total_s}s ({total_s // 60}min {total_s % 60}s)")
        log(f"总计 ok={ok_total} skip={skip_total} fail={fail_total} resumed={resumed_total}")
        log(f"UP 数 {len([s for s in summaries if 'error' not in s and 'aborted' not in s])}/{len(summaries)}")

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        tag = "已终止" if aborted else "全UP刷新"
        report_path = LOG_DIR / f"{stamp}_{tag}_汇总.log"
        report = {
            "started_at": datetime.fromtimestamp(t_start).strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "duration_s": total_s,
            "workers": workers,
            "force": force,
            "aborted": aborted,
            "total_ok": ok_total,
            "total_skip": skip_total,
            "total_fail": fail_total,
            "total_resumed": resumed_total,
            "authors": summaries,
        }
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"汇总已保存: {report_path}")
    finally:
        write_state(stage="finished" if not aborted else "aborted",
                    finished_at=now())
        time.sleep(2)  # let the WebUI observe the final state once
        try:
            STATE_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            _LIVE_F.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
