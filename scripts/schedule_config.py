#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""定时任务配置的单一真源（读写 / 校验 / Windows 计划任务登记）。

配置文件：douyin-mcp-server/schedule.json
- 结构：{"version": 1, "tasks": {<task_id>: {...}}}
- 每个任务字段：
    label          str     界面显示名
    enabled        bool    启用开关；false = 注销对应计划任务，不再触发
    time           "HH:MM" 执行时间（本地时区）
    weekdays       [1..7]  1=周一 … 7=周日；空列表 = 每天
    interval_days  int     每隔几天（仅 weekdays 为空时生效）
    task_name      str     Windows 计划任务名（同一配置内不得重复）
    script         str     相对 douyin-mcp-server 的入口脚本
    arguments      str     传给入口脚本的参数（默认 --once）
    workers/force  拉新任务专用，其它任务可省略

登记位置：Windows 任务计划程序根目录 "\\"，动作 = .venv\\Scripts\\pythonw.exe
"<脚本>" <arguments>。配置只落在 schedule.json，程序不写 C:\\WINDOWS\\system32\\tasks
（那是系统任务库目录，不是本项目的配置文件）。

程序内自管：WebUI 保存配置后调用 sync_tasks()，按配置重新登记/删除
Windows 计划任务；执行仍由系统任务计划服务拉起（进程无需常驻），
因此不存在「程序内调度 + 计划任务」双跑。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "schedule.json"
PS_SCRIPT = ROOT / "scripts" / "register_scheduled_task.ps1"
LEGACY_CFG = ROOT / "scripts" / "refresh_schedule.json"

# 旧版固定两个任务；新配置允许任意任务，这两个保持靠前显示
LEGACY_ORDER = ("refresh", "absorb")

LOG_DIRS = {
    "refresh": ROOT / "logs" / "scheduled_refresh",
    "absorb": ROOT / "logs" / "scheduled_absorb",
}

DEFAULTS = {
    "version": 1,
    "tasks": {
        "refresh": {
            "label": "全量 UP 拉新",
            "enabled": True,
            "time": "03:00",
            "weekdays": [],
            "interval_days": 1,
            "task_name": "DouyinRefreshDaily",
            "script": "scripts/scheduled_refresh.py",
            "arguments": "--once",
            "workers": 3,
            "force": False,
        },
        "absorb": {
            "label": "全量整理＋吸收",
            "enabled": True,
            "time": "05:00",
            "weekdays": [],
            "interval_days": 1,
            "task_name": "DouyinAbsorbDaily",
            "script": "scripts/scheduled_absorb.py",
            "arguments": "--once",
        },
    },
}

