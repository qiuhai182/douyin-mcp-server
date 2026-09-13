"use strict";
/**
 * 抖音文案提取器 - VS Code 服务控制插件
 *
 * 侧边栏提供：服务状态、启动 / 停止 / 重启 / 打开控制台 / 随 VS Code 自启开关。
 *
 * 设计要点：
 *  - 默认安装后【不会】自动启动服务（autoStartWithVscode 默认 false），
 *    由用户手动点「启动」，或在侧边栏勾选"随 VS Code 自动启动"。
 *  - 启动方式为 pythonw web/app.py（detached 独立进程）：服务不随 VS Code
 *    退出而停止，与 exe/托盘模式的体验一致，也不会写入开机自启注册表。
 *  - 重启优先走 POST /api/service/restart（热重载，正在跑的批量任务会
 *    推迟重启而不是被打断）；服务未运行时重启等价于启动。
 *  - 停止按监听端口的 PID 杀整棵进程树，对外部（托盘/exe）启动的服务同样有效。
 */
const vscode = require("vscode");
const { spawn, exec } = require("child_process");
const http = require("http");
const fs = require("fs");
const path = require("path");

const STATUS_POLL_MS = 5000;

// ---- service state (per session) ----
const state = {
    running: null, // null = 未知（首次检测中）
    paused: false,
    label: "",
    ownedChild: null, // 本会话内由本插件启动的进程句柄
    treeProvider: null,
};

function cfg() {
    const c = vscode.workspace.getConfiguration("douyinServer");
    return {
        root: c.get("serverRoot", ""),
        python: c.get("pythonPath", ""),
        port: c.get("port", 8080),
        autoStart: c.get("autoStartWithVscode", false),
    };
}

function baseUrl() {
    return `http://127.0.0.1:${cfg().port}`;
}

function resolvePython() {
    const c = cfg();
    if (c.python && fs.existsSync(c.python)) return c.python;
    const venvPy = path.join(c.root, ".venv", "Scripts", "pythonw.exe");
    if (fs.existsSync(venvPy)) return venvPy;
    return "python";
}

function probeStatus() {
    return new Promise((resolve) => {
        const req = http.get(`${baseUrl()}/api/service/status`, { timeout: 1500 }, (res) => {
            let body = "";
            res.on("data", (d) => (body += d));
            res.on("end", () => {
                try {
                    const j = JSON.parse(body);
                    resolve({ running: true, paused: !!j.batch_paused, label: j.batch_label || "" });
                } catch (e) {
                    resolve({ running: true, paused: false, label: "" });
                }
            });
        });
        req.on("timeout", () => { req.destroy(); resolve(null); });
        req.on("error", () => resolve(null));
    });
}

function refresh() {
    return probeStatus().then((st) => {
        const was = state.running;
        state.running = st ? st.running : false;
        state.paused = st ? st.paused : false;
        state.label = st ? st.label : "";
        if (state.treeProvider) state.treeProvider.refresh();
        if (was === null && state.running === false) {
            // 首次检测即离线：保持静默，不打扰用户（默认不自动启动）
        }
        return state.running;
    });
}

function notifyError(msg) {
    if (msg) vscode.window.showErrorMessage(msg);
}

// ---- lifecycle actions ----

function startService() {
    const c = cfg();
    if (state.running) return Promise.resolve();
    const appPy = path.join(c.root, "web", "app.py");
    if (!fs.existsSync(appPy)) {
        notifyError(`未找到 ${appPy}，请检查设置 douyinServer.serverRoot`);
        return Promise.resolve();
    }
    const py = resolvePython();
    // detached + unref：服务进程独立于 VS Code 生命周期，窗口关闭后继续运行
    const child = spawn(py, [appPy], {
        cwd: c.root,
        detached: true,
        stdio: "ignore",
        windowsHide: true,
        env: Object.assign({}, process.env, { HOST: "127.0.0.1", PORT: String(c.port) }),
    });
    state.ownedChild = child;
    child.unref();
    child.on("error", (e) => {
        notifyError(`启动失败: ${e.message}`);
    });
    // 静默轮询直到 HTTP 可达（最多 ~20s）；结果只在侧边栏体现
    return new Promise((resolve) => {
        let tries = 0;
        const timer = setInterval(() => {
            tries += 1;
            refresh().then((up) => {
                if (up || tries > 20) {
                    clearInterval(timer);
                    resolve();
                }
            });
        }, 1000);
    });
}

