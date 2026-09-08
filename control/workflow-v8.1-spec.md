# Workflow v8.1 正确性硬化规格

状态：历史设计规格；当前实现与测试以源码为准
适用范围：`control/supervisor_ctl.py`、控制状态 schema、监督提示词、控制面测试与运维文档
基线：workflow v8，单机、单写者、单 active job、非强对抗本地安全模型

## 1. 目标

v8.1 在不改变用户批准的项目 outcome、阶段、成功标准和 USER_ONLY 边界的前提下，修复下列正确性问题：

1. 将存储版本、业务语义版本和异常事件身份分离。
2. 将只读诊断与有副作用的事务恢复分离。
3. 以显式角色规则保护普通验收。
4. 将 accepted job 归档、出队和后继激活合并为一个确定性内部事务。
5. 以用户所有的 project contract 确定性验证项目完成条件。
6. 为上述不变量增加迁移、权限、并发和崩溃恢复测试。

v8.1 的“可证明”仅指控制器能够从受控状态和事务记录中证明：

- 状态转换和角色权限符合规则；
- CAS、事件身份和幂等约束成立；
- 验收记录结构完整且进入了受控事务；
- project/protocol/specification authority 链一致；
- 未完成事务可以按已准备目标快照恢复；
- 项目完成声明满足机器可枚举的控制面闭包。

v8.1 不声称证明：

- LLM 或调用者提交的文字陈述天然真实；
- 验证命令确实由操作系统执行；
- 外部平台的实际状态与本地记录始终一致；
- 本地文件权限持有者无法恶意伪造控制记录。

## 2. 非目标

以下增强不进入 v8.1：

- SQLite、WAL 或 generation/CURRENT 状态库迁移；
- 多 active job 或跨主机控制；
- 端到端外部 fencing；
- 密码学用户授权凭证；
- 完整结构化 progress/error/intent 生命周期；
- TLA+/PlusCal、状态面板和多 session attempt 模型。

出现多并发 reader、多 writer、跨主机、远程控制或更强对抗安全要求时，再评估上述能力。

## 3. 权威与字段所有权

权威层级保持不变：

1. `control/project-contract.json`：用户所有的 outcome、阶段、required job、保护指标和 USER_ONLY 边界。
2. `jobs/<job-id>/protocol-contract.json`：Sol 所有的当前技术 revision、authority、specification hash 和证据位置。
3. `jobs/<job-id>/specification.md`：任务行为、验收命令和允许范围。
4. 工作区配置、manifest、diff 和验证结果。
5. DSH、Luna、Sol 摘要仅作索引或审计陈述。

下列字段完全由控制器拥有，任何角色的普通 patch 都不得直接写入：

- `controller.state_revision`
- `controller.semantic_revision`
- `controller.event_generation`
- `controller.current_event.event_id`
- `controller.current_event.generation`
- `controller.current_event.origin_status`
- `controller.current_event.origin_semantic_revision`
- `controller.current_event.event_key`
- project 级 queue-empty event identity

调用者只能提交状态、决策、结果或错误分类等输入事实；控制器负责计算 revision、事件 identity 和 lifecycle 合法性。

## 4. 三类版本号

### 4.1 `state_revision`

`state_revision` 表示 job state 成功持久化更新的顺序，只用于：

- CAS；
- 事务前后快照校验；
- 调试和事务定位；
- 判断状态文件先后。

任何 job state 内容变化都恰好推进一次 `state_revision`，包括：

- `last_checked_at`、`updated_at`；
- 查询计数；
- Sol 派发、确认、thread ID；
- 其他纯观测或账务字段。

只有 runtime、queue 或 heartbeat 发生变化而 job state 未变化时，不推进 job 的 `state_revision`。

### 4.2 `semantic_revision`

`semantic_revision` 表示领域或用户可见的业务事实发生变化。它不表示所有会影响内部路由的账务变化。

控制器应实现稳定的 `semantic_projection(job)`，对提交前后 canonical projection 求 hash。调用者不得自行声明本次是否属于 semantic change。

建议纳入 projection：

