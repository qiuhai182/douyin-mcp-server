# -*- coding: utf-8 -*-
"""B14 全量吸收批处理（机械段可写版）

口径：
- 输入面＝douyin-mcp-server/output/<UP>/<序号>-<19位ID>.md（原始转写件），不再等写作理论册。
- 单位＝块（80–600字）；每块三件：逐字片段＋块指纹＋落位（册§节条N）。
- 三类判型：道术条／案例样本／世界知识（机判，标待核）。
- 道术条先与包内既有条文做 2-gram 近似比对：>=0.50 判重只指认（文件·节·行），不重立；
  否定极性相反者单列「互斥待核」；<0.50 作「新增待裁」落 00.10e，不直接立条文。
- 落位：00.10e 族 UP 原库待归类区（每册 <=55KB），覆盖账逐块一行。
- 未证实：含营销数字/绝对化承诺的块标「未证实」，只存原文，不作判据。
"""
from __future__ import annotations
import argparse, hashlib, io, json, re, sys, time, unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "douyin-mcp-server" / "output"
KB = ROOT / "maiji-writing-skill" / "00-知识库"
PKG = ROOT / "maiji-writing-skill"
MANIFEST = PKG / "MANIFEST.md"
LIFECYCLE = PKG / "flows" / "file_lifecycle.json"
LEDGER = PKG / "docs" / "账本" / "吸收覆盖账.md"
INDEX = KB / "00.0_案例库总索引.md"
ALIAS = KB / "00.0b_UP别名台账.md"
PENDING_DIR = ROOT / "0_工作区" / "docs"


def pending_path():
    """取最新一份待吸收清单（日更文件名带日期，不能写死）。"""
    cands = sorted(PENDING_DIR.glob("待吸收清单_*.md"), key=lambda p: p.stat().st_mtime)
    return cands[-1] if cands else (PENDING_DIR / "待吸收清单_20261114.md")
ABSORB_LEDGER = ROOT / "0_工作区" / "docs" / "抖音文案吸收账.md"
READ_JSONL = ROOT / "0_工作区" / "docs" / "抖音入档_读数台账.jsonl"
OPEN_LEDGER = ROOT / "0_工作区" / "docs" / "未收口吸收批台账.md"

SERIES = "00.10e"
VOL_BYTES = 55 * 1024
MIN_BLOCK, MAX_BLOCK = 80, 600
CJK = re.compile(r"[\u4e00-\u9fff]")
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")
METHOD = re.compile(r"写法|技巧|公式|方法|步骤|原则|禁忌|必须|不要|别|不能|要写|先写|再写|然后|结构|节奏|冲突|悬念|爽点|人设|大纲|开篇|断章|伏笔|反转|铺垫|钩子|情绪|爽|虐|升级|金手指|世界观")
PRESCRIPT = re.compile(r"必须|不要|不能|别|一定要|最好|建议|记住|核心是|关键是|第一步|第二步|原则|禁忌|公式|三步|四步|五步")
LORE = re.compile(r"【设定|【考据|【名词|火影|海贼|咒术|鬼灭|进击|原神|死神|龙珠|柯南|动漫|漫画|忍者|尾兽|查克拉|恶魔果实|咒力|呼吸法|鬼杀队|尸魂界|圣斗士|奥特曼|假面骑士")
NARR = re.compile(r"死了|战斗|打败|打赢|剧情|角色|他说|她说|故事|这一集|这一话|动画|漫画里|最后|结局|主角|反派")
UNVER = re.compile(r"(?:\d+(?:\.\d+)?\s*(?:万|亿|%|％|倍|元|块|单|本|章|集|天|年|月|小时|分钟))|月入|年入|过万|破万|百万|千万|稳赚|必火|保证|包赚|百分百|绝对|爆款|第一名|榜首|翻倍|躺赚")
NEG = re.compile(r"不|别|勿|禁|避免|不要|无需|不能|莫")
# 包治理：外部 UP 专名只进 00.0b 别名台账；册名/正文一律用 K 号替代。
FLAGGED_UPS = ("望舒君", "资料菌", "XXUP", "上上签", "啊云云", "尘三昧", "脑洞干货厅",
               "西窗雨落", "迷生写作", "张扬码字", "学院派Academia", "雨宸", "Kaison",
               "西门的眼镜", "众生蒙太奇", "Swag亮亮", "槛外人philosophy",
               "风筝线metaphysics", "Story")

