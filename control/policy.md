# 长时间任务监督策略（workflow v8.1）

## 1. 设计原则

LLM 被视为可能误读、遗漏或重复动作的语义组件。确定性控制由 `control/supervisor_ctl.py` 承担：

- 单写者 lease 防止心跳重叠；旧 token 不能提交。
- 每个外部副作用先写 intent；未知结果先恢复，不重派。
- 每次状态提交先写完整 prepare，再原子替换快照，最后写 commit；崩溃后统一 roll-forward。
- 每个提交使用 runtime/queue/job revision 与快照 hash 做 compare-and-swap。
- runtime、queue、job state 的任何直接修改都视为状态漂移。

事务历史是恢复证据，JSON 快照是当前查询界面；两者不一致时停止外部动作。

## 2. 权威层级

1. `control/project-contract.json`：用户所有的 outcome、批准阶段、核心成功标准、安全和预算边界。
2. `jobs/<job-id>/protocol-contract.json`：Sol 授权的当前技术 revision、authority、specification hash 和制品路径。
3. `jobs/<job-id>/specification.md`：任务行为、验收命令和允许范围。
4. 工作区冻结配置、manifest、Git diff 和可重复验证结果。
5. DSH/Sol 摘要仅是索引，不是验收事实。

Sol 可修改第 2、3 层，但不得改变第 1 层。project contract hash 变化只有用户明确授权的控制面迁移才能接受。

## 3. 状态机

合法状态：`DRAFT`、`QUEUED`、`RUNNING`、`REVIEW_PENDING`、`BLOCKED`、`FAILED`、`CANCELLED`、`ACCEPTED`、`ARCHIVED`。

```text
DRAFT -> QUEUED -> RUNNING -> REVIEW_PENDING -> ACCEPTED -> ARCHIVED
                    |              |
                    |              +-> QUEUED (same-session rework)
                    +-> BLOCKED/FAILED -> Sol event -> normal state
```

不存在 `REWORK` 中间状态。控制器分别维护：每次 job 内容持久化变化都递增的 `state_revision`、仅在 `semantic_projection` 变化时递增的 `semantic_revision`，以及只在出现新 actionable 技术异常时递增的 `event_generation`。技术事件 ID 为 `<job_id>:<event_generation>:<origin_status>`，派发、确认和 thread 账务不得改变它。队列空事件仍为 `project:<queue_revision>:<planning_generation>:QUEUE_EMPTY`。

## 4. 正常心跳

Luna 必须以控制器 `begin` 返回的动作路由：

- `DSH_START`：就绪检查通过后启动唯一 session。
- `DSH_CONTINUE`：只对 state 中同一 session 使用原样 continuation instruction。
- `DSH_STATUS`：查询一次；摘要或状态变化才更新 `last_progress_at`。
- `REVIEW`：只做契约预检和独立验证，不调用 DSH。
- `HANDOFF_ACCEPTED_JOB`：控制器在同一事务中归档 accepted job、出队、验证并激活合法后继；末项则运行项目完成闭包验证。本动作不调用外部系统。
- `SOL_ESCALATE/SOL_QUEUE_EMPTY`：先写 intent，按 event ID 创建唯一可见 Sol 任务，记录 thread ID 后结束。
- `RECOVER_INTENT`：先恢复未知外部结果；不允许先创建替代动作。

控制器根据 `runtime_tracking.started_at + accumulated_seconds` 执行自动运行上限，并根据 `last_progress_at` 判断 stale。旧 v7 无法可靠还原的运行时间明确标记 `legacy_runtime_unknown`，从 v8 下一次 DSH 派发开始计量。

## 5. 异常与升级

`BLOCKED/FAILED` 的本地可逆问题交给 Sol。每个 job 有总升级预算和同根因预算；耗尽时进入终结技术故障并通知用户，不伪装成用户审批。

Sol 事件必须绑定创建时的完整 event ID。旧事件、未派发事件或已经被新 root cause 取代的事件返回 `NO_ACTION_STALE_EVENT`。`REQUIRED -> DISPATCHED -> ACKNOWLEDGED -> RESOLVED/SUPERSEDED` 由控制器维护；没有真实语义结果不得关闭事件。Sol 不直接承担周期监督，也不建立第二层自动化。

USER_REQUIRED 使用独立 `user_gate` 收据；它不复用 Sol event identity。只有六类 USER_ONLY gate 合法，gate ID 与请求变更 hash 均由控制器生成。

队列空事件的有效结论只有：

1. 创建一个 outcome、约束、验收标准、工作区和基线均完整的新 job 并入队；或
2. 将当前用户批准阶段标记完成；如下一步会进入新阶段，则使用 `PROJECT_OUTCOME_PHASE` 等待用户。

## 6. 验收

`REVIEW_PENDING` 先做契约预检：project hash、protocol revision、authority hash、specification hash、制品路径、manifest 和审计参数必须一致。漂移时不执行旧审计，不把旧路径失败归因于实现。

契约一致后按 specification 顺序逐条运行原始验证命令。迁移后的普通 ACCEPT 只能由 Luna 在 `REVIEW` planned action 下提交，且必须提供 `V8_1 / LUNA_ATTESTED` verification record、非空命令、全部零退出码、空 `unverified_items` 及与当前 specification/protocol 一致的 hash。记录只证明 Luna 提交了结构完整的可审计陈述，不证明 OS 实际执行。历史 accepted job 保留原内容并以 `LEGACY_V8` 迁移收据验证，绝不伪造成新版 attestation。

## 7. 基线与模板

新实现 job 必须使用独立 Git 工作区或用户明确批准的既有工作区。dirty 工作区基线必须保存逐路径类型、mode、tracked/untracked 状态与内容 hash；仅有聚合 hash 不足以证明归属。模板 schema 必须通过 `supervisor_ctl check` 所依赖的同一校验规则。

## 8. 时间、工具与输出

所有具体时限、调用数和升级预算只以 `control/runtime.json` 为准，不在提示词中复制数值。收到后台 `running` 后立即提交状态并停止；不得连续轮询或读取完整日志。

`check` 是绝对只读诊断：使用 lease、最新有效 committed head 与 pending set 的双读屏障；发现 live/expired lease、并发变化或 pending transaction 时不会误报 drift。`recover` 是唯一事务恢复写入口；正式 `begin` 仍可 recover-first。

用户可见性依据 semantic revision、状态、决策、进展、验收和外部派发变化；纯轮询只推进 state revision 并保持静默。每次成功提交仍在 `control/heartbeat-log.jsonl` 和事务目录留痕。

## 9. 安全

USER_ONLY 六类以 project contract 为准。未经明确授权，不得删除/覆盖证据、commit/push、发布或通信、购买、提权、读取秘密、安装系统依赖或扩大项目阶段。所有本地可逆技术选择由 Sol 负责。
