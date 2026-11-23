#!/usr/bin/env python
"""当天「待吸收清单」自动生成（只读差集 → 写 0_工作区/docs/待吸收清单_YYYYMMDD.md）

背景：此前只有 absorb_full.py 读清单、没有任何生成方，导致每天读旧清单、待入恒为 0。
本脚本把 absorb_gap.py 的差集落成清单：

  输入：output/<UP>/*.md（盘上件）∖ 写作理论/知识库_<UP>_*.md 的源件台账
  过滤：已在 00.10e 分册里出现过的 (K 码, 件序) 不再列入
  分批：沿用现有规则——按 UP 连续分组、每批 ≤60 件／≤500 块（仅作计划展示；
        absorb_full.py 仍按同一规则自己选批，手工 `--batch` 入口不受影响）
  空差集：显式写「本批无可入件」，绝不复用上一份清单

用法：
    python -B scripts/gen_pending_list.py            # 生成清单
    python -B scripts/gen_pending_list.py --dry-run  # 只打印读数，不写文件
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import absorb_gap as ag  # noqa: E402
import absorb_full as af  # noqa: E402

DOCS = ag.DOCS
OUT = ag.OUT
MAX_ITEMS = 60
MAX_BLOCKS = 500

FILE_NAME = re.compile(r"^(\d+)-(\d{15,})\.md$")


def list_path(day: str = "") -> Path:
    day = day or datetime.now().strftime("%Y%m%d")
    return DOCS / ("待吸收清单_%s.md" % day)


def est_blocks(p: Path) -> int:
    """块估算（不读正文，按 UTF-8 字节折算中文 300 字/块，至少 1 块）。"""
    try:
        return max(1, int(round(p.stat().st_size / 900.0)))
    except OSError:
        return 1


def scan_dir(up: str) -> dict:
    """视频 ID -> (件序, 相对路径)；只认 <序号>-<19位ID>.md。"""
    out = {}
    d = OUT / up
    if not d.is_dir():
        return out
    for name in sorted(d.iterdir(), key=lambda p: p.name):
        m = FILE_NAME.match(name.name)
        if not m:
            continue
        out[m.group(2)] = (int(m.group(1)), "douyin-mcp-server/output/%s/%s" % (up, name.name))
    return out


def build_rows(res: dict) -> tuple:
    """差集 → 可入清单行；返回 (items, stats)。

    stats.raw  = 差集里的候选件数（三类之和）
    stats.unmatched = 盘上件名不符合 <序号>-<ID>.md、无法定位源件的件数
    对账恒等式：raw == len(items) + skipped_done + unmatched
    """
    kmap = af.k_map()
    done = af.processed_pairs()
    items, skipped_done, unmatched = [], 0, 0
    for r in res["rows"]:
        if r["status"] == "OK":
            continue
        up = r["up"]
        k = af.k_for(up, kmap)
        files = scan_dir(up)
        for vid in r["missing_ids"]:
            info = files.get(vid)
            if not info:
                unmatched += 1
                continue
            seq, rel = info
            anchor_k = k if k.startswith("K-") else ""
            key = (anchor_k, seq) if anchor_k else (af.norm(up), seq)
            if key in done:
                skipped_done += 1
                continue
            items.append({"up": up, "seq": seq, "vid": vid, "k": anchor_k or "—",
                          "reason": r["status"], "est": est_blocks(ag.ROOT / rel),
                          "path": rel})
    items.sort(key=lambda x: (x["up"], x["seq"]))
    return items, {"skipped_done": skipped_done, "unmatched": unmatched,
                   "raw": len(items) + skipped_done + unmatched}


def plan_batches(items: list) -> list:
    """按 UP 连续分组、每批 ≤60 件／≤500 块，返回每批的件/块/UP 跨度。"""
    batches, cur, ni, nb = [], [], 0, 0
    for it in items:
        if cur and (ni + 1 > MAX_ITEMS or nb + it["est"] > MAX_BLOCKS):
            batches.append((cur, ni, nb))
            cur, ni, nb = [], 0, 0
        cur.append(it)
        ni += 1
        nb += it["est"]
    if cur:
        batches.append((cur, ni, nb))
    return batches


def render(res: dict, items: list, batches: list, stats: dict) -> str:
    day = datetime.now().strftime("%Y-%m-%d")
    diff_total = stats["raw"]
    lines = [
        "# 当日待吸收清单｜%s" % day,
        "",
        "> 生成：`douyin-mcp-server/scripts/gen_pending_list.py`（自动；差集来自 output 盘上件 ∖ 知识库源件台账）",
        "> 过滤：已在 00.10e 分册出现的 (K 码, 件序) 不再列入；分批规则＝按 UP 连续分组、每批 ≤60 件／≤500 块。",
        "> 三类分开记：件级缺口（有册有台账）／NO_BOOK（连册都没有）／UNANCHORED（有册但无 ID 台账，无法逐件对账）。",
        "",
        "## 一、读数",
        "- 差集总件数 %d（件级缺口 %d／NO_BOOK %d／UNANCHORED %d）" % (
            diff_total, res["missing"], res["no_book_items"], res["unanchored_items"]),
        "- 已入 00.10e 过滤 %d 件｜源件名不可定位 %d 件" % (
            stats["skipped_done"], stats["unmatched"]),
        "- 本清单待入 %d 件｜按现有规则 %d 批" % (len(items), len(batches)),
        "- 盘上合计 %d｜可对账册已入 %d" % (res["on_disk"], res["absorbed"]),
        "",
        "## 二、按 UP 汇总",
        "| UP | 盘上 | 已入 | 状态 | 待入件 | 待入块 |",
        "| --- | ---: | ---: | --- | ---: | ---: |",
    ]
    per_up = {}
    for it in items:
        slot = per_up.setdefault(it["up"], [0, 0])
        slot[0] += 1
        slot[1] += it["est"]
    for r in res["rows"]:
        if r["status"] == "OK":
            continue
        cnt, blk = per_up.get(r["up"], (0, 0))
        lines.append("| %s | %d | %d | %s | %d | %d |" % (
            r["up"], r["on_disk"], r["absorbed"], r["status"], cnt, blk))
    lines += ["", "## 三、逐件待入清单", ""]
    if not items:
        lines.append("- 本批无可入件（差集为空或均已入 00.10e）。")
        return "\n".join(lines) + "\n"
    lines += ["| UP | 源序 | 视频 ID | K 码 | 差集原因 | 块估算 | 状态 | 源件 |",
              "| --- | ---: | --- | --- | --- | ---: | --- | --- |"]
    for it in items:
        lines.append("| %s | %d | %s | %s | %s | %d | 待入 | %s |" % (
            it["up"], it["seq"], it["vid"], it["k"], it["reason"], it["est"], it["path"]))
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印读数，不写清单")
    ap.add_argument("--up", default="", help="只生成某个 UP（调试用）")
    args = ap.parse_args()

    res = ag.collect(args.up)
    items, stats = build_rows(res)
    batches = plan_batches(items)
    text = render(res, items, batches, stats)

    print("GEN|diff=%d|list=%d|batches=%d|no_book_items=%d|unanchored_items=%d"
          "|skipped_done=%d|unmatched=%d|gap_missing=%d"
          % (stats["raw"], len(items), len(batches),
             res["no_book_items"], res["unanchored_items"],
             stats["skipped_done"], stats["unmatched"], res["missing"]))
    if items:
        print("清单待入 %d 件｜%d 批｜首批 %s 件%d（%d 件／%d 块）"
              % (len(items), len(batches), items[0]["up"], items[0]["seq"],
                 batches[0][1], batches[0][2]))
    else:
        print("本批无可入件（差集为空或均已入 00.10e）")

    if args.dry_run:
        return 0
    DOCS.mkdir(parents=True, exist_ok=True)
    p = list_path()
    p.write_text(text, encoding="utf-8")
    print("已生成：%s" % p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
