# Job: <title>

## Identity and authority

- Job ID: `REPLACE_ME`
- Owner: user
- DSH mode: `investigate | implement | review`
- Workspace: `/absolute/path/to/workspace`
- Project contract: `control/project-contract.json` / SHA-256 `REPLACE_ME`
- Protocol contract: `jobs/REPLACE_ME/protocol-contract.json`

## Producer / consumer layer identity

- If this job consumes artifacts created by a predecessor, keep the predecessor's producer identity in those artifacts. Do not rewrite producer `job_id`, specification hash, authority hash or protocol revision to resemble the consumer.
- List every producer in this job's protocol-contract `predecessors` and bind producer execution identity to consumer acceptance identity with `templates/layer-binding.json` plus `scripts/verify_layer_binding.py` (or an equivalent independently validated profile).
- Require the producer state to be `ACCEPTED`, or `ARCHIVED` with an existing `accepted_at`; verify producer specification, authority and protocol-contract hashes against state.
- Freeze the consumed evidence closure by path, size and SHA-256 before making consumer-side derived changes. Recompute domain identities from actual results and reject missing, extra or duplicate items.

## Outcome

描述完成后必须存在的可观察结果，而不是活动清单。必须位于 project contract 已批准的 outcome 和阶段内。

## Protected success criteria

逐项引用 project contract 中本 job 不能降低的门禁、配额、阈值或证据规则。若不适用，说明原因；不得留空。

## Technical revision scope

列出 Sol 可以基于证据修改的算法、配置、测试与实验参数，以及建立新 revision 时必须保留的旧证据。

## Inputs

- 必需文件、数据、仓库状态与权威顺序。

## Constraints

- 允许修改的路径。
- 必须保持的兼容性、安全和资源边界。
- 禁止的方法、外部副作用和阶段扩张。
- 时间、API 或自动返工的任务级覆盖值。

## Acceptance criteria

- [ ] 可观察、可验证的完成条件 1。
- [ ] 可观察、可验证的完成条件 2。
- [ ] 当前 protocol revision、authority、specification hash、manifest 与制品路径一致。
- [ ] 前序 producer 身份与当前 consumer 验收身份通过外部 layer binding 一致关联，未篡改 producer manifest。
- [ ] Git diff 可由逐路径 baseline 归属，且没有越权副作用。

## Verification commands

按执行顺序逐行列出安全、可重复且足以证明完成的命令。生成型命令由执行者运行一次；监督器只重复只读验证。

```text
REPLACE_ME
```

## User-only gates

只列 project contract 中真正适用的 USER_ONLY 类别及触发条件。技术路线选择、测试修复和新技术 revision 不属于用户门禁。

## DSH return contract

只返回：完成内容、变更路径、运行的验证、证据位置、失败或未验证事项、建议 `accept | continue same session | escalate`。完整证据留在工作区。
