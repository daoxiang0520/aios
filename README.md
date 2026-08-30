# Self-Evolving AIOS

Current release: **v0.8.0-alpha.4**. This build freezes alpha.3's temporal and
attribution measurement boundary, carries Task failures for the lifetime of an Attempt,
and adds a lightweight Host-validated failure-path / patch-reachability contract. The
Autonomous Diagnosis Benchmark now scores final disposition, causal attribution,
internal consistency, source localization, Mutation Semantic Precision, and the external gate separately. Historical answers are post-inference annotations and
are never exposed to the Runtime reasoner. Production, Root of Trust, activation,
and external fitness remain Host-owned and immutable.

> v0.6 将进化面从“增加专用 Tool Schema”迁移为“学习可执行、可测试、可版本化的 Skill”。模型的原生工具面仍固定为 `read / write / edit / bash`。

当前分层为：`Root Capability → Primitive Tool → Skill → Workflow → Harness`。v0.6 实现 Skill 层；Workflow 和 Harness 的自主进化仍是后续版本。

## 已实现

- SQLite 持久状态：事件、目标、追踪和通用状态表；
- 事件驱动 Runtime：支持单周期和常驻轮询；
- Goal Manager 与确定性 Intent Arbiter；
- Mock 与 OpenAI-compatible 两种控制器；
- 模型只可见四个通用工具：`read`、`write`、`edit`、`bash`；
- HTTP(S) 资源通过同一个 `read(URL)` 原语与 Host-managed `http_reader` Adapter 读取，不新增模型 Tool Schema；
- `resource.http.read` 的有效可用性同时要求 Provider 存在、Docker 可运行且 `network.external` Authority 已授予；
- CapabilityRegistry 与执行前能力检查；
- EvidenceContract 与执行/产物/证据/目标四层 Verifier；
- `completed`、`degraded`、`blocked_capability`、`needs_authority` 等任务语义；
- Docker-only SandboxBroker：事务工作区、资源限制、禁用宿主 Shell 回退；
- `aiosctl` 只读状态接口与沙盒内状态快照；
- `runtime_experience/v2` 事实 Capsule：联合投影 Tool Result、最终声明、Verifier checks 与成本；关键事实携带 phase/attempt/cycle 时间语义，但不输出 Host 推荐修复；
- 两阶段 Runtime Evolution Reasoner：先从源码符号索引自主选择检查文件，再读取所选源码并提出最小 Candidate patch；
- Candidate Runtime 源码快照与精确替换策略；生产源码永远不是写入目标；
- Root of Trust 禁止变更：Authority、SecurityKernel、Sandbox、审计/存储、CLI/部署、Evolution Controller 和外部 Evaluator；
- Host-owned Docker 外部门禁：Candidate 只读挂载、无网络、降权执行，要求 Task 74 基线 FAIL → Candidate PASS；
- Runtime Candidate 只能进入 `needs_review/rejected`，v0.8-alpha.1 不提供自动生产激活接口；
- Autonomous Diagnosis Benchmark 覆盖 Task 64/67/70/72/74/77/79，分别测量 Final Disposition、Causal Attribution、Reasoning Consistency、Localization、Mutation 与 External Gate，不压成单一 reward；
- 历史 defect 标注只在模型推理完成后用于离线评分，不进入 Experience Capsule、源码索引或 Reasoner prompt；
- `NO_ACTION` 同时区分“没有强行修改”的 epistemic safety 与“符合已知最优处置”的 precision；
- Task 77 首次 prospective holdout 已永久冻结为 Regression Case：相关源码选择与完整投递成功，但两次 Reasoner 均未识别自动重试继承旧 Attempt 预算的因果链；后续人工修复不得改写首次成绩；
- Task 79 已冻结为 Negative Mutation Regression：`NO_ACTION` 最终处置正确，但 causal layer 与假设生命周期错误；它与 Task 77 组成 `SHOULD_NOT_MUTATE / SHOULD_MUTATE` 判别对；
- Task 80 已冻结为 Prospective Mutation Authoring Holdout：Host-owned Attempt completion gate 区分零动作语言任务、计划文本与真实执行证据；首次盲跑和唯一一次 post-gate 结果分别保留，后者因错误归因、不可达补丁和 attribution contract 违规被 Host 安全拒绝，不宣称自主修复成功；
- Benchmark 分开记录 `generated_by_model` 与 `admitted_by_host`，External Gate 报告显式提供 `failure_path_exercised / candidate_changes_failure_outcome` PatchReachability；
- alpha.4 要求 Runtime Candidate 在成立前声明 `failure_path / patch_target / required_inputs / available_inputs / reachability / semantic_invariant`；Host 只检查结构和内部一致性，不建立 CodeGraph，也不替模型判断因果真伪；
- Mutation Semantic Precision 保持向量指标：`contract_valid / source_relevant / path_reachable / gate_effective / regression_safe`，不折叠成单一奖励分数；
- Reasoner hypothesis 具有 `supported/rejected/unresolved` 最终状态；Host 确定性检查 hypothesis、runtime-defect judgment 与 final disposition，矛盾输出被安全收敛为可审计的 `NO_ACTION/attribution_consistency_failed`，不得生成 Candidate；
- Localization 分开记录模型 `proposed_files`、Host `admitted_files`、首个相关文件排名与源码预算裁剪，避免把检索预算问题误记为模型定位失败；
- Historical Benchmark v2 对 Trace 事实完整性和 failure-era source fidelity 设置 eligibility gate；故障 Trace 配修复后源码的 Case 不进入有效 Diagnosis/Mutation 分母；
- 每个新 Task Cycle 在 Intent/Preflight 前绑定内容寻址的 execution-runtime 与 evaluator 源码快照；跨 Cycle 相同对象自动去重；
- Runtime Experience Capsule 记录 Host 的 preflight、continuation、retry、terminal 等因果决策及快照引用，不把整个数据库无界复制给模型；
- `DiagnosisEligible = TraceSufficient`；`RepairEligible` 还要求 failure-time 源码对齐、对象完整、Evaluator 与 Root-of-Trust 身份已知，并存在 Host-owned task-specific external gate；
- Candidate 没有对应的 Host-owned task-specific external gate 时直接 `rejected/unsupported_external_gate`，Candidate 自带测试不能替代外部 Fitness Authority；
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
- 统一沙盒路径语义：`read/write/edit` 同时接受相对路径与 `/workspace/...`，两者映射到同一事务快照；其他绝对路径和路径逃逸仍被 Security Kernel 拒绝。
- 可选完全出网：`capabilities.network_enabled=true` 时 Docker 使用 `bridge`，`network.external` 标记为 `available/unrestricted`；关闭时继续强制 `--network none`。当前没有域名白名单代理。
- 任务级 Python 依赖层：沙箱中的 `/deps` 在同一任务各轮间持久、任务结束即销毁；完全出网时 Agent 可用 `pip --target /deps` 临时组合 PDF/XLSX 等解析能力，不污染宿主环境。
- 有界工作区清单：Runtime 在首轮上下文中提供最多 200 个文件的相对路径与大小，减少模型用 `ls/find/file` 重复发现文件造成的轮次浪费。
- Resource Adapter：模型仍只看到 `read/write/edit/bash`；`read` 可结构化读取目录、文本/代码、CSV、ZIP、PDF 和 XLSX。PDF/XLSX 解析在只读 Docker 中运行，模型仍自行决定读取范围和分析策略。