function stopService() {
    if (state.running === false) return Promise.resolve();
    const port = cfg().port;
    // 按端口找监听 PID，杀整棵树（对外部启动的服务同样有效）
    const cmd =
        `powershell -NoProfile -Command ` +
        `"Get-NetTCPConnection -LocalPort ${port} -State Listen | ` +
        `Select-Object -First 1 -ExpandProperty OwningProcess | ` +
        `ForEach-Object { taskkill /F /T /PID \\$_ | Out-Null }"`;
    return new Promise((resolve) => {
        exec(cmd, { windowsHide: true }, (err) => {
            if (err) {
                notifyError(`停止失败：端口 ${port} 上未发现监听进程`);
            }
            state.ownedChild = null;
            setTimeout(() => refresh(), 800);
            resolve();
        });
    });
}

function restartService() {
    if (!state.running) return startService();
    // 热重载：正在跑的批量任务会推迟重启（HTTP 202），不打断
    return new Promise((resolve) => {
        const req = http.request(`${baseUrl()}/api/service/restart`, { method: "POST", timeout: 5000 }, (res) => {
            res.on("end", () => resolve());
        });
        req.on("error", () => {
            notifyError("重启请求失败（服务可能刚停止）");
            resolve();
        });
        req.end();
    });
}

function batchPause() {
    if (!state.running || state.paused) return Promise.resolve();
    return new Promise((resolve) => {
        const req = http.request(`${baseUrl()}/api/profile/batch/pause`, { method: "POST", timeout: 5000 }, (res) => {
            res.on("end", () => { refresh(); resolve(); });
        });
        req.on("error", () => { notifyError("暂停请求失败"); resolve(); });
        req.end();
    });
}

function batchResume() {
    if (!state.running || !state.paused) return Promise.resolve();
    return new Promise((resolve) => {
        const req = http.request(`${baseUrl()}/api/profile/batch/resume`, { method: "POST", timeout: 5000 }, (res) => {
            res.on("end", () => { refresh(); resolve(); });
        });
        req.on("error", () => { notifyError("继续请求失败"); resolve(); });
        req.end();
    });
}

function openWebUI() {
    if (!state.running) {
        // 未运行时直接拉起服务，打开动作顺带完成
        startService().then(() => {
            const wait = setInterval(() => {
                if (state.running) {
                    clearInterval(wait);
                    vscode.env.openExternal(vscode.Uri.parse(baseUrl()));
                }
            }, 1000);
            setTimeout(() => clearInterval(wait), 25000);
        });
        return;
    }
    vscode.env.openExternal(vscode.Uri.parse(baseUrl()));
}

function openLog() {
    const logFile = path.join(cfg().root, "logs", "webui.log");
    if (fs.existsSync(logFile)) {
        vscode.window.showTextDocument(vscode.Uri.file(logFile), { preview: true });
    } else {
        notifyError("日志文件不存在（服务可能从未启动过）");
    }
}

async function toggleAutoStart() {
    const c = vscode.workspace.getConfiguration("douyinServer");
    const cur = c.get("autoStartWithVscode", false);
    await c.update("autoStartWithVscode", !cur, vscode.ConfigurationTarget.Global);
    if (state.treeProvider) state.treeProvider.refresh();
}

// ---- tree view ----