_WEEKDAY_CN = ("一", "二", "三", "四", "五", "六", "日")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_SAFE_TASK_NAME = re.compile(r"^[A-Za-z0-9_\-\.]{1,80}$")
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,32}$")
_SAFE_ARGUMENTS = re.compile(r"^[A-Za-z0-9 ._\-/:=]*$")


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def pid_alive(pid) -> bool:
    """进程存活判断。

    Windows 上 os.kill(pid, 0) 会抛 ValueError（只支持 SIGTERM/CTRL_*），
    旧实现因此永远判定「已死」，并发守卫形同虚设；这里改用
    OpenProcess + GetExitCodeProcess。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- 读写

def _task_defaults(tid: str) -> dict:
    base = {
        "label": tid,
        "enabled": True,
        "time": "03:00",
        "weekdays": [],
        "interval_days": 1,
        "task_name": "Douyin" + re.sub(r"[^A-Za-z0-9]", "", tid.title()) + "Daily",
        "script": "",
        "arguments": "--once",
    }
    if tid in DEFAULTS["tasks"]:
        base.update(DEFAULTS["tasks"][tid])
    return base


def task_ids(cfg: dict | None = None) -> list:
    """配置里的任务顺序：refresh/absorb 优先，其余按文件顺序。"""
    cfg = cfg or load()
    ids = list(cfg["tasks"].keys())
    head = [t for t in LEGACY_ORDER if t in ids]
    return head + [t for t in ids if t not in LEGACY_ORDER]


def _merge_defaults(raw: dict) -> dict:
    cfg = {"version": DEFAULTS["version"], "tasks": {}}
    tasks = (raw or {}).get("tasks")
    if not isinstance(tasks, dict) or not tasks:
        tasks = {tid: dict(item) for tid, item in DEFAULTS["tasks"].items()}
    for tid, item in tasks.items():
        merged = _task_defaults(tid)
        if isinstance(item, dict):
            merged.update({k: v for k, v in item.items() if v is not None})
        cfg["tasks"][tid] = merged
    return cfg


def load() -> dict:
    """读配置（缺失时写默认值）。旧 refresh_schedule.json 自动迁移一次。"""
    raw = {}
    if CONFIG.exists():
        try:
            raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
    else:
        raw = _migrate_legacy()
    cfg = _merge_defaults(raw)
    if not CONFIG.exists():
        save(cfg)
    return cfg


def _migrate_legacy() -> dict:
    """scripts/refresh_schedule.json（任务C 的单任务配置）→ 新结构。"""
    if not LEGACY_CFG.exists():
        return {}
    try:
        old = json.loads(LEGACY_CFG.read_text(encoding="utf-8"))
    except Exception:
        return {}
    refresh = {}
    for k in ("enabled", "time", "workers", "force"):
        if k in old:
            refresh[k] = old[k]
    tasks = {}
    if refresh:
        tasks["refresh"] = refresh
        tasks["absorb"] = dict(DEFAULTS["tasks"]["absorb"])
    return {"tasks": tasks} if tasks else {}


def validate(cfg: dict) -> dict:
    """校验并归一化；返回可写入的新配置，非法值抛 ValueError（中文原因）。"""
    if not isinstance(cfg, dict) or not isinstance(cfg.get("tasks"), dict):
        raise ValueError("配置结构错误：缺少 tasks 对象")
    out = {"version": int(cfg.get("version") or 1), "tasks": {}}
    for tid, item in cfg["tasks"].items():
        tid = str(tid).strip()
        if not _TASK_ID_RE.match(tid):
            raise ValueError("任务 ID 只允许字母/数字/-/_（收到 %r）" % tid)
        if not isinstance(item, dict):
            raise ValueError("任务 %s：结构错误（须为对象）" % tid)
        t = _task_defaults(tid)
        t.update({k: v for k, v in item.items() if v is not None})

        t["label"] = str(t.get("label") or tid).strip() or tid
        t["enabled"] = bool(t.get("enabled"))

        m = _TIME_RE.match(str(t.get("time") or "").strip())
        if not m:
            raise ValueError("%s：执行时间须为 时:分（00:00–23:59），收到 %r"
                             % (t["label"], t.get("time")))
        t["time"] = "%02d:%02d" % (int(m.group(1)), int(m.group(2)))

        wd = t.get("weekdays") or []
        if not isinstance(wd, (list, tuple)):
            raise ValueError("%s：weekdays 须为数组（1=周一 … 7=周日）" % t["label"])
        days = []
        for d in wd:
            try:
                n = int(d)
            except (TypeError, ValueError):
                raise ValueError("%s：weekdays 含非法值 %r" % (t["label"], d))
            if n < 1 or n > 7:
                raise ValueError("%s：weekdays 只接受 1–7" % t["label"])
            if n not in days:
                days.append(n)
        t["weekdays"] = sorted(days) if len(days) < 7 else []

        try:
            interval = int(t.get("interval_days") or 1)
        except (TypeError, ValueError):
            raise ValueError("%s：interval_days 须为整数" % t["label"])
        if interval < 1 or interval > 30:
            raise ValueError("%s：interval_days 须在 1–30 之间" % t["label"])
        t["interval_days"] = interval

        name = str(t.get("task_name") or "").strip()
        if not _SAFE_TASK_NAME.match(name):
            raise ValueError("%s：计划任务名只允许字母/数字/-/_/.（收到 %r）"
                             % (t["label"], name))
        t["task_name"] = name

        script = str(t.get("script") or "").strip().replace("\\", "/")
        if not script:
            raise ValueError("%s：入口脚本不能为空" % t["label"])
        target = (ROOT / script).resolve()
        if ROOT not in target.parents or not target.is_file():
            raise ValueError("%s：入口脚本不存在（%s）" % (t["label"], script))
        t["script"] = script

        arguments = str(t.get("arguments") or "").strip()
        if not _SAFE_ARGUMENTS.match(arguments):
            raise ValueError("%s：参数只允许字母/数字/空格/-/_/./:/=（收到 %r）"
                             % (t["label"], arguments))
        t["arguments"] = arguments

        if "workers" in t and t.get("workers") is not None:
            try:
                workers = int(t["workers"])
            except (TypeError, ValueError):
                raise ValueError("%s：workers 须为整数" % t["label"])
            if workers < 1 or workers > 10:
                raise ValueError("%s：workers 须在 1–10 之间" % t["label"])
            t["workers"] = workers
        if "force" in t:
            t["force"] = bool(t.get("force"))

        out["tasks"][tid] = t

    # 同名计划任务（Windows 名字大小写不敏感）不允许同时存在，避免留下多份登记
    seen = {}
    for tid, t in out["tasks"].items():
        key = t["task_name"].lower()
        if key in seen:
            raise ValueError("计划任务名重复：%s 与 %s 都叫 %s"
                             % (seen[key], tid, t["task_name"]))
        seen[key] = tid
    return out


def save(cfg: dict) -> dict:
    cfg = validate(cfg)
    tmp = CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, CONFIG)
    return cfg


def get_task(task_id: str) -> dict:
    return load()["tasks"].get(task_id) or _task_defaults(task_id)


def set_enabled(task_id: str, enabled: bool) -> dict:
    cfg = load()
    if task_id not in cfg["tasks"]:
        raise ValueError("任务不存在：%s" % task_id)
    cfg["tasks"][task_id]["enabled"] = bool(enabled)
    return save(cfg)


def delete_task(task_id: str) -> tuple:
    """删掉配置里的任务并注销其计划任务；未登记也不报错。"""
    cfg = load()
    task = cfg["tasks"].pop(task_id, None)
    if task is None:
        return None, None
    cfg = save(cfg)
    return cfg, unregister(task_id, task)


# ---------------------------------------------------------------- 展示

def describe(task: dict) -> str:
    """人读的生效值：每天 03:00 / 每 2 天 05:00 / 每周一、三 05:00。"""
    t = task.get("time", "03:00")
    days = task.get("weekdays") or []
    if days:
        label = "、".join("周" + _WEEKDAY_CN[int(d) - 1] for d in days)
        return "%s %s" % (label, t)
    interval = int(task.get("interval_days") or 1)
    if interval <= 1:
        return "每天 %s" % t
    return "每 %d 天 %s" % (interval, t)


def last_run(task_id: str) -> dict | None:
    path = LOG_DIRS.get(task_id, ROOT / "logs" / task_id) / "last_run.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------- 计划任务登记

def _run_ps(args: list, timeout: int = 90) -> tuple:
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", str(PS_SCRIPT)] + [str(a) for a in args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode, out
    except Exception as e:
        return 1, "ERR|调用计划任务脚本失败：%r" % e


def _parse_ps(out: str) -> dict:
    """解析 ps1 的 OK|/ERR| 单行结果。"""
    result = {"ok": False, "message": (out or "").strip()[:500]}
    for line in (out or "").splitlines():
        line = line.strip()
        if line.startswith("OK|"):
            parts = line.split("|")
            result = {
                "ok": True,
                "action": parts[1] if len(parts) > 1 else "",
                "task_name": parts[2] if len(parts) > 2 else "",
                "state": parts[3] if len(parts) > 3 else "",
                "next_run": parts[4] if len(parts) > 4 else "",
                "last_result": parts[5] if len(parts) > 5 else "",
                "message": line,
            }
            break
        if line.startswith("ERR|"):
            result = {"ok": False, "message": line[4:].strip()}
            break
    return result


def register(task_id: str, task: dict) -> dict:
    days = task.get("weekdays") or []
    args = [
        "-Action", "register",
        "-TaskName", task["task_name"],
        "-ScriptPath", str(ROOT / task["script"]),
        "-Arguments", task.get("arguments") or "--once",
        "-Time", task["time"],
        "-Weekdays", ",".join(str(d) for d in days),
        "-IntervalDays", task.get("interval_days", 1),
        "-Label", task.get("label", task_id),
    ]
    rc, out = _run_ps(args)
    parsed = _parse_ps(out)
    parsed["task_id"] = task_id
    parsed["requested"] = {"time": task["time"], "weekdays": days,
                           "interval_days": task.get("interval_days", 1),
                           "arguments": task.get("arguments") or "--once"}
    if not parsed["ok"] and not parsed.get("message"):
        parsed["message"] = "计划任务登记失败（rc=%s）" % rc
    return parsed


def unregister(task_id: str, task: dict) -> dict:
    rc, out = _run_ps(["-Action", "delete", "-TaskName", task.get("task_name", "")])
    parsed = _parse_ps(out)
    parsed["task_id"] = task_id
    if not parsed["ok"] and not parsed.get("message"):
        parsed["message"] = "计划任务删除失败（rc=%s）" % rc
    return parsed


def query(task_id: str, task: dict) -> dict:
    rc, out = _run_ps(["-Action", "query", "-TaskName", task.get("task_name", "")],
                      timeout=45)
    parsed = _parse_ps(out)
    parsed["task_id"] = task_id
    return parsed


def scan_registered() -> tuple:
    """一次列出所有指向本项目的已登记计划任务。

    返回 (items, error)。items 每项：
    task_name / state / next_run / last_result / task_path / execute / arguments
    """
    rc, out = _run_ps(["-Action", "list", "-Root", str(ROOT)], timeout=90)
    items = []
    error = ""
    for line in (out or "").splitlines():
        line = line.strip()
        if line.startswith("ITEM|"):
            parts = line.split("|")
            if len(parts) < 8:
                continue
            items.append({
                "task_name": parts[1],
                "state": parts[2],
                "next_run": parts[3],
                "last_result": parts[4],
                "task_path": parts[5],
                "execute": parts[6],
                "arguments": "|".join(parts[7:]),
            })
        elif line.startswith("ERR|") and not error:
            error = line[4:].strip()
    return items, error


def sync_tasks(cfg: dict | None = None) -> list:
    """按配置登记启用的任务、删除停用的任务。返回逐条可读结果。"""
    cfg = cfg or load()
    results = []
    for tid in task_ids(cfg):
        task = cfg["tasks"][tid]
        if task.get("enabled"):
            results.append(register(tid, task))
        else:
            results.append(unregister(tid, task))
    return results


def status_snapshot() -> dict:
    """WebUI 用：配置值 + 计划任务实际登记状态 + 三类不一致。

    返回 {tasks, orphans, duplicates, mismatch_count, scan_error, scanned_at}
    """
    cfg = load()
    ids = task_ids(cfg)
    items, scan_error = scan_registered()

    by_name = {}
    for it in items:
        by_name.setdefault(it["task_name"].lower(), []).append(it)

    name_owners = {}
    for tid in ids:
        key = (cfg["tasks"][tid].get("task_name") or "").lower()
        name_owners.setdefault(key, []).append(tid)

    tasks = []
    for tid in ids:
        task = cfg["tasks"][tid]
        key = (task.get("task_name") or "").lower()
        regs = by_name.get(key) or []
        reg = regs[0] if regs else None
        reg_error = ""
        if reg is None and task.get("enabled"):
            # 扫描不到时再单查一次：区分「真没登记」与「查询失败」
            q = query(tid, task)
            if q.get("ok"):
                reg = {"task_name": task["task_name"], "state": q.get("state", ""),
                       "next_run": q.get("next_run", ""),
                       "last_result": q.get("last_result", ""),
                       "task_path": "\\"}
            elif not str(q.get("message", "")).startswith("task not registered"):
                reg_error = q.get("message", "") or scan_error

        registered = bool(reg)
        issues = []
        if task.get("enabled") and not registered:
            issues.append({"code": "missing", "text": "配置已启用，但系统里没登记"})
        if (not task.get("enabled")) and registered:
            issues.append({"code": "stale", "text": "配置已停用，系统里仍在登记"})
        if len(regs) > 1:
            issues.append({"code": "dup_sys",
                           "text": "系统里同名多份（%d 处）" % len(regs)})
        if len(name_owners.get(key) or []) > 1:
            others = [x for x in name_owners[key] if x != tid]
            issues.append({"code": "dup_cfg",
                           "text": "配置内同名：%s" % "、".join(others)})
        if reg_error:
            issues.append({"code": "query", "text": "登记状态查询失败：%s" % reg_error})

        tasks.append({
            "id": tid,
            "label": task.get("label", tid),
            "enabled": bool(task.get("enabled")),
            "time": task["time"],
            "weekdays": task.get("weekdays") or [],
            "interval_days": task.get("interval_days", 1),
            "task_name": task["task_name"],
            "script": task.get("script", ""),
            "arguments": task.get("arguments", ""),
            "workers": task.get("workers"),
            "force": task.get("force"),
            "effective": describe(task),
            "registered": registered,
            "task_state": (reg or {}).get("state", ""),
            "task_path": (reg or {}).get("task_path", ""),
            "next_run": (reg or {}).get("next_run", ""),
            "last_task_result": (reg or {}).get("last_result", ""),
            "reg_error": reg_error,
            "issues": issues,
            "last_run": last_run(tid),
        })

    orphans = []
    config_names = set(name_owners.keys())
    for key, regs in by_name.items():
        if key in config_names:
            continue
        for it in regs:
            orphans.append(it)

    duplicates = []
    for key, owners in name_owners.items():
        if len(owners) > 1:
            duplicates.append({"task_name": cfg["tasks"][owners[0]]["task_name"],
                               "scope": "config", "ids": owners})
    for key, regs in by_name.items():
        if len(regs) > 1:
            duplicates.append({"task_name": regs[0]["task_name"], "scope": "system",
                               "paths": [r["task_path"] for r in regs]})

    mismatch = sum(len(t["issues"]) for t in tasks) + len(orphans) + sum(
        1 for d in duplicates if d["scope"] == "system")

    return {
        "tasks": tasks,
        "orphans": orphans,
        "duplicates": duplicates,
        "mismatch_count": mismatch,
        "scan_error": scan_error,
        "scanned_at": now(),
    }


if __name__ == "__main__":
    # 手工核对/登记：python -B scripts/schedule_config.py [status|sync]
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "sync":
        for r in sync_tasks():
            print(json.dumps(r, ensure_ascii=False))
    else:
        print(json.dumps(status_snapshot(), ensure_ascii=False, indent=2))