- `status`
- `authorized_specification` 的 authority、revision 和 hash
- DSH session identity 与受控 lifecycle
- continuation route
- `runtime_tracking.progress_fingerprint`
- verification conclusion
- `decision.needs_user`
- `decision.user_only_gate`
- `decision.route`
- `result.accepted_at`
- result evidence identity/hash
- 当前问题的稳定 root-cause signature

不得纳入 projection：

- `updated_at`、`last_checked_at`
- `next_expected_run_at`
- 查询次数
- lease、transaction ID
- `source_thread_id`
- `dispatched_at`、`acknowledged_at`、thread ID
- Sol dispatch/ack lifecycle
- runtime 中的 `last_sol_event`

计算规则：

```text
new_state_revision = old_state_revision + 1

if semantic_hash(before) != semantic_hash(after):
    new_semantic_revision = old_semantic_revision + 1
else:
    new_semantic_revision = old_semantic_revision
```

用户可见性不得再由 `state_revision` 直接触发。纯轮询观测应保持 `SILENT`；真实状态、进展、决策、验收或用户门禁变化才允许产生非静默结果。

### 4.3 `event_generation`

`event_generation` 只表示产生了一个新的、需要独立处理的技术异常事件。它与 `state_revision`、`semantic_revision` 独立。

技术事件 ID：

```text
<job_id>:<event_generation>:<origin_status>
```

建议结构：

```json
{
  "event_generation": 12,
  "current_event": {
    "event_id": "job-x:12:BLOCKED",
    "generation": 12,
    "origin_status": "BLOCKED",
    "origin_semantic_revision": 37,
    "event_key": "sha256:...",
    "root_cause_signature": "DSH_SESSION_NOT_FOUND",
    "lifecycle": "REQUIRED"
  }
}
```

技术事件 lifecycle：

```text
REQUIRED -> DISPATCHED -> ACKNOWLEDGED -> RESOLVED
                                      \-> SUPERSEDED
```

其中 `SUPERSEDED` 只允许在旧事件已经不再代表当前问题、且控制器能够确定新问题 identity 时使用。

以下账务更新只推进 `state_revision`：

- `REQUIRED -> DISPATCHED`
- `DISPATCHED -> ACKNOWLEDGED`
- 写入 `dispatched_at`、`acknowledged_at`、thread ID
- 写入 thread status 查询与恢复时间

`ACKNOWLEDGED` 必须由原 Sol task 在取得 lease 后通过 `retain_lease=true` checkpoint 持久化，确认本身不计 dispatch 预算；只有事件形成真实语义结果并关闭时才计一次预算。只有创建新的 actionable technical event 才推进 `event_generation`。同一 open event 的重复查询、派发或确认不得产生新 generation。

Luna 遇到 `DISPATCHED/ACKNOWLEDGED` 不得永久静默：超过一个检查周期后返回 `SOL_STATUS`，只查询记录的非空 thread ID；仍在运行则记录观察，已经结束但事件仍开放则恢复同一 task，不得创建替代 task。

Sol protocol revision 必须从 `control/staging/` 发布。事务 `prepare.json` 同时保存 specification 与 protocol contract 的精确字节/hash、更新后的 job authority 和 queue hash；roll-forward 先发布文件再发布引用它们的状态，崩溃恢复重复执行同一事务。

事件是否为新的，由控制器根据 canonical `event_key` 和现有 lifecycle 判断。`event_key` 的输入字段必须在实现中固定，至少包括：

- job ID；
- origin status；
-稳定错误码或 gate-independent root-cause signature；
-相关资源 identity；
-产生问题时的 authority/protocol identity。

自由文本不得直接参与事件 identity。

技术事件标记为 `RESOLVED` 时，必须同时存在一个真实语义结果，例如：

- `BLOCKED -> QUEUED/RUNNING/REVIEW_PENDING`
- `FAILED -> QUEUED/RUNNING/BLOCKED`
- 进入合法用户门禁
- 进入终结技术故障

否则拒绝：

```text
EVENT_RESOLUTION_WITHOUT_SEMANTIC_OUTCOME
```

### 4.4 版本推进矩阵

