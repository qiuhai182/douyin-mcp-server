# 短视频文案提取器

[![Python version](https://img.shields.io/pypi/pyversions/douyin-mcp-server.svg)](https://pypi.org/project/douyin-mcp-server/)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

从短视频分享链接下载无水印视频，AI 自动提取语音文案。

> **Fork 说明**：本项目 Fork 自 [yzfly/douyin-mcp-server](https://github.com/yzfly/douyin-mcp-server)（原仓库已归档），在此基础上增强了**批量提取**能力：作者主页全量抓取、并发处理、暂停/继续、断点恢复、序号文件名与目录索引。

![WebUI 界面预览](douyin-video.png)

## ✨ 功能特性

- 🎬 **无水印视频** - 获取高质量无水印视频下载链接
- 🎙️ **AI 语音识别** - 使用硅基流动 SenseVoice 自动提取文案
- 📑 **大文件支持** - 自动分段处理超过 1 小时或 50MB 的音频
- 🌐 **WebUI** - 现代化浏览器界面，无需命令行
- 🔌 **MCP 集成** - 支持 Claude Desktop 等 AI 应用
- 📚 **批量提取**（本 Fork 增强）
  - 粘贴作者主页链接，一键抓取全部视频文案
  - 多视频并发处理（启动节流，不增加风控压力）
  - 暂停/继续：当前视频完成后暂停，随时续传
  - 断点恢复：任务被打断后自动续跑，不重复下载、不重复入库
  - 序号文件名（`1-视频ID.md`）+ 自动生成 `0-目录.txt` 索引

---

## 📦 使用方式

| 方式 | 适用场景 | 特点 |
|------|----------|------|
| [**WebUI**](#-webui-推荐) | 普通用户 | 浏览器操作，最简单 |
| [**MCP Server**](#-mcp-server) | Claude Desktop 用户 | AI 对话中直接调用 |
| [**命令行**](#️-命令行工具) | 开发者 | 批量处理，脚本集成 |

---

## 🌐 WebUI (推荐)

最简单的使用方式，打开浏览器即可使用。

### 快速开始

```bash
# 1. 克隆项目
git clone https://github.com/qiuhai182/douyin-mcp-server.git
cd douyin-mcp-server

# 2. 安装依赖
uv sync

# 3. 启动服务
uv run python web/app.py
```

Windows 用户也可以直接双击 `start.bat`（自动等待服务就绪并打开浏览器）。

打开浏览器访问 **http://localhost:8080**

### 配置 API Key

有两种方式配置 API Key：

**方式一：浏览器内配置（推荐）**

1. 打开 WebUI 页面
2. 点击顶部的「API 未配置」按钮
3. 在弹窗中输入 API Key 并保存
4. API Key 保存在浏览器本地，刷新页面后仍有效

**方式二：环境变量**

```bash
export API_KEY="sk-xxxxxxxxxxxxxxxx"
uv run python web/app.py
```

> 💡 获取免费 API Key：[硅基流动](https://cloud.siliconflow.cn/i/TxUlXG3u)（新用户有免费额度）

### 功能说明

| 操作 | 说明 | 需要 API |
|------|------|:--------:|
| **获取信息** | 解析视频标题、ID，获取无水印下载链接 | ❌ |
| **提取文案** | 下载视频 → 提取音频 → AI 语音识别 | ✅ |
| **批量提取** | 粘贴作者主页链接，抓取全部视频并批量提取文案 | ✅ |
| **下载视频** | 点击下载链接保存无水印视频 | ❌ |
| **复制/下载文案** | 一键复制或下载 Markdown 格式文案 | - |

### 使用步骤

1. **粘贴链接** - 将分享链接粘贴到输入框
2. **点击按钮** - 选择「获取信息」或「提取文案」
3. **查看结果** - 右侧显示视频信息和提取的文案
4. **导出** - 复制文案或下载 Markdown 文件

### 批量提取作者全部文案

1. 粘贴**作者主页链接**（如 `https://www.douyin.com/user/xxx`）
2. 点击「批量提取作者全部文案」
3. 进度面板实时显示：`[OK]` 成功 / `[跳过]` 已提取过 / `[失败]` 自动重试

**进度面板控制**：

- ⏸️ **暂停** - 当前视频完成后停止拉取新视频（进行中的视频不会中断）
- ▶️ **继续** - 从暂停处继续处理剩余视频

**容错机制**：

- 已提取的视频自动跳过（按 `history.json` 记录去重）
- 失败的视频下次自动重试（勾选「使用缓存视频列表补齐」可不重新滚动主页）
- 任务中途被打断（关机/断网）：下次执行时自动恢复 —— 文案已完整落盘的只补记账不重新下载，不完整的才重新解析
- 建议先点「登录抖音」一次（浏览器缓存会保存会话），否则匿名状态只能抓到部分视频列表

**输出组织**（按作者分目录，文件名带序号与目录对应）：

```
output/
└── 作者昵称/
    ├── 0-目录.txt              # 目录索引：文件名 ↔ 标题/简介
    ├── 1-7595175846302715163.md
    ├── 2-7597477483939728666.md
    └── ...
```

`0-目录.txt` 示例：

```
1-7595175846302715163.md
  标题: 血继限界 生而为神？ #火影忍者 #血继限界 #佐助 #二次元 #动漫
```

---

## 🚀 MCP Server

在 Claude Desktop、Cherry Studio 等支持 MCP 的应用中使用。

### 配置方法

编辑 MCP 配置文件，添加：

```json
{
  "mcpServers": {
    "douyin-mcp": {
      "command": "uvx",
      "args": ["douyin-mcp-server"],
      "env": {
        "API_KEY": "sk-xxxxxxxxxxxxxxxx"
      }
    }
  }
}
```

> 💡 `API_KEY` 填写[硅基流动](https://cloud.siliconflow.cn/i/TxUlXG3u)的密钥。也兼容旧版配置：设置 `DASHSCOPE_API_KEY`（阿里云百炼密钥）同样可用，两者设其一即可。

### 可用工具

| 工具名 | 功能 | 需要 API |
|--------|------|:--------:|
| `parse_douyin_video_info` | 解析视频信息 | ❌ |
| `get_douyin_download_link` | 获取下载链接 | ❌ |
| `extract_douyin_text` | 提取视频文案 | ✅ |
| `recognize_audio_file` | 识别本地音频文件 | ✅ (百炼) |
| `recognize_audio_url` | 识别在线音频链接 | ✅ (百炼) |

### 对话示例

```
用户：帮我提取这个视频的文案 https://v.douyin.com/xxxxx/

Claude：我来帮你提取视频文案...
[调用 extract_douyin_text 工具]
提取完成，文案内容如下：
...
```

---

## 🛠️ 命令行工具

适合开发者和批量处理场景。

### 安装

```bash
git clone https://github.com/qiuhai182/douyin-mcp-server.git
cd douyin-mcp-server
uv sync
```

### 命令说明

```bash
# 查看帮助
uv run python douyin-video/scripts/douyin_downloader.py --help

# 获取视频信息（无需 API）
uv run python douyin-video/scripts/douyin_downloader.py -l "分享链接" -a info

# 下载无水印视频
uv run python douyin-video/scripts/douyin_downloader.py -l "分享链接" -a download -o ./videos

# 提取文案（需要 API_KEY）
export API_KEY="sk-xxx"
uv run python douyin-video/scripts/douyin_downloader.py -l "分享链接" -a extract -o ./output

# 提取文案并保存视频
uv run python douyin-video/scripts/douyin_downloader.py -l "分享链接" -a extract -o ./output --save-video

# 批量提取作者全部视频文案（3 并发，自动断点恢复）
uv run python douyin-video/scripts/douyin_downloader.py -l "作者主页链接" -a batch -o ./output -w 3

# 强制重新提取已成功的视频
uv run python douyin-video/scripts/douyin_downloader.py -l "作者主页链接" -a batch --force -o ./output
```

### 输出格式

```
output/
└── 作者昵称/
    ├── 0-目录.txt          # 目录索引（批量模式自动生成）
    └── N-视频ID.md         # 每个视频的文案文件
```

**transcript.md 内容：**

```markdown
# 视频标题

| 属性 | 值 |
|------|-----|
| 视频ID | `7600361826030865707` |
| 提取时间 | 2026-01-30 14:19:00 |
| 下载链接 | [点击下载](url) |

---

## 文案内容

这里是 AI 识别的语音文案...
```

---

## 📋 系统要求

| 依赖 | 说明 | 安装方式 |
|------|------|----------|
| uv | Python 包管理 | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Python | 3.10+ | `uv python install 3.12` |
| FFmpeg | 音视频处理 | `brew install ffmpeg` (macOS) <br> `apt install ffmpeg` (Ubuntu) |

---

## 🔧 技术说明

### 大文件处理

当音频文件超过 API 限制时（1 小时或 50MB），自动执行：

1. 检测音频时长和文件大小
2. 使用 FFmpeg 分割成 9 分钟的片段
3. 逐段调用 API 转录
4. 合并所有文本结果

### API 说明

语音识别使用 [硅基流动 SenseVoice API](https://cloud.siliconflow.cn/)：

- 模型：`FunAudioLLM/SenseVoiceSmall`
- 限制：单次最大 1 小时 / 50MB（已自动处理）
- 费用：新用户有免费额度

---

## 📝 更新日志

### v1.5.0 (最新)

- 📚 **批量提取** - 粘贴作者主页链接，一键抓取作者全部视频文案
- ⚡ **并发处理** - 多视频流水线并行（可配 worker 数），单视频启动节流不加重风控
- ⏸️ **暂停/继续** - WebUI 一键暂停（当前视频完成后生效），随时继续
- 🔁 **断点恢复** - 打断后自动续跑：完整文案只补记账，半成品自动重新解析
- 🔢 **序号文件名** - 文案按 `N-视频ID.md` 命名，与目录一一对应
- 📑 **目录索引** - 自动生成 `0-目录.txt`（文件名 ↔ 标题/简介）
- 🔧 **多服务商** - ASR 支持硅基流动/百炼等多后端切换，可选 LLM 文案润色

### v1.4.1

- 🔧 **MCP Server 修复** - `API_KEY` 现在正确对应硅基流动密钥，与文档一致；同时兼容旧版 `DASHSCOPE_API_KEY` 配置
- ♻️ **恢复工具** - 恢复 `recognize_audio_file` / `recognize_audio_url` 工具及 `extract_douyin_text` 的 `context` 参数
- 🛡️ **WebUI 安全加固** - 下载接口不再代理任意 URL，默认仅监听本机
- ⚡ **WebUI 性能** - 提取文案不再阻塞其他请求
- 📦 **依赖精简** - WebUI 依赖改为可选安装（`pip install "douyin-mcp-server[web]"`）

### v1.4.0

- 🌐 **WebUI** - 新增浏览器可视化界面
- 🔑 **浏览器配置 API Key** - 无需环境变量
- 📑 **大文件支持** - 自动分段处理长音频

### v1.3.0

- ✨ Claude Code Skill 支持
- 📄 Markdown 格式输出

### v1.2.0

- 🔄 API 升级

### v1.0.0

- 🎉 首次发布

---

## ⚠️ 免责声明

- 本项目仅供学习和研究使用
- 使用者需遵守相关法律法规
- 禁止用于侵犯知识产权的行为
- 作者不对使用本项目产生的损失承担责任

---

## 📄 许可证

Apache License 2.0

## 👨‍💻 作者

**qiuhai182** - [GitHub](https://github.com/qiuhai182)

基于 [yzfly/douyin-mcp-server](https://github.com/yzfly/douyin-mcp-server)（原仓库已归档）Fork 增强，感谢原作者 [yzfly](https://github.com/yzfly)。
