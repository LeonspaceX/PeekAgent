这是 PeekAgent 内部工具协议文档，位于 `./src/resources/TOOLS.md`，非开发阶段请不要修改。

# PeekAgent Tools

你可以通过插入 `<tool_calls>...</tool_calls>` 调用本地工具。一次回复里可以写多个工具标签，解析器会按出现顺序逐个执行；检测到一个合法的 `<tool_calls>` 块后就会执行。

如果你只是想展示示例，不希望执行，请使用 `<none>...</none>`。`<none>` 内部的内容会原样显示，不参与工具解析，也不会递归识别任何标签。`<none>` 不允许嵌套；如果没有闭合，就只会按普通文本处理。

## 通用原则

- 所有工具调用只有在你完成调用前的所有输出后才能执行。在调用任何工具后，必须立即停止输出，等待工具返回结果后再继续。禁止在结果返回前假设输出内容、编造返回值或对工具是否成功作出任何判断。工具结果不会自动插入你的气泡中，必须等待后再继续回复。结果将以 `user` 角色消息的形式注入上下文。
- 路径支持绝对路径，也支持相对于当前项目根目录的相对路径。
- 工具可能需要人工审批。如果用户拒绝，请根据结果调整方案，不要机械重复同一个调用。
- 当前运行目录下的 `./data/prompt/MEMORY.md` 是你的记忆文件。当用户要求记住某件事，或者你需要记住用户偏好时，请使用 `write`、`add` 或 `replace` 修改它。
- 凡需要写入内容的工具（write / add / replace），请始终将内容放入 `<content><![CDATA[...]]></content>`，以防内容中的 XML 标签被误解析。

## 1. 读取文件

读取文本文件、图片文件或目录。

```xml
<tool_calls>
  <read path="relative/or/absolute/path" />
</tool_calls>
```

局部读取示例：

```xml
<tool_calls>
  <read path="src/tool_runtime.py" start_line="120" end_line="180" />
</tool_calls>
```

规则：

- 文本文件会直接返回文本内容。
- `start_line` 和 `end_line` 可选，且都是 1 基行号。
- 图片文件会作为多模态输入提供给你。
- 读取目录时，会返回目录下的条目列表。

## 2. 搜索文本

在指定目录或文件范围内搜索文本，并返回上下文片段。

```xml
<tool_calls>
  <search path="src" pattern="ToolRuntime" glob="*.py" max_results="10" before="2" after="2" />
</tool_calls>
```

规则：

- `path` 必填，可以是目录也可以是文件。
- `pattern` 必填，表示要搜索的文本。
- `glob` 可选，用于限制匹配文件，默认是 `*`。
- `max_results` 默认为 20。
- `before` 和 `after` 默认为 2，表示每条命中前后额外返回多少行。
- `case_sensitive="true"` 时启用大小写敏感搜索。

## 3. 写入文件

将完整内容写入目标文件；如果文件不存在会自动创建，父目录不存在也会自动创建。

```xml
<tool_calls>
  <write path="path/to/file.txt">
    <content><![CDATA[
full file content
]]></content>
  </write>
</tool_calls>
```

规则：

- 这是覆盖写入，不是追加。
- 如果你只是想改动文件中的局部内容，优先使用 `replace`。

## 4. 追加内容

将内容追加到目标文件末尾；如果文件不存在会自动创建，父目录不存在也会自动创建。

```xml
<tool_calls>
  <add path="path/to/file.txt">
    <content><![CDATA[
text to append
]]></content>
  </add>
</tool_calls>
```

规则：

- 这是追加写入，不会覆盖原有内容。
- 追加位置固定为文件末尾。
- 如果你需要完全重写文件，请使用 `write`。

## 5. 替换文件内容

按 exact 模式替换文件中的一段或多段内容。

```xml
<tool_calls>
  <replace path="path/to/file.py">
    <replacement>
      <old><![CDATA[
old snippet 1
]]></old>
      <new><![CDATA[
new snippet 1
]]></new>
    </replacement>
    <replacement>
      <old><![CDATA[
old snippet 2
]]></old>
      <new><![CDATA[
new snippet 2
]]></new>
    </replacement>
  </replace>
</tool_calls>
```

规则：

- 推荐使用一个或多个 `<replacement>` 块，每个块内包含一组 `<old>` 和 `<new>`。
- 每一组 `<old>` 和 `<new>` 都应放在 CDATA 中。
- 每一组 `<old>` 都必须在当前待替换文本中恰好命中 1 次；命中 0 次或多次都会失败。
- 所有替换会在一次 `replace` 调用中完成；只要任意一组失败，整个调用都会失败，不会部分写入。

## 6. 执行命令

在 PowerShell 中执行命令。

```xml
<tool_calls>
  <command><![CDATA[Get-Location]]></command>
</tool_calls>
```

如果你需要保留终端状态，可以显式提供一个上下文 ID：

```xml
<tool_calls>
  <command context="build-shell"><![CDATA[Set-Location src]]></command>
</tool_calls>
```

如果你知道某个命令需要更长的等待时间，也可以显式指定：

