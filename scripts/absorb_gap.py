#!/usr/bin/env python
"""待吸收差集（只读计算；默认把差集落盘到 0_工作区/docs 供复核）

用途：对比「盘上转写件」与「各 UP 知识库册的源件台账」，输出待吸收清单。
不改知识库、不触发蒸馏；只写 0_工作区/docs 下的差集文件（--dry-run 时连这个也不写）。

用法：
    python -B douyin-mcp-server/scripts/absorb_gap.py            # 人读表格 + 落盘
    python -B douyin-mcp-server/scripts/absorb_gap.py --json     # 机读 JSON + 落盘
    python -B douyin-mcp-server/scripts/absorb_gap.py --dry-run  # 只打印，不落盘
    python -B douyin-mcp-server/scripts/absorb_gap.py --up 影级社长

口径：
  盘上件    = output/<UP>/*.md 的文件名里的 15+ 位视频 ID（兼容 <序号>-<ID>.md）
  已入件    = 知识库册里表格行 `| <序号> - <ID> |` 的 ID（写作理论/知识库_<UP>_*.md）
  件级缺口  = 盘上件 ∖ 已入件（只统计「有册且有源件台账」的 UP）→ 可对账差额
  无册      = NO_BOOK：连知识库册都没有的 UP，盘上件单独计，不进可对账差额
  无锚册    = UNANCHORED：有册但册内没有 19 位 ID 台账，无法逐件对账，盘上件单独计
"""
import argparse
import io
import json
import os
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DY = ROOT / "douyin-mcp-server"
OUT = DY / "output"
KB_DIR = ROOT / "写作理论"
DOCS = ROOT / "0_工作区" / "docs"
SKIP_DIRS = {"归档", "刷新日志", "_提炼", "_知识库增量对照"}
CATALOG = "目录.md"

ID_IN_NAME = re.compile(r"(\d{15,})")
LEDGER_ROW = re.compile(r"^\|\s*\d+\s*-\s*(\d{15,})\s*\|")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"[\s_·\-—（）()「」“”\"'’]+", "", s).lower()


