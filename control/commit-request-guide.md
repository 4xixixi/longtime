# Commit request 速查

状态只能通过 `& .\control\invoke-supervisor.ps1 commit` 写入。request 顶层字段：

```json
{
  "reason": "短且可审计的原因",
  "job_id": "当前 job；无 active job 时可省略",
  "expected": {
    "runtime_updated_at": "原样复制 begin.expected",
    "queue_revision": 0,
    "job_state_revision": 0,
    "job_status": "QUEUED"
  },
  "patches": {
    "runtime": {},
    "queue": {},
    "job": {}
  },
  "resolve_intent": {
    "intent_id": "仅有外部 intent 时填写",
    "outcome": "真实外部结果",
    "external_id": "session/thread id 或 null",
    "summary": "短摘要"
  },
  "finish": {
    "outcome": "本轮结果代码",
    "summary": "非静默结果的简短用户可见摘要；纯 NO_ACTION 可省略"
  }
}
```

控制器自行写时间、递增 `state_revision`、比较 semantic projection、生成 technical event identity、累计 RUNNING 时间并更新真实 progress；不要在 patch 中写 `state_revision`、`semantic_revision`、`event_generation`、`current_event`、`user_gate` 或 event identity 字段。

## 常用 job patch

DSH start/continue 已接受：

```json
{"status":"RUNNING","dsh":{"session_id":"原 session 或新建 session","lifecycle_status":"running","continuation_required":false,"last_checked_at":"工具返回时间","last_result_summary":"短摘要"}}
```

状态查询完成：

```json
{"status":"REVIEW_PENDING","dsh":{"lifecycle_status":"completed","last_checked_at":"工具返回时间","last_result_summary":"短摘要"}}
```

查询仍运行：

```json
{"status":"RUNNING","dsh":{"lifecycle_status":"running","last_checked_at":"工具返回时间","last_result_summary":"只有确有新进展才替换摘要"}}
```

明确返工：

```json
{"status":"QUEUED","dsh":{"lifecycle_status":"completed","auto_rework_count":1,"continuation_required":true,"continuation_instruction":"精确、单次、同 session 指令"},"decision":{"needs_user":false,"route":"CONTINUE_SAME_DSH_SESSION","reason":"证据化原因"}}
```

验收通过：

```json
{"status":"ACCEPTED","verification":{"record_version":"V8_1","provenance":"LUNA_ATTESTED","review_run_id":"uuid","specification_sha256":"...","protocol_contract_sha256":"...","started_at":"...","completed_at":"...","result":"PASSED","commands":[{"command_id":"verify-001","command":"原始命令","exit_code":0,"key_output":"短定位信息","evidence_paths":["..."],"evidence_sha256":["..."]}],"unverified_items":[]},"result":{"accepted_at":"验收完成时间","summary":"证据摘要","changed_files":["..."]},"decision":{"needs_user":false,"route":"ACCEPT","reason":"全部标准通过"}}
```

只有 Luna 在 `planned_action=REVIEW` 时可提交上述普通 ACCEPT。Sol 不能 ACCEPT；历史接受记录只能由迁移器标记为 `LEGACY_V8`。

异常升级派发后保持原状态，通过 request 顶层事实输入更新 lifecycle；event ID 仍由控制器拥有：

```json
{"event_update":{"lifecycle":"DISPATCHED","thread_id":"新 Sol thread","dispatched_at":"真实时间"},"patches":{"job":{}}}
```

`thread_id` 必须是 `py -3 control/exception_handler.py dispatch --token <lease_token>` 返回的非空可见任务 ID。启用 Full Access 门禁后，派发和恢复必须原样附带该入口返回的顶层 `dispatch_receipt={path,sha256}`，同时以本次 prepare-intent 的 ID 和原 thread ID 提交 resolve_intent；新版 PREPARED 回执证明原 thread 已分配/装载且平台实际配置为 Full Access + never，此时 turn 尚未启动。控制器在同一事务绑定回执、event/thread 并关闭 intent；后台执行端检查稳定的完整提交记录与 lease 已释放后才启动 turn，并另外保存 started 回执。历史 started 回执仅用于兼容恢复。不要把 PREPARED 或对话 completed 状态描述为业务目标完成。开放事件由后续 Luna 路由为 `SOL_STATUS`；查询仍在运行时可提交：

恢复 `SOL_ESCALATE` 的未决 intent 时，若已从原创建记录核实 correlation、event 和原 thread，可在 `RECOVER_INTENT` 下由 LUNA 或用户授权的 USER/MIGRATION 提交上述 `event_update`，并同时提交 `resolve_intent`。其中 intent ID 必须匹配本轮 intent，external ID 必须等于原 thread ID，summary 必须记录恢复证据。控制器在同一事务中绑定原任务并关闭 intent，不改变 event identity 或消耗异常预算。普通任务列表没有匹配项不代表任务未创建；用户维护可检查归档任务和原派发记录，恢复原任务后再唤醒。

