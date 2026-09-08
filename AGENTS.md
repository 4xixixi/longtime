# 长时间任务工作区约定（workflow v8.1）

本目录是长期任务的控制面。LLM 负责解释证据和选择技术方案；`control/supervisor_ctl.py` 负责确定性的状态校验、路由、幂等、租约与持久化。

## 事实与写入边界

- 当前状态由 `control/runtime.json`、`control/queue.json` 和 `jobs/*/state.json` 表示；不可变事务位于 `control/transactions/`，未完成外部动作位于 `control/intents/`。
- `control/project-contract.json` 是用户所有的 outcome、阶段、核心成功标准和 USER_ONLY 边界。普通 Luna/Sol 不得修改或重新授权它。
- 每个 job 的 `protocol-contract.json` 是当前技术 revision；Sol 可以在不改变 project contract、保留旧证据并更新 specification hash 后建立新 revision。
- 自动运行不得直接编辑 runtime、queue 或 job state。所有状态变更必须由 `& .\control\invoke-supervisor.ps1 commit ...` 提交；直接编辑会在下一次校验时被判为 `STATE_DRIFT`。
- `& .\control\invoke-supervisor.ps1 check`（`verify` 兼容别名）绝对只读；任何 expired lease、pending transaction 或不稳定双读屏障都必须先由 `recover`/`begin` 恢复，不能由 check 写入。
- 工作区和控制记录不得保存 API key、token 或其他秘密。

## 固定角色

- Luna 是唯一周期监督器：正常查询、独立验收、从已批准队列推进任务。
- 异常处理程序（内部兼容角色码 `SOL`，不限定 Sol 模型）是按唯一 technical `event_id` 创建的一次性异常处理任务：处理阻塞、契约漂移、session 恢复和队列耗尽。不同 event 必须使用不同任务；相同 event 只恢复原任务。派发/确认只推进 `state_revision`，不得改变 `semantic_revision`、`event_generation` 或 event ID；只有 Sol 成功接管并提交后才以 `acknowledged_at` 计入异常处理预算。
- DSH 只执行有完整规格、完成标准和有效工作区的具体 epoch。一个 job 默认只保留一个 session。
- 不创建 Codex 子代理。用户只授权按本策略创建的可见异常处理事件任务。部署前需要用户明确授权异常处理任务使用 Full Access；通过 `control/exception_handler.py` 显式配置 `:danger-full-access + approvalPolicy=never`，不固定模型。

## 每次 Luna 心跳

本节约束定时或自动触发的 Luna 心跳。用户在当前会话中直接授权的控制面维护不是 Luna 心跳；它可以连续完成本地实现与验证，但任何 runtime、queue 或 job state 写入仍必须通过一个受控 `USER/MIGRATION` 事务提交，不得直接编辑状态文件。

1. 首个本地动作必须是：

   `& .\control\invoke-supervisor.ps1 begin --role LUNA [--source-thread-id <本轮真实ID>]`

   无法直接取得本轮 ID 时省略参数；不得继承旧 ID。命令会记录 `started_at`、恢复未完成事务、取得单写者 lease、校验契约与状态，并返回唯一下一动作。
2. `ok=false` 且原因为 `LEASE_HELD/LEASE_RACE_LOST` 时直接返回 `NO_ACTION`。其他控制器错误属于控制面故障：不要手工修补状态，也不要派发外部动作。
3. 只执行控制器返回的一种动作。需要 DSH 调用或创建 Sol 任务时，调用前必须运行：

   `& .\control\invoke-supervisor.ps1 prepare-intent --token <lease_token>`

   将返回的 `correlation_id` 放进外部任务说明或可恢复标题中。存在未解决 intent 时只能恢复，不得重新派发。
4. 把本轮结果写成 `begin` 返回的 `request_path` JSON；格式见 `control/commit-request-guide.md`。所有诊断、验证和外部调用必须先完成。
5. 本轮最后一次本地调用必须是：

   `& .\control\invoke-supervisor.ps1 commit --token <lease_token> --request <request_path>`

   它会校验预期 revision、提交可恢复事务、写真实 `finished_at`、记录 heartbeat 并释放 lease。异常任务的后台执行端随后依据已提交的原 event/thread/回执启动模型；Luna 无需也不得在 commit 后另行调用启动工具。之后只能输出最终回复。

## 路由与验收

- `DSH_START/DSH_CONTINUE/DSH_STATUS`：遵守 runtime 中的调用、状态查询和墙钟上限。返回 `running` 就持久化并停止，不连续轮询。
- `SOL_STATUS`：只查询事件记录的原 thread 一次；仍在运行则记录观察并停止，已结束但事件仍开放则只恢复同一 thread。状态观察写入 event 的 `status_checked_at/thread_status`，在一个检查周期内不得重复查询或恢复。
- `RECOVER_INTENT`：已知 session 查询一次；未知 start 只列 session 一次，并用 correlation/job/time 唯一匹配。无法唯一恢复时转控制面异常，绝不重复派发。
- `REVIEW`：先核对 project/protocol contract、specification hash、revision、authority、replay/formal 路径与 manifest；一致后才逐条运行 specification 的原始验证命令。验收轮不调用 DSH；普通 ACCEPT 必须提交完整 `V8_1 / LUNA_ATTESTED` verification record。
- 全部通过转 `ACCEPTED`；明确可返工问题转 `QUEUED + CONTINUE_SAME_DSH_SESSION`；含糊、冲突、越界或漂移转 `BLOCKED`，下一轮创建 Sol 事件。
- `HANDOFF_ACCEPTED_JOB` 提交空 patches，由控制器在同一事务中归档、出队、验证并激活后继，或验证项目闭包；不在同一轮调用任何外部系统。
- `LIMIT_REACHED/INVALID_STATE/TERMINAL_TECHNICAL_FAILURE` 不得自行绕过。保存为阻塞或终结故障并按策略通知。

