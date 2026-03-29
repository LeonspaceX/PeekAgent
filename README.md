<div align="center">
  <img src="icon.png" width="120"/>
  <h1>PeekAgent</h1>
  <p>基于 PySide6 和 QFluentWidgets 的轻量悬浮 AI 助手，支持多会话、流式对话、文件工具和基础 Agent 能力。</p>
</div>

# 特点

- 全局快捷键一键唤起或隐藏悬浮窗口
- 基于 PySide6 + QFluentWidgets +QWebEngine 的桌面 UI，美观实用
- 支持多会话、会话重命名、删除和自动生成标题
- 支持 Markdown、代码高亮、KaTeX 渲染
- 支持附件输入、拖拽文件、图片输入
- 支持 OpenAI 兼容端点和 Anthropic 兼容端点
- 支持自动拉取模型列表，配置简单
- 内置基础 Agent 工具：读文件、搜索文本、写入/追加/替换文件、PowerShell 命令、截图、网页抓取、网页搜索、剪贴板
- 支持使用 Tavily api 进行联网搜索

## 依赖

- Python 3.12+
- `pip install -r requirements.txt`

## 安装

1. 通过源码运行

```bash
git clone https://github.com/LeonspaceX/PeekAgent
cd PeekAgent
pip install -r requirements.txt
python main.py
```

2. 通过 Releases 下载 Windows 构建产物

```bash
# 下载 zip，解压后运行 PeekAgent 目录中的可执行文件
```

## 项目结构

```text
src/
  api_client.py        应用层 API 客户端
  chat_manager.py      会话持久化与附件管理
  config.py            运行路径与设置读写
  llm_client.py        模型接口适配
  tool_runtime.py      Agent 工具解析与执行
  ui/                  主窗口、设置页、输入区、聊天视图
  resources/           chat.html、TOOLS.md、highlight 主题、前端 vendor 资源
data/
  context/             会话与附件数据
  prompt/              SYSTEM.md / MEMORY.md
  settings.json        用户设置
main.py                应用入口
build_win.py           Windows onedir 打包脚本
```

## 模型与端点

PeekAgent 当前支持两类端点：

- OpenAI 兼容：`/v1/chat/completions`
- Anthropic 兼容：`/v1/messages`

在设置页中可以配置：

- 端点 URL
- API Key
- 端点类型
- 模型名称
- 流式输出开关

## Agent 工具

当前内置的工具能力包括：

- `read`：读取文本文件、图片文件或目录
- `search`：在文件或目录中搜索文本
- `write` / `add` / `replace`：写入、追加、精确替换文件内容
- `command`：在 PowerShell 中执行命令
- `capture`：截图当前屏幕
- `web-search`：通过 Tavily 搜索网页结果
- `web-fetch`：抓取网页正文并转换为 Markdown
- `clipboard`：写入文本或文件列表到系统剪贴板
- `client_list`，`client_connect`，`client_command`，`client_disconnect`：SSH远程执行

工具协议文档位于 [`src/resources/TOOLS.md`](src/resources/TOOLS.md)。

## 设置页

当前设置页已包含：

- 通用设置：全局快捷键、窗口置顶、外部 prompt 编辑开关
- 外观设置：主题色、代码高亮主题导入与恢复默认
- 模型设置：端点、模型、连接测试
- Tavily 设置：API Key、套餐和用量刷新
- 工具设置：文件、命令、截图、联网搜索、剪贴板等能力开关和模式

## 打包

本项目当前提供 Windows `onedir` 打包脚本：

```bash
pip install -r requirements.txt
pip install pyinstaller
python build_win.py
```

输出产物：

- `dist/PeekAgent/`
- `dist/PeekAgent-windows-amd64.zip`

## LICENSE

GNU General Public License v3.0 (GPLv3)

Copyright (C) 2026 Leonxie



*项目由GPT-5.4 & Claude Opus 4.6辅助完成，难免有许多不足，作者技术有限，求各位多多提issue和pr喵！最重要的是，给个star罢）

