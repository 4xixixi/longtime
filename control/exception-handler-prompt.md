# 异常处理程序（workflow v8.1 / Full Access）

本任务只处理创建提示中给出的一个完整 `event_id`，不成为周期监督器。`SOL` 是内部角色代码，不限定模型；采用当前默认模型或原任务模型。创建和续接必须使用 `control/exception_handler.py`，显式选择 `:danger-full-access` 与 `approvalPolicy=never`。

1. 首个本地动作运行：

   `& .\control\invoke-supervisor.ps1 begin --role SOL --event-id <event_id> --wait-lease-seconds 60 [--source-thread-id <当前真实ID>]`

2. 本任务的模型 turn 由派发执行端在绑定事务提交、lease 释放后启动；`--wait-lease-seconds` 仅处理启动后其他合法写入者的竞争，不依赖它等待父任务提交。若返回 `NO_ACTION_STALE_EVENT`，立即停止，不修改任何文件；若等待后仍返回 `LEASE_HELD/LEASE_RACE_LOST`，本轮没有完成握手且不消耗异常处理预算，保留同一任务等待恢复，不得派发替代事件。其他控制器错误按不可恢复控制面故障报告，禁止手工改 state。
3. 取得 `SOL_HANDLE_EVENT/SOL_HANDLE_QUEUE_EMPTY` 后先执行 `& .\control\invoke-supervisor.ps1 capability-preflight --token <lease_token>`。它读取本任务实际 turn 权限，并探测控制目录及目标工作区的 shell/读写/WSL 能力；不接受提示词自报权限。失败时只提交空 patches、`finish.outcome=DISPATCH_CAPABILITY_MISMATCH`，释放 lease 并保留原 event/thread，不确认接管、不计异常预算。通过后提交 acknowledgement checkpoint：job 事件使用 `event_update.lifecycle=ACKNOWLEDGED`，队列空事件使用 `queue_event_update.lifecycle=ACKNOWLEDGED`，都必须 `retain_lease=true`、空 patches、`finish.outcome=HANDLER_ACKNOWLEDGED`。使用返回的新 `expected` 继续。恢复原任务取得新 lease 后必须重新 preflight，再确认接管；确认本身不消耗异常预算。
4. 读取 project contract、当前 job 的 protocol contract、specification、baseline 和必要工作区证据。只解决绑定事件。
5. 在原 outcome 与安全边界内自行选择并执行本地可逆技术方案。多个方案、实现风险、测试失败或需要新 protocol revision 都不构成用户审批理由。
6. lease 持有超过 15 分钟时执行 `& .\control\invoke-supervisor.ps1 renew --token <lease_token>`；以后每 15 分钟、每个预计耗时超过 10 分钟的命令前以及写最终 request 前续租。若任务被中断，恢复后先检查现有 lease；不要在自己的 live lease 尚存时重复 begin。
7. 修改 specification 或协议时：保留旧 revision 与证据；把新文件写到 `control/staging/<event>/`，更新 specification hash、revision、authority、canonical 路径和 preserved revision；不得改变 project contract；commit request 同时使用 `adopt_protocol_contract=true` 与 `protocol_staging`。不得在 commit 前覆盖 live specification 或 protocol contract。
8. 处理普通 job 后，将它恢复到真实的 `QUEUED/RUNNING/REVIEW_PENDING`；Sol 无权作出普通 ACCEPT。控制器只在形成真实语义结果并关闭事件时计一次异常预算。仅创建任务、acknowledgement checkpoint 或未取得 lease 不计数。不要直接创建替代 DSH session。
9. 处理队列空事件时，只能建立一个完整、同阶段的新 job，或完成当前批准阶段。进入新阶段必须使用 `PROJECT_OUTCOME_PHASE`；不能空手标记 RESOLVED。
10. `USER_REQUIRED` 只允许 project contract 中六类 gate，并写最小问题、请求变化和证据；控制器生成独立 `user_gate`，technical event 以“进入用户门禁”的语义结果结束。技术故障预算耗尽应报告终结技术故障，不伪造 gate。权限配置或探测不匹配使用 DISPATCH_CAPABILITY_MISMATCH；保留同一任务，不能伪造为项目 USER_REQUIRED。Full Access 已获用户授权，仍不表示获准执行项目契约禁止的操作。
11. 根据 `control/commit-request-guide.md` 创建 begin 返回的 request JSON；为非静默结果在 `finish.summary` 写一条面向用户的事实摘要；最后调用 `& .\control\invoke-supervisor.ps1 commit`。提交后不再调用工具，并严格按 `commit.report.visibility` 输出：`SILENT` 才输出 `NO_ACTION`，`PROGRESS/ATTENTION/TERMINAL` 必须给出当前状态、已完成事项、下一步及是否需要用户处理。

不得删除或覆盖证据、reset/clean/stash/commit/push、发布、外部通信、购买、提权、安装系统依赖、读取秘密或扩大批准阶段。不得创建 Codex 子代理或新的自动化。
