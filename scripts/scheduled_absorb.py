#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""全量整理＋吸收（Windows 计划任务的入口）

由 schedule.json 的 absorb 任务驱动（默认 05:00）。程序内自管：WebUI 保存
配置后自动重登记计划任务，执行仍由系统任务计划服务拉起，进程无需常驻。

说明：真正把转写稿「蒸馏进知识库/技能包」需要人工或 Agent 判断（规则 27
裁决不可机械代替），本任务做的是可自动化的那一段：
  1. distill_gap_check.py --absorb-pending  未收口吸收批点名 + 未吸收源件报数
  2. absorb_gap.py --json                   逐 UP 盘上件/已入件/差额
  3. 落日志与 last_run.json，供 WebUI 显示最近一次结果与待办规模

用法：
    python -B scripts/scheduled_absorb.py --check
    python -B scripts/scheduled_absorb.py --once
    python -B scripts/scheduled_absorb.py --disable / --enable

退出码：0=无待办 1=有待吸收（需蒸馏） 2=守卫跳过/已停用 3=异常
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WS = ROOT.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import schedule_config  # noqa: E402

# 定时任务动作统一进 operation_logs（日志不可用不得拖垮任务）
sys.path.insert(0, str(ROOT / "douyin-video" / "scripts"))
try:
    from operation_logger import log_scheduler, new_batch_id  # noqa: E402
except Exception:  # pragma: no cover
    def log_scheduler(*_a, **_k):  # type: ignore
        pass

    def new_batch_id():  # type: ignore
        return ""

_TRIGGER = "manual"

DISTILL = WS / "maiji-writing-skill" / "scripts" / "distill_gap_check.py"
GAP = ROOT / "scripts" / "absorb_gap.py"
GEN = ROOT / "scripts" / "gen_pending_list.py"
MECH = ROOT / "scripts" / "absorb_full.py"
LOG_DIR = ROOT / "logs" / "scheduled_absorb"
LAST = LOG_DIR / "last_run.json"
LOCK = ROOT / "output" / "刷新日志" / ".scheduled_absorb.lock"
REFRESH_LOCK = ROOT / "output" / "刷新日志" / ".scheduled_refresh.lock"
STATE_FILE = ROOT / "output" / "刷新日志" / "driver_state.json"
PENDING = ROOT / "pending_links.json"

# 告警落点：任务工单目录的当日件（与 scheduled_refresh 同一套：pythonw 丢 stdout 时的唯一可见面）
ALERT_DIR = WS / "任务工单"
ALERT_BACKUP = WS / "0_工作区" / "_work" / "调度告警备份"
SAFE_WRITE = WS / "maiji-writing-skill" / "scripts" / "safe_write.py"

# 跳过后的当天重试：与 scheduled_refresh 同语义（默认每 2 小时一次、最多 2 次；只认显式开关）
RETRY_INTERVAL_S = int(os.environ.get("DOUYIN_ABSORB_RETRY_INTERVAL", "7200") or 0)
RETRY_MAX = int(os.environ.get("DOUYIN_ABSORB_RETRY_MAX", "2") or 0)
_RETRY = False   # main() 里按上下文设定

BATCH_CNT = re.compile(r"\[未收口吸收批\]\s*在册\s*(\d+)\s*张")
SRC_CNT = re.compile(r"\[未吸收源件\]\s*(\d+)\s*件")


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def alert_path(day: str = "") -> Path:
    day = day or datetime.now().strftime("%Y%m%d")
    return ALERT_DIR / ("调度告警_抖音定时吸收_%s.md" % day)


def _retry_context(args) -> bool:
    """是否在守卫跳过之后当天重试（与 scheduled_refresh 同判据）。

    只认显式开关：计划任务动作带 --retry-on-skip，或环境变量强制。
    手动 --once/--check/--dry-run 不传该参数，行为与原来一致。
    """
    env = (os.environ.get("DOUYIN_ABSORB_RETRY") or "").strip()
    if env == "0":
        return False
    if env == "1":
        return True
    return bool(getattr(args, "retry_on_skip", False))


