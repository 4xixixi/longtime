# Longtime

[![Tests](https://github.com/4xixixi/longtime/actions/workflows/tests.yml/badge.svg)](https://github.com/4xixixi/longtime/actions/workflows/tests.yml)

**低成本模型做日常工作，强模型在关键时刻兜底。**

Longtime 是一套面向长期 AI 编程任务的分层模型协作系统。日常执行和例行监督交给低成本模型；遇到卡住、反复失败或复杂技术问题时，再由强模型诊断、纠偏，解决后把任务交回低成本模型继续执行。目标是在保持验收标准的前提下，减少昂贵模型的持续参与和重复工作。

Longtime is an experimental model-tiered workflow for long-running AI tasks: lower-cost models handle routine work, stronger models step in for difficult failures, then hand execution back. It aims to reduce expensive-model usage while preserving independent acceptance checks.

```text
用户目标 → 低成本监督者 → 低成本执行模型（DSH）
                              │
                  ┌───────────┴───────────┐
               正常完成                阻塞 / 失败
                  ↓                       ↓
               独立验收             强模型诊断、纠偏
                  ↓                       ↓
               下一阶段          低成本模型在原会话续跑
                                          ↓
                                       独立验收

Python 控制器维护状态、重试边界与恢复记录；MCP bridge 连接执行器。
```

## 如何减少消耗

- **按需使用强模型**：让复杂异常进入独立处理事件，避免强模型全程执行和反复查询。
- **解决后交回**：强模型完成纠偏后恢复原任务，由低成本执行模型继续工作。
- **复用已有会话**：保存 session 和进度，减少中断后重新解释背景、重复调查和重复派发。
- **限制无效循环**：设置返工、运行时间和异常处理次数上限；达到上限按策略停止或报告。
- **保留验收标准**：用独立验证判断完成，不以“少调用模型”代替任务质量。

“强/弱”是相对于具体任务的能力分工，不固定某个模型品牌，也不意味着便宜模型一定更弱。控制器负责按任务状态路由；模型档位由部署者配置，当前没有自动比价或按价格选择模型的功能。

**目前尚无节省比例的实测结论。** 强模型兜底也会增加调用和上下文成本；如果低成本模型频繁失败，总消耗可能更高。评估应在相同任务和验收标准下，对比分层方案与全程强模型方案的总费用、token、完成率和耗时，包含监督、返工与异常处理开销。当前运行/重试限制不是完整的 token 或费用计量系统。

## 一次夜间运行记录

这套系统的日常监督由 **Codex 定时任务**驱动：定时唤醒监督模型，检查进度，并通过 MCP 调用 DSH 执行任务；遇到异常时，再由更强的模型介入处理。

在作者提供的一次夜间运行记录中，系统在约 **6 小时内进行了 8 轮定时监督**，期间触发了 **1 次异常处理**。模型分工与观察到的消耗如下：

| 环节 | 配置 / 观察 |
| --- | --- |
| 定时监督模型 | GPT-5.6 Luna，推理强度 Max |
| 异常处理模型 | GPT-5.6 Sol，推理强度 Medium |
| DSH 执行模型 | OpenCode Go 中的 dsflash，推理强度 High（名称沿用作者记录） |
| Codex 侧消耗 | 约占作者当时观察的 Plus 可用额度的 10% |
| OpenCode Go 侧消耗 | 约 2.8 美元额度 |

这是一次个人运行观察，未在本仓库中附带账单或逐调用计量日志。8 轮指调度轮次，不等于底层模型请求总数；Plus 额度的统计窗口未记录，10% 不表示订阅费用的 10%，也不与 2.8 美元直接相加。该记录展示了“低成本模型常规执行、强模型按需兜底”的实际使用方式，尚不能据此推导相较于全程强模型的节省比例。

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

适合日常步骤较多、只有少数环节需要强模型介入的长期任务，也适合作为模型分工与成本控制的实验基础。跨会话恢复、独立验收和事件去重服务于这个目标。对几分钟的一次性脚本，或大部分步骤都超出低成本模型能力的任务，协作与返工开销可能抵消节省。

## 许可

当前未授予本仓库代码的开源许可证；公开可见不等于授予任意复制、修改或分发许可。第三方组件按各自许可证提供。
