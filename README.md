# miniAgent

一个用 Python 实现的最小 Agent：**决策交给模型，能力由插件提供，内核只负责装配与生命周期。**

默认组合包括模型、工具、会话、循环、CLI 五类插件。Loop 本身可替换，内核不导入任何默认插件。

## 运行

需要 Python 3.11+，目前支持 macOS / Linux。运行时依赖只有 `httpx` 和 `jsonschema`。

```bash
uv venv --python 3.11
uv pip install -e .

export MINIAGENT_MODEL="你的模型名称"
export OPENAI_API_KEY="你的 API Key"
export OPENAI_BASE_URL="https://api.openai.com/v1"

.venv/bin/miniagent
```

`OPENAI_BASE_URL` 可以指向支持 Chat Completions 流式工具调用的兼容接口。密钥只从环境变量读取；模型名称和接口地址也可以写入模型插件配置。

单次运行：

```bash
.venv/bin/miniagent "读取 README.md，说明这个项目的模块关系"
```

交互模式输入 `/exit` 退出，执行期间按 `Ctrl+C` 取消当前运行。也支持：

```bash
.venv/bin/python -m miniagent --config miniagent.toml "你的任务"
```

退出码：`0` 正常结束，`1` 错误或达到步骤上限，`130` 用户取消。`completed` 表示模型正常结束当前轮次，不表示系统证明了任务成功。

## 模块

```text
src/miniagent/
├── kernel.py           插件加载、服务依赖、通知、资源释放
├── contracts.py        Model / Tools / Session / AgentLoop 接口
├── __main__.py         读取 TOML，按配置加载插件，启动入口
└── plugins/
    ├── model.py        OpenAI 兼容流式适配器
    ├── tools.py        工具注册表 + shell / file
    ├── session.py      JSONL 会话存储与恢复
    ├── loop.py         默认模型—工具循环
    └── cli.py          终端输入、输出、中断
```

调用关系：

```text
CLI → Loop → Model
           → Tools
           → Session
```

内核管理插件，但不参与任务决策。服务调用推进业务；通知只用于观察，不修改请求或阻止执行。

| 服务 | 最小接口 |
|---|---|
| Model | `prepare(messages, tools)` 构造实际请求；`complete(request, on_text)` 发起请求 |
| Tools | `definitions()` 暴露工具；`execute(name, arguments)` 执行调用 |
| Session | `append(kind, data)` 追加事实；`history()` 恢复可继续的历史 |
| AgentLoop | `run(prompt)` 驱动一轮自主执行 |

消息以 JSON 字典保存，保留 `reasoning_content` 等供应商返回的扩展字段。首版共享消息格式使用 Chat Completions 的消息和工具调用结构；其他模型协议需要由适配器转换，未实现 Responses API 或 Anthropic 原生协议。

## 默认功能

- 流式文本输出、多轮对话、工具反馈后的自动续跑。
- `shell`：在配置目录启动独立 `/bin/sh`，返回退出码、标准输出、标准错误。
- `file`：读取、写入、精确替换 UTF-8 文本；替换目标必须恰好出现一次。
- 工具参数通过 JSON Schema 校验；未知工具、参数错误、执行错误作为结果交回模型。
- 同一会话串行执行；工具调用也顺序执行。
- Shell 超时、中断时终止进程组并等待退出；不支持持久终端和后台任务。
- 最大模型步骤数，默认 20；达到上限会明确停止。
- JSONL 追加日志、进程级单写锁、已结束轮次的历史恢复。
- 插件初始化失败时回滚注册，关闭时逆序释放资源。

本地工具使用当前操作系统用户的权限。`cwd` 是工作目录，不是访问边界或沙箱；绝对路径及 `..` 都可以访问目录外。插件也是同进程的受信任 Python 代码。

`output_limit` 限制返回给模型的字符数，并标记截断；Shell 输出先在内存中收集，因此它不是进程内存配额。需要大量输出时，可以让命令写入文件后分段读取。

## 配置与会话

`miniagent.toml` 是默认装配。插件按声明顺序初始化，依赖必须先加载；缺少服务、插件名称重复、服务重复都会报错。首版替换配置后重启，不做热更新。

