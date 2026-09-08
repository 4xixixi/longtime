# 手动首轮 / 定时监督任务提示

在 `<CONTROL_ROOT>` 运行一轮长期任务监督。先阅读该目录的 AGENTS.md 和 control/supervisor-prompt.md。不要自行修改 runtime、queue 或 job state。

使用已连接的常驻 DSH MCP 服务。首个控制动作是 begin --role LUNA；执行返回的唯一规划动作。外部调用前 prepare-intent，把 correlation_id 写入任务说明，保存真实 session/thread 标识。调用返回 running 后提交并结束，不连续轮询。最后使用 begin 返回的 expected 和 request_path 完成 commit。

REVIEW 轮独立执行已批准规格中的验证命令，不能仅依赖 DSH 自报通过。异常只能绑定控制器给出的原 event；遵守异常处理权限和恢复协议，不自行创建替代事件或替代 session。没有明确配置并授权异常任务适配器时，保留阻塞证据并报告具体缺失条件。

按控制器 visibility 报告真实进展；无变化保持安静。不得擅自扩大项目目标、预算或用户专属边界。
