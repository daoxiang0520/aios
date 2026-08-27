# Self-Evolving AIOS v0.6

Current release: **v0.6.5**. It adds immutable Task Capsules, content-addressed
workspace and Skill snapshots, isolated baseline/candidate worlds, replicated
counterfactual execution, multidimensional objective comparison, and an optional
order-reversed semantic judge. Historical replay remains available as weaker evidence.

> v0.6 将进化面从“增加专用 Tool Schema”迁移为“学习可执行、可测试、可版本化的 Skill”。模型的原生工具面仍固定为 `read / write / edit / bash`。

当前分层为：`Root Capability → Primitive Tool → Skill → Workflow → Harness`。v0.6 实现 Skill 层；Workflow 和 Harness 的自主进化仍是后续版本。

## 已实现

- SQLite 持久状态：事件、目标、追踪和通用状态表；
- 事件驱动 Runtime：支持单周期和常驻轮询；
- Goal Manager 与确定性 Intent Arbiter；
- Mock 与 OpenAI-compatible 两种控制器；
- 模型只可见四个通用工具：`read`、`write`、`edit`、`bash`；
- CapabilityRegistry 与执行前能力检查；
- EvidenceContract 与执行/产物/证据/目标四层 Verifier；
- `completed`、`degraded`、`blocked_capability`、`needs_authority` 等任务语义；
- Docker-only SandboxBroker：事务工作区、资源限制、禁用宿主 Shell 回退；
- `aiosctl` 只读状态接口与沙盒内状态快照；
- 仅 `completed` 任务写入 Episodic Memory；
- 能力白名单、工作区路径隔离、读写大小限制和覆盖保护；
- 每周期计划、动作、结果、评价与错误追踪；
- 单周期模型/工具预算；
- 崩溃后 `processing` 事件恢复；
- CLI 和自动化测试。
- 持久任务、结果收件箱和阶段 Checkpoint；
- 自动重试、人工重试与死信队列；
- Working / Episodic / Semantic / Procedural 四类记忆；
- 关键词相关性检索与受预算的 Context Composer；
- 确定性 Verifier；
- Trace 聚合和失败模式诊断；
- Prompt / 行动上限 / 记忆预算的人工 Harness Candidate；
- 重复失败自动触发诊断、声明式工具候选生成、隔离测试、自动启用与回滚备份；
- 自动生成 `query_tasks`、`query_traces`、`query_dead_letters`、`search_files` 等受限能力；
- 模型可见的剩余预算、完成调用预留与显式 `BudgetDeferred` 反馈；
- 观察结果回传模型，多轮读取后继续生成目标产物；
- 目标级完成验证，防止把 `list_files` 成功误判为整个任务完成；
- 精简结果列表与 `result answer` 最终回答命令。
- 原生 Tool Calling、函数Schema和 `tool_call_id` 结果回传；
- 普通模型文本直接作为最终回答，不再要求最终回答使用JSON；
- 所有原生工具调用仍经过白名单、路径隔离和预算检查。
- 版本化 Skill Registry：`candidate / active / history / deprecated / reports`；
- Skill Manifest：名称、SemVer、输入 Schema、测试和 `required_capabilities`；
- Docker-only Skill Benchmark，候选代码绝不在宿主 Python 中执行；
- 需人工确认的 Promote / Rollback / Deprecated 生命周期；
- Agent 可在 `workspace/skill_candidates/`产生候选 Skill，默认只注册、不自动执行或晋升；
- 沙盒只读挂载 `skills/runtime`，不暴露候选、历史、报告和废弃代码；
- 内置 `workspace_search`、`state_query`、`trace_failure_analyzer` 三个可复用 Skill。
- Task Capsule：保存任务前 workspace、Skill、Capability、Harness、Model 与 Sandbox 身份；
- 内容寻址对象存储：多个 Capsule 共享文件对象，不为每次实验复制完整项目；
- Experiment Orchestrator：从同一 Capsule 分叉隔离世界，仅允许 Skill 作为 v0.6.5 实验变量；
- Baseline/Candidate 默认各执行 3 次，记录完成率、Verifier、模型调用、Token、延迟、安全与产物证据；
- Counterfactual Evaluator 输出完整指标向量和 `REJECTED / INSUFFICIENT_EVIDENCE / NEEDS_REVIEW / PROMOTABLE`；
- 可选盲语义 Judge 会交换 A/B 顺序检测位置偏差，且不能覆盖安全和 Verifier 硬证据。

## Skill 使用与进化