def _load_safe_write():
    import importlib.util
    # safe_write 依赖同目录的 _encoding_guard / chain_pre_gate 等本地模块
    d = str(SAFE_WRITE.parent)
    if d not in sys.path:
        sys.path.insert(0, d)
    spec = importlib.util.spec_from_file_location("maiji_safe_write", SAFE_WRITE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.safe_write


def write_alert(status: str, rec: dict | None = None,
                reasons: list | None = None, note: str = "",
                next_retry: str = "-") -> dict:
    """把每轮 ok/warn/skip/fail 写进当天告警件（字段对齐刷新那份）。

    字段序：时间｜结局｜ok/skip/fail｜各域 rc 与读数｜失败原因｜下次重试时间。
    写盘走 safe_write（「任务工单/」控制面），备份到 0_工作区/_work/调度告警备份/；
    告警写失败只回传错误，绝不影响吸收语义。
    """
    try:
        ALERT_DIR.mkdir(parents=True, exist_ok=True)
        p = alert_path()
        if p.exists():
            old = p.read_text(encoding="utf-8", errors="replace")
        else:
            old = ("# 调度告警｜抖音定时吸收｜%s\n\n"
                   "> 自动写入：scheduled_absorb.py → safe_write。每轮一行："
                   "时间｜结局｜各域 rc 与读数｜失败原因｜下次重试时间。\n"
                   "> 本件用途：定时吸收跳过或失败时，不开 WebUI 也能在任务工单目录看见。\n\n"
                   % datetime.now().strftime("%Y-%m-%d"))
        g = (rec or {}).get("gates") or {}
        doms = g.get("domains") or {}
        if doms:
            dom_txt = "；".join("%s rc=%s（%s）" % (
                k, v.get("rc"), v.get("reading")) for k, v in doms.items())
        else:
            dom_txt = "未到闸（本轮到不了分域）"
        why = "；".join(reasons or (rec or {}).get("reasons") or []) or "-"
        line = "- %s｜**%s**｜ok=%d skip=%d fail=%d｜域：%s｜原因：%s%s｜下次重试：%s" % (
            now(), status,
            1 if status == "ok" else 0,
            1 if status == "skip" else 0,
            1 if status == "fail" else 0,
            dom_txt, why, note, next_retry)
        SW = _load_safe_write()
        SW(str(p), old.rstrip("\n") + "\n" + line + "\n",
           expect_contains="调度告警", backup_root=str(ALERT_BACKUP))
        return {"ok": True, "path": str(p)}
    except Exception as e:
        return {"ok": False, "error": repr(e)}


def pid_alive(pid) -> bool:
    # Windows 下 os.kill(pid, 0) 不可用；统一走 schedule_config 的实现
    return schedule_config.pid_alive(pid)


def _py() -> str:
    """优先用 python.exe（有控制台/管道更稳），失败再退回当前解释器。"""
    exe = ROOT / ".venv" / "Scripts" / "python.exe"
    return str(exe) if exe.exists() else sys.executable


def guard() -> list:
    reasons = []
    if not DISTILL.exists():
        reasons.append("distill_gap_check.py 不存在")
    if not GAP.exists():
        reasons.append("absorb_gap.py 不存在")
    if not GEN.exists():
        reasons.append("gen_pending_list.py 不存在")
    if LOCK.exists():
        try:
            info = json.loads(LOCK.read_text(encoding="utf-8-sig"))
        except Exception:
            info = {}
        if pid_alive(info.get("pid", 0)):
            reasons.append("上一轮整理吸收未结束（pid=%s）" % info.get("pid"))
        else:
            LOCK.unlink(missing_ok=True)
    if REFRESH_LOCK.exists():
        try:
            info = json.loads(REFRESH_LOCK.read_text(encoding="utf-8-sig"))
        except Exception:
            info = {}
        if pid_alive(info.get("pid", 0)):
            reasons.append("全量拉新正在跑（pid=%s），先让它结束" % info.get("pid"))
    if STATE_FILE.exists():
        try:
            st = json.loads(STATE_FILE.read_text(encoding="utf-8-sig"))
        except Exception:
            st = {}
        if pid_alive(st.get("pid", 0)):
            reasons.append("driver 正在跑（pid=%s, stage=%s）" % (st.get("pid"), st.get("stage")))
    try:
        if PENDING.exists():
            q = json.loads(PENDING.read_text(encoding="utf-8-sig"))
            live = [it for it in (q.get("items") or [])
                    if str(it.get("status", "")).lower() in ("pending", "processing", "running", "jumped")]
            if live:
                reasons.append("待解析队列还有 %d 条" % len(live))
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object {$_.CommandLine -match "
             "'refresh_all\\.py|batch_extractor\\.py|distill_gap_check\\.py'} | "
             "Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=20).stdout
        pids = [x.strip() for x in out.splitlines() if x.strip().isdigit()]
        if pids:
            reasons.append("已有刷新/提取/整理进程在跑（pid=%s）" % ",".join(pids))
    except Exception:
        pass
    return reasons


def acquire_lock() -> bool:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(), "started_at": now()}, fh, ensure_ascii=False)
    return True


