# AIOS v0.5 实现与测试报告

日期：2026-08-25

## 已实现

- `EvidenceContract`：从任务中提取产物、网络、系统状态和命令执行证据要求。
- `CapabilityRegistry`：区分 available、composable、missing、needs_authority、forbidden。
- 执行前能力检查：缺能力或缺授权时不调用模型、不重试、不写成功记忆。
- 新状态：`degraded`、`blocked_capability`、`needs_authority`、`retryable_failure`、`terminal_failure`。
- 分层 Verifier：Execution、Artifact、Evidence、Goal 四层独立检查。
- 降级检测：把“无法联网但基于内置知识生成文件”识别为 `degraded`，不再视为成功。
- 模型可见工具收缩为 `read/write/edit/bash`；v0.4 工具仅作执行兼容。
- Docker-only SandboxBroker：任务工作区快照、CPU/内存/进程/超时限制、只读根文件系统、网络默认关闭、禁止 Host Shell 回退。
- 事务发布：验证通过后才把快照中的文件复制回 workspace；失败则丢弃。
- `aiosctl`：tasks、traces、dead-letters、memory、capabilities 的只读 JSON 接口。
- 沙盒内只读状态快照，不向容器挂载生产数据库或 API Key。
- v0.4 自动 Tool Evolution 默认冻结且不向模型暴露动态工具。

## 测试结果

执行：

```powershell
python -m unittest discover -s tests -v
```

结果：40 项测试全部通过。新增验收覆盖：

1. 网络任务在无授权时进入 `needs_authority`，且模型调用次数为 0；
2. Trace/死信任务在无强沙盒时进入 `blocked_capability`；
3. 降级替代回答不能通过目标验证；
4. 普通文件不能伪装成真实网络证据；
5. Docker 不可用时 `bash` 不回退宿主机；
6. 文件先写快照，成功后才提交；
7. 非 completed 状态不写成功记忆；
8. 原 v0.4 回归测试继续通过。
9. 原生 Tool Calling 的最终文本直接结束任务，不再错误转换成已移除的 `echo` 工具。
10. 内部状态快照独立挂载在 `/aios-state`，不会污染用户工作区搜索。
11. Agent 可以观察一次工具失败并在后续轮次恢复；未恢复的失败仍进入重试/死信。
12. 最后一个模型轮次强制 `tool_choice=none`，保证工具预算用尽前仍有最终回答。
13. 沙盒拒绝 `cd /`、`find /` 等容器根目录广泛扫描，只允许任务使用 `/workspace` 与 `/aios-state` 接口。
14. 新尝试进入 `running` 时清空任务表中的旧结果，历史尝试仍保存在 Checkpoint 与 Trace。
15. Docker stdout/stderr 固定以 UTF-8 解码并用替换字符容错，不再触发 Windows GBK reader thread 异常。
16. DSML/XML 等文本化 Tool Call 不能作为最终答案通过 Controller 与 Verifier。
17. 历史中已存在的协议污染成功记忆会在检索阶段被隔离。
18. 显式 `../`、Windows 绝对路径和敏感 POSIX 根路径在模型调用前标记为 forbidden，不再浪费 Agent 轮次或重试。

## 迁移修复

任务 19 暴露了一个 v0.4→v0.5 迁移遗漏：真实文件修改已经在沙盒成功，但控制器把模型最终文本包装成旧 `echo` 动作，随后被 v0.5 权限层拒绝，导致快照丢弃并最终进入死信。现已改为最终文本返回 `done=true, actions=[]`，保持最终回答不经过工具执行。

任务 23 暴露了两个闭环问题：内部 `state.json` 位于工作区导致关键词统计被历史记录污染；同时 `grep` 对零匹配返回退出码 1 后，Runtime 立即中断，模型没有纠错机会。现已将状态改为独立只读挂载，并允许失败结果回传模型后继续规划。Verifier 只在模型观察失败并形成最终计划时将其认定为已恢复。

任务 24 进一步暴露了模型轮次预算问题：模型连续四轮复查“零匹配”，没有留下最终回答轮。现已在最后一个模型调用设置 `tool_choice=none`，强制基于已有观察收束答案；这与工具调用预留共同构成完成预算。

任务 27 暴露了 Windows 子进程解码与模型协议污染问题：Docker 输出被宿主按 GBK 解码后触发 reader thread 异常，模型又在禁用工具的最终轮输出 DSML 工具标记，旧 Verifier 将其误判为自然语言完成。现已固定 UTF-8 容错解码，并在 Controller、Verifier、Memory 三层拒绝或隔离文本化 Tool Call。

任务 31 是路径逃逸边界测试。SecurityKernel 已拒绝 `../config.json`，但旧流程仍让 Agent 继续探索并最终触发 DSML。现已将显式工作区外路径前移到 Capability Preflight，直接以 `blocked_capability` 结束，不调用模型、不重试；可预期的模型协议拒绝改用 WARNING 审计日志而非完整异常堆栈。

## 当前环境限制

- Docker Desktop Engine 已恢复在线（29.6.1）；真实强沙盒烟雾测试通过：`python:3.12-slim` 容器在网络关闭、只读根文件系统和资源限制下返回 `exit_code=0` 与 `sandbox-ok`。
- Codex 测试进程未检测到 `DEEPSEEK_API_KEY`，因此未发起真实 DeepSeek 请求。
- API Key 不应写入仓库、配置、任务内容、Trace 或沙盒；只能通过宿主进程环境变量或未来的 Credential Broker 注入。

## 尚未实现

- 受控网络代理和域名级出口审计；
- Credential Broker；
- 容器磁盘配额和镜像供应链校验；
- Skill/Workflow 注册、回放基准、Canary 和生产晋升；
- 领域级语义验证器。

这些项目属于 v0.5 后续增量；在它们完成前不重新开放自主进化。