| 变化 | state_revision | semantic_revision | event_generation |
|---|---:|---:|---:|
| 仅更新时间、轮询记录 | +1 | 不变 | 不变 |
| progress fingerprint 变化 | +1 | +1 | 不变 |
| `RUNNING -> REVIEW_PENDING` | +1 | +1 | 不变 |
| 新出现 BLOCKED 根因 | +1 | +1 | +1 |
| Sol 事件派发或确认 | +1 | 不变 | 不变 |
| Sol 修复后恢复正常状态 | +1 | +1 | 不变 |
| 已关闭问题再次出现 | +1 | +1 | +1 |
| `ACCEPTED` 或 `ARCHIVED` | +1 | +1 | 不变 |

项目级 queue-empty 事件继续使用：

```text
project:<queue_revision>:<planning_generation>:QUEUE_EMPTY
```

它的派发、确认和 thread 账务不得推进 `queue_revision` 或 `planning_generation`。

## 5. 用户门禁与技术事件分离

Sol 技术事件与 USER_REQUIRED gate 不共用同一个 lifecycle。

当 Sol 确认问题命中合法用户门禁时：

1. 当前 technical event 以“进入用户门禁”为语义结果结束；
2. `decision.needs_user` 设为 `true`；
3. 创建独立的 `user_gate`；
4. 后续用户授权、拒绝或过期只更新 `user_gate`，不复用 Sol event identity。

建议结构：

```json
{
  "decision": {
    "needs_user": true,
    "user_only_gate": "PROJECT_OUTCOME_PHASE",
    "route": "USER_REQUIRED"
  },
  "user_gate": {
    "gate_id": "gate-...",
    "gate_type": "PROJECT_OUTCOME_PHASE",
    "lifecycle": "REQUIRED",
    "requested_change_sha256": "...",
    "created_at": "..."
  }
}
```

`gate_id` 和 `requested_change_sha256` 由控制器计算。v8.1 中该结构是动作绑定的授权审计收据，不是不可伪造的身份凭证。

项目完成要求既无 open technical event，也无 open user gate。

## 6. ACCEPT 的结构性约束

v8.1 使用 `verification_record`，不使用 `verification_receipt`。它表示 Luna 提交了一份结构完整、声称通过的可审计记录，不证明命令实际由 OS 执行。

### 6.1 新版记录

```json
{
  "verification": {
    "record_version": "V8_1",
    "provenance": "LUNA_ATTESTED",
    "review_run_id": "uuid",
    "specification_sha256": "...",
    "protocol_contract_sha256": "...",
    "started_at": "...",
    "completed_at": "...",
    "result": "PASSED",
    "commands": [
      {
        "command_id": "verify-001",
        "command": "原始命令文本",
        "exit_code": 0,
        "key_output": "简短定位信息",
        "evidence_paths": ["path/to/result.json"],
        "evidence_sha256": ["..."]
      }
    ],
    "unverified_items": []
  }
}
```

### 6.2 普通 ACCEPT 硬规则

普通的新 ACCEPT 必须同时满足：

```text
role == LUNA
planned_action == REVIEW
old_status == REVIEW_PENDING
new_status == ACCEPTED
verification.record_version == V8_1
verification.provenance == LUNA_ATTESTED
verification.result == PASSED
verification.unverified_items == []
commands 非空且每条 exit_code == 0
verification 中的 specification/protocol hash 与当前 authority 一致
project/protocol/specification authority 链有效
```

Sol 提交普通 ACCEPT 时明确拒绝：

```text
ROLE_NOT_AUTHORIZED_FOR_ACCEPTANCE
```

其他建议错误码：

```text
ACCEPTANCE_RECORD_INCOMPLETE
ACCEPTANCE_AUTHORITY_MISMATCH
ACCEPTANCE_UNVERIFIED_ITEMS_REMAIN
ACCEPTANCE_REQUIRES_REVIEW_ACTION
```

### 6.3 Roll-forward 例外

已存在的 `prepare.json` 若包含已经通过提交前校验的 ACCEPTED 目标快照，`recover` 或 `begin` 可以重放该快照。

这是既有事务恢复，不是 recover、Sol 或 Luna 作出新的验收决定。