`read` 返回统一的资源观察：`resource.path / type / metadata / representations`。目录条目使用工作区相对路径；PDF 表示包含页数和可分页文本；XLSX 表示包含工作表维度与有界行预览。适配器只改善数据接入，不自动总结、建模或选择求解流程。
- 最终回答协议修复连续失败时，不再直接把任务送入死信；系统生成协议干净、带最后工具证据的受限回答，并由 Verifier 标记为 `degraded`。

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

Skill 不会产生权限。有效权限始终是 `Manifest 声明能力 ∩ Host 已授予能力`。默认示例配置关闭网络；宿主显式设置 `capabilities.network_enabled=true` 后，任务和 Skill 容器获得不受域名限制的 Docker Bridge 出网能力。

## 安全边界与尚未实现

v0.6 继续冻结 v0.4 的 Tool Evolution，旧插件仅保留兼容且不再暴露给模型。模型生成的命令和 Skill 测试只能进入 Docker 强沙盒；Docker 不可用时绝不降级到宿主 PowerShell。网络默认关闭，但可由宿主显式开启完全出网；域名代理、凭据代理、Workflow Evolution、Harness Evolution 与生产发布审批仍未实现。完全出网时，沙盒代码能够把 Workspace 内容发送到任意地址，应视为显著的信任边界扩张。

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
python -m aios --config config.json evolution runtime-provenance <task_id>
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

## v0.6.6.1 Runtime Hardening