def load_kb():
    """返回 [(册文件名, UP 段, ID 集合, 是否有台账)]"""
    books = []
    if not KB_DIR.is_dir():
        return books
    for f in sorted(os.listdir(KB_DIR)):
        if not (f.startswith("知识库_") and f.endswith(".md")):
            continue
        p = KB_DIR / f
        try:
            text = io.open(p, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        ids = set()
        rows = 0
        for line in text.splitlines():
            m = LEDGER_ROW.match(line.strip())
            if m:
                rows += 1
                ids.add(m.group(1))
        up_seg = f[len("知识库_"):].rsplit(".", 1)[0].split("_")[0]
        books.append((f, up_seg, ids, rows > 0))
    return books


def dir_ids(path: Path):
    ids = set()
    for name in os.listdir(path):
        if not name.endswith(".md") or name == CATALOG:
            continue
        m = ID_IN_NAME.search(name)
        if m:
            ids.add(m.group(1))
    return ids


def collect(up_filter: str = "") -> dict:
    """计算差集；纯读，不写文件。

    返回 {rows, on_disk, absorbed, missing, no_book_items, unanchored_items,
          no_book, unanchored}
    其中 missing 只含「有册且有台账」UP 的件级缺口（可对账差额）；
    NO_BOOK / UNANCHORED 的盘上件分别计入 no_book_items / unanchored_items，
    不再混进 missing。
    """
    books = load_kb()
    rows = []
    for name in sorted(os.listdir(OUT)) if OUT.is_dir() else []:
        d = OUT / name
        if not d.is_dir() or name in SKIP_DIRS:
            continue
        ids = dir_ids(d)
        if not ids:
            continue
        if up_filter and name != up_filter:
            continue
        book = next(((f, seg, bids, has) for f, seg, bids, has in books
                     if norm(seg) == norm(name)), None)
        if book is None:
            book = next(((f, seg, bids, has) for f, seg, bids, has in books
                         if norm(name) and norm(name) in norm(f)), None)
        if book is None:
            rows.append({"up": name, "on_disk": len(ids), "book": "", "absorbed": 0,
                         "missing": None, "status": "NO_BOOK",
                         "missing_ids": sorted(ids)})
            continue
        fname, _seg, bids, has = book
        if not has:
            rows.append({"up": name, "on_disk": len(ids), "book": fname, "absorbed": 0,
                         "missing": None, "status": "UNANCHORED",
                         "missing_ids": sorted(ids)})
            continue
        missing = ids - bids
        rows.append({"up": name, "on_disk": len(ids), "book": fname,
                     "absorbed": len(ids & bids), "missing": len(missing),
                     "status": "OK" if not missing else "GAP",
                     "missing_ids": sorted(missing)})

    no_book_rows = [r for r in rows if r["status"] == "NO_BOOK"]
    unanchored_rows = [r for r in rows if r["status"] == "UNANCHORED"]
    return {
        "rows": rows,
        "on_disk": sum(r["on_disk"] for r in rows),
        "absorbed": sum(r["absorbed"] for r in rows),
        "missing": sum(r["missing"] for r in rows if r["missing"]),
        "no_book_items": sum(r["on_disk"] for r in no_book_rows),
        "unanchored_items": sum(r["on_disk"] for r in unanchored_rows),
        "no_book": len(no_book_rows),
        "unanchored": len(unanchored_rows),
    }


def _write_diff(res: dict, up_filter: str = "") -> tuple:
    DOCS.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y%m%d")
    jp = DOCS / ("吸收差集_%s.json" % day)
    mp = DOCS / ("吸收差集_%s.md" % day)
    payload = {"schema": "absorb_gap_v2", "date": datetime.now().strftime("%Y-%m-%d"),
               "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "up_filter": up_filter,
               **{k: v for k, v in res.items() if k != "rows"},
               "rows": res["rows"]}
    jp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# 吸收差集｜%s" % payload["date"],
        "",
        "> 生成：`douyin-mcp-server/scripts/absorb_gap.py`（只读计算；本文件仅供复核）",
        "> 口径：件级缺口只统计「有册且有源件台账」的 UP；NO_BOOK／UNANCHORED 的盘上件单列，不混入可对账差额。",
        "",
        "## 一、读数",
        "- 盘上合计 %d ｜可对账册已入 %d ｜**可对账差额 %d**" % (
            res["on_disk"], res["absorbed"], res["missing"]),
        "- 无册（NO_BOOK）：%d UP／%d 件（不进可对账差额）" % (
            res["no_book"], res["no_book_items"]),
        "- 无锚册（UNANCHORED）：%d UP／%d 件（无 ID 台账，无法逐件对账）" % (
            res["unanchored"], res["unanchored_items"]),
        "",
        "## 二、逐 UP",
        "| UP | 盘上 | 已入 | 差额 | 状态 | 册 |",
        "| --- | ---: | ---: | ---: | --- | --- |",
    ]
    for r in res["rows"]:
        miss = "—" if r["missing"] is None else r["missing"]
        lines.append("| %s | %d | %d | %s | %s | %s |" % (
            r["up"], r["on_disk"], r["absorbed"], miss, r["status"], r["book"] or "—"))
    mp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jp, mp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--up", default="", help="只看某个 UP")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写差集文件")
    args = ap.parse_args()

    res = collect(args.up)
    rows = res["rows"]

    if not args.dry_run:
        _write_diff(res, args.up)

    if args.json:
        print(json.dumps({k: v for k, v in res.items()}, ensure_ascii=False, indent=2))
        return 0

    print("UP                     盘上   已入   差额   状态        册")
    for r in rows:
        miss = "-" if r["missing"] is None else r["missing"]
        print("%-20s %6d %6d %6s   %-10s %s"
              % (r["up"], r["on_disk"], r["absorbed"], miss, r["status"], r["book"]))
    print("-" * 78)
    print("盘上合计 %d ｜可对账册已入 %d ｜可对账差额 %d ｜无册 %d UP(%d 件) ｜无锚册 %d UP(%d 件)"
          % (res["on_disk"], res["absorbed"], res["missing"],
             res["no_book"], res["no_book_items"],
             res["unanchored"], res["unanchored_items"]))
    for flag, status, label in (("no_book", "NO_BOOK", "[无知识库册]"),
                                ("unanchored", "UNANCHORED", "[无锚册·仅能人工核件数]")):
        sub = [r for r in rows if r["status"] == status]
        if sub:
            print("\n%s " % label + "、".join("%s(%d)" % (r["up"], r["on_disk"]) for r in sub))
    return 0


if __name__ == "__main__":
    sys.exit(main())