模型通过稳定的 `bash` 原语调用 Skill，不会将 Skill 注入 Tool Schema：

```bash
python /skills/skill.py list
python /skills/skill.py run workspace_search --input-json '{"query":"hello"}'
python /skills/skill.py run trace_failure_analyzer --input-json '{"limit":100}'
```

宿主管理命令：

```powershell
python -m aios --config config.json skill list
python -m aios --config config.json skill candidates
python -m aios --config config.json skill propose --manifest manifest.json --source skill.py
python -m aios --config config.json skill benchmark <candidate_id>
python -m aios --config config.json skill promote <candidate_id> --approve
python -m aios --config config.json skill versions <name>
python -m aios --config config.json skill rollback <name> --approve
python -m aios --config config.json skill deprecate <name> --approve
python -m aios --config config.json skill telemetry --limit 50
python -m aios --config config.json skill telemetry --name workspace_search --limit 20
python -m aios --config config.json skill replay <candidate_id> --runs 3
python -m aios --config config.json skill compare <candidate_id>
python -m aios --config config.json skill utility <active_skill_name>
python -m aios --config config.json capsule capture <queued_task_id>
python -m aios --config config.json capsule verify <capsule_id>
python -m aios --config config.json experiment run --capsule <capsule_id> --candidate skill:<candidate_id> --runs 3
python -m aios --config config.json experiment compare <experiment_id>
python -m aios --config config.json skill counterfactual-replay <candidate_id> --capsule <capsule_id> --runs 3
```

Each `skill run` produces `SKILL_INVOKE`, `SKILL_CAPABILITY_CHECK`, and
`SKILL_RESULT` traces plus a durable `skill_usage` row. Telemetry stores an input
digest and input field names rather than the raw input payload. Query the same
read-only data with `aiosctl --config config.json skill-usage list`.

Skill manifests can declare `parent_version`, `mutation_reason`, `source_task_ids`,
`source_trace_ids`, `hypothesis`, and `benchmark_delta`. Promoting a new version of
an existing Skill requires its `parent_version` to match the active version.
`replay_task_ids` identifies explicit historical baseline tasks. Historical replay is
retained for diagnosis and weak evidence, but it no longer opens the Agent-candidate
promotion gate. Agent candidates must pass the regular Docker benchmark and produce a
`PROMOTABLE` paired counterfactual report from the same initial state before manual promotion.
Missing or contaminated baselines produce `insufficient_historical_baseline`, not a
fabricated utility gain.

The v0.6.4 historical replay is an **execution proxy**, not a strict causal end-to-end A/B: it
repeats candidate test cases in Docker, compares them with real recorded task metrics,
assumes two model calls for the Skill-enabled path, and holds unobserved replay Tokens
equal to baseline. v0.6.5 counterfactual replay instead restores a prospective pre-task
Capsule and re-executes the complete Model → Agent → Tools/Skill → Verifier trajectory.
Post-hoc captures and unresolved container identities are marked `PARTIAL` and cannot
become `PROMOTABLE` without review.

Skill 不会产生权限。有效权限始终是 `Manifest 声明能力 ∩ Host 已授予能力`；例如声明 `network.external` 的 Skill 在默认配置下仍是 `needs_authority`。

## 安全边界与尚未实现

v0.6 继续冻结 v0.4 的 Tool Evolution，旧插件仅保留兼容且不再暴露给模型。模型生成的命令和 Skill 测试只能进入 Docker 强沙盒；Docker 不可用时绝不降级到宿主 PowerShell。网络默认关闭。域名代理、凭据代理、Workflow Evolution、Harness Evolution 与生产发布审批仍未实现。

## 快速开始

Windows PowerShell：

```powershell
python -m pip install --no-build-isolation -e .
Copy-Item config.example.json config.json
python -m aios --config config.json init
python -m aios --config config.json goal add "完成第一个受控任务" --type user --priority 80
python -m aios --config config.json event emit USER_REQUEST --message "你好，AIOS"
python -m aios --config config.json run --once
python -m aios --config config.json trace --limit 20
```

## 持久任务与结果收件箱

推荐使用 `task submit` 代替裸事件：

```powershell
python -m aios --config config.json task submit "读取 workspace 中的资料并生成 summary.md" --max-attempts 3
python -m aios --config config.json task list
python -m aios --config config.json task show 1
python -m aios --config config.json task reconcile 1
python -m aios --config config.json result list
python -m aios --config config.json result show 1
python -m aios --config config.json result answer 1
```