- Bash 固定由 `bash -o pipefail -lc` 执行，pipeline 中前段失败不再被 `head` 等末端命令掩盖。
- 沙盒命令超时拆为 `default_timeout_seconds` 与 `max_timeout_seconds`；旧 `timeout_seconds` 配置会自动迁移，任务请求的较长超时不再被默认值静默压回。
- `CycleBudget` 只控制单周期资源，`TaskBudget` 控制完整任务；周期耗尽产生 `budget_deferred` checkpoint 和 `TASK_CONTINUE`，不增加任务 attempt。
- 只有任务真正完成、明确失败或 TaskBudget 到达终点才进入 Verifier/finalization；周期最后一轮不再自动禁用工具。
- PDF/XLSX/CSV/text 采用 metadata-first 小预览，Tool Result 有字符预算；旧的大结果从 HOT context 压缩成摘要与 `trace:<id>` 引用。
- 首次需要科学计算时，Host Provider 构建固定版本的 numpy/pandas/scipy/statsmodels/openpyxl/pypdf 环境；后续任务只读复用。任务私有 `/deps` 跨 continuation 保留，真正终态才清理。
- Evidence 新增任务累计 Model Calls、Tokens、Task Cycles、首次计算轮次与依赖准备延迟。

## v0.6.6.2 Context Working Set & Continuation Efficiency

- 每次模型调用把 Prompt 拆为 system、task、workspace map、memory、history、working state、skills、environment、continuation、runtime 和 tool schema；记录字符数、估算 Token、API 实际 Prompt Tokens、SHA256 与重复 Token 比例。
- 新增 Host 管理的 `TaskWorkingState`：保存目标、已确认资源事实、DONE 步骤、可用产物、执行环境、待办与 Evidence References；原始结果继续留在 Trace，不充当长期上下文。
- `budget_deferred` 冻结 Working State；下一 Cycle 使用 fresh context 恢复状态，不恢复上一 Cycle 对话或 Tool History。
- 同 Cycle 默认只保留最近两个 Tool Round 为 HOT；Working State 为 WARM；完整 Trace 为 COLD。详细观测丢弃后，按需窄范围 reread。
- Workspace Inventory 只在任务首次调用完整注入；之后仅投影已访问资源与产物。Capability、Skill 和 Environment Map 同样使用差量形式。
- 新增 120k Soft Token / 12 Soft Model Calls 压力线；超过后提示 Agent 停止 discovery、沿关键路径收敛。300k/24 仍是 hard limit。
- `task retry` 写入 `retry_reset`，确保新一次验收不继承旧 continuation 的预算与 Working State。

## v0.6.6.3 Structured Completion Semantics

- Controller finalization now carries internal `completion_metadata`; user-visible answers remain natural text.
- `CompletionArbiter` resolves outcomes in the order Host execution/capability, evidence contract, controller declaration, then scoped language heuristics.
- Result evidence includes a five-dimensional `result_vector`: completion, evidence, capability, quality, and protocol.
- Generic business statements containing “无法” no longer imply Agent degradation.
- Explicit Agent/environment inability and substituted evidence remain degraded outcomes.
- Memory remains gated to fully verified `completed` results; the richer result vector is retained for later policy evolution.

## v0.7.1 Semantic Fitness & Persistence

- Fixes the Task 66 false positive where a section saying “content was not fully presented; complete it next round” was accepted as full target coverage.
- Answer Coverage recognizes explicit deferral/incompleteness signals including `未能在此轮完整呈现`, `待下一轮`, `待补充`, and equivalent English phrases.
- Topic section boundaries now accept headings such as `B题PDF`; the previous lexical boundary could accidentally merge B's disclaimer into A's section.
- Every complete textual Resource Observation carries a bounded `semantic_residue` in Task Working State. It preserves up to 1,200 characters per important resource and 4,000 characters across the task.
- The controller must use semantic residue before requesting the same full resource again. Detailed observations remain COLD Trace data.
- Task 66's historical `completed/full` label is not valid Self-Evolution evidence. It must be rerun under v0.7.1 before being captured as a positive Capsule.

## v0.7.1.1 Runtime Correctness Hotfix

