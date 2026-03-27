这是 PeekAgent 内部工具协议文档，位于 `./src/resources/TOOLS.md`，请不要修改。

# PeekAgent Tools

你可以通过插入 `<tool_calls>...</tool_calls>` 调用本地工具。一次回复里可以写多个工具标签，解析器会按出现顺序逐个执行；检测到一个合法的 `<tool_calls>` 块后就会执行，不要求它必须出现在回复末尾。

如果你只是想展示示例，而不是真的执行，请使用 `<none>...</none>`。`<none>` 内部的内容会原样显示，不参与工具解析，也不会递归识别任何标签。`<none>` 不允许嵌套；如果没有闭合，就只会按普通文本处理。

## 通用原则

- 路径支持绝对路径，也支持相对于当前项目根目录的相对路径。
- 工具可能需要人工审批。如果用户拒绝，请根据结果调整方案，不要机械重复同一个调用。
- 工具调用结束后，再根据返回结果决定下一步，不要预设调用一定成功。
- 当前运行目录下的 `./data/prompt/MEMORY.md` 是你的记忆文件。当用户要求记住某件事，或者你需要记住用户偏好时，请使用 `write`、`add` 或 `replace` 修改它。

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
- 图片文件会作为图片传入上下文。
- 二进制无意义文件不会被展开，只会告诉你它是二进制文件。
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
- `search` 和 `read` 一样，只有开关，没有审批模式。

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
- 请使用 `<content><![CDATA[...]]></content>`，避免内容里的 XML 标签被误解析。
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
- 请使用 `<content><![CDATA[...]]></content>`。
- 追加位置固定为文件末尾。
- 如果你需要完全重写文件，请使用 `write`。

## 5. 替换文件内容

按 exact 模式替换文件中的一段内容。

```xml
<tool_calls>
  <replace path="path/to/file.py">
    <old><![CDATA[
old snippet
]]></old>
    <new><![CDATA[
new snippet
]]></new>
  </replace>
</tool_calls>
```

规则：

- `replace` 只支持 exact 模式：把 `<old>` 的完整内容替换成 `<new>`。
- `<old>` 和 `<new>` 都应放在 CDATA 中，避免内容里的 XML 标签被误解析。
- `<old>` 必须在目标文件中恰好命中 1 次；命中 0 次或多次都会失败。
- 这是字符串级替换，不保留任何“锚点”。

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

- 不填写 `context` 时，会按一次性命令执行，本次不会保留终端上下文。
- 只有在你显式提供字符串 `context` 时，才会进入持久 PowerShell 上下文。
- `timeout_seconds` 是可选属性；如果不写，默认是 30 秒。
- 同一个持久上下文会保留当前工作目录和会话状态，适合多步命令。
- 如果你传入一个还不存在的上下文 ID，系统会创建一个新会话。

## 7. 联网搜索

使用 Tavily 搜索网页结果，返回轻量候选来源，供你后续决定是否继续 `web-fetch`。

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
- `search_depth` 可选值为 `basic`、`advanced`、`fast`。
- `search_depth` 默认是 `basic`。
- `days` 只在 `topic="news"` 时有意义。
- `include_domains` 和 `exclude_domains` 使用逗号分隔。
- 结果只返回标题、URL、摘要和发布时间等轻量信息，不会直接返回整页原文。

## 8. 抓取网页

抓取网页正文，并尽量以 Markdown 形式返回。

```xml
<tool_calls>
  <web-fetch url="https://example.com" />
</tool_calls>
```

规则：

- 只支持 `http` 和 `https` URL。
- 如果网页直接返回 Markdown，就直接使用它；否则会尝试提取正文并转换为 Markdown。
- 请求头会使用桌面版 Google Chrome User-Agent。
- 如果网页正文为空或无法提取，会返回失败信息。

## 9. 截图

截取当前整个屏幕。

```xml
<tool_calls>
  <capture />
</tool_calls>
```

规则：

- 截图结果会作为图片继续传入上下文。
- 当你需要观察当前界面状态时再调用，避免连续重复截图。

## 10. 写入剪贴板

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
