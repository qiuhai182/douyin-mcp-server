# -*- coding: utf-8 -*-
"""动作日志覆盖自查：列出「有入口但没有落 operation_logs」的清单。

口径（静态扫描，不依赖运行态）：
  * 入口 ＝ web/app.py 里带 @app.get/post/put/delete/patch 的函数
           + scripts/ 下定时任务脚本（scheduled_*.py、mechanical_absorb.py）
  * 已覆盖 ＝ 函数体内直接调用 log_operation / log_video_result /
             _batch_publish / log_scheduler，或调用了本模块内会落日志的辅助函数
  * 只读 GET 默认不算「动作」，但要显式登记在 READ_ONLY_GETS 里；
    有副作用的 GET（如下载代理）登记在 WRITE_GETS，必须落日志。

用法：
  python scripts/log_coverage.py            # 人读清单
  python scripts/log_coverage.py --json     # 机器读
GET /api/logs/coverage 返回同一份结果（缓存 60s）。
"""
import ast
import io
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "web" / "app.py"

LOG_CALLS = {"log_operation", "log_video_result", "_batch_publish",
             "append_operations", "log_scheduler"}
MUTATING_METHODS = {"POST", "PUT", "DELETE", "PATCH"}
# 只读 GET：不需要动作日志（HTTP 请求本身由中间件记录为 http.request）
READ_ONLY_GETS = {
    "/", "/api/health", "/api/config", "/api/schedule", "/api/pending-links",
    "/api/service/status", "/api/profile/batch/stream", "/api/refresh-task",
    "/api/profile/history", "/api/history/failed", "/api/authors",
    "/api/profile/batch", "/api/douyin/login-status", "/api/logs",
    "/api/logs/videos", "/api/logs/runtime", "/api/logs/coverage",
    "/api/video/info",
}
# 有副作用的 GET：必须落日志
WRITE_GETS = {"/api/video/download"}

SCHEDULER_SCRIPTS = ("scheduled_refresh.py", "scheduled_absorb.py",
                     "mechanical_absorb.py", "refresh_all.py")

_cache = {"ts": 0.0, "data": None}


def _calls_of(node):
    names = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            name = getattr(f, "id", None) or getattr(f, "attr", None)
            if name:
                names.add(name)
    return names


def audit(use_cache=True):
    if use_cache and _cache["data"] and time.time() - _cache["ts"] < 60:
        return _cache["data"]

    src = io.open(APP, encoding="utf-8").read()
    tree = ast.parse(src)
    funcs = {}
    routes = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = _calls_of(node)
        funcs[node.name] = calls
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                continue
            if getattr(dec.func.value, "id", "") not in ("app", "router"):
                continue
            if dec.func.attr not in ("get", "post", "put", "delete", "patch"):
                continue
            if not (dec.args and isinstance(dec.args[0], ast.Constant)):
                continue
            routes.append({
                "method": dec.func.attr.upper(),
                "path": dec.args[0].value,
                "fn": node.name,
                "direct": sorted(calls & LOG_CALLS),
            })

    def covered(fn_name, seen=None):
        seen = seen or set()
        if fn_name in seen:
            return False
        seen.add(fn_name)
        calls = funcs.get(fn_name, set())
        if calls & LOG_CALLS:
            return True
        # 一层：调用本模块内落日志的辅助函数（如 _queue_order_response）
        return any(covered(c, seen) for c in calls if c in funcs)

    covered_routes, read_only, missing = [], [], []
    for r in routes:
        is_read = (r["method"] == "GET" and r["path"] in READ_ONLY_GETS
                   and r["path"] not in WRITE_GETS)
        ok = covered(r["fn"])
        item = {"method": r["method"], "path": r["path"], "fn": r["fn"],
                "via": "direct" if r["direct"] else ("helper" if ok else "")}
        if is_read:
            read_only.append(item)
        elif ok:
            covered_routes.append(item)
        else:
            missing.append(item)

    scripts = []
    for name in SCHEDULER_SCRIPTS:
        p = ROOT / "scripts" / name
        text = io.open(p, encoding="utf-8", errors="replace").read() if p.exists() else ""
        # 定时脚本既可直接用 log_scheduler，也可能像 refresh_all 一样用
        # log_operation 落自己的动作，两种都算已覆盖
        n = sum(text.count(fn + "(") for fn in LOG_CALLS)
        n -= text.count("def log_scheduler") + text.count("def log_operation")
        scripts.append({"script": name, "log_calls": max(0, n),
                        "covered": n > 0})

    data = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "http": {
            "routes": len(routes),
            "mutations": len(routes) - len(read_only),
            "covered": len(covered_routes),
            "read_only": len(read_only),
            "missing": len(missing),
            "missing_list": missing,
            "read_only_list": read_only,
            "covered_list": covered_routes,
        },
        "scheduler": {
            "scripts": len(scripts),
            "covered": sum(1 for s in scripts if s["covered"]),
            "missing": [s["script"] for s in scripts if not s["covered"]],
            "detail": scripts,
        },
    }
    _cache.update(ts=time.time(), data=data)
    return data


def main():
    data = audit(use_cache=False)
    if "--json" in sys.argv:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0 if not data["http"]["missing"] and not data["scheduler"]["missing"] else 1
    h = data["http"]
    print("HTTP 路由 %d：写操作 %d，已落 %d，未落 %d，只读登记 %d"
          % (h["routes"], h["mutations"], h["covered"], h["missing"], h["read_only"]))
    for m in h["missing_list"]:
        print("  未落  %-7s %-38s %s" % (m["method"], m["path"], m["fn"]))
    s = data["scheduler"]
    print("定时脚本 %d：已落 %d，未落 %d %s"
          % (s["scripts"], s["covered"], len(s["missing"]),
             ("（%s）" % "，".join(s["missing"])) if s["missing"] else ""))
    for d in s["detail"]:
        print("  %-22s log_scheduler 调用 %d" % (d["script"], d["log_calls"]))
    return 0 if not h["missing"] and not s["missing"] else 1


if __name__ == "__main__":
    sys.exit(main())