```json
{"event_update":{"thread_status":"active","status_checked_at":"真实时间"},"patches":{"job":{}},"finish":{"outcome":"SOL_RUNNING"}}
```

若查询发现原任务已结束但事件仍开放，只恢复该任务一次，并额外记录 `recovery_requested_at` 与 `recovery_outcome`。新的 PREPARED 回执随该事务替换事件中的 dispatch_receipt，后台执行端据此在提交释放后续接原对话；若等待进程中断，status 重启相同回执的等待进程，不新建对话。平台明确报告原对话已归档时，受控入口先解除原对话归档，再用同一 ID 装载；仅装载原 thread 的失败可安全重试，未知的新建任务结果仍禁止重建。启动请求结果不确定时保留启动声明，禁止自动重放。控制器在一个检查周期内抑制重复查询和恢复。

异常处理程序取得事件 lease 后先运行 `capability-preflight --token <lease_token>`。控制器只接受本次 run、原 event/thread 对应的实际权限及工作区探测回执；失败时提交空 patches 与 `finish.outcome=DISPATCH_CAPABILITY_MISMATCH`，保留事件和预算。通过后持久化接管 checkpoint，并保留同一 lease：

```json
{
  "event_update": {"lifecycle": "ACKNOWLEDGED"},
  "retain_lease": true,
  "patches": {"job": {}},
  "finish": {"outcome": "SOL_ACKNOWLEDGED"}
}
```

后续最终 request 必须复制该 checkpoint 返回的新 `expected`。确认本身不计异常预算；形成 `QUEUED/RUNNING/REVIEW_PENDING/USER_REQUIRED/TERMINAL_TECHNICAL_FAILURE` 等真实语义结果并关闭事件时才计数。

队列空事件确认接管使用 `queue_event_update={"lifecycle":"ACKNOWLEDGED"}` 与 `retain_lease=true`，在预检通过后提交，确认不推进规划代次。队列空异常处理派发使用顶层 `queue_event_update={"lifecycle":"DISPATCHED","thread_id":"..."}`；project event ID 由控制器写入，且派发账务不推进 queue revision 或 planning generation。Sol 解决时必须增加新 job 或把 `patches.runtime.project_status` 设为 `COMPLETED`，控制器随后关闭该 queue-empty event。

接受后的 `HANDOFF_ACCEPTED_JOB` 使用空 patches。控制器会先验证队首位置、project 顺序、predecessor、authority、queue hash 与 session/continuation，再在一个事务中归档、出队并激活后继；最后一个 job 同时运行 project completion 验证。调用者不得手工 patch 多个 job，也不得调用 DSH。

Sol 确认合法 USER_ONLY gate 时，只提交 `decision.needs_user/user_only_gate/requested_change` 等事实。控制器生成独立 `user_gate` 收据并关闭绑定 technical event。后续用户处理使用顶层 `user_gate_update`，必须绑定原 `gate_id`。

`NO_ACTION` 也要提交空 patches，让控制器记录健康心跳并释放 lease。纯观测 job patch 会推进 state revision，但 semantic revision 不变，用户可见性仍保持静默：

```json
{"reason":"NO_ACTION","patches":{},"finish":{"outcome":"NO_ACTION"}}
```

没有外部 intent 时省略 `resolve_intent`。外部结果不确定时不要伪造 resolved marker；保留 intent，提交阻塞事实或让下一轮优先恢复。

## 用户可见性

`commit` 返回 `report.visibility`，调用者不得自行降级：

- `SILENT`：没有 material change，最终只输出 `NO_ACTION`；
- `PROGRESS`：正常派发、运行进展或阶段转换，输出简短进度；
- `ATTENTION`：阻塞、失败、限制或用户门禁，输出根因与下一步；
- `TERMINAL`：接受、归档或项目/阶段完成，输出结果摘要。

`report` 同时提供 `status_before`、`status_after`、`next_action`、`needs_user` 与 `material_change`。非静默请求应填写 `finish.summary`，让最终回复直接使用可审计事实。只有 `report.needs_user=true` 才要求用户作答。

## Sol 协议 revision 的事务发布

Sol 不得先覆盖 live `specification.md` 或 `protocol-contract.json`。新 revision 写入 `control/staging/<event>/` 后，在 request 中使用：

```json
{
  "adopt_protocol_contract": true,
  "protocol_staging": {
    "specification": "control/staging/<event>/specification.md",
    "protocol_contract": "control/staging/<event>/protocol-contract.json"
  }
}
```

控制器验证 job、project authority、连续 revision、绑定 event authority 和旧 revision 副本，然后把两个文件的精确字节、job authority 与 queue hash 写入同一事务。若发布中断，`recover` 会完成全部文件更新后再写 commit 标记。

## 用户授权的整批规划迁移

只有用户明确改变 project outcome/phase 时，`USER` 或 `MIGRATION` lease 才可在一个事务中使用：