## 7. 历史 ACCEPTED 兼容

v8.1 不追溯要求迁移前已经提交的 ACCEPTED/ARCHIVED job 满足新版验证记录结构，也不得伪造不存在的验证结果。

迁移前已接受 job：

- 保留原始 verification 内容；
- 写入 `verification.record_version = LEGACY_V8`；
- 记录原接受事务 ID、原 verification hash 和迁移时间；
- 不把空结果、缺少 hash 或旧格式记录改写成 `LUNA_ATTESTED V8_1`；
- 后续完成验证使用 `validate_legacy_acceptance()`。

迁移后新接受 job：

- 必须使用 `record_version = V8_1`；
- 使用 `validate_v81_acceptance()`。

`validate_legacy_acceptance()` 至少检查：

- job 当前为 ACCEPTED/ARCHIVED；
- `result.accepted_at` 非空；
- 原接受状态存在于已提交事务快照；
- 原 verification hash 与迁移记录一致；
- 当时适用的 project/protocol/specification authority 链合法；
- job 不在 queue，且不是 active job，或正处于合法的 accepted handoff 前状态。

当前 schema 使用 `dsh.lifecycle_status`。v8.1 可以继续保留该字段，或一次性迁移为 `dsh.lifecycle`，但必须统一命名并提供显式映射：

```text
completed/cancelled/failed/lost -> terminal
starting/running              -> active
null/unknown                  -> unknown
```

不能因为字段改名把既有 completed session 误判为 UNKNOWN。

## 8. `check` 与 `recover`

### 8.1 `check` 是绝对只读命令

命令：

```text
py -3 control/supervisor_ctl.py check
```

兼容别名：

```text
verify -> check
```

`check` 不得：

- 创建、移动或删除 lease；
- roll-forward；
- 补写 commit 或 resolved intent；
- 写 heartbeat；
- 更新时间戳；
- 创建控制目录临时文件。

### 8.2 Committed head

v8.1 必须新增“最新有效 committed transaction”选择器。它不能直接复用“最新包含 prepare 的事务目录”作为 committed head。

有效 committed head 至少要求：

- `prepare.json` 和 `commit.json` 都存在且可解析；
- transaction ID 一致；
- commit 中的 document hashes 与 prepare documents 一致；
- 该事务是排序规则下最新的有效 committed transaction。

有 prepare、无 commit 的更新目录属于 pending transaction，不是 committed head。

### 8.3 双读屏障

屏障至少包含：

```json
{
  "lease": {
    "state": "ABSENT | LIVE | EXPIRED",
    "sha256": "..."
  },
  "committed_head": {
    "transaction_id": "...",
    "commit_sha256": "..."
  },
  "pending_transactions_sha256": "..."
}
```

算法：

```text
barrier_1 = read_transaction_barrier()

if lease is LIVE:
    return BUSY
if lease is EXPIRED:
    return STALE_LEASE_RECOVERY_REQUIRED

snapshot = read controlled documents
candidate = validate snapshot against committed head

barrier_2 = read_transaction_barrier()

if lease is LIVE:
    return BUSY
if lease is EXPIRED:
    return STALE_LEASE_RECOVERY_REQUIRED
if barrier_1 != barrier_2:
    return RETRY_CONCURRENT_CHANGE
if pending set is non-empty:
    return RECOVERY_REQUIRED

return candidate
```

只有屏障前后一致、无 live/expired lease、无 pending transaction 时，才允许返回 `STATE_DRIFT`。

返回状态：

```text
OK
BUSY
RETRY_CONCURRENT_CHANGE
STALE_LEASE_RECOVERY_REQUIRED
RECOVERY_REQUIRED
STATE_DRIFT
CONTRACT_MISMATCH
SCHEMA_ERROR
```

`next_action_preview` 只作诊断，必须标记 `advisory_only = true`。它不能用于 commit，也不能替代后续 `begin` 返回的 lease、CAS 和 planned action。

### 8.4 `recover` 是有副作用命令

命令：

```text
py -3 control/supervisor_ctl.py recover
```

它可以：