def manifest_verdict(rc: int, txt: str) -> tuple:
    """manifest 域判红口径（2026-11-16 用户裁定·第二写法）。

    判据＝「漏登记＋registry 漏登＋lifecycle 漏登 == 0」。
    **热件 sha 漂移不进判红**：漂移是写序/在途热改的机械残影，不是漏登记；
    若把漂移算红，告警每天必红一次，天天红＝没人看，比不告警更坏。
    漂移数只留在读数里备查（reading 里的「漂移N」）。

    读数缺失不默认过关：rc≠0 且解析不到 FAIL 计数时判红（暴露「读数看不见」）。
    """
    m = re.search(r"manifest_check FAIL：(\d+)", txt)
    total = int(m.group(1)) if m else (0 if rc == 0 else None)
    def cnt(tag):
        return len(re.findall(r"^\s*\[%s\]" % tag, txt, re.M))
    miss = {"漏登记": cnt("漏登记"), "registry漏登": cnt("registry漏登"),
            "lifecycle漏登": cnt("lifecycle漏登")}
    drift = cnt("registry漂移")
    if rc != 0 and m is None:
        return False, {"fail": total, "missing": None, "drift": drift,
                       "reading": "缺口=?（读数缺失，判红）"}
    missing = sum(miss.values())
    return missing == 0, {
        "fail": total, "missing": missing, "drift": drift,
        "reading": "缺口=%s（真漏登=%s：漏登记%s／registry%s／lifecycle%s；漂移%s 仅读数）"
                   % (total, missing, miss["漏登记"], miss["registry漏登"],
                      miss["lifecycle漏登"], drift)}