- 顶层 `project_contract`：完整的新用户契约；必须在 `supersedes_contract_sha256` 中记录当前契约 hash，并在 `authorization.basis` 记录用户授权依据；
- 顶层 `new_jobs`：按队列顺序给出 `{job_id, state}`；每个 job 的 `specification.md` 与 `protocol-contract.json` 必须先准备好；
- `patches.queue.jobs`：job ID 顺序必须与 `new_jobs` 完全一致；
- `patches.runtime.project_status`：需要后续心跳继续时设为 `ACTIVE`。

控制器会把新 project contract、全部 job state、runtime 和 queue 放进同一可恢复事务，自动写入 project/protocol hash、job revision、queue revision 与 planning generation。Luna 和普通 Sol 不能使用该入口。

用户明确授权对现有排队 job 做批量协议 revision 时，`USER` 或 `MIGRATION` lease 可使用顶层 `protocol_migrations`：

```json
{"protocol_migrations":[{"job_id":"queued-job-a"},{"job_id":"queued-job-b"}]}
```

每个目标必须仍为 `QUEUED`、在队列中且新合同 revision 恰好递增 1；旧 `specification.md` 与 `protocol-contract.json` 必须按原哈希保存在 `history/protocol-revision-N/`。控制器会验证历史副本、新规格/合同/project authority，再原子更新 job authority、state revision、queue specification/contract hashes、queue revision 与 planning generation。不得把此入口用于运行中或已验收 job。

若旧版本遗留的 queue authority hash 已与 job state 和 live 文件不一致，但没有新的协议 revision，用户授权的 `USER` 或 `MIGRATION` 维护事务可使用：

```json
{"reconcile_queue_authority":true,"patches":{},"finish":{"outcome":"QUEUE_AUTHORITY_RECONCILED"}}
```

控制器会逐项验证 live specification、protocol contract 与 job state 的 authority，再只同步 queue hash 并推进 queue revision；任何 live 文件或 state 本身不一致时都会拒绝，不能用它掩盖协议漂移。

## 诊断、恢复与 schema 迁移

### 用户明确授权的暂停流程恢复

`USER/MIGRATION` 可提交 `maintenance_resolution`，用于用户直接要求恢复、原 Sol 已接管且修复证据可复核的暂停任务。普通自动 Sol/Luna 不得使用此入口。该维护不新增自动异常处理次数，不清零历史计数，也不提高预算；后续自动升级仍受原上限约束。

- 必填 `event_id`、原 `thread_id`、`authorization_basis`（本次用户指令）和 `evidence` 数组；每项为 `{"path":"control/diagnostics/...","sha256":"..."}`，控制器校验文件与哈希。
- 仅允许 `PAUSED` 项目、`ACKNOWLEDGED` 原事件、已有 session 且无未授权用户门禁。
- `patches.runtime` 必须恰为 `{"project_status":"ACTIVE"}`；job 只能修改 status/dsh/decision，恢复 `QUEUED`、`continuation_required=true` 和明确的原 session 续接指令；不能替换 session、修改预算、协议或宣告验收。
- 控制器在同一事务内关闭原事件为 `USER_MAINTENANCE_REQUEUED`，保留事件身份及维护收据，并恢复原会话续接路由。提交后运行只读 `check` 验证交接。

- `& .\control\invoke-supervisor.ps1 check`（`verify` 为兼容别名）绝对只读；其 `next_action_preview.advisory_only=true`，不能用于 commit。
- `& .\control\invoke-supervisor.ps1 recover` 才能移动 expired lease、roll-forward pending transaction 和补写 commit/intent resolution。
- v8 到 v8.1 先运行 `migrate-v81 --dry-run`；实际 `migrate-v81` 自取 MIGRATION lease，并在 unresolved intent、open event、其他 live lease 或 pending transaction 存在时拒绝。


## 用户要求立即恢复原异常任务

用户明确确认未绑定 thread 的 Luna 已结束、并要求立即恢复时，可使用 `recover --abandoned-run-id <精确旧run_id> --user-authorization <本次授权原文>`。仅接受路由截止时间已过且无 external intent 的未绑定 Luna，保存完整租约与授权回执后撤销旧 token。普通心跳不得使用该入口或自行生成用户授权。恢复后以 USER begin/commit 提交实际状态；该入口本身不修改 runtime/queue/job，也不能证明未知 owner 已停止。

用户明确要求立即恢复时，USER/MIGRATION 可提交空 patches 和顶层 `handler_retry={"event_id":"完整原事件","thread_id":"原对话ID","authorization_basis":"当前用户恢复指令"}`。控制器验证 ACTIVE 项目、开放的已派发/确认事件、原 thread 与无用户门禁，然后记录一次性 user_retry_requested_at。原 status_checked_at 和历史证据保留，下一轮可以提前执行一次 SOL_STATUS；该轮查询写入新 status_checked_at 后恢复原检查间隔。普通 Luna/Sol 不能自行授权，不改变事件身份、预算、DSH session、项目契约或任务状态。