- 在没有其他 live writer 时取得恢复 lease；
- 移动 expired lease；
- 重查未完成事务；
- roll-forward；
- 写 intent resolution 和 commit；
- 释放自己的 lease；
- 返回恢复报告。

`begin` 作为正式执行入口可以继续 recover-first，因为它不是只读命令。

## 9. `HANDOFF_ACCEPTED_JOB`

原 `FINALIZE_ACCEPTED -> ACTIVATE_JOB` 合并为控制器拥有的：

```text
HANDOFF_ACCEPTED_JOB
```

### 9.1 前置条件

- `active_job_id != null`
- active job status 为 `ACCEPTED`
- planned action 为 `HANDOFF_ACCEPTED_JOB`
- queue 中当前 job 的存在/位置符合既定归档策略

### 9.2 后继就绪校验

若移除当前 job 后 queue 仍非空，控制器必须在构造目标快照前验证新队首：

- status 为 `QUEUED`；
- 它是 project contract 顺序中的合法后继；
- 所有声明 predecessor 均已 ACCEPTED/ARCHIVED；
- project/protocol/specification hash 有效；
- 不存在非法既有 session 或 continuation 冲突；
- queue entry 与 job state 的 specification/protocol hash 一致。

任一条件失败时，整个 handoff 不提交，不得先归档旧 job。返回明确的控制面错误或安全阻塞结果。

### 9.3 单事务变化

控制器在一个事务内：

1. 将当前 active job 变为 `ARCHIVED`；
2. 从 queue 移除当前 job；
3. queue 非空时把 `active_job_id` 设置为已验证的新队首；
4. queue 为空时把 `active_job_id` 设置为 null，并运行项目完成验证；
5. 更新相关 runtime、queue 和 job revision。

Luna 只确认完成 planned action，不提交任意多 job patch；目标快照由控制器确定性生成。

本轮禁止调用：

- `DSH_START`
- `DSH_CONTINUE`
- `DSH_STATUS`
- `SOL_ESCALATE`
- `SOL_QUEUE_EMPTY`

一轮可以包含多个确定性、无外部副作用且不可分割的内部状态转换，但最多执行一个不可重放的外部副作用。

## 10. 项目完成证明

项目 required job 集合只来自用户所有的：

```text
project-contract.json.required_queue_order
```

Sol-owned protocol 可以声明证据、canonical result、predecessor 和技术 revision，但不得增加、删除、替代 project contract 中的 required job。

### 10.1 `validate_project_completion()`

只有下列条件全部成立时才允许 `project_status = COMPLETED`：

- 当前 project status 为 `ACTIVE`；
- `active_job_id == null`；
- queue 为空；
- `required_queue_order` 中每个 job 都存在；
- 每个 required job 都具有合法 ACCEPTED/ARCHIVED 记录；
- 不存在 unresolved intent；
- 不存在 open technical event；
- 不存在 open user gate；
- 控制记录中不存在 active 或 unknown external session；
- project/protocol/specification authority 链全部有效；
- 不存在未处理的 required BLOCKED/FAILED/REVIEW_PENDING job。

“不存在活动外部 session”只表示控制记录中没有 `STARTING/RUNNING/UNKNOWN` lifecycle，不表示控制器独立证明了外部平台现实状态。

若 session identity 存在但没有明确终态观察，返回：

```text
EXTERNAL_SESSION_STATE_UNVERIFIED
```

### 10.2 队列空路由

```text
queue empty
    |
    v
validate_project_completion()
    |
    +-- PASS -> COMPLETED
    |
    +-- 批准范围内仍缺 required job
    |      -> SOL_QUEUE_EMPTY，并附机器化 gap list
    |
    +-- 需要改变 required job 或进入新阶段
           -> USER_REQUIRED / PROJECT_OUTCOME_PHASE
```

Sol 可以提出完成候选，但 commit 仅在 `validate_project_completion()` 通过时接受完成状态。

## 11. Schema 迁移

迁移必须通过受控 `USER/MIGRATION` 事务执行，不得直接编辑 runtime、queue 或 job state。

### 11.1 迁移前置条件