相对路径以**启动命令时的工作目录**为基准，不以配置文件所在目录为基准。

默认日志为 `.miniagent/session.jsonl`。重启会恢复该文件的历史；创建新会话时修改会话插件的 `path`：

```toml
[[plugins]]
factory = "miniagent.plugins.session:SessionPlugin"
[plugins.options]
path = ".miniagent/another-session.jsonl"
system = "You are a helpful agent. Use the available tools when needed."
```

每条日志都有顺序号和时间。主要记录：

| 记录 | 内容 |
|---|---|
| `session` | 格式版本和初始系统指令 |
| `run_started / run_ended` | 运行开始、终止状态、错误原因 |
| `request` | 适配器构造的实际 JSON 请求体，包含消息、工具定义、模型名称 |
| `response` | 完整模型响应、结束原因，以及接口提供的用量 |
| `message` | 被接纳进历史的用户消息、助手消息、工具结果 |
| `tool_started` | 开始执行的工具调用 ID |

请求体写入日志后才调用模型；助手的工具调用写入后才执行工具。认证请求头不写入日志。日志包含用户内容、工具参数和结果，不会自动脱敏；它不是网络抓包，也不保存接口未返回的内部推理。

恢复只重建上下文，不重放工具：

- 正常结束、达到步骤上限，或没有悬空工具调用的已记录失败，可以继续。
- 进程崩溃留下未结束轮次、工具执行中取消留下未知结果、日志尾部不完整时，拒绝继续该会话。
- 这些情况需要先检查日志与实际文件／进程状态，再配置新的会话路径；系统不猜测副作用，也不自动补跑。
- 修改系统指令需要新会话。更换模型时，已有扩展字段是否兼容由模型适配器负责。

流式中途失败的文本可能已经显示，但不会作为完整助手消息提交。`length` 等截断结束原因也不会被当作正常完成。

## 扩展插件

插件只需要 `name`、`requires` 和异步 `setup(ctx)`。构造函数只接收配置；资源应在 `setup` 中申请并及时登记清理函数。

```python
from datetime import UTC, datetime


class ClockPlugin:
    name = "clock"
    requires = ("tools",)

    async def setup(self, ctx):
        async def now(arguments):
            return {"utc": datetime.now(UTC).isoformat()}

        remove = ctx.require("tools").register(
            "current_time",
            "Get the current UTC time.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            now,
        )
        ctx.defer(remove)
```

将插件放到可导入的 Python 模块中，在工具插件后添加：

```toml
[[plugins]]
factory = "my_plugins:ClockPlugin"
```

`my_plugins` 需要已安装或位于 `PYTHONPATH`。替换模型、会话或 Loop 时，替换对应配置项，并提供相同的服务接口，无需修改内核。

`ctx` 提供：

- `provide(name, service)` / `require(name)`：服务注册与获取。
- `on(event, callback)` / `emit(event, value)`：同步观察通知。
- `defer(cleanup)`：登记同步或异步清理函数。

服务注册和通知订阅自动清理；工具等业务注册使用其返回的撤销函数配合 `defer`。观察者异常会被记录，但不会改变任务执行。

内置通知：`text`、`response_end`、`tool_started`、`tool_finished`、`run_ended`。运行期间只通过已装配服务工作，不热替换工具和模型。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -v
```

测试使用可替换模型、HTTP mock 和本地模拟 API，无需真实密钥，不调用付费模型。覆盖实际文件修改与 Shell 验证、日志请求一致性、会话恢复、插件回滚、流式工具参数拼接、错误反馈、步骤上限，以及 CLI 中断与子进程清理。

## 当前边界

首版没有固定规划流程、多 Agent、长期记忆、自动摘要、向量数据库、MCP、Skills 加载器、Web UI 或插件市场。

上下文按会话历史直接构造；超出模型限制时返回接口错误，不自动裁剪。模型请求不自动重试，未知副作用不自动重放。首版也没有工具事务回滚、崩溃后的进程接管或文件内容撤销功能。
