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

启动时在 bridge home 写入权限 0600 的 hub.json，仅包含地址、PID、启动时间，
**不包含 token**，关闭时删除。每次启动生成的 bearer token 保存在 OS keyring 中。
发现顺序是环境变量 AGENT_BRIDGE_HUB_URL/TOKEN，然后读取 hub.json 的地址、PID，
从 keyring 获取该 Bridge home 的 token 并检查健康接口。`AGENT_BRIDGE_HUB=off` 可禁用发现。

### 密钥存储

严格遵守 **NO plaintext secrets anywhere on disk (keys, hub token, callback secrets, issued tokens)**。
使用 `agent-bridge` keyring 服务，只接受安全的 OS 后端，不使用明文文件后端。
`agent-bridge secrets set cursor` / `set devin` 使用 getpass 隐藏输入；`secrets list`
仅显示是否存在；`secrets delete cursor` 删除。修改后重启 Hub。`ask --callback-secret`
同样隐藏输入，不要把密钥放在命令参数、shell 历史、配置、URL 或提示词中。
如果 keyring 不可用/锁定，明确警告，只保存在内存中，绝不回退到文件。
此时独立 CLI/MCP 进程无法发现 Hub 的内存 token，需先配置/解锁安全的 OS keyring；
Hub 启动的 worker 仍通过环境获得凭证。重启后找不到签名密钥的回调以
`secret_unavailable_after_restart` 失败，绝不降级为无签名发送。
启动会删除旧 hub.json 明文 token，迁移并删除旧 callback-secrets/ 文件。
无法抹除旧备份、快照、swap 或第三方 CLI 自己的凭证/日志，请自行加固并轮换旧密钥。

所有 REST 请求（包括健康检查）都需要 bearer token。默认只监听回环地址；
远程监听必须显式使用 `--allow-remote`。这是可信本机用户的接口，不是沙箱；
持有 token 即可操作所有 session、启动 CLI。不要暴露到公网，远程使用需 TLS 隧道与防火墙。

## 阻塞调用与异步回推

`invoke` / `ask` 默认等到终态，不再有 180 秒上限；显式 `timeout_sec` /
`--timeout` 只限制等待，超时返回运行中的 task ID，不取消任务。CLI/HubClient
分段等待，HTTP 读取不限时；直接 REST invoke 每 15 秒发送 JSON 空白保活，
最终返回完整 JSON 对象。反向代理也需允许长连接。ask 启动时打印 task ID，
Ctrl-C 仅停止等待，并打印取结果命令。Worker 的 stall/turn 限制仍生效。

需要像工具一样直接拿结果时用 MCP `invoke_agent`，会周期发送 progress。
部分客户端即便收到 progress 也有工具总时限，此时用异步 dispatch 或分段 wait。
Worker 中 `ask --async` 默认 notify_caller=true，立即返回 task ID。
REST/MCP dispatch 支持 `notify_caller`、`callback_url` 和可选 `callback_secret`。
没有 Hub worker caller session 的 notify_caller 会明确拒绝；顶层协调器可用回调或轮询。

回推会在 caller 空闲后按 dispatch 顺序作为新一轮消息交付，包含 callee/task/status、
结果预览、files/PR 与继续获取方式。回推轮不增加调用深度，且其发起的调用不会再触发
通知或回调，避免自动循环。持久化 task-ID receipt 去重；pending 通知重启后继续，
包括 bridge_restarted 失败。交付表示已提交新一轮，不保证模型处理成功；已结束的 caller
记录失败。check_task/get_result 暴露 caller_delivery 和 callback_delivery。

回调 POST get_result 第一页 JSON（含 ok:true），最多四次，指数退避 1/2/4 秒。
有 secret 时 X-Agent-Bridge-Signature 为 `sha256=HEX`，校验原始 body 的
HMAC-SHA256；X-Agent-Bridge-Task-Id 为去重键。回调至少一次，请按 task ID 去重。
secret 按 task 存在 OS keyring 中（不可用时仅内存），不进入 state JSON、
结果或日志；最终投递成功/失败或 task 清理后删除。不跟随重定向，仅可信 HTTP(S) 地址，禁止 URL
内嵌密码；Hub 能访问内网端点，不应接受不可信来源提供的 callback URL。

## 云端 API worker

支持 Cursor v1 和 Devin v3，与本地 ACP/CLI 并存，无需本地 CLI 或 cwd。
将 `agents.toml.example` 中注释的 `cursor-cloud` / `devin-cloud` 配置复制到用户配置。
使用 `agent-bridge secrets set cursor` / `set devin` 保存密钥并重启 Hub；
`CURSOR_API_KEY` / `DEVIN_API_KEY` 是带警告的显式环境回退。Devin 还需
`org_id` 或 `DEVIN_ORG_ID`。密钥不写入普通文件/日志；不要把密钥放进
配置、URL 或提示词。探测 available 只检查凭证/组织是否存在，不验证额度或权限。

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
Cursor 取消当前 run，end_session 则 archive agent。Hub/Registry 关闭和空闲卸载只断开本地连接，
不会 archive Cursor、取消远程 run 或 terminate Devin；远程工作和计费可能继续。
需要停止时请显式 end_session 或取消。非正常重启仍报告 bridge_restarted，但保留 remote ID/URL，
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