- 不存在 unresolved intent；
- 不存在 REQUIRED/DISPATCHED/ACKNOWLEDGED technical event；
- 不存在其他 live lease；迁移命令自己取得的 lease 不计；
- begin/recover-first 完成后不存在 pending transaction。

失败错误码：

```text
MIGRATION_BLOCKED_UNRESOLVED_INTENT
MIGRATION_BLOCKED_OPEN_EVENT
MIGRATION_BLOCKED_LIVE_LEASE
MIGRATION_BLOCKED_PENDING_TRANSACTION
```

### 11.2 Revision 基线

对现有 job：

```text
semantic_revision = state_revision
event_generation  = state_revision
```

这只建立迁移后的单调基线，不声称还原历史语义或事件次数。

迁移不得：

- 重写历史 event ID；
- 改写 resolved intent identity；
- 重新绑定已经派发的事件；
- 伪造新版 verification record；
- 因 `lifecycle_status` 字段改名把终态 session 变成 UNKNOWN。

## 12. 冻结不变量

| ID | 不变量 |
|---|---|
| `INV-REV-001` | 每次 job state 持久化内容变化恰好推进一次 state revision。 |
| `INV-REV-002` | semantic revision 只能由控制器比较 semantic projection 决定。 |
| `INV-EVT-001` | Sol 派发、确认和 thread 账务不得改变 current event ID。 |
| `INV-EVT-002` | 只有新的 actionable technical event 才推进 event generation。 |
| `INV-EVT-003` | 同一 open event 不得因重复派发产生新 generation。 |
| `INV-EVT-004` | event identity 字段只能由控制器生成。 |
| `INV-EVT-005` | DISPATCHED/ACKNOWLEDGED 事件必须绑定非空 thread ID；非空 source thread 不匹配时拒绝接管。 |
| `INV-EVT-006` | ACKNOWLEDGED checkpoint 可恢复且不消耗异常预算；预算只在语义解决时计数。 |
| `INV-TXN-001` | Sol protocol revision 的文件与 authority/queue hash 必须在同一可恢复事务发布。 |
| `INV-ACC-001` | 只有 Luna 在 REVIEW planned action 下可以作出新的普通 ACCEPT。 |
| `INV-ACC-002` | v8.1 ACCEPT 证明记录结构完整，不证明命令实际执行。 |
| `INV-ACC-003` | Roll-forward ACCEPTED 是事务恢复，不是新的验收决定。 |
| `INV-ACC-004` | 历史 ACCEPTED 按原 schema 验证，不伪造新版记录。 |
| `INV-CMP-001` | 项目完成所需 job 集合只能来自 project contract。 |
| `INV-CMP-002` | Sol-owned protocol 不得替代 required job。 |
| `INV-CMP-003` | 队列空只触发完成验证，不自动等于完成。 |
| `INV-CMP-004` | 完成时不得存在 open technical event 或 user gate。 |
| `INV-CHK-001` | check 在应用层绝对无写入。 |
| `INV-CHK-002` | 只有稳定双读屏障下才能报告 STATE_DRIFT。 |
| `INV-CHK-003` | expired lease 必须要求恢复，不能视为无 lease。 |
| `INV-HOF-001` | accepted job 归档、出队和后继激活在同一事务完成。 |
| `INV-HOF-002` | handoff 不调用任何外部系统。 |
| `INV-HOF-003` | 后继未通过就绪校验时 handoff 整体不提交。 |
| `INV-MIG-001` | unresolved intent、open event 或其他 live writer 存在时不得迁移。 |

## 13. 开发 Issue

| Issue | 内容 | 完成标准 |
|---|---|---|
| `V81-01` | 三类 revision 和 technical event 模型 | 纯观测只推进 state revision；dispatch 不改变 event ID。 |
| `V81-02` | check/recover 和 committed-head 双读屏障 | check 零写入；并发写不误报 drift；recover 可恢复 pending transaction。 |
| `V81-03` | ACCEPT 角色约束和 verification record | Sol ACCEPT 明确拒绝；Luna 新验收记录不完整时拒绝；历史接受可兼容。 |
| `V81-04` | HANDOFF_ACCEPTED_JOB | 归档、出队、校验并激活后继一次提交；不启动 DSH。 |
| `V81-05` | validate_project_completion | required job、intent、事件、门禁和 session 任一不闭合时拒绝完成。 |
| `V81-06` | Schema migration、测试和文档同步 | 迁移不破坏旧事件、验收记录和 session 终态；策略与测试一致。 |

