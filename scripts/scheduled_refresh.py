#!/usr/bin/env python
"""全量 UP 拉新（Windows 计划任务的入口）

机制：Windows 计划任务按 schedule.json 里的时间直接调本脚本，跑完退出；
不在托盘/WebUI 里再挂一套内置调度，避免双跑。时间/开关/星期范围全部由
schedule.json 驱动，WebUI 保存后会自动重登记计划任务。

流程：读开关 -> 并发守卫 -> 起 refresh_all.py（增量）-> 等结束 -> 解析
driver_live.log 新增段 -> 写本轮日志 + last_run.json（含跳过原因，失败下次可见）。

用法：
    python -B scripts/scheduled_refresh.py --check      # 只看守卫状态，不跑
    python -B scripts/scheduled_refresh.py --dry-run    # 同上（兼容写法）
    python -B scripts/scheduled_refresh.py --once       # 手动跑一次（同定时）
    python -B scripts/scheduled_refresh.py --disable    # 一键关掉定时
    python -B scripts/scheduled_refresh.py --enable     # 重新打开

退出码：0=成功 1=有 UP 失败 2=守卫跳过/已停用 3=异常
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import schedule_config  # noqa: E402  配置单一真源

# 定时任务也要进 operation_logs：日志不可用绝不能拖垮任务，故降级为 no-op
sys.path.insert(0, str(ROOT / "douyin-video" / "scripts"))
try:
    from operation_logger import log_scheduler, new_batch_id  # noqa: E402
except Exception:  # pragma: no cover
    def log_scheduler(*_a, **_k):  # type: ignore
        pass

    def new_batch_id():  # type: ignore
        return ""

# 触发来源：--retry-on-skip（计划任务动作）＝scheduled，否则＝manual
_TRIGGER = "manual"

DRIVER = ROOT / "scripts" / "refresh_all.py"
PYW = ROOT / ".venv" / "Scripts" / "pythonw.exe"
LOG_DIR = ROOT / "logs" / "scheduled_refresh"
LIVE_LOG = ROOT / "logs" / "driver_live.log"
STATE_FILE = ROOT / "output" / "刷新日志" / "driver_state.json"
PENDING = ROOT / "pending_links.json"
LOCK = ROOT / "output" / "刷新日志" / ".scheduled_refresh.lock"
LAST = LOG_DIR / "last_run.json"

# 告警落点：任务工单目录的当日件（每天巡检查看；不靠人开 WebUI 也能看见）
WS = ROOT.parent
ALERT_DIR = WS / "任务工单"
ALERT_BACKUP = WS / "0_工作区" / "_work" / "调度告警备份"
SAFE_WRITE = WS / "maiji-writing-skill" / "scripts" / "safe_write.py"

# 跳过后的当天重试：默认每 2 小时一次、最多 2 次；只对定时上下文（pythonw）自动生效
RETRY_INTERVAL_S = int(os.environ.get("DOUYIN_REFRESH_RETRY_INTERVAL", "7200") or 0)
RETRY_MAX = int(os.environ.get("DOUYIN_REFRESH_RETRY_MAX", "2") or 0)
_RETRY = False   # main() 里按上下文设定


def _retry_context(args) -> bool:
    """是否在跳过之后当天重试。

    判据只认显式开关：计划任务动作里带 --retry-on-skip，或环境变量强制。
    不用 sys.stdout 判断——本机 pythonw 的 stdout 不是 None，判据不可靠；
    手动 --once/--check/--dry-run 不传该参数，行为与原来一致。
    """
    env = (os.environ.get("DOUYIN_REFRESH_RETRY") or "").strip()
    if env == "0":
        return False
    if env == "1":
        return True
    return bool(getattr(args, "retry_on_skip", False))


def alert_path(day: str = "") -> Path:
    day = day or datetime.now().strftime("%Y%m%d")
    return ALERT_DIR / ("调度告警_抖音定时刷新_%s.md" % day)


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


def write_alert(status: str, totals: dict | None = None,
                reasons: list | None = None, failed: list | None = None,
                note: str = "") -> dict:
    """把每轮的 ok/skip/fail 与失败原因写进当天告警件。

    写盘走 safe_write（仓内唯一整文件写入原语，目标在「任务工单/」控制面免前链闸）；
    告警写失败只回传错误，绝不影响刷新语义。
    """
    try:
        p = alert_path()
        if p.exists():
            old = p.read_text(encoding="utf-8", errors="replace")
        else:
            old = ("# 调度告警｜抖音定时刷新｜%s\n\n"
                   "> 自动写入：scheduled_refresh.py → safe_write。每轮一行：状态｜ok/skip/fail｜失败原因。\n"
                   "> 本件用途：定时任务跳过或失败时，不开 WebUI 也能在任务工单目录看见。\n\n"
                   % datetime.now().strftime("%Y-%m-%d"))
        t = totals or {}
        stamp = now()
        if status == "skip":
            line = "- %s｜**skip**｜ok=0 skip=0 fail=0｜原因：%s%s" % (
                stamp, "；".join(reasons or []) or "-", note)
        else:
            line = "- %s｜**%s**｜ok=%s skip=%s fail=%s%s%s" % (
                stamp, status, t.get("ok", 0), t.get("skip", 0), t.get("fail", 0),
                ("｜失败：" + "、".join(failed[:8])) if failed else "",
                note)
        SW = _load_safe_write()
        SW(str(p), old.rstrip("\n") + "\n" + line + "\n",
           expect_contains="调度告警", backup_root=str(ALERT_BACKUP))
        return {"ok": True, "path": str(p)}
    except Exception as e:
        return {"ok": False, "error": repr(e)}


UP_OK = re.compile(r"^\s*✓\s+(.+?)\s+ok=(\d+)\s+skip=(\d+)\s+fail=(\d+)\s+resumed=(\d+)")
UP_ERR = re.compile(r"^\s*✗\s+FATAL\s+(.+?):\s*(.*)$")
DONE = re.compile(r"===\s*全 UP 刷新(完成|已终止|已自动暂停)")
TOTAL = re.compile(r"总计\s+ok=(\d+)\s+skip=(\d+)\s+fail=(\d+)\s+resumed=(\d+)")


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_cfg() -> dict:
    """来自 schedule.json 的 refresh 任务（默认值兜底）。"""
    return schedule_config.get_task("refresh")


def pid_alive(pid: int) -> bool:
    # Windows 下 os.kill(pid, 0) 不可用；统一走 schedule_config 的实现
    return schedule_config.pid_alive(pid)


def guard() -> list[str]:
    """返回跳过原因；空列表=可跑。"""
    reasons = []
    if not DRIVER.exists():
        reasons.append("refresh_all.py 不存在")
    if LOCK.exists():
        try:
            info = json.loads(LOCK.read_text(encoding="utf-8-sig"))
        except Exception:
            info = {}
        if pid_alive(info.get("pid", 0)):
            reasons.append("上一轮定时任务未结束（pid=%s）" % info.get("pid"))
        else:
            LOCK.unlink(missing_ok=True)   # 陈旧锁自动清
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
             "'refresh_all\\.py|batch_extractor\\.py'} | Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=20).stdout
        pids = [x.strip() for x in out.splitlines() if x.strip().isdigit()]
        if pids:
            reasons.append("已有刷新/提取进程在跑（pid=%s）" % ",".join(pids))
    except Exception:
        pass
    # 自测钩子：注入一条守卫拦截原因，用于验证「跳过→重试→告警」链路（正常运行为空）
    forced = (os.environ.get("DOUYIN_REFRESH_GUARD_FORCE") or "").strip()
    if forced:
        reasons.append(forced)
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


def read_new_lines(path: Path, baseline: int) -> list[str]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []
    return lines[baseline:]


def run_once(force_flag: bool = False) -> int:
    cfg = load_cfg()
    reasons = guard()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id = new_batch_id()
    log_scheduler("refresh", "trigger", trigger=_TRIGGER, run_id=run_id,
                  force=bool(force_flag), retry_context=_RETRY,
                  guard="；".join(reasons) or "可跑",
                  enabled=bool(cfg.get("enabled", True)),
                  workers=cfg.get("workers", 3))

    prev = {}
    if LAST.exists():
        try:
            prev = json.loads(LAST.read_text(encoding="utf-8"))
        except Exception:
            prev = {}
    prev_fail = prev.get("failed_ups") or []

    # 跳过不再直接放弃：定时上下文（pythonw）当天每 2 小时再试，最多 2 次
    attempts = 0
    while reasons and _RETRY and attempts < RETRY_MAX:
        attempts += 1
        wait = max(1, RETRY_INTERVAL_S)
        note = "（第 %d 次拦截，%s后重试）" % (
            attempts, ("%d 分钟" % (wait // 60)) if wait >= 120 else ("%d 秒" % wait))
        line = "%s SKIP 守卫拦截：%s%s" % (now(), "；".join(reasons), note)
        (LOG_DIR / ("%s_r%d.log" % (stamp, attempts))).write_text(line + "\n", encoding="utf-8")
        alert = write_alert("skip", reasons=reasons, note=note)
        LAST.write_text(json.dumps({
            "status": "skip",
            "started_at": now(),
            "finished_at": now(),
            "duration_s": 0,
            "reasons": reasons,
            "previous_status": prev.get("status", ""),
            "previous_failed_ups": prev_fail,
            "message": "；".join(reasons),
            "attempts": attempts,
            "retry_max": RETRY_MAX,
            "retry_interval_s": wait,
            "retry_exhausted": False,
            "next_retry_at": datetime.fromtimestamp(time.time() + wait).strftime("%Y-%m-%d %H:%M:%S"),
            "alert": alert,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(line)
        log_scheduler("refresh", "retry", status="skip", run_id=run_id,
                      attempt=attempts, wait_s=wait, reasons=reasons)
        time.sleep(wait)
        reasons = guard()

    if reasons:
        note = ("（当天已重试 %d 次仍被拦，放弃到次日）" % attempts) if attempts else ""
        line = "%s SKIP 守卫拦截：%s%s" % (now(), "；".join(reasons), note)
        log_name = ("%s_final.log" % stamp) if attempts else (stamp + ".log")
        (LOG_DIR / log_name).write_text(line + "\n", encoding="utf-8")
        alert = write_alert("skip", reasons=reasons, note=note)
        # 跳过也要落 last_run.json：WebUI 要能看出「最近一次为什么没跑」
        LAST.write_text(json.dumps({
            "status": "skip",
            "started_at": now(),
            "finished_at": now(),
            "duration_s": 0,
            "reasons": reasons,
            "previous_status": prev.get("status", ""),
            "previous_failed_ups": prev_fail,
            "message": "；".join(reasons),
            "attempts": attempts + 1,
            "retry_max": RETRY_MAX,
            "retry_interval_s": RETRY_INTERVAL_S,
            "retry_exhausted": bool(attempts),
            "next_retry_at": "",
            "alert": alert,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(line)
        log_scheduler("refresh", "skip", status="skip", run_id=run_id,
                      reasons=reasons, attempts=attempts + 1,
                      retry_exhausted=bool(attempts))
        if prev_fail:
            print("%s 提示：上一轮失败 %d 家：%s" % (now(), len(prev_fail), "、".join(prev_fail)))
        return 2

    if not acquire_lock():
        reason = "锁已被占用（另一轮刚启动）"
        (LOG_DIR / (stamp + ".log")).write_text(
            "%s SKIP %s\n" % (now(), reason), encoding="utf-8")
        alert = write_alert("skip", reasons=[reason], note="（锁被占用，未重试）")
        LAST.write_text(json.dumps({
            "status": "skip", "started_at": now(), "finished_at": now(),
            "duration_s": 0, "reasons": [reason], "message": reason,
            "attempts": 1, "retry_exhausted": False, "alert": alert,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print("%s SKIP %s" % (now(), reason))
        log_scheduler("refresh", "skip", status="skip", run_id=run_id,
                      reasons=[reason], note="锁被占用")
        return 2

    baseline = 0
    try:
        if LIVE_LOG.exists():
            baseline = len(LIVE_LOG.read_text(encoding="utf-8", errors="ignore").splitlines())
    except Exception:
        baseline = 0

    py = str(PYW) if PYW.exists() else sys.executable
    argv = [py, str(DRIVER), str(cfg.get("workers", 3))]
    if force_flag or cfg.get("force"):
        argv.append("force")

    started = now()
    t0 = time.time()
    code = -1
    err = ""
    try:
        proc = subprocess.Popen(argv, cwd=str(ROOT))
        log_scheduler("refresh", "step", run_id=run_id, step="driver",
                      driver_pid=proc.pid, argv=" ".join(argv[1:])[:200])
        code = proc.wait()
    except Exception as e:
        err = repr(e)
        log_scheduler("refresh", "step", status="error", run_id=run_id,
                      step="driver", error=err[:300])
    duration = int(time.time() - t0)

    stats, failed, done_mark, totals = [], [], "", {}
    for ln in read_new_lines(LIVE_LOG, baseline):
        m = UP_OK.match(ln)
        if m:
            stats.append({"up": m.group(1).strip(), "ok": int(m.group(2)),
                          "skip": int(m.group(3)), "fail": int(m.group(4)),
                          "resumed": int(m.group(5))})
            continue
        m = UP_ERR.match(ln)
        if m:
            failed.append({"up": m.group(1).strip(), "error": m.group(2)[:300]})
            continue
        m = TOTAL.search(ln)
        if m:
            totals = {"ok": int(m.group(1)), "skip": int(m.group(2)),
                      "fail": int(m.group(3)), "resumed": int(m.group(4))}
            continue
        m = DONE.search(ln)
        if m:
            done_mark = m.group(1)

    failed_ups = [f["up"] for f in failed] + [
        s["up"] for s in stats if s.get("fail")]
    # 「完成」才算成功；已终止/已自动暂停（余额不足等）按未完成处理
    ok = (code == 0 and done_mark == "完成" and not failed_ups and not err)

    rec = {
        "status": "ok" if ok else "fail",
        "started_at": started,
        "finished_at": now(),
        "duration_s": duration,
        "exit_code": code,
        "done_mark": done_mark,
        "totals": totals,
        "authors": stats,
        "failed_ups": failed_ups,
        "failures": failed,
        "error": err,
        "reasons": [],
        "message": ("完成：ok=%s skip=%s fail=%s" % (
            totals.get("ok"), totals.get("skip"), totals.get("fail"))
            if ok else (
                ("启动异常：" + err) if err else
                ("本轮未完成（%s）" % done_mark) if done_mark else
                ("失败：%d 家 UP 异常" % len(failed_ups)) if failed_ups else
                "本轮未完成（无完成标记）")),
        "previous_run_failed": prev_fail,
    }
    # 无论 ok/fail 都写当天告警件，保证不开 WebUI 也能看见失败
    rec["alert"] = write_alert(rec["status"], totals=totals,
                               failed=failed_ups or [f["up"] for f in failed])
    (LOG_DIR / (stamp + ".json")).write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    LAST.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    log_scheduler("refresh", "finish", status=rec["status"], run_id=run_id,
                  trigger=_TRIGGER, start=started, end=rec["finished_at"],
                  duration_s=duration, exit_code=code, done_mark=done_mark or "",
                  ok=totals.get("ok"), skip=totals.get("skip"),
                  fail=totals.get("fail"), resumed=totals.get("resumed"),
                  failed_ups=failed_ups, error=err,
                  message=rec["message"])

    lines = [
        "# 定时拉新 %s" % started,
        "- 结束：%s（耗时 %d 秒，退出码 %s，完成标记=%s）" % (now(), duration, code, done_mark or "无"),
        "- 总计：ok=%s skip=%s fail=%s resumed=%s" % (
            totals.get("ok"), totals.get("skip"), totals.get("fail"), totals.get("resumed")),
        "- UP 成功 %d 家；失败 %d 家" % (len(stats), len(failed_ups)),
    ]
    if prev_fail:
        lines.append("- 上轮失败（本轮开始前可见）：%s" % "、".join(prev_fail))
    for s in stats:
        lines.append("  - %s ok=%d skip=%d fail=%d" % (s["up"], s["ok"], s["skip"], s["fail"]))
    for f in failed:
        lines.append("  - FATAL %s：%s" % (f["up"], f["error"]))
    if err:
        lines.append("- 启动异常：%s" % err)
    (LOG_DIR / (stamp + ".log")).write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n".join(lines))
    LOCK.unlink(missing_ok=True)
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--enable", action="store_true")
    ap.add_argument("--disable", action="store_true")
    ap.add_argument("--retry-on-skip", action="store_true",
                    help="跳过时当天重试（定时/pythonw 上下文默认开启）")
    args = ap.parse_args()

    global _RETRY, _TRIGGER
    _RETRY = _retry_context(args)
    _TRIGGER = "scheduled" if _RETRY else "manual"

    cfg = load_cfg()
    if args.enable or args.disable:
        schedule_config.set_enabled("refresh", bool(args.enable))
        cfg = load_cfg()
        results = schedule_config.sync_tasks()
        for r in results:
            if r.get("task_id") == "refresh":
                print("定时拉新已%s（%s，生效值：%s）"
                      % ("打开" if args.enable else "关闭",
                         r.get("message", ""), schedule_config.describe(cfg)))
        print("配置：%s" % schedule_config.CONFIG)
        return 0

    if args.check or args.dry_run:
        reasons = guard()
        print("配置：%s（%s）" % (json.dumps(cfg, ensure_ascii=False),
                                 schedule_config.describe(cfg)))
        print("守卫：" + ("可跑" if not reasons else "跳过 — " + "；".join(reasons)))
        if LAST.exists():
            try:
                prev = json.loads(LAST.read_text(encoding="utf-8"))
                print("上轮：%s 状态=%s 失败 %d 家 %s" % (
                    prev.get("started_at"), prev.get("status", "-"),
                    len(prev.get("failed_ups") or []),
                    "、".join(prev.get("failed_ups") or [])))
            except Exception:
                pass
        return 0

    if not cfg.get("enabled", True) and not args.once:
        reason = "定时已关闭（WebUI「定时任务」或 --enable 可恢复）"
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        alert = write_alert("skip", reasons=[reason], note="（配置停用，未重试）")
        (LOG_DIR / "last_run.json").write_text(json.dumps({
            "status": "skip", "started_at": now(), "finished_at": now(),
            "duration_s": 0, "reasons": [reason], "message": reason,
            "attempts": 1, "retry_exhausted": False, "alert": alert,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print("%s SKIP %s" % (now(), reason))
        log_scheduler("refresh", "skip", status="skip", run_id=new_batch_id(),
                      trigger=_TRIGGER, reasons=[reason], note="配置停用")
        return 2
    return run_once(force_flag=args.force)


if __name__ == "__main__":
    sys.exit(main())