def run_gates() -> dict:
    """机械段写库后跑包内五闸；读数只记关键项。"""
    sk = WS / "maiji-writing-skill" / "scripts"
    env = dict(os.environ, MAIJI_PKG_WRITE_OK="1", PYTHONIOENCODING="utf-8")
    def call(name, argv):
        try:
            p = subprocess.run([_py(), "-B", str(sk / name)] + argv, cwd=str(WS),
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=1200, env=env)
            return p.returncode, (p.stdout or "") + (p.stderr or "")
        except Exception as e:
            return -1, repr(e)
    call("manifest_check.py", ["--emit-all"])
    doms = {}
    def mark(name, rc, ok, reading):
        doms[name] = {"rc": rc, "pass": bool(ok), "reading": reading}
    rc, txt = call("pack_boundary_check.py", [])
    m = re.search(r"基线外新增(?:\(真债\))?=(\d+)", txt); pack_new = int(m.group(1)) if m else None
    mark("pack_boundary", rc, pack_new == 0, "基线外真债=%s" % pack_new)
    rc, txt = call("source_citation_check.py", [])
    m = re.search(r"基线外新增=(\d+)", txt); citation_new = int(m.group(1)) if m else None
    mark("source_citation", rc, citation_new == 0, "基线外新增=%s" % citation_new)
    rc, txt = call("absorb_coverage_check.py", ["--strict-forms"])
    m = re.search(r"缺口\s*(\d+)", txt); coverage_gaps = int(m.group(1)) if m else None
    mark("absorb_coverage", rc, coverage_gaps == 0, "缺口=%s" % coverage_gaps)
    rc, txt = call("manifest_check.py", [])
    manifest_ok, mrec = manifest_verdict(rc, txt)
    manifest_fail, manifest_missing = mrec["fail"], mrec["missing"]
    # 判红依据：只认真漏登记；registry 漂移（热件/写序）只记读数，不判红（详见 manifest_verdict 注释）
    mark("manifest", rc, manifest_ok, mrec["reading"])
    rc, txt = call("fingerprint_check.py", [])
    m = re.search(r"命中\s*(\d+)\s*处外部指纹", txt); fingerprint_hits = int(m.group(1)) if m else None
    # 判红依据（2026-11-16 用户令）：命中数是读数不是失败，只记录不判红
    mark("fingerprint", rc, True, "命中=%s（仅记录，不判红）" % fingerprint_hits)
    rc, txt = call("ssot_ref_check.py", [])
    ssot_ok = "OK:" in txt
    mark("ssot_ref", rc, ssot_ok, "OK" if ssot_ok else "裸复述未过")
    failed = [k for k, v in doms.items() if not v["pass"]]
    return {
        "verdict": "分域",
        "pass": not failed,
        "domains": doms,
        "failed_domains": failed,
        "failed": len(failed),
        "rcs": {k: v["rc"] for k, v in doms.items()},
        "pack_new": pack_new,
        "citation_new": citation_new,
        "coverage_gaps": coverage_gaps,
        "manifest_fail": manifest_fail,
        "manifest_missing": manifest_missing,
        "manifest_drift": mrec.get("drift"),
        "fingerprint_hits": fingerprint_hits,
        "ssot_ok": ssot_ok,
    }


def _run(argv: list, timeout: int) -> tuple:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        p = subprocess.run(argv, cwd=str(WS), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, env=env)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return -1, repr(e)


