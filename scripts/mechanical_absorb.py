#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""吸收机械段自动落库（05:00 任务调用）

只做可机械判定的一段：
  - 纯【设定】世界知识块（无手法/解读/机制词）→ 逐字块入册 + 登记号锚 + 题材标签 + 覆盖账行
  - 其余（手法/解读/机制/重复可疑/无登记号）→ 语义待裁清单，不写库

用法：
  python -B scripts/mechanical_absorb.py --once [--dry-run] [--limit N]
退出码：0=有写入或无需处理；2=有待裁但无写入；3=异常
"""
from __future__ import annotations
import argparse, hashlib, io, json, os, re, sys, time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 定时子步骤的动作日志（不可用则降级 no-op，不影响主流程）
sys.path.insert(0, str(ROOT / "douyin-video" / "scripts"))
try:
    from operation_logger import log_scheduler, new_batch_id  # noqa: E402
except Exception:  # pragma: no cover
    def log_scheduler(*_a, **_k):  # type: ignore
        pass

    def new_batch_id():  # type: ignore
        return ""
WS = ROOT.parent
KB = WS / "maiji-writing-skill" / "00-知识库"
LEDGER = WS / "docs" / "吸收覆盖账.md"
REG = WS / "maiji-writing-skill" / "docs" / "源件登记册.md"
ALIAS = KB / "00.0b_UP别名台账.md"
VOL = KB / "00.10h1_机械吸收_世界知识暂存分册一.md"
STATE = ROOT / "logs" / "scheduled_absorb" / "last_mechanical.json"
FPIDX = ROOT / "logs" / "scheduled_absorb" / "absorb_fp_index.json"
PEND = ROOT / "logs" / "scheduled_absorb" / "语义待裁_%s.md"
VOL_BYTES = 55 * 1024
MECH = re.compile(r"手法|解读|机制|技巧|写法|公式|套路|模板")
TOPICS = [("古代历史", r"朝代|古代|历史|礼制|官制|兵制|服饰|科举"),
          ("军事", r"军事|战争|兵器|阵法|战术"),
          ("武侠玄幻穿越", r"武侠|玄幻|修真|仙侠|穿越|蛊|门派"),
          ("侦探法医", r"侦探|法医|刑侦|验尸|推理|凶案"),
          ("都市异闻", r"都市|异闻|民俗|怪谈|民间"),
          ("恐怖悬疑", r"恐怖|悬疑|惊悚|灵异"),
          ("世界观制度", r"世界|制度|种族|地理|势力|政治|经济|宗教")]

def now(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def fp(s): return hashlib.sha256(re.sub(r"\s+", "", s).encode("utf-8")).hexdigest()[:12]

def read(p): return io.open(p, encoding="utf-8-sig", errors="replace").read()

def k_map():
    m = {}
    if not ALIAS.exists():
        return m
    for l in read(ALIAS).splitlines():
        c = [x.strip() for x in l.strip().strip("|").split("|")]
        if len(c) >= 3 and re.fullmatch(r"K-\d+", c[0]):
            m[c[1]] = c[0]
    return m

def registered_k():
    s = set()
    if REG.exists():
        for l in read(REG).splitlines():
            mm = re.match(r"\|\s*(K-\d+)\s*\|", l)
            if mm:
                s.add(mm.group(1))
    return s

def topic_of(name, text):
    blob = name + " " + text[:120]
    for t, pat in TOPICS:
        if re.search(pat, blob):
            return t
    return "世界观制度"

def load_idx():
    if FPIDX.exists():
        try:
            return set(json.loads(read(FPIDX)))
        except Exception:
            pass
    # 首跑：从包内 00.4z/00.10* 现有块指纹建索引
    out = set()
    for p in list(KB.glob("00.4z*.md")) + list(KB.glob("00.10*.md")):
        for m in re.finditer(r"〔块:([0-9a-f]{12})｜", read(p)):
            out.add(m.group(1))
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=200)
    a = ap.parse_args()
    t0 = time.time()
    run_id = new_batch_id()
    log_scheduler("mechanical", "trigger", run_id=run_id,
                  dry_run=bool(a.dry_run), limit=a.limit)
    last = 0
    if STATE.exists():
        try:
            last = json.loads(read(STATE)).get("last_mtime", 0)
        except Exception:
            pass
    kmap, regk = k_map(), registered_k()
    idx = load_idx()
    cands, pending = [], []
    files = sorted(WS.joinpath("写作理论").glob("知识库_*_*.md"))
    for p in files:
        mt = p.stat().st_mtime
        if mt <= last:
            continue
        name = p.stem
        up = re.sub(r"^知识库_", "", name)
        up = re.sub(r"_(动漫叙事与IP|写作方法论|.*)$", "", up)
        k = kmap.get(up, "")
        sec = ""
        for l in read(p).splitlines():
            if l.startswith("### "):
                sec = l[3:].strip()
            if not re.match(r"^-\s+", l) or "〔源" not in l and "〔来源" not in l:
                continue
            text = re.sub(r"〔[^〕]*〕", "", l)
            text = re.sub(r"[（(]\d{19}[）)]", "", text)
            text = re.sub(r"^-\s*", "", text).strip()
            text = re.sub(r"\s+", " ", text)
            if len(text) < 80:
                pending.append((up, k, sec, text, "短于 80 字／疑空条"))
                continue
            f = fp(text)
            if f in idx:
                continue
            if "【设定" not in text and "【考据" not in text and "【名词" not in text:
                pending.append((up, k, sec, text, "非纯设定（含解读/手法/机制）"))
                continue
            if MECH.search(text):
                pending.append((up, k, sec, text, "设定条含机制/手法词"))
                continue
            if not k or k not in regk:
                pending.append((up, k, sec, text, "无已登记 K 码"))
                continue
            cands.append({"up": up, "k": k, "sec": sec, "text": text, "fp": f,
                          "topic": topic_of(name, text)})
            idx.add(f)
            if len(cands) >= a.limit:
                break
        if len(cands) >= a.limit:
            break

    wrote = 0
    if cands and not a.dry_run:
        # 卷容量守卫
        cur = read(VOL) if VOL.exists() else (
            "# 机械吸收·世界知识暂存（00.10h1）\n\n"
            "> 05:00 机械段自动落库：纯【设定】块逐字入册（块指纹＋登记号锚＋题材标签＋覆盖账行）。\n"
            "> 语义项（判收不收／同义重复／按需使用）不出现在本册，只进「语义待裁」清单。\n\n")
        lines = [cur]
        used = len(cur.encode("utf-8"))
        sec = 0
        ledger_rows = []
        for i, c in enumerate(cands, 1):
            ent = ("## §1 %s\n" % c["topic"] if i == 1 else "") + (
                "- **【机械吸收·%s·%d】**：%s〔块:%s｜源#%s〕 `#题材:%s`\n" % (
                    c["k"], i, c["text"], c["fp"], c["k"], c["topic"]))
            if used + len(ent.encode("utf-8")) > VOL_BYTES:
                pending.append((c["up"], c["k"], c["sec"], c["text"], "单册 60KB 将超，转下批"))
                continue
            lines.append(ent); used += len(ent.encode("utf-8"))
            ledger_rows.append("| 写作理论/%s（机械段） | 源#%s-%d（§机械吸收 条%d） | 案例 | 世界知识 | 00.10h1 §1 条%d | 已入 |" % (
                c["up"], c["k"], i, i, i))
            wrote += 1
        if wrote:
            VOL.write_text("".join(lines), encoding="utf-8")
            lt = read(LEDGER).splitlines()
            j = next(k for k, l in enumerate(lt) if l.startswith("## 二、待入"))
            lt[j:j] = ledger_rows + [""]
            io.open(LEDGER, "w", encoding="utf-8", newline="\n").write("\n".join(lt) + "\n")
            if not any("00.10h1" in l for l in lt if l.startswith("|")):
                pass
    # 待裁清单
    stamp = datetime.now().strftime("%Y%m%d")
    mp = Path(str(PEND) % stamp)
    mp.parent.mkdir(parents=True, exist_ok=True)
    if pending:
        out = ["# 语义待裁清单 %s" % now(), "",
               "> 机械段不判「收不收／是否同义重复／要不要按需使用」；以下逐条待裁，未写入包。", "",
               "| 源 | K 码 | 节 | 待裁理由 | 原文摘录 |", "| --- | --- | --- | --- | --- |"]
        for up, k, sec, text, why in pending[:500]:
            out.append("| %s | %s | %s | %s | %s |" % (up, k or "未登记", sec, why, text[:60].replace("|", "／")))
        if len(pending) > 500:
            out.append("| … | — | — | 其余 %d 条见 json 台账 | — |" % (len(pending) - 500))
        mp.write_text("\n".join(out) + "\n", encoding="utf-8")
        io.open(str(mp).replace(".md", ".json"), "w", encoding="utf-8").write(json.dumps(
            [{"源": u, "K": k, "节": s, "理由": w, "原文": t} for u, k, s, t, w in pending],
            ensure_ascii=False, indent=1))
    if not a.dry_run:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"last_run": now(), "last_mtime": time.time(),
                                     "written": wrote, "pending": len(pending),
                                     "dry_run": False}, ensure_ascii=False, indent=2), encoding="utf-8")
        io.open(FPIDX, "w", encoding="utf-8").write(json.dumps(sorted(idx), ensure_ascii=False))
    print("机械段：写库 %d 块｜待裁 %d 条｜用时 %.1fs%s" % (
        wrote, len(pending), time.time() - t0, "（dry-run）" if a.dry_run else ""))
    log_scheduler("mechanical", "finish", run_id=run_id,
                  dry_run=bool(a.dry_run), wrote=wrote, pending=len(pending),
                  duration_s=round(time.time() - t0, 1))
    return 0 if wrote or not pending else 2

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("机械段异常：%r" % (e,))
        sys.exit(3)
