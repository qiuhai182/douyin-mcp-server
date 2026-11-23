#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手动重跑历史 ignored 视频（默认只列出，绝不自动重跑）。

背景：历史 105 条 `ignored` 是旧质量闸（写死中文 CJK 判据）的产物。新闸按
语言分流后，其中英语等拉丁语种视频可能可以重新判定；但按用户令，历史
ignored **不得自动重跑**，只提供本手动入口。

用法（在 douyin-mcp-server 目录下执行）：
  python scripts/rerun_ignored.py --list
      只列出全部 ignored 记录（默认行为，安全）。
  python scripts/rerun_ignored.py --ids 7595...,7595...
      手动重跑指定的视频 id。
  python scripts/rerun_ignored.py --all --yes
      重跑全部 ignored（必须显式加 --yes 防误触）。

可选过滤：
  --reason-filter low_cjk
      只处理 error 字段包含该子串的条目。

说明：
- 重跑复用单条路径（与 WebUI「单条解析」完全一致）：语种识别 -> 语言分流
  质量闸 -> 外文翻译 -> 双语 md -> library 记录 language/original/translated。
- 失败不会把记录改成 fail，仍保持 ignored，避免污染失败清单。
- 成功后 history 状态置为 ok，后续批量不会重复处理。
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "douyin-video" / "scripts"))
sys.path.insert(0, str(ROOT / "web"))


def _load_history():
    from batch_extractor import TranscriptHistory
    return TranscriptHistory()


def _ignored_entries(history, reason_filter: str = "") -> list:
    entries = []
    for vid, entry in history._data.items():
        if (entry or {}).get("status") != "ignored":
            continue
        if reason_filter and reason_filter not in (entry.get("error") or ""):
            continue
        entries.append((vid, entry))
    entries.sort(key=lambda x: x[1].get("ignored_at") or "")
    return entries


def _print_entries(entries: list) -> None:
    print(f"ignored 记录：{len(entries)} 条")
    for vid, entry in entries:
        print(f"  {vid}  {entry.get('title') or '(无标题)'}")
        reason = (entry.get("error") or "").strip()
        if reason:
            print(f"      原因: {reason[:160]}")
        if entry.get("ignored_at"):
            print(f"      忽略时间: {entry['ignored_at']}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="手动重跑历史 ignored 视频（默认只列出）")
    parser.add_argument("--list", action="store_true",
                        help="只列出 ignored 记录（默认行为）")
    parser.add_argument("--ids", default="",
                        help="要重跑的视频 id，逗号分隔")
    parser.add_argument("--all", action="store_true",
                        help="重跑全部 ignored（必须同时加 --yes）")
    parser.add_argument("--yes", action="store_true",
                        help="确认执行 --all")
    parser.add_argument("--reason-filter", default="",
                        help="只处理 error 包含该子串的条目，如 low_cjk")
    args = parser.parse_args()

    history = _load_history()
    entries = _ignored_entries(history, args.reason_filter)
    _print_entries(entries)

    ids = [i.strip() for i in args.ids.split(",") if i.strip()]
    if not ids and not args.all:
        print("\n仅列出，未执行重跑。")
        print("用法：--ids <id1,id2> 或 --all --yes")
        return 0

    if args.all and not args.yes:
        print("\n--all 需同时加 --yes 确认，未执行。")
        return 2

    known = {vid for vid, _ in entries}
    targets = ids if ids else [vid for vid, _ in entries]
    unknown = [i for i in targets if i not in known]
    if unknown:
        print(f"\n以下 id 不在 ignored 清单中，已跳过：{', '.join(unknown)}")
    targets = [i for i in targets if i in known]
    if not targets:
        print("没有可重跑的目标。")
        return 0

    print(f"\n开始手动重跑 {len(targets)} 条……")
    from app import _extract_single_video

    ok, failed = 0, 0
    for idx, vid in enumerate(targets, 1):
        url = f"https://www.iesdouyin.com/share/video/{vid}"
        title = (history._data.get(vid) or {}).get("title") or ""
        print(f"[{idx}/{len(targets)}] {vid} {title}")
        try:
            video_info, _text = _extract_single_video(url, "")
            history.add(vid, video_info.get("title") or title, "")
            ok += 1
            print("  ✓ 成功")
        except Exception as e:
            failed += 1
            print(f"  ✗ 失败（保持 ignored）：{str(e)[:200]}")

    print(f"\n完成：成功 {ok} 条，失败 {failed} 条。")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