`result list` 只显示摘要；`result show` 显示完整轮次和证据；`result answer` 直接输出最终文字或生成文件的绝对路径。

`task reconcile` 使用当前Verifier重新检查历史动作证据，适合修复“文件已成功生成但旧Verifier误判”的任务；原死信记录不会删除。

失败任务会自动重试，耗尽次数后进入死信队列：

```powershell
python -m aios --config config.json dead-letter list
python -m aios --config config.json task retry 1
```

## 记忆与诊断

```powershell
python -m aios --config config.json memory add semantic "所有研究报告都需要注明数据来源" --key report-policy --importance 0.9
python -m aios --config config.json memory list
python -m aios --config config.json diagnose
```

完成的任务会自动写入 Episodic Memory；Semantic 和 Procedural Memory 当前由用户或可信系统显式添加。

## v0.4 进化兼容层（已冻结）

旧候选、记录和插件仍可查询，但 v0.5 默认 `evolution.enabled=false`，不会因重复失败自动生成或晋升工具：

```powershell
python -m aios --config config.json evolution runs
python -m aios --config config.json evolution tools
python -m aios --config config.json evolution list
python -m aios --config config.json status
```

配置：

```json
"evolution": {
  "enabled": false,
  "auto_promote": false,
  "trigger_repetitions": 2,
  "retry_after_evolution": false,
  "extensions_path": "./extensions"
}
```

人工 Harness 候选路径仍保留，用于显式配置实验：

候选只能修改三个可变字段，不能修改权限、凭证、沙箱或晋升规则：

```powershell
python -m aios --config config.json evolution propose --rationale "加强产物验证" --prompt-append "写入文件后必须检查产物是否存在" --max-actions 6
python -m aios --config config.json evolution benchmark 1
python -m aios --config config.json evolution promote 1 --approve
python -m aios --config config.json evolution versions
python -m aios --config config.json evolution rollback 1 --approve
```

人工 `promote` 和 `rollback` 没有 `--approve` 时仍会被拒绝。新一轮进化设计将在能力、证据和沙盒边界稳定后再启用。

常驻运行：

```powershell
python -m aios --config config.json run
```

按 `Ctrl+C` 可安全停止。

安装后也可以使用控制台命令：

```powershell
aios --config config.json status
aiosctl --config config.json capabilities list
aiosctl --config config.json traces list --limit 20
```

## 使用真实模型

OpenAI-compatible 服务可使用 `openai_compatible`；DeepSeek 可直接使用 `deepseek`：

```json
{
  "model": {
    "provider": "deepseek",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-v4-flash",
    "api_key_env": "DEEPSEEK_API_KEY",
    "timeout_seconds": 60,
    "max_tokens": 8192,
    "temperature": 0.1,
    "thinking": "disabled",
    "protocol": "tool_calling"
  }
}
```

然后只在进程环境中设置密钥，不要写入配置或事件：

```powershell
$env:DEEPSEEK_API_KEY = Read-Host "DeepSeek API Key" -MaskInput
python -m aios --config config.json run
```

`api_key_env` 必须是环境变量名称，不能直接填写 `sk-...` 密钥。

控制器默认使用非思考 Tool Calling。模型通过API的 `tool_calls` 请求能力，AIOS审核执行后使用同一 `tool_call_id` 返回结果；模型没有请求工具时，普通文本直接成为最终回答。人工 `task retry` 会清空旧结果并重置尝试次数。

如需兼容旧模式，可将 `protocol` 改成 `json_plan`。旧模式解析失败时只记录 `finish_reason` 与字符数，不把模型原文或密钥写入错误日志。

真实模型只能规划配置白名单内的工具。每一个路径仍会由 Security Kernel 重新解析并限制在 `workspace` 下。

## 本地确定性动作

Mock 控制器支持从可信的本地事件生产者传入动作，适合测试 Runtime：

```powershell
python -m aios --config config.json event emit LOCAL_PLAN --payload '{"actions":[{"tool":"write","arguments":{"path":"hello.txt","content":"Hello from AIOS"}}]}'
python -m aios --config config.json run --once
```

`write` 创建或整体替换文件；精确修改使用 `edit`。二者都先写入任务快照，验收后才发布到真实 workspace。

## 测试

无需 pytest：

```powershell
python -m unittest discover -s tests -v
```

## 当前阶段

v0.6 已开始 Skill Evolution：Trace 或重复任务可以被沉淀为候选 Skill，但是只有通过沙盒测试和晋升门的版本才会进入运行时。它是受约束的能力学习，不是任意宿主源码自修改。
