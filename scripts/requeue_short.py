#!/usr/bin/env python
"""短件回队（正文不足 60 字）

背景：转写结果过短（音乐/噪声/截断）时，md 仍被写成「完整」并记 history=ok，
之后每轮刷新都按「已解析」跳过，等于静默丢弃。

本工具把这类件从「已完成」打回待办：
  - md 移入 output/归档/短件待重跑_<日期>/<UP>/（保留原文，不删）
  - history 条目 status 置 fail（下轮刷新按失败重试，由 ASR 质量闸重新判定）

用法：
    python -B douyin-mcp-server/scripts/requeue_short.py            # 只列，不改
    python -B douyin-mcp-server/scripts/requeue_short.py --apply    # 执行回队
    python -B douyin-mcp-server/scripts/requeue_short.py --apply --date 20261111
"""
import argparse
import io
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DY = ROOT / "douyin-mcp-server"
OUT = DY / "output"
HIST = DY / "history.json"
SKIP_DIRS = {"归档", "刷新日志", "_提炼", "_知识库增量对照"}
_PUNCT = "，。！？、；：""''（）《》〈〉…—·,.!?;:()[]{}<>\"'`~@#$%^&*+=|\\/"
STRIP = re.compile(r"[\s" + re.escape(_PUNCT) + r"]+")


def body_of(text: str) -> str:
    i = text.find("## Transcript")
    if i < 0:
        return ""
    tail = text[i + len("## Transcript"):]
    for marker in ("## 中文翻译", "## Translation"):
        if marker in tail:
            tail = tail.split(marker, 1)[0]
    return STRIP.sub("", tail)


def scan():
    hits = []
    for up in sorted(os.listdir(OUT)):
        d = OUT / up
        if not d.is_dir() or up in SKIP_DIRS:
            continue
        for name in sorted(os.listdir(d)):
            if not name.endswith(".md"):
                continue
            p = d / name
            try:
                text = io.open(p, encoding="utf-8", errors="ignore").read()
            except Exception:
                continue
            body = body_of(text)
            if len(body) >= 60:
                continue
            m = re.search(r"(\d{15,})", name)
            hits.append({"up": up, "file": name, "path": p,
                         "video_id": m.group(1) if m else "", "chars": len(body)})
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--date", default=datetime.now().strftime("%Y%m%d"))
    args = ap.parse_args()

    hits = scan()
    hist = json.load(io.open(HIST, encoding="utf-8"))
    print("正文<60字 = %d 件；history 中存在 = %d"
          % (len(hits), sum(1 for h in hits if h["video_id"] in hist)))
    if not hits:
        return 0
    if not args.apply:
        for h in hits[:10]:
            print("  %-16s %s %d字" % (h["up"], h["video_id"], h["chars"]))
        print("  ...（只列前 10；加 --apply 执行回队）")
        return 0

    backup_dir = ROOT / "_deploy_backups"
    backup_dir.mkdir(exist_ok=True)
    backup = backup_dir / ("history_%s_pre_requeue.json" % args.date)
    shutil.copy2(HIST, backup)

    dest_root = OUT / "归档" / ("短件待重跑_%s" % args.date)
    moved = 0
    for h in hits:
        dest = dest_root / h["up"]
        dest.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(h["path"]), str(dest / h["file"]))
            moved += 1
        except Exception as e:
            print("  MOVE_FAIL %s %s: %s" % (h["up"], h["file"], e))
            continue
        vid = h["video_id"]
        if vid:
            prev = hist.get(vid) or {}
            hist[vid] = {
                "title": prev.get("title", ""),
                "extracted_at": prev.get("extracted_at", ""),
                "output": "",
                "status": "fail",
                "error": "正文<60字·回队重跑（%s 复核）" % args.date,
            }
    tmp = HIST.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, HIST)
    print("已回队 %d 件；history 置 fail %d 条；备份 = %s"
          % (moved, len(hits), backup))
    return 0


if __name__ == "__main__":
    sys.exit(main())
