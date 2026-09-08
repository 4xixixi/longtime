# 从零开始

Longtime 的目标是用低成本模型承担日常工作，让强模型按需兜底，减少昂贵模型的持续参与。仓库包含两个可以独立使用的部分：Python 控制器管理任务状态，Node bridge 将 DSH 暴露为 MCP。先跑离线演示，再连接真实 DSH，最后启用长期监督。

部署时先确定三项分工：周期监督者使用低成本模型，DSH 使用能胜任常规任务的低成本执行模型，异常处理任务使用更强的模型。仓库不自动选择最便宜模型；异常适配器沿用宿主默认或原异常任务模型，需要核对实际配置。离线演示验证流程，不测量模型质量或节省比例。

## 1. 安装仓库依赖并离线验证

需要 Python 3.10+、Node.js 22.16+ 和 npm。在 Windows 中可将 `python` 替换为 `py -3`。

```sh
git clone https://github.com/4xixixi/longtime.git
cd longtime
python -m unittest discover -s tests -v
python examples/demo.py
npm ci --prefix bridge
npm test --prefix bridge
```

Python 演示会检测绕过事务的状态修改；Node 测试通过真实 HTTP/MCP 协议驱动模拟的 DSH 运行接口，不读取凭据、不启动模型。

两个终端可以观察模拟任务的 start → continue → status：

```sh
# 终端 A，仓库根目录
npm run demo --prefix bridge
```

```sh
# 终端 B，仓库根目录
npm run demo:client --prefix bridge
```

演示固定监听 `127.0.0.1:3421`，输出明确标记 SIMULATED；它证明接口和生命周期可以贯通，不证明模型完成了代码任务。Ctrl+C 停止演示服务。

## 2. 安装并配置真实 DSH