def up_label(up, k):
    return k if any(x in (up or "") for x in FLAGGED_UPS) else (up or "未登记")

def anchor_tag(k, up, with_up=False):
    """源锚：默认 K码；补收/拆混编时带 UP 名。"""
    kk = k or "K-未登记"
    return "%s｜UP %s" % (kk, up) if with_up else kk

def now(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def stamp(): return datetime.now().strftime("%Y%m%d_%H%M%S")
def read(p):
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return io.open(p, encoding=enc, errors="strict").read()
        except Exception:
            continue
    return io.open(p, encoding="utf-8", errors="replace").read()
def write(p, s):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    io.open(p, "w", encoding="utf-8", newline="\n").write(s)
def norm(s):
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"[\s_·\-—（）()「」“”\"'’、，,。.：:；;！!？?]+", "", s).lower()
def cn(n):
    d = "零一二三四五六七八九"
    if n <= 10: return "十" if n == 10 else d[n]
    if n < 20: return "十" + d[n % 10]
    if n < 100: return d[n // 10] + "十" + (d[n % 10] if n % 10 else "")
    return str(n)

def k_map():
    m = {}
    if not ALIAS.exists(): return m
    for line in read(ALIAS).splitlines():
        if not line.startswith("| K-"): continue
        c = [x.strip() for x in line.strip().strip("|").split("|")]
        k = c[0]
        for field in [c[2] if len(c) > 2 else "", c[5] if len(c) > 5 else ""]:
            for part in re.split(r"[；;]", field):
                part = re.sub(r"^知识库_", "", part.strip())
                for x in {part, part.split("_")[0]}:
                    key = norm(x)
                    if key and key not in m: m[key] = k
    return m

def k_for(up, kmap):
    n = norm(up)
    if n in kmap: return kmap[n]
    best = ""
    for key in kmap:
        if key and (key in n or n in key):
            if len(key) > len(best): best = key
    return kmap.get(best, "")

def clean_line(s):
    s = EMOJI.sub("", s).strip()
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()

def is_noise(s):
    if not s: return True
    cjk = len(CJK.findall(s))
    if len(s) >= 60 and cjk / max(len(s), 1) < 0.35: return True
    if len(s) < 40 and cjk < 4: return True
    return False

def split_long(s, limit=520):
    if len(s) <= MAX_BLOCK: return [s]
    parts = re.split(r"(?<=[。！？!?])", s)
    out, cur = [], ""
    for p in parts:
        if not p: continue
        while len(p) > MAX_BLOCK:
            cut = max(p.rfind("，", 0, MAX_BLOCK), p.rfind("、", 0, MAX_BLOCK), p.rfind(" ", 0, MAX_BLOCK))
            if cut < 300: cut = MAX_BLOCK
            if cur: out.append(cur); cur = ""
            out.append(p[:cut]); p = p[cut:]
        if cur and len(cur) + len(p) > limit:
            out.append(cur); cur = p
        else:
            cur += p
    if cur: out.append(cur)
    return out

def fp(s): return hashlib.sha256(re.sub(r"\s+", "", s).encode("utf-8")).hexdigest()[:12]

def parse_file(path):
    lines = read(path).splitlines()
    title, vid, ext, ti = "", "", "", None
    for i, l in enumerate(lines):
        if l.startswith("# ") and not title: title = l[2:].strip()
        m = re.search(r"\|\s*Video ID\s*\|\s*`?(\d+)`?", l)
        if m: vid = m.group(1)
        m = re.search(r"\|\s*Extracted at\s*\|\s*([\d\-: ]+)", l)
        if m: ext = m.group(1).strip()
        if l.strip() == "## Transcript": ti = i; break
    start = ti + 1 if ti is not None else 0
    blocks = []
    for i in range(start, len(lines)):
        t = clean_line(lines[i])
        if is_noise(t): continue
        for chunk in split_long(t):
            chunk = chunk.strip()
            if not chunk: continue
            if blocks and len(chunk) < MIN_BLOCK and len(blocks[-1][0]) + len(chunk) <= MAX_BLOCK:
                prev, a, _ = blocks[-1]
                blocks[-1] = (prev + " " + chunk, a, i + 1)
            else:
                blocks.append((chunk, i + 1, i + 1))
    return {"title": title, "video_id": vid, "extracted": ext, "blocks": blocks}

def short_title(t):
    t = re.sub(r"#[^\s#]+", "", t).strip()
    t = re.sub(r"\s+", " ", t)
    return (t[:28] or "（无题）")

def classify(text):
    lore = bool(LORE.search(text)); narr = bool(NARR.search(text))
    m = len(METHOD.findall(text)); p = bool(PRESCRIPT.search(text))
    if re.search(r"【设定|【考据|【名词", text): return "世界知识"
    if lore and not narr: return "世界知识"
    if p and m >= 2: return "道术条"
    if m >= 4 and not narr: return "道术条"
    return "案例样本"

def has_neg(s): return bool(NEG.search(s))

def grams(s, n=2):
    s = "".join(CJK.findall(s))
    return {s[i:i+n] for i in range(max(0, len(s) - n + 1))}

def load_ids(text):
    return set(re.findall(r"(?<!\d)(\d{19})(?!\d)", text))

def load_kb_fp():
    out = set()
    for p in KB.glob("*.md"):
        try: out.update(re.findall(r"〔块:([0-9a-f]{12})", read(p)))
        except Exception: pass
    return out

def load_canon_lines():
    rows = []
    for p in sorted(KB.glob("*.md")):
        name = p.name
        if name.startswith("00.10e") or name.startswith("00.10h"): continue
        sec = ""
        try: lines = read(p).splitlines()
        except Exception: continue
        for i, l in enumerate(lines, 1):
            if l.startswith("#"): sec = l.lstrip("#").strip()
            if not l.startswith("- **"): continue
            txt = re.sub(r"〔[^〕]*〕", "", l)
            txt = re.sub(r"^\-\s*\*\*[^*]*\*\*", "", txt).strip()
            if len(txt) < 60: continue
            rows.append((name, i, sec, txt))
    return rows

def build_sim_index(rows):
    idx = defaultdict(list); sets = []
    for rid, (_, _, _, txt) in enumerate(rows):
        g = grams(txt); sets.append(g)
        for x in g: idx[x].append(rid)
    return idx, sets

def best_match(text, rows, idx, sets):
    g = grams(text)
    if not g: return 0.0, None
    cnt = Counter()
    for x in g:
        for rid in idx.get(x, ()): cnt[rid] += 1
    best, brid = 0.0, None
    for rid, shared in cnt.most_common(80):
        if shared < 3: continue
        denom = min(len(g), len(sets[rid]))
        if not denom: continue
        sim = shared / denom
        if sim > best: best, brid = sim, rid
    return (best, rows[brid] if brid is not None else None)

def _parse_rows(t):
    out = []
    for line in t.splitlines():
        if not line.startswith("|") or "douyin-mcp-server/output/" not in line or "---" in line: continue
        c = [x.strip() for x in line.strip().strip("|").split("|")]
        if len(c) < 8: continue
        up, seq, vid, k, reason, est, status, rel = c[:8]
        out.append({"up": up, "seq": int(seq), "vid": vid, "k": k, "reason": reason,
                    "est": int(est) if est.isdigit() else 0, "path": rel})
    return out

def load_pending():
    s = read(pending_path())
    seen, out = set(), []
    for mk in ("### 本批后历史待入清单", "## 三、今日新增逐件清单", "## 三、逐件待入清单"):
        i = s.find(mk)
        if i < 0: continue
        j = s.find("\n## ", i + len(mk))
        seg = s[i:] if j < 0 else s[i:j]
        for it in _parse_rows(seg):
            key = (it["up"], it["seq"])
            if key in seen: continue
            seen.add(key); out.append(it)
    return out

def processed_pairs():
    done = set()
    for p in KB.glob("00.10e*.md"):
        try: txt = read(p)
        except Exception: continue
        m_fn = re.match(r"00\.10e\d+_创作工程案例库_UP原库_(.+?)分册", p.name)
        vol_up = norm(m_fn.group(1)) if m_fn else ""
        for m in re.finditer(r"###\s*源#(K-(?:\d+|未登记))(?:｜UP ([^｜\n]+))?｜件(\d+)", txt):
            k, up_in, seq = m.group(1), m.group(2), int(m.group(3))
            if k == "K-未登记":
                done.add((norm(up_in or vol_up), seq))
            else:
                done.add((k, seq))
    return done

def pick_batch(items, max_items, max_blocks):
    groups = []; cur_up, cur = None, []
    for it in items:
        if it["up"] != cur_up:
            if cur: groups.append((cur_up, cur))
            cur_up, cur = it["up"], []
        cur.append(it)
    if cur: groups.append((cur_up, cur))
    chosen, ni, nb = [], 0, 0
    for up, g in groups:
        if chosen and (ni + len(g) > max_items or nb + sum(x["est"] for x in g) > max_blocks):
            break
        gg = g[:max_items] if not chosen else g
        chosen.extend(gg); ni += len(gg); nb += sum(x["est"] for x in gg)
        if ni >= max_items or nb >= max_blocks: break
    return chosen, ni, nb

def existing_parts(up, k=""):
    safe = re.sub(r"[「」（）()·\s\"]", "", up_label(up, k))[:12]
    return len(list(KB.glob("00.10e*_创作工程案例库_UP原库_%s分册*.md" % safe)))

def next_tag():
    mx = 0
    for p in KB.glob("00.10e*.md"):
        m = re.match(r"00\.10e(\d+)_", p.name)
        if m: mx = max(mx, int(m.group(1)))
    return mx + 1

def register_volumes(vols):
    """新册补登 MANIFEST 表行＋生命周期表；失败只告警，不阻断吸收。"""
    if not vols:
        return 0
    added = 0
    try:
        s = read(MANIFEST)
        anchor = None
        for m in re.finditer(r"^\| 00\.10e\d+ \|.*$", s, re.M):
            anchor = m
        rows = []
        for v in vols:
            if ("| %s |" % v["file"]) in s:
                continue
            rows.append("| %s | %s 创作工程案例库·UP 原库·%s分册%s（块级摄取·待归类；%s） |" % (
                v["file"], v["tag"], up_label(v["up"], v["k"]), cn(v["part"]), v["k"] or "未登记"))
            added += 1
        if rows:
            if anchor:
                s = s[:anchor.end()] + "\n" + "\n".join(rows) + s[anchor.end():]
            else:
                s += "\n" + "\n".join(rows) + "\n"
            write(MANIFEST, s)
    except Exception as e:
        print("WARN: MANIFEST 补登失败 %r" % e, file=sys.stderr)
    try:
        if LIFECYCLE.exists():
            d = json.load(io.open(LIFECYCLE, encoding="utf-8-sig"))
            have = {f.get("path") for f in d.get("files", [])}
            for v in vols:
                p = "00-知识库/" + v["file"]
                if p in have:
                    continue
                d["files"].append({"path": p, "layer": "00-知识库", "doc_type": "UP原库摄取册",
                                   "status": "生效", "owner": "包内", "status_since": datetime.now().strftime("%Y-%m-%d"),
                                   "version": datetime.now().strftime("%Y%m%d"),
                                   "note": "块级摄取·待归类（源#%s）" % (v["k"] or "未登记")})
            d["counts"]["total"] = len(d["files"])
            for st in ("生效", "候选", "退役"):
                d["counts"][st] = sum(1 for f in d["files"] if f.get("status") == st)
            d["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            io.open(LIFECYCLE, "w", encoding="utf-8", newline="\n").write(
                json.dumps(d, ensure_ascii=False, indent=2) + "\n")
    except Exception as e:
        print("WARN: lifecycle 补登失败 %r" % e, file=sys.stderr)
    return added

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", default="")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--max-items", type=int, default=60)
    ap.add_argument("--max-blocks", type=int, default=500)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--up", default="")
    ap.add_argument("--seqs", default="", help="只取该件序号区间，如 48-99")
    ap.add_argument("--skip-seqs", default="", help="再排除的件序号，逗号分隔")
    ap.add_argument("--no-anchor-dedup", action="store_true",
                    help="跳过 源锚已入 过滤（别名同 K 码的补收用）")
    ap.add_argument("--anchor-up", action="store_true",
                    help="源锚带 UP 名：源#K-xxx｜UP <名>｜件N")
    args = ap.parse_args()
    if not args.batch:
        args.batch = "AUTO-" + datetime.now().strftime("%Y%m%d")
    t0 = time.time()

    kmap = k_map()
    items = load_pending()
    done = processed_pairs()
    if not args.no_anchor_dedup:
        items = [x for x in items
                 if ((x["k"] if x["k"].startswith("K-") else k_for(x["up"], kmap)), x["seq"]) not in done]
    if args.up: items = [x for x in items if x["up"] == args.up]
    if args.seqs:
        lo, _, hi = args.seqs.partition("-")
        lo, hi = int(lo), int(hi or lo)
        items = [x for x in items if lo <= x["seq"] <= hi]
    if args.skip_seqs:
        skip = {int(x) for x in re.split(r"[,，\s]+", args.skip_seqs) if x.strip().isdigit()}
        items = [x for x in items if x["seq"] not in skip]
    chosen, ni, est_blocks = pick_batch(items, args.max_items, args.max_blocks)
    print("待入总 %d 件／%d UP；本批 %d 件／估 %d 块；起 %s 件%d" % (
        len(items), len(set(x["up"] for x in items)), len(chosen), est_blocks,
        chosen[0]["up"] if chosen else "-", chosen[0]["seq"] if chosen else 0), file=sys.stderr)

    ledger_text = read(LEDGER)
    ledger_ids = load_ids(ledger_text)
    kb_fp = load_kb_fp()
    canon = load_canon_lines()
    sim_idx, sim_sets = build_sim_index(canon)
    print("既有条目 %d｜指纹 %d｜比对键 %d" % (len(canon), len(kb_fp), len(sim_idx)), file=sys.stderr)

    tag_counter = [next_tag()]
    per_up = defaultdict(list)
    records, dup_rows, conflict_rows, noise_files = [], [], [], []
    unverified = skipped_fp = near = 0

    for it in chosen:
        p = ROOT / it["path"]
        if not p.exists():
            noise_files.append((it["up"], it["seq"], it["vid"], "源件缺失")); continue
        d = parse_file(p)
        if not d["blocks"]:
            noise_files.append((it["up"], it["seq"], it["vid"], "空正文/纯噪声")); continue
        k = it["k"] if it["k"].startswith("K-") else k_for(it["up"], kmap)
        per_up[it["up"]].append({"it": it, "k": k, "d": d})
        for text, a, b in d["blocks"]:
            f = fp(text)
            if f in kb_fp:
                skipped_fp += 1; continue
            form = classify(text)
            rec = {"up": it["up"], "seq": it["seq"], "vid": it["vid"], "k": k,
                   "anc": anchor_tag(k, it["up"], args.anchor_up),
                   "title": d["title"], "text": text, "fp": f, "a": a, "b": b,
                   "form": form, "unver": bool(UNVER.search(text)), "sim": 0.0,
                   "match": None, "conflict": False, "dup": False, "vol": "", "no": 0}
            if form == "道术条":
                sim, match = best_match(text, canon, sim_idx, sim_sets)
                rec["sim"], rec["match"] = sim, match
                if sim >= 0.50:
                    rec["dup"] = True
                    if match and (has_neg(text) != has_neg(match[3])):
                        rec["conflict"] = True
                        conflict_rows.append(rec)
                    else:
                        dup_rows.append(rec)
                else:
                    rec["form"] = "道术条·新增待裁"
                    if sim >= 0.25: near += 1
            records.append(rec)
            kb_fp.add(f)

    volumes = []
    written = unverified_written = 0

    def flush(up, cur, cur_bytes, cur_seqs, cur_blocks, part, safe, kk):
        nonlocal written
        if not cur: return None
        tag = "%s%d" % (SERIES, tag_counter[0]); tag_counter[0] += 1
        lab = up_label(up, kk)
        anc_v = anchor_tag(kk, up, args.anchor_up)
        base = "%s_创作工程案例库_UP原库_%s分册%s.md" % (tag, safe, cn(part))
        head = [
            "# %s 创作工程案例库·UP 原库·%s分册%s（%s·查询库，禁整读）" % (tag, lab, cn(part), kk or "未登记"),
            "",
            "> **文档性质**：UP 原库机械摄取卷（块级逐字，未做语义归并；%s 全量吸收批）" % args.batch,
            "> **本册范围**：%s 抖音口播源件第 %d–%d 件（源#%s）。" % (lab, min(cur_seqs), max(cur_seqs), anc_v),
            "> **册界**：00.10e 族＝创作工程与入门域（00.10）下的 UP 原库待归类区；本卷＝%s 第 %s 卷（源#%s）。" % (lab, cn(part), anc_v),
            "> **摄取纪律**：块级逐字（禁改写／禁摘要／禁截断）；每条尾锚＝块指纹＋登记号＋源行区间；单块 80–600 字。",
            "> **语义状态**：待裁——道术条已做近似比对（>=0.50 判重只指认；新增者标「新增待裁」）；同义重复／按需使用见 `docs/账本/语义待裁清单.md`。",
            "> **覆盖状态**：源件 %d｜块 %d｜已入 %d｜判不收 0。" % (len(cur_seqs), cur_blocks, cur_blocks),
            "> **取用**：按 `源#%s-<序号>` 或块指纹回查；本卷不作定论级引用。" % anc_v,
            "",
            "## §1 %s 源件 %d–%d（源#%s）" % (lab, min(cur_seqs), max(cur_seqs), anc_v),
            "",
        ]
        text = "\n".join(head + cur) + "\n"
        v = {"tag": tag, "file": base, "up": up, "k": kk, "part": part,
             "items": len(cur_seqs), "blocks": cur_blocks,
             "bytes": len(text.encode("utf-8")),
             "seq_from": min(cur_seqs), "seq_to": max(cur_seqs)}
        volumes.append(v)
        if not args.dry_run: write(KB / base, text)
        return v

    global_blocks = Counter()
    for up, units in per_up.items():
        kk = next((u["k"] for u in units if u["k"]), "")
        safe = re.sub(r"[「」（）()·\s\"]", "", up_label(up, kk))[:12]
        part = existing_parts(up, kk) + 1
        cur, cur_bytes, cur_seqs, cur_blocks = [], 0, set(), 0
        cur_tag = ""
        for u in units:
            it, k, d = u["it"], u["k"], u["d"]
            head = "### 源#%s｜件%d %s\n\n" % (anchor_tag(k, it["up"], args.anchor_up), it["seq"], short_title(d["title"]))
            if cur and cur_bytes + len(head.encode("utf-8")) > VOL_BYTES:
                if cur_blocks:
                    flush(up, cur, cur_bytes, cur_seqs, cur_blocks, part, safe, kk); part += 1
                cur, cur_bytes, cur_seqs, cur_blocks = [], 0, set(), 0
            cur.append(head); cur_bytes += len(head.encode("utf-8")); cur_seqs.add(it["seq"])
            for rec in [r for r in records if r["up"] == it["up"] and r["seq"] == it["seq"] and not r["dup"]]:
                tagf = rec["form"]
                if rec["unver"]: tagf += "·未证实"
                if rec["conflict"]: tagf += "·互斥待核"
                line = "- **【%s】**%s〔块:%s｜源#%s｜件%d L%d-L%d〕\n" % (
                    tagf, rec["text"], rec["fp"],
                    rec["anc"], it["seq"], rec["a"], rec["b"])
                if cur and cur_bytes + len(line.encode("utf-8")) > VOL_BYTES:
                    if cur_blocks:
                        flush(up, cur, cur_bytes, cur_seqs, cur_blocks, part, safe, kk); part += 1
                    cur, cur_bytes, cur_seqs, cur_blocks = [], 0, set(), 0
                    cur.append(head); cur_bytes += len(head.encode("utf-8")); cur_seqs.add(it["seq"])
                cur.append(line); cur_bytes += len(line.encode("utf-8"))
                cur_blocks += 1; written += 1
                global_blocks[rec["form"]] += 1
                if rec["unver"]: unverified_written += 1
                rec["vol"] = "%s%d" % (SERIES, tag_counter[0])
                rec["no"] = cur_blocks
        if cur_blocks:
            flush(up, cur, cur_bytes, cur_seqs, cur_blocks, part, safe, kk)

    if not args.dry_run and volumes:
        register_volumes(volumes)

    # 覆盖账行
    led_rows = []
    for r in records:
        if r["dup"]:
            m = r["match"]
            led_rows.append("| 源#%s | 源#%s（ID %s·块%s） | 道 | 道术条 | 判重·只指认 | 判重（%s L%d） |" % (
                r["anc"], r["anc"], r["vid"], r["fp"],
                m[0] if m else "-", m[1] if m else 0))
        else:
            axis = "道" if r["form"].startswith("道术条") else "案例"
            form = "道术条" if r["form"].startswith("道术条") else r["form"]
            status = "已入·待裁" if r["form"].startswith("道术条") else "已入"
            led_rows.append("| 源#%s | 源#%s（ID %s·块%s） | %s | %s | %s §1 条%d | %s |" % (
                r["anc"], r["anc"], r["vid"], r["fp"],
                axis, form, r["vol"], r["no"], status))

    if not args.dry_run and led_rows:
        i = ledger_text.find("\n## 二、")
        if i < 0: i = len(ledger_text)
        write(LEDGER, ledger_text[:i] + "\n" + "\n".join(led_rows) + "\n" + ledger_text[i:])
        ix = read(INDEX)
        last = None
        for m in re.finditer(r"^\| 00\.10e\d+ \|.*$", ix, re.M): last = m
        add = "\n".join("| %s | %s | %s（%s） | %d→%d |" % (
            v["tag"], v["file"], up_label(v["up"], v["k"]), v["k"] or "未登记", v["items"], v["blocks"]) for v in volumes)
        if last: ix = ix[:last.end()] + "\n" + add + ix[last.end():]
        else: ix += "\n" + add + "\n"
        write(INDEX, ix)

    c = Counter(r["form"] for r in records)
    total_dao = c["道术条"] + c["道术条·新增待裁"]
    total_dup = len(dup_rows) + len(conflict_rows)

    if not args.dry_run:
        lines = ["", "## %s｜全量吸收批 %s（%s）" % (args.batch, args.batch, datetime.now().strftime("%Y-%m-%d")), "",
                 "> 源面：`douyin-mcp-server/output` 原始转写件；本批 %d 件／入 %d 块。机械摄取卷＝00.10e 族，语义待归并。" % (len(chosen), written),
                 "> 判型：道术条（含新增待裁）／案例样本／世界知识；判重 >=0.50 只指认；未证实只存原文不作判据。", "",
                 "| 条号 | 摘要 | 源锚 | 层位 | 落点册 | 形式 | 编号 | 接线 | 依据／说明 |",
                 "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for n, r in enumerate(records, 1):
            summary = re.sub(r"[|\n\r]+", "／", r["text"])[:42] + ("…" if len(r["text"]) > 42 else "")
            if r["dup"]:
                m = r["match"]; loc = "既有册"; no = "L%d" % (m[1] if m else 0); form = "道术条"
                basis = "判重·只指认（sim %.3f；%s L%d §%s）" % (r["sim"], m[0], m[1], m[2]) if m else "判重·只指认"
            else:
                loc = r["vol"]; no = "§1 条%d" % r["no"]; form = r["form"]
                basis = "块%s；机判型待核" % r["fp"]
                if r["sim"]:
                    m0 = r["match"]
                    basis += "；近似最高 %.3f（%s L%d §%s）" % (r["sim"], m0[0], m0[1], m0[2]) if m0 else "；近似最高 %.3f" % r["sim"]
            if r["unver"]: basis += "；未证实"
            if r["conflict"]: basis += "；互斥待核"
            lines.append("| %s-%03d | %s | 源#%s｜件%d L%d-L%d | %s | %s | %s | %s | 机械落位 | %s |" % (
                args.batch, n, summary, r.get("anc") or (r["k"] or "K-未登记"), r["seq"], r["a"], r["b"],
                form, loc, form, no, basis))
        lines += ["", "**本批读数**：源件 %d｜入块 %d｜道术条 %d（新增待裁 %d／判重 %d／同题指认 %d）｜案例样本 %d｜世界知识 %d｜判重 %d｜未证实 %d｜互斥 %d｜不切块/缺件 %d｜新册 %d。" % (
            len(chosen), written, total_dao, c["道术条·新增待裁"], total_dup, near,
            c["案例样本"], c["世界知识"], total_dup, unverified_written,
            len(conflict_rows), len(noise_files), len(volumes))]
        write(ABSORB_LEDGER, read(ABSORB_LEDGER) + "\n".join(lines) + "\n")

        all_out_ids = set()
        for p in OUT.glob("*/*.md"):
            m = re.search(r"(\d{19})", p.name)
            if m: all_out_ids.add(m.group(1))
        new_ids = load_ids(read(LEDGER))
        denom = len(all_out_ids)
        rem = len(items) - len(chosen)
        value = denom - rem
        rec = {"schema": "douyin_read_v1", "ts": stamp(), "value": value, "denom": denom,
               "unanchored": rem, "ups": len(set(x["up"] for x in items)),
               "source": "B14 全量吸收批（output 原始转写→00.10e 块级摄取→覆盖账）",
               "ticket": "T-1670", "history": "上一读数 20261108_122800（12557/12934/377）｜中间批未逐次落账",
               "note": "%s：本批 %d 件／%d 块；道术条 %d（新增待裁 %d／判重 %d／同题指认 %d）／案例样本 %d／世界知识 %d；未证实 %d；互斥 %d；剩余 %d 件" % (
                   args.batch, len(chosen), written, total_dao, c["道术条·新增待裁"], total_dup, near,
                   c["案例样本"], c["世界知识"], unverified_written, len(conflict_rows),
                   len(items) - len(chosen))}
        io.open(READ_JSONL, "a", encoding="utf-8", newline="\n").write(json.dumps(rec, ensure_ascii=False) + "\n")

        rem_items = len(items) - len(chosen)
        rem_blocks = sum(x["est"] for x in items[len(chosen):])
        nxt = items[len(chosen)] if rem_items else None
        sec = ["", "## %s｜全量吸收批 %s" % (datetime.now().strftime("%Y-%m-%d"), args.batch), "",
               "- 批次：%s｜%d 件／%d UP｜估 %d 块" % (args.batch, len(chosen), len(set(x["up"] for x in chosen)), est_blocks),
               "- 读数：入 %d 块（道术条 %d／新增待裁 %d／判重 %d／同题指认 %d；案例样本 %d；世界知识 %d）；未证实 %d；互斥 %d；不切块/缺件 %d" % (
                   written, total_dao, c["道术条·新增待裁"], total_dup, near, c["案例样本"], c["世界知识"],
                   unverified_written, len(conflict_rows), len(noise_files)),
               "- 落位：%s" % ("、".join(v["tag"] for v in volumes) or "（dry-run 未写）"),
               "- 剩余：%d 件／估 %d 块" % (rem_items, rem_blocks),
               "- 下一批起点：%s" % (("%s 件%d" % (nxt["up"], nxt["seq"])) if nxt else "无"),
               "- 关联：T-1670 复活（用户令「继续吸收」）；旧台账行未改动，仅追加。"]
        write(OPEN_LEDGER, read(OPEN_LEDGER) + "\n".join(sec) + "\n")

    out = {"batch": args.batch, "items": len(chosen), "ups": len(set(x["up"] for x in chosen)),
           "est_blocks": est_blocks, "written": written,
           "forms": {"道术条": total_dao, "道术条新增待裁": c["道术条·新增待裁"],
                     "案例样本": c["案例样本"], "世界知识": c["世界知识"]},
           "dup": total_dup, "unverified": unverified_written, "conflict": len(conflict_rows),
           "near": near, "noise": len(noise_files), "skipped_fp": skipped_fp,
           "volumes": volumes, "remaining_items": len(items) - len(chosen),
           "remaining_blocks": sum(x["est"] for x in items[len(chosen):]),
           "next": ({"up": items[len(chosen)]["up"], "seq": items[len(chosen)]["seq"]} if len(items) > len(chosen) else None),
           "conflicts": [{"up": r["up"], "seq": r["seq"], "text": r["text"][:60],
                          "match": (r["match"][0], r["match"][1]) if r["match"] else None} for r in conflict_rows],
           "secs": round(time.time() - t0, 1)}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    print("机械段：写库 %d 块｜判重 %d 条｜待裁 %d 条｜用时 %.1fs" % (
        written, total_dup, c["道术条·新增待裁"], time.time() - t0))

if __name__ == "__main__":
    main()
