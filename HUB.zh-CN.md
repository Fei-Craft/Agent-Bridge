# 中央 Hub

先运行 `agent-bridge serve`，再打开协调器。现有 stdio MCP 配置会自动代理到
同一个 HTTP Hub；所有本地 agent 共享一个 Registry。没有 Hub 时保持原来的本地模式。

```text
协调器 MCP / shell / worker → HTTP Hub → Registry → 本地 agent CLI
                                  ↑ worker 可通过 API 调用其他 agent
```

使用 `agent-bridge ask codex '检查项目' --cwd /绝对路径`，或
`agent-bridge call list_sessions --active_only true`。Hub 的 `[hub]` 配置支持
host、port、max_depth（默认 3）；每次同伴调用深度加一，禁止调用自己的 session_id。
worker 自动继承 Hub 地址、token、调用者 session id，嵌套 MCP 也会自动代理。

`ask` 支持 `--model 模型ID` 和 `--effort 推理强度`，并原样传给 invoke/dispatch；
省略时使用 worker 默认值。模型 ID 和支持的推理强度取决于 worker/供应商。例如：
`agent-bridge ask opencode '检查项目' --cwd /绝对路径 --model opendesign/deepseek-v4.1-flash-eco`。
`call invoke` / `call dispatch_task` 也可通过通用参数传入 `--model` 和 `--effort`。

启动时在 bridge home 写入权限 0600 的 hub.json，包含地址、PID、随机 bearer token、
启动时间，关闭时删除。发现顺序是环境变量 AGENT_BRIDGE_HUB_URL/TOKEN，然后检查
hub.json 的 PID 与健康接口。`AGENT_BRIDGE_HUB=off` 可禁用发现。

所有 REST 请求（包括健康检查）都需要 bearer token。默认只监听回环地址；
远程监听必须显式使用 `--allow-remote`。这是可信本机用户的接口，不是沙箱；
持有 token 即可操作所有 session、启动 CLI。不要暴露到公网，远程使用需 TLS 隧道与防火墙。

完整 REST 路由、curl 示例、分页与超时语义见 [HUB.md](HUB.md)。

可选无头 CLI：在用户配置的 `[agents.opencode]`（也支持 claude/cursor 预设）下设置
`protocol = "cli"`，重启 Hub 并新建 session；改回 `"acp"` 即可恢复，默认协议不变。
两种协议都继承同伴调用环境。OpenCode/Claude CLI 预设会自动批准工具，仅用于可信目录。
详细参数与解析器配置见 [SETUP.md](SETUP.md#optional-headless-cli-workers)。
`uv run python scripts/bench_protocols.py --agent opencode --model opencode/big-pickle --n 3`
可比较首轮、首个 worker 事件、续接轮耗时；首事件不一定是首个文本 token。