公开源码适配的本地版本为 `@deepseek-ai/dsh@0.1.2-rc.1`。DSH 官方说明其仍处于 developer preview，接口可能发生不兼容变化。不要把依赖最新版自动升级等同于兼容性验证。参考 [DSH 上游](https://github.com/deepseek-ai/deepseek-harness)。

```sh
npm install -g @deepseek-ai/dsh@0.1.2-rc.1
dsh web
```

按 DSH 界面配置自己的 provider、模型和凭据，先在 DSH 中确认一个小任务能执行。bridge 复用 DSH 配置，不负责供应模型账户。若想隔离配置，先将 `DSH_HOME` 设到自己的私有目录，再进行 DSH 配置与 bridge 启动；两者必须使用同一个 home。

## 3. 启动常驻 bridge

把 `bridge/.env.example` 复制为 `bridge/.env`。至少将 `DSH_BRIDGE_WORKSPACES` 改为目标代码仓库的绝对路径；多个根用逗号分隔，路径本身含逗号暂不支持。Windows 路径可以写成 `D:/projects/my-app`。

```dotenv
DSH_BRIDGE_HOST=127.0.0.1
DSH_BRIDGE_PORT=3420
DSH_BRIDGE_WORKSPACES=/absolute/path/to/my-app
DSH_BRIDGE_EPOCH_TIMEOUT_MS=45000
```

自动查找 DSH 失败时，再填写 `DSH_BRIDGE_INSTALL`，值必须是含 `package.json`、`lib/bin.js` 和 `node_modules/` 的 DSH 包目录。Node 的 `--env-file-if-exists` 不覆盖已有进程环境变量；遇到旧配置生效，先检查终端中同名 `DSH_*` 变量。

```sh
npm run doctor --prefix bridge
npm start --prefix bridge
```

另一个终端执行：

```sh
npm run probe --prefix bridge
```

成功时看到 `ok: true` 和六个工具。`doctor` 只检查版本、模块解析和路径；`probe` 只验证 MCP 连接，两者都不会执行模型任务。启动会创建/更新 `$DSH_HOME/profiles/dsh-mcp-bridge/`，把该 profile 的依赖链接到所选 DSH 安装，并加载用户原有的 DSH 设置。

长期任务使用单个常驻 HTTP bridge。不要让每次心跳都创建一个独立 stdio bridge。停止或升级前先确认没有正在执行的 epoch，详见[运维说明](operations.md)。

## 4. 在 Codex 配置 MCP

将 [配置示例](../examples/codex-mcp.toml) 的表合并进自己的 `~/.codex/config.toml`，不要覆盖整个配置文件：

```toml
[mcp_servers.dsh]
url = "http://127.0.0.1:3420/mcp"
startup_timeout_sec = 20
tool_timeout_sec = 60
```

Codex 支持以 `[mcp_servers.<name>]` 配置 HTTP 服务和工具超时；配置方式来自 [官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。重载 MCP/重新打开任务后先调用 `ping`。工具在客户端可能显示为带连接器前缀的名称，以实际发现的工具列表为准。

这里的 `127.0.0.1` 指运行 MCP 客户端的机器。远端或云端客户端不能直接访问你的本机端口；本仓库不提供公网鉴权网关，不应直接把这个执行接口暴露到公网。

## 5. 单独体验真实 MCP 任务

在你明确允许 DSH 操作的测试仓库中，向监督者提出：

> 请通过 DSH 调查这个测试仓库的结构和测试入口。先调用 dsh_start_task，mode 选 investigate，workspace 使用我的绝对路径。只调查，不改代码。保存返回的 session_id；如果返回 running，本轮结束，稍后只查询 dsh_get_status。

拿到完成结果后再提出：

> 请用 dsh_continue 继续刚才同一个 session，核实你发现的测试入口，并给出实际命令和结果。

`investigate/review` 是给模型的模式提示，不是文件系统只读沙箱。实际权限仍由 DSH 配置决定。此步骤只演示 MCP 执行器，尚未使用 Longtime 的事务/验收门禁。

## 6. 创建长期任务控制工作区

把目标、允许修改的范围、完成标准、原始验证命令、预算与禁止事项写进自己的 `specification.md`。建议从 [规格模板](../templates/job-specification.md) 开始。

然后在公开仓库根目录运行（下面的路径均需替换）：

```sh
python scripts/init_workspace.py --root /absolute/path/to/new-control --workspace /absolute/path/to/my-app --spec /absolute/path/to/specification.md --job-id first-job
python control/supervisor_ctl.py --root /absolute/path/to/new-control check
```

初始化器只接受一个尚不存在的控制目录，复制运行代码，生成一项排队任务、契约 hash 和初始事务，项目保持 **PAUSED**。不会修改目标代码，也不会生成虚假的 Git baseline、执行证据或用户授权。初始化失败时保留新目录以便检查，不覆盖或清理现有工作区。

启用前让监督者在该控制目录中检查规格：为真实项目补齐 baseline、核心成功标准、协议 authority 和适用的制品路径，通过 USER/MIGRATION 事务采纳。生成的最小契约只是起点；复制占位模板不能替代这一步。

完成核对并决定运行后，操作者执行以下受控状态切换；这会让后续监督轮可以派发任务：

```sh
python scripts/set_project_status.py --root /absolute/path/to/new-control --status ACTIVE --reason "已核对任务规格和执行边界，批准开始"
```

## 7. 执行一轮监督并接入定时运行

作者的日常运行方式是使用 **Codex 定时任务**周期性唤醒监督者，再由监督者通过 MCP 调用 DSH；强模型仅在异常处理环节介入。一份约 6 小时、8 轮监督的个人运行记录及模型配置见 [README](../README.md#一次夜间运行记录)。其他调度器也可以触发相同的单轮协议。

在新控制目录打开一个监督任务，使用 [监督任务说明](../examples/supervisor-task.md)；里面的 `<CONTROL_ROOT>` 替换成真实路径。第一轮先手动执行并查看事务和报告，确认闭环后，再由你使用的调度器定期触发相同提示。

推荐起始间隔是 45 分钟，控制器默认按这一间隔判断查询节奏；调度器仍需你自行配置。本仓库不自动创建 Codex 定时任务或系统服务。每轮只执行一个规划动作，返回 running 后结束该轮，验收轮独立执行规格中的验证命令。

下一步阅读[架构与状态转换](architecture.md)和[恢复/排错手册](operations.md)。
