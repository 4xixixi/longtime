# Longtime

文件驱动的长期 AI 任务控制器：用确定性状态机管理租约、任务路由、验收和中断恢复。

Longtime is an experimental, file-backed control plane for long-running AI tasks. It provides single-writer leases, write-ahead intents, recoverable commits, and evidence-based acceptance. The core uses only the Python standard library; live agent integrations are optional and environment-specific.

## 解决什么问题

长任务常在会话中断、重复派发、任务规格漂移和“自报完成”处失控。Longtime 把技术判断交给模型，把可机械校验的规则交给 Python 控制器：

- **单写者租约**：避免多个监督器同时修改任务状态。
- **副作用意图记录**：外部调用前持久化 intent，恢复时优先找回原 session 或原事件。
- **可恢复事务**：prepare/commit 与 hash 校验用于恢复跨文件状态写入；这不是数据库级多文件原子写。
- **契约与验收**：绑定项目契约、协议 revision、任务规格和验收记录，拒绝未经授权的漂移。
- **异常事件去重**：同一开放事件保留身份，异常处理与独立验收分开。

```text
Scheduler / supervisor
        |
        v
begin (lease + validation + one planned action)
        |
        +--> prepare-intent --> external executor / exception handler
        |
        v
commit (validate + persist + release lease)
        |
        v
runtime + queue + jobs + transaction history
```

LUNA、SOL、DSH 是原工作流的角色/执行接口名称。核心状态机无需调用任何模型；仓库不附带调度服务、模型凭据或 DSH bridge。

## 快速体验

需要 Python 3.10+；本地发布验证使用 Python 3.13。核心与测试没有第三方 Python 依赖。

```sh
git clone https://github.com/4xixixi/longtime.git
cd longtime
python -m unittest discover -s tests -v
python examples/demo.py
python control/supervisor_ctl.py --help
```

Windows 可将 `python` 替换为 `py -3`。演示复用测试中的合成任务 fixture，在临时目录验证健康状态，再演示直接修改状态被拒绝，最后清理临时目录；不会启动模型、连接账户或触碰真实任务。它不是生产初始化器。

## 目录

| 路径 | 用途 |
| --- | --- |
| `control/supervisor_ctl.py` | 状态机、租约、事务、校验、路由 |
| `control/exception_handler.py` | 可选的本地异常任务派发与恢复 |
| `control/app_server_client.py` | 本地 App Server stdio 适配器 |
| `control/handler_capabilities.py` | 实际执行权限与工作区能力校验 |
| `control/invoke-supervisor.ps1` | Windows Python 入口包装 |
| `templates/` | job state、规格、协议、结果等模板 |
| `tests/` | 自建临时 fixture 的回归测试 |
| `examples/demo.py` | 无外部依赖的控制器演示 |
| `AGENTS.md` | 自动运行协议 |

协议详情见 [workflow v8.1](control/workflow-v8.1-spec.md)、[提交格式](control/commit-request-guide.md)、[策略](control/policy.md)、[监督器提示](control/supervisor-prompt.md) 和 [异常处理提示](control/exception-handler-prompt.md)。

## 接入真实工作区

公开仓库只包含通用源码，不包含可直接继续的真实任务状态。当前没有通用的一键生产初始化命令：

1. 按自己的目标建立 `control/project-contract.json`、`runtime.json`、`queue.json`，并依据模板填写 `jobs/<job-id>/`。字段结构可参考测试中的 `make_root()`；其中 session、hash、工作区等合成值必须替换为真实数据。
2. 核对契约、规格和 hash 后，使用 `python control/supervisor_ctl.py --root /absolute/control-root bootstrap` 建立初始事务快照。`bootstrap` 只处理已准备好的状态，不会替你生成项目。
3. 使用 `python control/supervisor_ctl.py --root /absolute/control-root check` 只读检查。`--root` 放在子命令之前。
4. 配置自己的调度和执行器，遵守 `begin → prepare-intent（需要外部动作时）→ commit`；初始化后所有状态变更通过事务提交。

检查时解析 JSON 的 `ok`、`status`、`error`，不能只依赖 CLI 退出码；部分非健康结果仍以零退出码返回。

## 集成限制

- 异常处理适配器来自 Windows 本地环境，依赖已安装的 Codex runtime，以及其 App Server 和本地状态/rollout 格式。平台升级后需要重新验证；通过单元测试不代表真实模型派发已联调通过。
- 异常处理入口显式请求 Full Access 与 `approvalPolicy=never`。部署者必须先授权和审核该执行方式；控制器的业务门禁不能替代操作系统隔离。
- DSH bridge、机器专用 session-routing 补丁、系统登录任务和特定项目的迁移脚本未打包。需要执行真实 DSH 任务时，自行提供对应接口。
- 当前是单项目、单写者的实验性实现；核心控制器仍较大，尚未拆为稳定公共库，也没有通用 UI、安装包或生产支持承诺。

## 精简范围与价值

此仓库从实际长期任务工作区提取，保留控制器、8 个测试模块和通用协议模板。排除了真实 job、事务/心跳记录、诊断备份、运行缓存、个人任务清单、项目专属契约和机器路径；原工作区继续独立保存这些资料。

适合需要跨会话恢复、多阶段验收和外部任务去重的个人自动化项目，也适合作为可靠 agent 工作流的实现参考。若只做几分钟的一次性脚本，这套契约和状态维护成本通常不划算。后续最值得做的是通用初始化器、执行器接口和控制器模块拆分。

## 许可

当前未授予开源许可证。公开可见不等于授予任意复制、修改或分发许可；复用前请联系仓库所有者。
