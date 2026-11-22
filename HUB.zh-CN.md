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

## 云端 API worker

支持 Cursor v1 和 Devin v3，与本地 ACP/CLI 并存，无需本地 CLI 或 cwd。
将 `agents.toml.example` 中注释的 `cursor-cloud` / `devin-cloud` 配置复制到用户配置。
在启动 Hub 的进程环境中绑定 `CURSOR_API_KEY` / `DEVIN_API_KEY`，Devin 还需
`org_id` 或 `DEVIN_ORG_ID`。密钥仅在请求时读取，不保存、不写日志；不要把密钥放进
配置、URL 或提示词。探测 available 只检查变量是否存在，不验证额度或权限。

```sh
agent-bridge ask cursor-cloud '修复测试' --repo https://github.com/org/repo --ref main --model composer-2 --timeout 3600
agent-bridge ask devin-cloud '添加功能' --repo https://github.com/org/repo --model fast --timeout 3600
agent-bridge ask cursor-cloud '补充测试' --session-id sess_FROM_RESULT --timeout 3600
```

REST/MCP dispatch 和 invoke 支持 `repo` / `ref` 覆盖配置。续接保持原仓库与 ref，
显式传不同值会拒绝。通过 `call dispatch_task` / `call get_result` 获取 session ID。
Cursor `--model` 是模型 ID；Devin 使用 `--model` > `--effort` > `devin_mode`
选择模式（model 如 normal/fast/lite/ultra/fusion），续接不能更换模式。
effort 映射 off→fast、low→lite、medium→normal、high/max→ultra（高模式可能更贵）。
Cursor 不映射 effort。
Devin 没有专用 ref 字段，ref 作为提示词指令传入，不保证服务端强制 checkout。

结果 `remote` 包含提供商 ID、URL、PR、分支、状态和结构化输出；不扫描本地文件，
`files_changed` 不可用。Cursor 分支信息是整个 agent 的状态，不是单轮 diff。
Cursor token 用量尽力获取（usage 接口可能 403）；Devin `acus_consumed` 是累计
session ACU，不是 token 或本轮增量。**云端调用会产生费用，Devin 按 ACU 计费**，
建议设置 `max_acu_limit`。默认每 5 秒轮询，禁用静默超时，单轮上限 24 小时；可配置。
状态变化和新消息才算进展。Devin waiting_for_user 完成本轮，waiting_for_approval
完成并警告，额度/信用限制判失败。

**取消 Devin 会 terminate（archive=false），不可续接，需新建 session。**
Cursor 取消当前 run，end_session 则 archive agent。正常 Hub 关闭也会 archive Cursor /
terminate Devin。非正常重启仍报告 bridge_restarted，但保留 remote ID/URL，
云端任务可能仍在运行，需人工检查/停止；不会自动重新接管。
不向云端发送 Hub URL/token/caller 环境，云端无法回调默认 loopback Hub。
base_url 仅用于可信提供商/网关，真实密钥应走 HTTPS。

完整 REST 路由、curl 示例、分页与超时语义见 [HUB.md](HUB.md)。

可选无头 CLI：在用户配置的 `[agents.opencode]`（也支持 claude/cursor 预设）下设置
`protocol = "cli"`，重启 Hub 并新建 session；改回 `"acp"` 即可恢复，默认协议不变。
两种协议都继承同伴调用环境。OpenCode/Claude CLI 预设会自动批准工具，仅用于可信目录。
详细参数与解析器配置见 [SETUP.md](SETUP.md#optional-headless-cli-workers)。
`uv run python scripts/bench_protocols.py --agent opencode --model opencode/big-pickle --n 3`
可比较首轮、首个 worker 事件、续接轮耗时；首事件不一定是首个文本 token。