class ControlsProvider {
    constructor() {
        this._emitter = new vscode.EventEmitter();
        this.onDidChangeTreeData = this._emitter.event;
    }
    refresh() {
        this._emitter.fire();
    }
    getTreeItem(el) {
        return el;
    }
    getChildren() {
        const c = cfg();
        const items = [];
        // 状态行：彩色圆点 + 粗体状态字 + 明细（端口 / 当前任务）
        const dot = (color) => new vscode.ThemeIcon("circle-filled", new vscode.ThemeColor(color));
        let statusLabel, statusIcon, statusDesc, statusTip;
        if (state.running === null) {
            statusLabel = "检测中...";
            statusIcon = new vscode.ThemeIcon("sync");
            statusDesc = "";
            statusTip = "正在探测服务状态";
        } else if (state.running) {
            statusLabel = "● 服务运行中";
            statusIcon = dot("charts.green");
            statusDesc = state.paused
                ? `批量已暂停${state.label ? " · " + state.label : ""}`
                : (state.label ? `正在解析: ${state.label}` : "空闲，等待任务");
            statusTip = `WebUI: ${baseUrl()}\n批量: ${state.paused ? "已暂停" : state.label || "空闲"}`;
        } else {
            statusLabel = "○ 服务已停止";
            statusIcon = dot("charts.red");
            statusDesc = "点击下方「启动服务」";
            statusTip = `端口 ${cfg().port} 上没有响应`;
        }
        const statusItem = this._item(statusLabel, statusIcon, "douyinServer.refresh", undefined, statusTip);
        statusItem.description = statusDesc;
        statusItem.label = statusLabel; // keep explicit
        items.push(statusItem);

        // 只显示当前状态下有意义的操作：运行中不给「启动」，停止时不给「停止/重启/打开」
        if (state.running) {
            if (state.paused) {
                items.push(this._item("▶ 恢复批量（继续暂停的任务）", "debug-continue", "douyinServer.resume"));
            } else if (state.label) {
                items.push(this._item("⏸ 暂停批量（当前视频完成后）", "debug-pause", "douyinServer.pause"));
            }
            items.push(this._item("打开控制台界面", "link-external", "douyinServer.open", undefined, baseUrl()));
            items.push(this._item("重启服务（热重载，不打断批量）", "debug-restart", "douyinServer.restart"));
            items.push(this._item("查看运行日志", "output", "douyinServer.openLog"));
            items.push(this._item("■ 停止服务", "debug-stop", "douyinServer.stop"));
        } else {
            items.push(this._item("▶ 启动服务", "play", "douyinServer.start"));
        }
        items.push(this._item(
            c.autoStart ? "✓ 随 VS Code 自动启动（点击关闭）" : "○ 随 VS Code 自动启动（点击开启）",
            c.autoStart ? "eye" : "eye-closed",
            "douyinServer.toggleAutoStart",
            undefined,
            "默认关闭。开启后每次 VS Code 启动会自动拉起服务"
        ));
        return Promise.resolve(items);
    }
    _item(label, icon, command, args, tooltip) {
        const it = new vscode.TreeItem(label, vscode.TreeItemCollapsibleState.None);
        if (icon) it.iconPath = new vscode.ThemeIcon(icon);
        if (command) it.command = { command, title: label, arguments: args || [] };
        if (tooltip) it.tooltip = tooltip;
        return it;
    }
}

// ---- status bar: 已移除（状态只显示在侧边栏，不占用窗口底部状态栏） ----

function activate(context) {
    state.treeProvider = new ControlsProvider();

    context.subscriptions.push(
        vscode.window.registerTreeDataProvider("douyinServerControls", state.treeProvider),
        vscode.commands.registerCommand("douyinServer.start", startService),
        vscode.commands.registerCommand("douyinServer.stop", stopService),
        vscode.commands.registerCommand("douyinServer.restart", restartService),
        vscode.commands.registerCommand("douyinServer.open", openWebUI),
        vscode.commands.registerCommand("douyinServer.openLog", openLog),
        vscode.commands.registerCommand("douyinServer.toggleAutoStart", toggleAutoStart),
        vscode.commands.registerCommand("douyinServer.pause", batchPause),
        vscode.commands.registerCommand("douyinServer.resume", batchResume),
        vscode.commands.registerCommand("douyinServer.refresh", refresh)
    );

    // 刚装好插件 / 每次启动：完全静默——不探测、不轮询、不自动启动（默认）。
    // 状态检测与轮询只在用户打开侧边栏视图时进行；关闭视图即停止。
    const treeView = vscode.window.createTreeView("douyinServerControls", {
        treeDataProvider: state.treeProvider,
    });
    context.subscriptions.push(treeView);
    let pollTimer = null;
    treeView.onDidChangeVisibility((e) => {
        if (e.visible) {
            refresh();
            if (!pollTimer) {
                pollTimer = setInterval(refresh, STATUS_POLL_MS);
                context.subscriptions.push({ dispose: () => { clearInterval(pollTimer); pollTimer = null; } });
            }
        } else if (pollTimer) {
            clearInterval(pollTimer);
            pollTimer = null;
        }
    });

    // 仅当用户曾显式开启「随 VS Code 自动启动」时才拉起服务（默认 false 不做任何事）
    if (cfg().autoStart) {
        refresh().then((up) => { if (!up) startService(); });
    }
}

function deactivate() {
    // 服务是 detached 独立进程：VS Code 退出不停止服务（与 exe 模式一致）。
}

module.exports = { activate, deactivate };