- Verification receives an explicit `CanonicalAnswer`: user message plus the body of relevant final-deliverable artifacts. A tool-returned path is no longer treated as answer content.
- Task results retain compatibility `final_output` while separately recording `user_message`, `artifacts`, and `canonical_answer`.
- `TASK_CONTINUE` events carry explicit task/checkpoint/generation columns. A partial unique index enforces at most one pending/processing continuation per task.
- Enqueue is idempotent; consumption fences terminal tasks, stale checkpoints, and stale generations before any model call.
- A failed continuation retries as `TASK_REQUEST`, so retry attempts cannot remain constant forever.
- Runtime telemetry records `continuation_duplicates_suppressed` and `stale_continuations_discarded`.
- Task 64 was quarantined as `needs_review`; its cost evidence is marked contaminated and invalid for learning. Task 67 was reverified without a model call, its recorded artifact restored, and its prior verifier failure retained in the audit chain.
- URL capability parsing distinguishes `https://` from Windows drive prefixes, treats explicit URLs as `network.external`, and preserves multi-level domains such as `luogu.com.cn`.
- Eight focused Runtime/capability gates pass; the complete suite passes **132/132**.

## v0.7.1.2 Capability Reference Classification

- Structured HTTP(S) spans are recognized before generic filesystem scanning and masked from path inference.
- URL fetch/read/reference intent requires `network.external`; merely explaining a URL string does not.
- Windows absolute paths, protected POSIX host paths, parent traversal, and mixed URL/path requests retain independent capability requirements.
- Multi-level domains such as `luogu.com.cn` are preserved in `source_domain` evidence.
- Task 70 re-preflight succeeds under v0.7.1.2; its original Host misclassification is recorded as invalid for Agent learning. A resident pre-hotfix Runtime must be restarted before the task is requeued for real execution.
- Focused gates pass **11/11**; the complete suite passes **135/135**.

## v0.7.1.3 Cross-Cycle Evidence Persistence

- Host-observed EvidenceContract facts are stored in a bounded `evidence_ledger` with kind, value, state, and Trace reference.
- The ledger crosses checkpoint continuation through Task Working State; Verifier checks current actions plus established evidence, so a final local computation does not erase an earlier web fetch.
- Models cannot self-declare ledger entries; only successful ActionResults matching an active EvidenceContract can establish them.
- `task reconcile` can rebuild the same ledger from historical `plan_created/action_result` traces and re-evaluate without a model call.
- A caught network exception printed as `ERR ...` is not evidence even when the wrapper exits with code 0.
- Task 71 was reverified from the real Luogu observation and existing `P1593.py`, then recovered from dead letter to completed with zero additional model calls. Its retry-inflated cost is marked contaminated.
- Focused gates pass **14/14**; the complete suite passes **138/138**.

## v0.7 Self-Evolution Loop

AIOS now has a slow evolution loop separate from the normal task loop:

```text
Experience → AI diagnosis → Hypothesis → Harness mutation
           → Capsule counterfactuals → Constraint/Pareto selection
```

- `ExperienceAnalyzer` compresses cross-task Task/Trace/Verifier/cost telemetry but deliberately does not recommend an architecture change.
- `ModelEvolutionReasoner` receives the evidence packet and autonomously selects one recurring friction, one hypothesis, and at most one mutable-environment mutation.
- The immutable Kernel boundary covers authority, credentials, isolation, audit, experiment boundaries, rollback, and human override. A Kernel mutation is rejected before candidate creation.
- The v0.7 MVP opens only the declarative `harness` surface: `prompt_append`, `max_actions_per_cycle`, or `memory_context_characters`. Skill evolution remains available through its existing lifecycle; other Component kinds are still closed.
- Harness candidates run against baseline in immutable Task Capsules through the same counterfactual evaluator used for Skills.
- Selection is constraint/Pareto based: security and correctness cannot regress; only then may cost, calls, and latency establish an improvement.
- A winning candidate becomes `selected`, not active. The slow loop never modifies the production Harness automatically.
- After reviewing the proposal and experiment reports, a human can activate a selected candidate with `python -m aios --config config.json evolution promote CANDIDATE_ID --approve`.

Run against explicitly selected capsules:

```powershell
python -m aios --config config.json evolution auto-run `
  --capsule CAP_ID_1 --capsule CAP_ID_2 --runs 3