建议按 `V81-01 -> V81-03 -> V81-04 -> V81-05 -> V81-02 -> V81-06` 实现；迁移脚本最后执行，但兼容测试应在各 Issue 开发期间同步建立。

## 14. 最小测试集

### Revision 与事件

- `test_observation_patch_only_increments_state_revision`
- `test_semantic_change_increments_semantic_revision`
- `test_dispatch_bookkeeping_preserves_event_id`
- `test_same_open_event_does_not_increment_generation`
- `test_reoccurring_closed_event_increments_generation`
- `test_sol_rejects_a_different_visible_thread`
- `test_dispatch_requires_a_visible_thread_id`
- `test_sol_status_observation_throttles_rechecks`
- `test_staged_protocol_adoption_updates_files_state_and_queue`
- `test_prepared_transaction_rolls_forward_file_updates_after_crash`
- `test_event_identity_fields_are_controller_owned`

### ACCEPT 与历史兼容

- `test_sol_cannot_transition_to_accepted`
- `test_luna_accept_requires_review_action`
- `test_luna_accept_requires_complete_passing_verification_record`
- `test_roll_forward_replays_prepared_accepted_snapshot`
- `test_legacy_accepted_record_is_not_rewritten_as_v81_attestation`
- `test_legacy_required_job_remains_valid_after_migration`

### Check 与 recover

- `test_check_is_application_level_read_only`
- `test_check_returns_busy_if_lease_appears_between_barriers`
- `test_check_returns_retry_if_commit_head_changes`
- `test_check_returns_stale_lease_recovery_required`
- `test_state_drift_requires_stable_double_barrier`
- `test_pending_transaction_is_not_committed_head`
- `test_recover_rolls_forward_pending_transaction`

### Handoff

- `test_handoff_archives_and_activates_successor_atomically`
- `test_handoff_rejects_unready_successor_without_partial_commit`
- `test_handoff_does_not_start_dsh`
- `test_handoff_last_job_runs_completion_validation`
- `test_handoff_recovers_from_each_file_replacement_crash_point`

### 项目完成

- `test_completion_uses_project_contract_required_queue_order`
- `test_protocol_cannot_claim_substitute_required_job`
- `test_completion_rejects_missing_required_job`
- `test_completion_rejects_unresolved_intent`
- `test_completion_rejects_open_event`
- `test_completion_rejects_open_user_gate`
- `test_completion_rejects_active_or_unknown_controlled_session`
- `test_completion_accepts_full_control_plane_closure`

### 迁移

- `test_migration_refuses_unresolved_intent`
- `test_migration_refuses_open_event`
- `test_migration_refuses_other_live_lease`
- `test_migration_does_not_rewrite_historical_event_ids`
- `test_migration_preserves_legacy_verification_hash`
- `test_migration_maps_completed_lifecycle_status_to_terminal`

## 15. 交付与回滚要求

实施提交前必须保留：

- workflow v8 控制文件的完整 hash 清单；
- 当前 job state、queue、runtime 和 project contract 的事务快照；
- 所有历史 verification 原始内容与 hash；
- schema migration 的 dry-run 报告；
- 新旧 schema 的 round-trip/兼容测试结果。

若迁移验证失败：

- 不得部分采用 v8.1 schema；
- 不得直接编辑状态回退；
- 由同一个受控 MIGRATION 事务恢复到准备前快照；
- 保留失败迁移事务和诊断证据。

## 16. 一句话语义

```text
state_revision      = 存储/CAS 顺序
semantic_revision   = 领域与用户可见业务事实版本
event_generation    = 独立技术异常事件身份
verification_record = Luna 提交的可审计验收陈述
project contract    = 项目完成所需 job 的唯一来源
check               = 并发安全的只读诊断
recover             = 所有事务恢复写入的入口
handoff             = 无外部副作用的原子内部交接
```
