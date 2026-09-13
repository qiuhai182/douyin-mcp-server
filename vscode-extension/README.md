# 抖音文案提取器 · VS Code 服务控制

[![Version](https://img.shields.io/badge/version-1.3.0-blue)](./CHANGELOG.md)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](./LICENSE)

在 VS Code / Trae 侧边栏控制本机 **douyin-mcp-server** 服务：
启动 / 停止 / 重启（热重载）/ 打开控制台 / 查看日志 / 批量暂停与恢复。

![icon](media/icon.png)

## 特性

- **侧边栏一站式控制**：活动栏抖音图标 → 面板内完成全部操作
- **状态一目了然**：彩色圆点 + 当前任务明细（运行中 / 解析中·任务名 / 批量已暂停 / 已停止），5 秒自动刷新
- **操作随状态裁剪**：停止时不显示「停止/重启」，解析中直接提供「⏸ 暂停批量」、暂停中直接提供「▶ 恢复批量」
- **零打扰**：状态栏、弹窗通知一律不占用；所有信息只在面板内呈现
- **默认完全静默**：安装后不探测、不轮询、不自动启动；只有打开侧边栏面板时才进行状态检测
- **热重载重启**：批量任务运行中重启会自动推迟到任务结束，从不打断提取

## 安装

1. 打开扩展面板（`Ctrl+Shift+X`）
2. 右上角「…」→ **从 VSIX 安装...**
3. 选择 `douyin-transcript-server-1.3.0.vsix`

## 使用

1. 点击活动栏抖音图标打开「服务控制」面板
2. 点击 **▶ 启动服务** —— 服务作为独立进程运行（关闭 VS Code 后仍继续运行）
3. 点击 **打开控制台界面** 在浏览器中使用完整 WebUI
4. 批量解析时可在面板内直接 **⏸ 暂停 / ▶ 恢复**
5. 不再需要时点击 **■ 停止服务**

> 想让服务随 VS Code 自动启动：点击面板底部「随 VS Code 自动启动（点击开启）」。
> 默认关闭，每次启动都需要手动运行——这是本插件与 exe 开机自启模式的核心区别。

## 命令（`Ctrl+Shift+P` 搜索 "抖音"）

| 命令 | 说明 |
|---|---|
| `启动服务` | 运行 `<项目根>/.venv/Scripts/pythonw.exe web/app.py`（独立进程） |
| `停止服务` | 按监听端口杀进程树（对托盘 / exe 启动的服务同样有效） |
| `重启服务（热重载）` | 批量任务运行中自动推迟重启 |
| `打开控制台界面` | 浏览器打开 WebUI（服务未运行时自动先启动） |
| `查看运行日志` | 打开 `logs/webui.log` |
| `暂停批量 / 恢复批量` | 控制正在运行的批量提取 |
| `随 VS Code 自动启动` | 切换自启设置（默认关） |

## 设置项

| 设置 | 默认值 | 说明 |
|---|---|---|
| `douyinServer.serverRoot` | `c:\fitions-novels-stories\douyin-mcp-server` | 项目根目录（含 `web/app.py`） |
| `douyinServer.pythonPath` | 空 | 解释器路径；留空自动使用项目 venv 的 `pythonw.exe` |
| `douyinServer.port` | `8080` | 服务端口 |
| `douyinServer.autoStartWithVscode` | `false` | 随 VS Code 自动启动 |

## 与 exe / 托盘服务模式的关系

| | 插件模式 | 托盘 / exe 模式 |
|---|---|---|
| 自启 | 默认关闭，手动或显式配置随 VS Code 启动 | 写入 Windows 登录自启注册表 |
| 进程 | `pythonw web/app.py` 独立进程 | 托盘进程 + 后台服务线程 |
| 生命周期 | 关闭 VS Code 不停服 | 常驻系统托盘 |

两种模式共用同一个 8080 端口与数据目录，切换使用无需迁移。

## 作者

**yzfly** · [douyin-mcp-server](https://github.com/qiuhail82/douyin-mcp-server)

## 许可证

[MIT](./LICENSE)
