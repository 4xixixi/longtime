# 运行、暂停与恢复

## 日常检查

```sh
python control/supervisor_ctl.py --root /absolute/control-root check
npm run probe --prefix bridge
```

`check` 是只读的；解析 JSON 中的 `ok/status/error`。`probe` 默认检查 3420，其他端口通过 `MCP_URL` 指定，例如 `http://127.0.0.1:3422/mcp`。不要把包含本机路径、任务内容或 session ID 的完整日志提交到公开仓库。

## 暂停、停止与升级

```sh
python scripts/set_project_status.py --root /absolute/control-root --status PAUSED --reason "维护前暂停派发"
```

该命令通过 USER 事务暂停后续规划，不会杀死已运行的 DSH epoch。先查询已有 session；需要终止时显式调用 `dsh_cancel`，确认 epoch 已结束，再 Ctrl+C 停止 bridge。不要通过“重启服务”代替任务取消。

升级前保留控制目录和 DSH_HOME。确认无活跃 epoch，再更新仓库、执行 `npm ci --prefix bridge`、测试并重启。初始化器复制到独立控制目录的 Python 文件不会随公开仓库自动更新；应在暂停、无活跃租约时有计划地同步代码，并重新 check。不要在升级时重新初始化已有状态。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| `cannot locate ... dsh` | 设置 `DSH_BRIDGE_INSTALL` 到 DSH 包根；运行 doctor |
| 新环境启动缺 plugin 模块 | bridge 会建立 profile/node_modules 链接；检查 DSH 安装完整性和该 profile 的写权限 |
| profile/node_modules 指向另一版本 | 使用独立 DSH_HOME，或在停机后人工核对链接；启动器不会覆盖已有依赖目录 |
| `EADDRINUSE` | 确认是不是已有 bridge；不要盲目杀占用进程，演示默认用 3421 |
| 工作区被拒绝 | 填写绝对路径，并确认 realpath 位于允许根内；跨界符号链接/junction 会被拒绝 |
| `.env` 修改没有生效 | 已有环境变量优先；还要确认执行目录及 bridge/.env 位置 |
| MCP 调用超时 | 先按已知 session 查询，或在同一进程 list_sessions 查 correlation；不重复 start |
| 重启后 `not_found` | 只说明内存 registry 不认识；用已保存的 DSH 日志和 session 证据确认，再决定是否 continue |
| `STATE_DRIFT` / 契约 hash 不符 | 保留现场，找出事务之外的修改；不要重写 hash 掩盖差异 |
| `LEASE_HELD` | 另一个写者仍持有租约；不要删除 lease 文件 |
| `DISPATCH_CAPABILITY_MISMATCH` | 核对实际异常处理任务权限和本地 runtime 兼容性；不要手工补写成功预检 |

可恢复事务中断使用 `python control/supervisor_ctl.py --root /absolute/control-root recover`，然后再次 check。发现损坏事务、旧 pending transaction 或未知外部副作用时先保留证据；参阅控制器协议，不要删除 prepare/commit 文件后强行继续。

## 可选 session routing 补丁

`DSH_BRIDGE_SESSION_ROUTING=1` 会在启动时安装原环境中的 opencode-go 兼容补丁：每次请求使用实际 DSH session 设置 `x-opencode-session`，不改共享 profile。它只针对 `dsh-llm-pi-ai` 的 `streamWithSnapshot` 接口，默认关闭。更换 DSH 版本前重新验证；若该接口不存在会明确报错。

## 权限与网络

bridge 默认仅监听本机；校验 Host/Origin 并限制请求体为 1 MiB。允许根检查只限制任务入口，不阻止模型借助有权限的 shell 访问其他文件。MCP 没有内建身份验证/公网部署能力。`DSH_BRIDGE_ALLOW_NON_LOOPBACK=1` 是高级部署开关，开启时需要部署者提供认证、网络隔离和传输保护。

异常处理的 Codex 适配器会请求 Full Access/never，需部署者明确授权。普通 DSH 权限由 DSH 配置控制，两者不是同一权限设置。`investigate/review` 模式也不等于系统层面的只读。

## 验证范围

- CI：Python 控制器回归测试；Node 配置/路径、真实 MCP 协议、模拟 DSH 生命周期、续跑去重与 routing 补丁测试。均不消耗模型额度。
- 本次本地验证：Windows + 已安装的 DSH 0.1.2-rc.1，使用新的私有 DSH_HOME 成功启动 runtime，并发现六个 MCP 工具和 ping。
- 尚未重新验证：此次打包版本的真实模型任务、模型 provider 连通性，以及实际 Codex 异常任务派发。模拟测试不应宣传为这些外部集成的验收结果。