```

When `--capsule` is omitted, the loop selects up to three recent replayable capsules. With the mock provider it records `NO_ACTION`; a configured remote model is required to author a real hypothesis.

## v0.6.8.2 Goal-Oriented Coverage

- Coverage obligations are semantic targets, not every narrative file under the resolved directory. A modeling-folder request resolves to `A题 / B题 / C题` rather than all PDF/Markdown files.
- Each target selects one deterministic best evidence source. Explicitly named and canonical topic files are preferred; PDF/DOCX sources outrank derived analysis notes.
- `required_for_coverage` remains only as a compatibility projection on the selected evidence paths; `coverage_targets` owns verification truth.
- Verification requires every target to have a complete evidence reference and substantive answer coverage. A disclaimer such as “B题正文未完整呈现” does not count as B-topic coverage.
- No Resource ontology, database table, Registry, Role Resolver, or separate Claim Consistency Verifier was added.
- The Task 65 regression fixture selects only `A题.pdf / B题.pdf / C题.pdf`; four derived Markdown files create no mandatory coverage debt.

## v0.6.8.1 Coverage Scoping + Observation Reuse + Sandbox Health Stabilization + Adapter Retry

- Resolves a structured Coverage Scope before selecting obligations. A request for the modeling folder resolves to `MathModeling/`; files outside that subtree are excluded rather than counted as unread debt.
- Adds a task-scoped persistent Observation Cache keyed by normalized path, SHA-256 content digest, representation, offset, and limit. Identical reads return `reused=true` plus the original observation reference without executing the Resource Adapter again.
- Separates repeated requests from repeated executions through `repeated_resource_reads`, `repeated_resource_executions`, and `observation_reuse_hits`.
- Docker health is cached as a Sandbox Session invariant with a configurable TTL (`sandbox.health_ttl_seconds`, default 30). It is reprobed only for session creation, TTL expiry, explicit invalidation, or recovery.
- PDF/XLSX adapters retry exactly once only for classified transient Docker daemon/container-start failures. Corrupt files, unsupported formats, permission failures, and parse errors are not retried.
- The Task 64 deterministic release gate completes with `coverage_scope.root=MathModeling`, zero outside-root obligations, zero identical repeated executions, positive reuse hits, and health probes equal to Sandbox sessions.

## v0.6.8 Runtime Situation Resolution

- Replaces the model-visible static Environment Map with a dynamic `SituationMap`: relevant resources, required/available capabilities, relevant procedures, operational state, constraints, and environment readiness.
- Splits Working State into Host-maintained operational state and semantic state while retaining the v0.6.6 compatibility fields. Resource records include normalized path, completeness, representation, evidence reference, access count, and conservative coverage labels.
- The Host resolves declared and authority-available providers before presenting capability routes; the model does not traverse the full Component graph.
- Repeated complete-resource reads, `bash cat/head/tail/file` Resource Adapter bypasses, and generic environment probes remain permitted escape hatches but emit explicit telemetry and task metrics.
- Broad folder-summary tasks receive deterministic resource/topic coverage checks. A premature final answer gets an in-cycle coverage repair opportunity using existing evidence, without rereading complete resources.
- Skill candidate ingestion now requires explicit Skill-authoring intent plus a candidate package attributable to the current task. Incidental or stale workspace packages produce a `NO_ACTION` trace instead of a false candidate.
- Adds `RepeatedResourceReads`, `RedundantResourceBypasses`, `EnvironmentProbeCalls`, `ProtocolRepairCalls`, and `ProtocolRepairTokens` to terminal evidence.
- The Task 63 deterministic fixture completes in three model rounds, performs zero repeated reads, covers A/B/C, and rejects the original A/B-only answer.

## v0.6.7 Unified Component Model + Capability Graph

- Adds a Host-owned `ComponentRegistry` and deterministic `ComponentManifest` for primitives, skills, workflows, resource adapters, environment providers, plugins, and kernel components.
- Components declare `requires` and `provides`; explicit capability implications form the first in-process Component/Capability graph without a graph database.
- Declared supply and usable supply are separate APIs: `resolve_provider` reports who declares a capability, while `resolve_available_provider` additionally applies current Authority and dependency checks. A provider's existence never grants permission.
- `SkillManager` is the single source of truth for Skills. Active Skills are reconciled into a read-only `ComponentRegistry(kind=skill)` compatibility projection, so deprecation/removal cannot leave an active Component behind; promotion, telemetry, runner, and replay behavior remain unchanged.
- Every resolved manifest exposes the same runtime core (`plane`, `isolation`, `runner_kind`) and keeps kind-specific configuration under `spec`.
- Multiple providers are supported with deterministic exactness/trust/version/cost/utility ordering. Capability inheritance occurs only through explicit implication edges, never by dotted-name prefix.
- PDF/XLSX/CSV adapters and `scientific-py312-v1` are registered as Host-managed providers; the Agent sees only a compact capability-centric environment map.
- Host Trust Policy overrides Manifest claims. Only Skill candidates remain Agent-authorable; no Component kind gains automatic promotion.
- Task Capsules include the active Component Set hash. Experiment variants expose a generic Component mutation schema while execution remains restricted to `kind=skill`.
- The model-visible Tool surface remains exactly `read`, `write`, `edit`, and `bash`.