## 异常处理事件（内部角色码 SOL）

- 首个本地动作必须是 `& .\control\invoke-supervisor.ps1 begin --role SOL --event-id <创建任务时给出的完整event_id> --wait-lease-seconds 60 [--source-thread-id <本轮真实ID>]`。派发入口先保存 PREPARED 对话回执，后台执行端确认绑定事务已提交且 lease 已释放后才启动 turn；这 60 秒只用于处理随后其他合法写入者的竞争，不再承担等待派发父任务提交的职责；控制器会拒绝与派发记录不一致的非空 thread ID。
- 控制器返回 `NO_ACTION_STALE_EVENT` 时停止，不修改文件。
- 等待结束后若仍返回 `LEASE_HELD/LEASE_RACE_LOST`，本次没有完成 Sol 握手、不得计入升级预算；保留原 `event_id` 和原任务，后续只能恢复该任务，Luna 不得创建新事件或新任务。
- 取得 `SOL_HANDLE_EVENT/SOL_HANDLE_QUEUE_EMPTY` 后先运行 `capability-preflight --token <lease_token>`，验证实际 turn 的 Full Access + never、控制目录和工作区读写/shell/WSL 能力。通过后才可用 `commit` 提交 `ACKNOWLEDGED + retain_lease=true` checkpoint，并使用返回的新 expected 继续；job 事件用 event_update，队列空事件用 queue_event_update。失败只提交空 patches 和 DISPATCH_CAPABILITY_MISMATCH，保留事件/任务、不计预算。每次恢复取得新 lease 后重做预检。该账务不改变 semantic revision、event identity 或异常预算。
- Sol 负责原 outcome 内的本地、可逆技术选择；不能只列方案或要求用户批准技术细节。
- Sol 持有 lease 超过 15 分钟时必须运行 `& .\control\invoke-supervisor.ps1 renew --token <lease_token>`，以后每 15 分钟或任何预计耗时超过 10 分钟的命令前续租一次。
- Sol 修改 specification 时必须先把新 specification 与 `protocol-contract.json` 写入 `control/staging/<event>/`，保留旧 revision 证据，并在 commit request 中同时使用 `adopt_protocol_contract=true` 与 `protocol_staging`；控制器在同一可恢复事务中发布文件、job authority 和 queue hash。
- Sol 完成异常处理后把 job 恢复为 `QUEUED/RUNNING/REVIEW_PENDING`；普通 ACCEPT 仅由 Luna 在 REVIEW 动作下作出。确实命中 USER_ONLY 时只提交事实，由控制器生成独立 `user_gate` 并以此语义结果关闭 technical event。
- 队列空事件只有在建立完整新 job，或将当前批准阶段明确标记完成时才能 RESOLVED；空手 RESOLVED 会被控制器拒绝。

## 用户专属门禁

`USER_REQUIRED` 只允许：`DESTRUCTIVE_DATA_LOSS`、`EXTERNAL_REMOTE_EFFECT`、`PRIVILEGE_SECRET_SYSTEM`、`PAID_BUDGET_INCREASE`、`PROJECT_OUTCOME_PHASE`、`LEGAL_SAFETY_HUMAN`。高技术风险、多方案、协议 revision 或本地可逆变更不属于用户门禁。

未经明确授权，不得删除或覆盖数据、清理 Git、commit/push、发布、发送外部消息、购买服务、提权、安装系统依赖或进入未批准阶段。

## 输出

控制器 `commit` 返回的 `report.visibility` 是用户可见性依据：

- `SILENT`：仅限本轮没有任何状态、进度、队列或外部任务变化；最终回复严格为 `NO_ACTION`。
- `PROGRESS`：任务派发、开始/继续运行、取得新进展、转入 `QUEUED/REVIEW_PENDING` 或其他正常阶段变化；输出一条简短状态摘要，不要求用户回复。
- `ATTENTION`：`BLOCKED/FAILED/LIMIT_REACHED/INVALID_STATE/USER_REQUIRED` 等需要关注的结果；说明根因、当前保护状态和下一步。只有合法 `USER_REQUIRED` 才向用户提问。
- `TERMINAL`：job 接受/归档、阶段完成或项目完成；输出结果和关键证据摘要。

非 `SILENT` 回复统一包含：`[状态] job_id`、`已完成`、`当前`、`下一步`、`需要你处理：是/否`。相同 outcome 且没有 material change 时不得重复通知。`LEASE_HELD/LEASE_RACE_LOST/NO_ACTION_STALE_EVENT` 属于无变化，仍输出 `NO_ACTION`。普通心跳不得查看自动化配置或 automation memory；状态和事务文件才是恢复来源。