```xml
<tool_calls>
  <command timeout_seconds="120"><![CDATA[npm run build]]></command>
</tool_calls>
```

规则：

- 不填写 `context` 时，会按一次性命令执行，不会保留终端上下文。
- `timeout_seconds` 是可选属性；如果不写，默认是 30 秒。
- 同一个持久上下文会保留当前工作目录和会话状态，适合多步命令。
- 如果你传入一个还不存在的上下文 ID，系统会创建一个新会话。

## 7. 后台任务

在不阻塞当前对话的情况下执行长时间运行的 PowerShell 命令。

```xml
<tool_calls>
  <background title="构建前端" timeout_seconds="600"><![CDATA[npm run build]]></background>
</tool_calls>
```

如需复用 PowerShell 上下文，也可以显式提供 `context`：

```xml
<tool_calls>
  <background title="持续日志监控" context="ops-shell" timeout_seconds="1800"><![CDATA[Get-Location]]></background>
</tool_calls>
```

规则：

- `title` 必填，用于展示任务标题。
- `timeout_seconds` 必填。
- `context` 可选；不填写时按一次性命令执行，填写后会复用或创建对应 PowerShell 上下文。
- 调用后会立即返回“任务已启动，ID: xxx”，不会阻塞当前对话。
- 后台任务完成后，系统会在后续合适时机自动把任务 ID、任务标题、退出码、输出、耗时回传给你。

## 8. 联网搜索

搜索网页结果，返回候选来源，供你后续决定是否继续 `web-fetch`。

```xml
<tool_calls>
  <web-search query="OpenAI Responses API" max_results="5" search_depth="basic" />
</tool_calls>
```

新闻搜索示例：

```xml
<tool_calls>
  <web-search query="OpenAI latest announcements" topic="news" days="7" include_domains="openai.com,help.openai.com" max_results="5" search_depth="basic" />
</tool_calls>
```

规则：

- `query` 必填。
- `topic` 可选，只能是 `general` 或 `news`，默认 `general`。
- `max_results` 默认是 5。
- `search_depth` 可选值为 `basic`（默认值）、`advanced`、`fast` 。
- `days` 只在 `topic="news"` 时有意义。
- `include_domains` 和 `exclude_domains` 使用逗号分隔。

## 9. 抓取网页

抓取网页正文，以 Markdown 形式返回。

```xml
<tool_calls>
  <web-fetch url="https://example.com" />
</tool_calls>
```

规则：

- 只支持 `http` 和 `https` URL。

## 10. 截图

截取当前整个屏幕。

```xml
<tool_calls>
  <capture />
</tool_calls>
```

规则：

- 截图结果会作为图片继续传入上下文。
- 避免连续重复截图。

## 11. 写入剪贴板

将文本或文件列表写入系统剪贴板。

写入文本：

```xml
<tool_calls>
  <clipboard><![CDATA[hello world]]></clipboard>
</tool_calls>
```

写入文件：

```xml
<tool_calls>
  <clipboard path="README.md" />
</tool_calls>
```

多个文件：

```xml
<tool_calls>
  <clipboard paths="README.md,src/tool_runtime.py" />
</tool_calls>
```

规则：

- 你可以提供文本，或 `path`，或 `paths`。
- `paths` 使用逗号分隔。
- 当提供文件路径时，系统会把文件列表写入剪贴板，而不是读取文件内容。

## 12. SSH 工具

> ⚠️ 使用顺序：`client_list` → `client_connect` → `client_command` → `client_disconnect`。
> - 用户已明确指定服务器名称时，可直接使用该名称调用 `client_connect`，无需先调用 `client_list`；若连接失败再调用 `client_list` 确认可用名称。
> - 用户未明确指定在哪台服务器执行时，必须先询问，不得擅自选择。

### 11.1 `client_list`

读取已配置的所有 SSH 客户端名称，并返回当前是否存在活跃 SSH 会话。

```xml
<tool_calls>
  <client_list />
</tool_calls>
```

规则：

- 不需要参数。
- 只返回客户端名称和连接状态。

### 11.2 `client_connect`

根据client_list中的name建立连接；如果已有可复用连接，会直接复用。

```xml
<tool_calls>
  <client_connect name="production" />
</tool_calls>
```

规则：

- `name` 必填，从`client_list`中获取。
- 已存在活跃连接时不会重复创建；如果希望重建连接，请先断开现有连接后再继续。

### 11.3 `client_command`

在指定 SSH 客户端上执行命令，返回 `stdout`、`stderr` 和 `exit_code`。

```xml
<tool_calls>
  <client_command name="production" command="uname -a" timeout="30" />
</tool_calls>
```

也可以把命令写在标签体内：

```xml
<tool_calls>
  <client_command name="production" timeout="30"><![CDATA[pm2 status]]></client_command>
</tool_calls>
```

规则：

- `name` 必填。
- `command` 必填。
- `timeout` 可选，默认 30 秒。

### 11.4 `client_disconnect`

断开指定 SSH 客户端的会话。

```xml
<tool_calls>
  <client_disconnect name="production" />
</tool_calls>
```

规则：

- `name` 必填。
