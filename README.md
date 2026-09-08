# Longtime

[![Tests](https://github.com/4xixixi/longtime/actions/workflows/tests.yml/badge.svg)](https://github.com/4xixixi/longtime/actions/workflows/tests.yml)

面向长期 AI 编程任务的控制工作区：**监督者负责判断，Python 控制器负责状态，DSH 负责执行，MCP bridge 负责连接。**

Longtime is an experimental control plane for long-running AI tasks, with a local DSH MCP bridge, recoverable transactions, single-writer leases, and independent acceptance checks.

```text
用户目标 → 监督者 → Python 控制器：规划 / 租约 / 事务
                    ↓
                MCP bridge → DSH agent → 代码工作区
                    ↓
                执行结果 → 独立验收 → 下一阶段
```

## 从哪里开始

- [完整使用教程](docs/quickstart.md)：离线体验、安装 DSH、启动 bridge、接入 Codex、初始化任务和定时监督。
- [架构与一轮执行](docs/architecture.md)：状态转换、intent/commit、两套完成状态以及恢复边界。
- [运行与排错](docs/operations.md)：暂停、升级、超时、session 恢复和权限。
- [MCP 工具与环境变量](bridge/README.md)：六个工具及配置参考。

## 先跑离线版本

需要 Python 3.10+、Node.js 22.16+。

```sh
git clone https://github.com/4xixixi/longtime.git
cd longtime
python -m unittest discover -s tests -v
python examples/demo.py
npm ci --prefix bridge
npm test --prefix bridge
```

Windows 可用 `py -3` 替换 `python`。上述测试不需要 DSH、模型账户或 API key。两个终端分别运行 `npm run demo --prefix bridge` 和 `npm run demo:client --prefix bridge`，可以观察模拟任务在真实 MCP 协议中的 start → continue → status。

## 核心能力

- 租约保证单写者；所有业务状态变更由控制器校验后提交。
- 外部动作前保存 intent，超时后优先恢复原 session，避免盲目重复派发。
- prepare/commit 事务与 hash 校验支持崩溃后的前滚恢复和漂移检测。
- 项目契约、协议 revision 与独立验收记录约束任务边界。
- bridge 提供 start、continue、status、list、cancel 和 ping；45 秒等待上限后可返回 running，让后台 epoch 继续。
- 初始化器创建独立的 PAUSED 控制工作区；激活/暂停也通过 USER 事务执行。

## 目录

| 路径 | 用途 |
| --- | --- |
| `control/` | Python 控制器、可选 Codex 异常处理适配器、详细协议 |
| `bridge/` | DSH runtime 启动、MCP server、runner、lockfile、离线测试 |
| `scripts/` | 独立工作区初始化、受控激活/暂停 |
| `templates/` | 任务规格、状态、协议与结果模板 |
| `examples/` | 离线演示、Codex MCP 配置、监督者提示 |
| `docs/` | 从零使用教程、架构、运维 |
| `tests/` | Python 回归测试 |
| `AGENTS.md` | 自动运行协议 |

## 当前边界

这是从个人长期任务环境提取的工程原型。真实 DSH runtime 适配的本地版本是 `0.1.2-rc.1`；上游插件 API 和 Codex 本地异常处理接口变化时需要重新验证。真实 provider 配置、模型额度和系统权限由部署者提供。公开仓库不携带真实任务历史、会话、个人路径或凭据。

核心支持一个项目的单写者调度；还没有通用 Web UI、自动安装的常驻服务或全自动生产配置。初始化器生成的最小契约需要结合实际规格补齐 baseline 和验收标准。MCP completed 只表示 epoch 结束，任务通过仍需独立验收。

详细变更见 [CHANGELOG](CHANGELOG.md)。DSH 本体通过 [上游项目](https://github.com/deepseek-ai/deepseek-harness) 安装，不打包 node_modules；相关依赖信息见 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md)。

## 为什么值得保留

适合跨会话恢复、多阶段验收和外部任务去重的个人自动化项目，也可作为可靠 agent 工作流的实现参考。对几分钟的一次性脚本，维护契约和状态的成本通常不划算。

## 许可

当前未授予本仓库代码的开源许可证；公开可见不等于授予任意复制、修改或分发许可。第三方组件按各自许可证提供。