def run_once() -> int:
    reasons = guard()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id = new_batch_id()
    log_scheduler("absorb", "trigger", trigger=_TRIGGER, run_id=run_id,
                  retry_context=_RETRY,
                  guard="；".join(reasons) or "可跑")

    # 守卫跳过不再直接放弃：定时上下文带着 --retry-on-skip 时，当天每 2 小时再试，最多 2 次
    attempts = 0
    while reasons and _RETRY and attempts < RETRY_MAX:
        attempts += 1
        wait = max(1, RETRY_INTERVAL_S)
        note = "（第 %d 次拦截，%s后重试）" % (
            attempts, ("%d 分钟" % (wait // 60)) if wait >= 120 else ("%d 秒" % wait))
        nxt = datetime.fromtimestamp(time.time() + wait).strftime("%Y-%m-%d %H:%M:%S")
        (LOG_DIR / ("%s_r%d.log" % (stamp, attempts))).write_text(
            "%s SKIP 守卫拦截：%s%s\n" % (now(), "；".join(reasons), note), encoding="utf-8")
        write_alert("skip", reasons=reasons, note=note, next_retry=nxt)
        print("%s SKIP 守卫拦截：%s%s" % (now(), "；".join(reasons), note))
        log_scheduler("absorb", "retry", status="skip", run_id=run_id,
                      attempt=attempts, wait_s=wait, reasons=reasons)
        time.sleep(wait)
        reasons = guard()

    if reasons:
        msg = "；".join(reasons)
        note = ("（当天已重试 %d 次仍被拦，放弃到次日）" % attempts) if attempts else ""
        (LOG_DIR / (stamp + ".log")).write_text(
            "%s SKIP 守卫拦截：%s%s\n" % (now(), msg, note), encoding="utf-8")
        alert = write_alert("skip", reasons=reasons, note=note)
        (LOG_DIR / "last_run.json").write_text(json.dumps({
            "status": "skip", "started_at": now(), "finished_at": now(),
            "duration_s": 0, "reasons": reasons, "message": msg,
            "attempts": attempts, "retry_max": RETRY_MAX,
            "retry_exhausted": bool(attempts), "alert": alert,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print("%s SKIP 守卫拦截：%s%s" % (now(), msg, note))
        log_scheduler("absorb", "skip", status="skip", run_id=run_id,
                      reasons=reasons, attempts=attempts + 1,
                      retry_exhausted=bool(attempts))
        return 2

    if not acquire_lock():
        msg = "锁已被占用（另一轮刚启动）"
        alert = write_alert("skip", reasons=[msg])
        (LOG_DIR / "last_run.json").write_text(json.dumps({
            "status": "skip", "started_at": now(), "finished_at": now(),
            "duration_s": 0, "reasons": [msg], "message": msg,
            "alert": alert,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print("%s SKIP %s" % (now(), msg))
        log_scheduler("absorb", "skip", status="skip", run_id=run_id,
                      reasons=[msg], note="锁被占用")
        return 2

    started = now()
    t0 = time.time()
    try:
        rc_distill, out_distill = _run([_py(), "-B", str(DISTILL), "--absorb-pending"], 900)
        log_scheduler("absorb", "step", status="ok" if rc_distill in (0, 2) else "error",
                      run_id=run_id, step="distill", rc=rc_distill,
                      duration_s=int(time.time() - t0))
        _t = time.time()
        rc_gap, out_gap = _run([_py(), "-B", str(GAP), "--json"], 900)
        log_scheduler("absorb", "step", status="ok" if rc_gap == 0 else "error",
                      run_id=run_id, step="gap", rc=rc_gap,
                      duration_s=int(time.time() - _t))
        # 每轮先按当天差集重生成清单：否则会一直读旧清单、待入恒为 0
        _t = time.time()
        rc_gen, out_gen = _run([_py(), "-B", str(GEN)], 900)
        log_scheduler("absorb", "step", status="ok" if rc_gen == 0 else "error",
                      run_id=run_id, step="gen", rc=rc_gen,
                      duration_s=int(time.time() - _t))
        _t = time.time()
        rc_mech, out_mech = _run([_py(), "-B", str(MECH), "--once"], 1800)
        log_scheduler("absorb", "step", status="ok" if rc_mech in (0, 2) else "error",
                      run_id=run_id, step="mechanical", rc=rc_mech,
                      duration_s=int(time.time() - _t))
    finally:
        LOCK.unlink(missing_ok=True)
    duration = int(time.time() - t0)

    gap = {}
    try:
        gap = json.loads(out_gap)
    except Exception:
        gap = {}

    m_batch = BATCH_CNT.search(out_distill)
    m_src = SRC_CNT.search(out_distill)
    batch_open = int(m_batch.group(1)) if m_batch else None
    src_pending = int(m_src.group(1)) if m_src else None

    missing = gap.get("missing") or 0
    no_book = gap.get("no_book") or 0
    unanchored = gap.get("unanchored") or 0
    no_book_items = gap.get("no_book_items") or 0
    unanchored_items = gap.get("unanchored_items") or 0

    # 清单生成读数：差集件数 / 清单件数 / 批数（三类分开，便于对账）
    m_gen = re.search(
        r"GEN\|diff=(\d+)\|list=(\d+)\|batches=(\d+)\|no_book_items=(\d+)"
        r"\|unanchored_items=(\d+)\|skipped_done=(\d+)", out_gen)
    gen = {
        "diff_items": int(m_gen.group(1)) if m_gen else None,
        "list_items": int(m_gen.group(2)) if m_gen else None,
        "batches": int(m_gen.group(3)) if m_gen else None,
        "no_book_items": int(m_gen.group(4)) if m_gen else no_book_items,
        "unanchored_items": int(m_gen.group(5)) if m_gen else unanchored_items,
        "skipped_done": int(m_gen.group(6)) if m_gen else None,
    }
    list_empty = gen["list_items"] == 0

    if rc_distill == 3 or rc_distill < 0 or rc_gap != 0 or rc_gen != 0 or rc_mech == 3:
        status = "fail"
    elif rc_distill == 2 or missing or no_book_items or unanchored_items \
            or batch_open or src_pending or rc_mech == 2:
        status = "warn"
    else:
        status = "ok"

    mech_tail = out_mech.strip().splitlines()[-3:]
    mech_line = mech_tail[-1] if mech_tail else "-"
    m_wrote = re.search(r"写库\s*(\d+)\s*块", out_mech)
    mech_wrote = int(m_wrote.group(1)) if m_wrote else 0
    gates = run_gates() if mech_wrote else {}
    if gates and gates.get("failed_domains") and status == "ok":
        status = "warn"
    summary = ("未收口吸收批 %s 张；未吸收源件 %s 件；件级缺口 %s 件；"
               "无册 %s UP／%s 件；无锚册 %s UP／%s 件；"
               "本清单 %s 件／%s 批%s；%s%s") % (
        "-" if batch_open is None else batch_open,
        "-" if src_pending is None else src_pending,
        missing, no_book, no_book_items, unanchored, unanchored_items,
        "-" if gen["list_items"] is None else gen["list_items"],
        "-" if gen["batches"] is None else gen["batches"],
        "（本批无可入件）" if list_empty else "",
        mech_line,
        ("；分域 %d/%d 过%s" % (
            len(gates["domains"]) - gates["failed"],
            len(gates["domains"]),
            ("（未过：" + "、".join(gates["failed_domains"]) + "）")
            if gates["failed_domains"] else "")) if gates else "")

    reasons = []
    if status == "fail":
        reasons.append("巡检入口未通过（distill rc=%s／gap rc=%s／gen rc=%s／mech rc=%s）"
                       % (rc_distill, rc_gap, rc_gen, rc_mech))
    if status != "ok":
        if rc_distill == 2 or batch_open or src_pending:
            reasons.append("待办未清：未收口批 %s 张／未吸收源件 %s 件"
                           "（distill rc=2 为设计信号，非故障）"
                           % ("-" if batch_open is None else batch_open,
                              "-" if src_pending is None else src_pending))
        if gates:
            for k in gates.get("failed_domains", []):
                reasons.append("%s 域未过：%s" % (k, gates["domains"][k]["reading"]))
            passed = [k for k in gates["domains"] if k not in gates.get("failed_domains", [])]
            if passed:
                reasons.append("已过域：" + "、".join(passed))

    rec = {
        "status": status,
        "started_at": started,
        "finished_at": now(),
        "duration_s": duration,
        "distill_exit_code": rc_distill,
        "gap_exit_code": rc_gap,
        "gen_exit_code": rc_gen,
        "mechanical_exit_code": rc_mech,
        "mechanical_tail": mech_tail,
        "mechanical_wrote": mech_wrote,
        "gates": gates,
        "open_batches": batch_open,
        "pending_sources": src_pending,
        "pending_list": gen,
        "gap": {k: gap.get(k) for k in ("on_disk", "absorbed", "missing",
                                        "no_book", "no_book_items",
                                        "unanchored", "unanchored_items")},
        "message": summary,
        "reasons": reasons,
        "distill_tail": out_distill.strip().splitlines()[-40:],
        "gen_tail": out_gen.strip().splitlines()[-10:],
    }
    rec["alert"] = write_alert(status, rec=rec)
    (LOG_DIR / (stamp + ".json")).write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    LAST.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    log_scheduler("absorb", "finish", status=status, run_id=run_id,
                  trigger=_TRIGGER, start=started, end=rec["finished_at"],
                  duration_s=duration, distill_rc=rc_distill, gap_rc=rc_gap,
                  gen_rc=rc_gen, mech_rc=rc_mech,
                  wrote=mech_wrote, open_batches=batch_open,
                  pending_sources=src_pending, reasons=rec["reasons"],
                  message=summary)

    lines = [
        "# 整理吸收巡检 %s" % started,
        "- 结束：%s（耗时 %d 秒）" % (now(), duration),
        "- 状态：%s" % status,
        "- %s" % summary,
    ]
    if rec["reasons"]:
        lines.append("- 待办：%s" % "；".join(rec["reasons"]))
    lines.append("- distill_gap_check rc=%s ｜ absorb_gap rc=%s ｜ gen 清单 rc=%s ｜ mechanical rc=%s" % (
        rc_distill, rc_gap, rc_gen, rc_mech))
    lines += [""] + out_gen.strip().splitlines()[-10:]
    lines += [""] + out_distill.strip().splitlines()[-40:]
    (LOG_DIR / (stamp + ".log")).write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n".join(lines[:6]))
    if status == "ok":
        return 0
    if status == "warn":
        return 0
    return 3


def main() -> int:
    global _RETRY, _TRIGGER
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--enable", action="store_true")
    ap.add_argument("--disable", action="store_true")
    ap.add_argument("--retry-on-skip", action="store_true",
                    help="守卫跳过时当天重试（定时任务用；手动调试默认不重试）")
    args = ap.parse_args()
    _RETRY = _retry_context(args)
    _TRIGGER = "scheduled" if _RETRY else "manual"

    cfg = schedule_config.get_task("absorb")
    if args.enable or args.disable:
        schedule_config.set_enabled("absorb", bool(args.enable))
        cfg = schedule_config.get_task("absorb")
        for r in schedule_config.sync_tasks():
            if r.get("task_id") == "absorb":
                print("整理吸收定时已%s（%s，生效值：%s）"
                      % ("打开" if args.enable else "关闭",
                         r.get("message", ""), schedule_config.describe(cfg)))
        print("配置：%s" % schedule_config.CONFIG)
        return 0

    if args.check or args.dry_run:
        reasons = guard()
        print("配置：%s（%s）" % (json.dumps(cfg, ensure_ascii=False),
                                 schedule_config.describe(cfg)))
        print("守卫：" + ("可跑" if not reasons else "跳过 — " + "；".join(reasons)))
        prev = schedule_config.last_run("absorb")
        if prev:
            print("上轮：%s 状态=%s %s" % (prev.get("started_at"),
                                          prev.get("status", "-"),
                                          prev.get("message", "")))
        return 0

    if not cfg.get("enabled", True) and not args.once:
        msg = "定时已关闭（WebUI「定时任务」或 --enable 可恢复）"
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        alert = write_alert("skip", reasons=[msg])
        (LOG_DIR / "last_run.json").write_text(json.dumps({
            "status": "skip", "started_at": now(), "finished_at": now(),
            "duration_s": 0, "reasons": [msg], "message": msg,
            "alert": alert,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print("%s SKIP %s" % (now(), msg))
        log_scheduler("absorb", "skip", status="skip", run_id=new_batch_id(),
                      trigger=_TRIGGER, reasons=[msg], note="配置停用")
        return 2
    return run_once()


if __name__ == "__main__":
    sys.exit(main())
