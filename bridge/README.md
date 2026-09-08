# DSH MCP bridge

本目录同时包含 MCP server 和 DSH runtime adapter。完整步骤见[快速开始](../docs/quickstart.md)，恢复语义见[架构说明](../docs/architecture.md)。

```text
MCP client → mcp-server.mjs → runner-core.mjs → runner-plugin.mjs / DSH services
```

## 命令

在此目录执行：

```sh
npm ci
npm test         # 离线，真实 MCP + 模拟 DSH
npm run doctor  # 只读检查已安装的 DSH 与工作区配置
npm start       # 真实 runtime + 常驻 HTTP 服务
npm run probe   # 只调用 ping 和列工具
```

先复制 `.env.example` 为 `.env` 并填写绝对路径。`npm start` 和 `doctor` 自动载入 `.env`；其他命令不读取模型配置。

## 六个工具

| 工具 | 参数 | 用途 |
| --- | --- | --- |
| `ping` | 无 | 连通检查 |
| `dsh_start_task` | task, workspace?, mode? | 新建 session 并运行一个 epoch |
| `dsh_continue` | session_id, feedback | 继续同一 session；运行中不会追加重复 epoch |
| `dsh_get_status` | session_id | 查询内存生命周期与 last_result |
| `dsh_list_sessions` | 无 | 当前 bridge 进程的会话列表 |
| `dsh_cancel` | session_id, reason? | 请求取消当前 epoch |

`start/continue` 可能返回 running 或 waiting_for_review；`get_status` 把等待审查的 epoch 映射为 completed。此处 completed 不是 Longtime 的 ACCEPTED。结果的事实字段无法可靠提取时保持空值，不生成额外“证据”。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| DSH_BRIDGE_HOST | 127.0.0.1 | HTTP 监听地址 |
| DSH_BRIDGE_PORT | 3420 | HTTP 端口 |
| DSH_BRIDGE_WORKSPACES | 启动目录 | 逗号分隔的允许根 |
| DSH_BRIDGE_EPOCH_TIMEOUT_MS | 45000 | 返回 running 前最多等待多久；不是任务总时限 |
| DSH_BRIDGE_INSTALL | 自动发现 | DSH 安装根 |
| DSH_BRIDGE_STDIO | 未设置 | 1 时改用 stdio，长期监督推荐 HTTP |
| DSH_BRIDGE_SESSION_ROUTING | 0 | 可选 opencode-go session 兼容补丁 |
| DSH_BRIDGE_ALLOW_NON_LOOPBACK | 未设置 | 高级网络部署开关，不提供鉴权 |
| DSH_HOME | DSH 默认 home | 会话、配置、私有凭据所在位置 |

`runner-core.mjs` 不导入 DSH，可注入测试接口；`runner-plugin.mjs` 只负责真实 DSH 接口绑定；`bridge.mjs` 负责 profile 依赖链接、树启动和传输。依赖使用 lockfile 固定。DSH 本体通过上游安装，未复制第三方 node_modules。
