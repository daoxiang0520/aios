# Self-Evolving AIOS

> **Research prototype frozen — 2026-09-05**
>
> 本仓库现定位为：**支持 Free Runtime、Component/Capability、Task Capsule、Autonomous Lineage 与 Counterfactual Experiment 的 Agent Runtime 实验平台**。
>
> 当前版本保留用于研究、复现和展示；不再以“可靠递归自进化 AI”作为继续扩展目标，也不建议作为无人监管的生产执行器。

## 最终研究报告

### 项目结论

AIOS 并非“没有做出来”。截至冻结点，它已经真实实现并运行了下面这条链：

```text
Task / Experience
        ↓
Harness 或 Component Candidate
        ↓
Isolated Experimental Lineage
        ↓
Immutable Capsule Replay
        ↓
Parent / Candidate Counterfactual Measurement
```

项目最重要的实验结论是：

> 自修改、候选生成、版本谱系、隔离执行和反事实实验都可以工程化；真正困难的是，在开放任务中可靠判断哪一种修改“确实更好”，并避免错误归因、无效测量、负迁移和高昂实验成本。

因此，项目在“演化基础设施可运行、自主演化有效性尚未证明”的位置冻结。这个停点保留了已完成工作的研究价值，也避免继续进入高成本、低反馈、不断补实验脚手架的循环。

### 已证明

- Durable Task/Event Queue、Result Inbox、Trace、Memory 和 Dead Letter 可运行。
- DeepSeek 原生 Tool Calling、协议恢复与多轮 Agent Runtime 可运行。
- `read/write/edit/bash` 最小工具面、Docker Sandbox、权限和工作区隔离可运行。
- PDF、XLSX、CSV、HTTP Resource Adapter 与 Capability Preflight 可运行。
- Component/Capability 模型可以统一登记 Primitive、Skill、Workflow、Plugin、Resource Adapter、Environment Provider 与 Kernel Component。
- Task Capsule 能冻结初始世界，Experiment 能从相同状态重放父/子版本。
- Free Runtime 能把 `Agent 声明停止` 与 `Host 证明完成` 分开。
- Autonomous Lineage 能创建 Harness 后代、继承 Component Set、绑定任务、创作隔离 Skill 候选并请求反事实测量。
- Counterfactual Evaluation 不会自动晋升生产；失败实验可以复用已持久化 replicate 和语义判断后续跑。
- 冻结前完整测试套件共 **306 项，全部通过**。

### 未证明

- AIOS 能稳定自主诊断并修复新的 Runtime 缺陷。
- 模型能稳定生成最小、可达且语义安全的代码 Patch。
- Agent 能稳定从 Experience 创作并保留有用 Skill。
- Agent 能可靠选择出跨任务更优的 Harness/Component 后代。
- Workflow、Plugin、Adapter、Provider 和 Kernel 已形成自主创作闭环。
- Free Runtime 中的 `stopped` 能代表任务真实完成。
- 谱系机制相对于更轻量的 Memory/Skill 学习能稳定产生额外收益。

### 最后一次关键实验

Run 93 比较了父谱系 `lin_30779d600a014ba9` 与 read-ledger 子谱系 `lin_bf4054b853224d09`：

| Capsule | 观察结果 |
|---|---|
| Task 90 资源复现 | 重复资源动作从 3 降到 0，但中位 Tokens 增加 25,029 |
| Task 89 数据集分析 | 重复动作从 4 增到 5，Tokens 增加约 135 万，Model Calls 增加 78 |
| Task 88 PDF+XLSX | 父子四次运行全部 abandoned，且缺少可追溯终止错误，不能作为有效对照 |

这次实验没有选出赢家，也没有激活生产版本。它真实测出了负迁移，同时暴露了实验错误证据丢失、无效 replicate 仍被聚合、Free 模式缺少在线质量结论以及无预算实验成本过高等问题。

### 为什么停止继续扩展

1. 主要瓶颈已经从工程基础设施转移到模型的因果归因与开放任务评价。
2. 每增加一层合同或特例，都更容易针对既有 Benchmark 过拟合，而不是获得可泛化的自进化能力。
3. 真实 Counterfactual Replay 成本已达到单轮数百万 Tokens，反馈密度不足以支持继续快速迭代。
4. 当前成果已经形成完整且可展示的研究原型，继续扩大目标会降低项目边界和结论的清晰度。

### 冻结后的维护边界

- 接受：安全修复、依赖兼容、文档、可复现性和明确的测量正确性修复。
- 默认不再增加：新的 Reasoner scaffold、针对冻结案例的提示规则、新的自动变异面或无人监管生产晋升。
- 如果未来恢复研究，应先获得新的 provenance-complete Future Holdout，并为 Experiment 设置独立 Token、调用和墙钟预算。
- 在线 Verifier 保持可关闭；原 Verifier 作为 shadow/offline research instrument 保留。

完整的模块、配置、谱系、实验和已知问题清单见：
[AIOS 当前工作总览（2026-09-05）](AIOS_当前工作总览_2026-09-05.md)。

Current release: **v0.9.0-alpha.6**. Autonomous Lineage now versions both Harness settings and a
unified Component Set. Every lineage snapshots registered primitives, Skills, Plugins, resource
adapters and environment providers; Workflow and kernel kinds share the same manifest/policy model.
The first executable Component mutation surface is deliberately limited to Skills. A lineage may
author a new isolated Skill candidate from bounded, redacted lineage evidence, validate its manifest,
and benchmark it in Docker. Authoring never adopts the candidate. A later, separate decision may add
or replace one benchmarked Skill in a child lineage without changing the production SkillRegistry,
Plugin set, Harness default, or Root of Trust.

The Free Runtime semantics introduced in alpha.4 remain unchanged: the online Verifier is disabled,
Agent stop/yield declarations are recorded without turning them into true-completion claims, and
world changes remain versioned and auditable. Authority, sandbox, immutable history, resource limits,
and rollback remain Host-owned. The former Verifier is retained only as the explicit, read-only
`result shadow-verify` research instrument. Legacy configurations without
`runtime.completion_mode` remain in verified mode for compatibility.

```powershell
python -m aios --config config.json evolution lineage-list
python -m aios --config config.json evolution lineage-run lin_root
python -m aios --config config.json evolution lineage-run
python -m aios --config config.json task submit "完成一个真实任务" --lineage current
python -m aios --config config.json run
python -m aios --config config.json evolution lineage-show lin_xxxxxxxxxxxxxxxx
```

`lineage-run` never promotes to production. It can create an isolated Skill variation through
`AUTHOR_COMPONENT_CANDIDATE`, fork a Harness mutation, or adopt an eligible Skill Component candidate
through the separate `ADOPT_COMPONENT_CANDIDATE` action. Workflow, Plugin, resource-adapter,
environment-provider, primitive and kernel mutation remain closed until their own governed candidate
runner and rollback semantics exist. Runtime, Sandbox, Authority, Storage/Audit and external
evaluators remain outside the autonomous mutation surface.

### Lineage Behavioral Observability

Lineage decisions now receive `behavior_digest` per task and bounded `cross_task_patterns`.
These are factual projections of recorded action results, including successful procedures,
operational families, failure classes, cache reuse, written paths and existing Trace references.
Unexecuted plans are excluded; a repeated family shape is not evidence of equivalent purpose or
a recommendation to create a Skill. The model remains free to choose `CONTINUE`.

The projection reads at most 400 plan/result records per task, for at most 20 tasks. Task digests
share a 24,000-character budget and cross-task patterns an 8,000-character budget; task summaries
are capped at 1,200 characters. Sampling/truncation and missing history are explicit. Raw stdout,
file bodies, source code and full traces are not added to the prompt. Selected task digests also
reach the separate Skill authoring stage. Decision calls remain one-shot: Trace references are
audit references, not newly enabled model inspection tools. No Skill trigger, automatic authoring
threshold, or additional adoption policy is introduced.

See [Lineage Behavioral Observability report](AIOS_Lineage_Behavioral_Observability_实现与测试报告.md).

## Agent Workbench（第一版）

本地 Web UI 提供三栏工作台：任务列表、对话/任务详情、实时 Runtime 活动；
`Inspect` 模式额外展示 Evidence、Evolution、模型意图与 Host 处置，并可查看
Runtime Candidate 的修改内容。UI 仅投影现有 SQLite/Trace/Evolution 状态，不复制
Runtime、安全或评估决策。

v0.9-alpha.4 的 UI 将 Runtime policy 与显示模式明确分离：顶部蓝色 `FREE LOOP` / 绿色
`VERIFIED LOOP` 表示在线终止语义，`Normal / Inspect` 只控制信息密度。历史任务按自身证据显示
执行时模式；`agent stopped / agent yielded / runtime abandoned` 不再使用完成态的绿色表达，
Chat 与 Evidence 页都会说明 Agent 声明是否经过 Host Verifier。

```powershell
# 终端 1：打开 UI
python -m aios --config config.json ui

# 终端 2：执行 UI 中排队的任务
python -m aios --config config.json run
```

默认地址为 `http://127.0.0.1:8765`。可通过 `ui --host` 和 `ui --port` 修改；
除非明确需要局域网访问，否则应保持默认回环地址。

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
- Free Runtime 可通过 `runtime.completion_mode=free` 将 Verifier 完全移出在线闭环；Agent 的
  `done=true` 记录为 `stopped`，无完成声明的主动退出记录为 `yielded`，执行/协议预算耗尽记录为
  `abandoned`，三者均不等价于 `completed`；
- `python -m aios --config config.json result shadow-verify <task_id>` 可离线测量记录结果，
  但不会写 Task、Checkpoint、Trace、Memory 或工作区；
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
- alpha.5 在 Attribution validation 前只规范化无歧义的表示差异：`supported_by: "H1"` 可确定性转换为 `supported_by: ["H1"]` 并留下审计记录；Host 不修改 decision、selected hypothesis、causal layer 或 hypothesis status，非字符串复杂类型继续拒绝；
- alpha.5 要求选中假设显式给出 `observed_transition(before/boundary/after)`、`expected_invariant(statement/boundary_behavior)`、`contradiction` 和带 counterfactual 的 causal predecessors；Host 只检查结构完整，因果内容仍由模型负责；
- alpha.6 将 hypothesis references 与 evidence references 分为 `H*` / `E*` 命名空间，使用 `supported_by_hypotheses` 与 `supported_by_evidence` 两个独立字段；Host 验证引用存在性且禁止旧的歧义 `supported_by`；
- alpha.6 的最终 action 严格限定为 `PROPOSE / NO_ACTION`。Mutation Boundary 作为 Host-owned `runtime_mutation_boundary/v2` 事实提供，明确 `src/aios/evaluation.py` 可变而 `external_evaluators/` 不可变；模型不再从自然语言文件名猜测权限；
- Future Holdout Task 83 首次在未见 Runtime Experience 上得到近似正确诊断、正确 Verifier 定位和正确 Mutation 意图，但正则化 goal matching 补丁语义不安全且未创建 Candidate；Run 39 已冻结且不重采样；
- Host-owned Task 83 gate 以显式等价后置条件证明定义 compensation，不接受命令字符串相似、任意后续成功或部分补偿；`model_intended_decision` 作为不可变观察证据保存，Host rejection 只改变 effective decision；
- alpha.6.1 修复 Windows ReadOnly 文件导致 Sandbox discard/下一 Attempt prepare 连续失败的问题；`.git` 元数据不进入 workspace commit，生产工作区隔离保持不变；
- Evolution Run 分开保存 `model_attribution / model_intended_disposition / effective_host_disposition`，模型跨阶段改变 Runtime-defect 判断但没有显式 hypothesis revision 时，仅标记 attribution inconsistency；Host 不替模型选择真值；
- Benchmark 只新增轻量事实测量：`source_support` 区分相关源码是否选择/投递，`unsupported_action_claim` 核对模型明确声称的 Agent 行为是否存在于 Action History；二者都不是 Reasoner 硬门禁；
- Task 84 作为冻结 Root-of-Trust abstention regression：Runtime bug 存在但 `sandbox.py` 不可由 Evolution 修改，期望 `NO_ACTION / authority_boundary|root_of_trust / sandbox_lifecycle`，不重跑原盲测；
- v0.9-alpha.1 的 `soft_friction_experience/v1` 只选择 `completed + verifier success` 任务，逐任务记录 Tokens、Model/Tool Calls、Cycles、重复读取/执行、重试、Context Reuse、Evidence 后续额外动作，以及失败后成功的事实序列；未知的分段 Token 成本保留为 `null`，不由 Host 猜测；
- failed/dead-letter Strategy Adaptation Experience 不自动混入样本，只能通过 `--task-id` 显式选择，并且存在 Host-confirmed Runtime regression 的任务会被排除。此类事实记录重复命令族、退出码、TimeoutError 签名是否出现、未恢复失败数、最终 Cycle 动作数、Controller done 与 Host completion、continuation-like 文本测量信号，不提供错误根因；
- Soft Friction Experience 不输出 `patterns`、坏行为标签、推荐策略或诊断答案。模型用开放问题自主选择 Workflow、Context、Retrieval、Tool-use、Cycle 或 Procedural Strategy，并且一次只能产生一个现有低风险 Harness mutation；
- v0.9-alpha.2 将既有 Structured Evolution 冻结为对照组，并新增独立的 Open Evolution Agent。它在 failure-time Candidate repo 中多轮调用 `inspect_experience/read_source/search_source/run_diagnostic/edit_candidate/write_candidate_test/inspect_diff`，直到明确 `submit_candidate` 或 `NO_ACTION`；Host 不要求 Hypothesis、Failure Path 或 Patch Contract 表单；
- Open Evolution 的诊断命令只在无网络、只读 Candidate 挂载的 Docker 中执行；源码修改必须经过白名单 edit，Root-of-Trust、凭据、外部门禁和生产激活权限均不开放。候选仍交给原 `ExternalRuntimeEvaluator`，因此 `MutableSystem != FitnessAuthority` 保持不变；
- v0.9-alpha.2.1 将 Open Evolution 的可见世界限制为 failure-time `src/aios/ + pyproject.toml + Agent 当次创建的 candidate_tests/`；当前 README、当前 tests、外部 Evaluator、实验报告和 benchmark annotation 不再进入 Candidate world。缺少对齐快照或 Docker 环境时可以验证机制，但实验明确标记无效，不能计入能力评估；
- 原任务作为 `original_task` 事实输入，当前目标单独绑定为 `evolution_objective=诊断/实验/修改候选 AIOS Runtime`。Tool Result 首次完整投递，之后降为带 digest 的事实投影，可用 `read_observation(Rxxxx)` 按需重载；Host 不生成诊断结论；
- Open Evolution Run 分开记录 `model_intended_disposition`、`effective_host_disposition` 与 `terminal_decision_missing`，并给出 `mechanism_validity / experimental_validity / benchmark_role / capability_evaluation_eligible`。Run 41 因当前测试泄漏和目标错绑仅保留为机制回归，不作为能力成绩；
- v0.9-alpha.2.2 修复 Observation reload identity：`read_observation(Rxxxx)` 只是 immutable canonical payload 的字符分页视图，不创建新引用，不保存 reload wrapper，也不把正文重复持久化进 Run transcript。重复读取只增加固定大小的访问元数据；
- v0.9-alpha.3 新增 Capsule-bound Harness Sensitivity：H0 保留完整上下文与认知 scaffold，H1 只保留持久任务状态、资源寻址、工具和基础完成观察，H2 仅保留目标、工作区、通用工具与小型持久状态；三档继承 Capsule 捕获时的 Harness 基线，唯一实验变量是 `harness_profile`；
- `harness-sensitivity` 按 Profile 和 replicate 从同一个 immutable world fork，分别报告完成、Verifier、Tokens、Model/Tool Calls、Cycles、Latency、重复资源动作、失败恢复与失败后工具切换；跨任务只统计中位数和相对 H0 的效应符号，不生成 winner、单一 reward 或 promotion；
- 首轮 27-run 真实矩阵中 H0/H1/H2 均为 0 completion；H1/H2 显著降低 Tokens 和耗时但只是更快失败，因此结果固定为 `MEASUREMENT_ONLY`。详见 `AIOS_v0.9_alpha3_Harness_Sensitivity_实验报告.md`；
- `runtime-compare` 并列投影 Structured/Open 的 Candidate、外部门禁、回归、调用、Token、耗时和主动诊断实验次数；缺失的正确诊断/正确 abstention 人工判据保留为 `null`，不会被合成为单一 reward；
- Strategy Candidate 只在与成功轨迹 Task ID 对齐的 replayable Capsule 上进行 baseline/candidate 回放。Evaluator 将 Completion、Verifier、True Completion 和 Security 作为硬约束，再分别比较 Quality、Model Calls、Tool Calls、Cycles、Tokens、Latency 与 Completion Consistency；Pareto 冲突进入 `NEEDS_REVIEW`，不合成单一 reward；
- DeepSeek Runtime Evolution 请求继续启用官方 `response_format={"type":"json_object"}`，并新增真实 JSON 样例及单顶层文档 framing；只允许剥离完整外层 Markdown fence，多个 JSON 文档绝不猜选。空内容、截断、双文档等失败会形成不含原文的 `protocol_failed` Evolution Run，且不自动重采样；
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

# 查看成功任务的 bounded soft-friction facts（不调用模型）
python -m aios --config config.json evolution optimize-observe --task-limit 10

# 显式加入一个未确认 Runtime regression 的失败/死信 Strategy 样本
python -m aios --config config.json evolution optimize-observe --task-id 85

# 让模型提出一个 Strategy Candidate，并在同批 Task Capsules 上做对照
python -m aios --config config.json evolution optimize-run --capsule <capsule_id> --runs 3
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

### 可选无预算执行

谱系决策的 `current_execution_config` 将保存值与有效限制分开：预算开关、有效周期/任务配额、失效的 Harness 参数、完成模式，以及仍保留的模型输出/命令超时/沙盒/上下文边界。Runtime 与谱系决策共用解析函数，运行时另记录 `execution_config_resolved` Trace。它仅描述本次加载的配置，不声称其他正在运行的进程已热更新；历史任务的 `execution_policy` 来自当时结果，缺失即未知。事实采用白名单，不发送 API 密钥、服务地址或宿主路径。缺少 Settings 的离线调用明确返回配置未知。已有 Run 不改写，也不因本功能触发模型调用。

谱系原生动作 `REQUEST_COUNTERFACTUAL_EVALUATION` 可以从模型可见的执行前 Full Capsule 中选择 1–3 个，并让直接父 Harness 与当前 Harness 各从同一个不可变世界重放 1–3 次。它复用 ExperimentOrchestrator，保存每个实验 ID、父/子测量向量、差值和双序语义测量；事件只保存有界摘要，完整运行证据留在 Experiment 表。该动作不改变实验谱系头、不采用候选、不晋升生产，也不把 Host 指标聚合成适应度裁决。只有带直接 Harness 父节点的谱系可以使用；Root、Component mutation、Partial/Post-hoc Capsule 会在执行前被拒绝。实验失败作为 `lineage_evaluation_failed` 记录，不伪装成谱系选择。下一轮模型可以基于测量自行 `CONTINUE`、`RETURN` 或继续变异。

相同 Capsule、父/子变体和重复次数的失败实验可以断点恢复。已落库的 `(variant, replicate)` 运行证据及语义判断会直接复用，仅执行缺失的 replicate；恢复报告通过 `resumed_from_persisted_runs`、`reused_run_count` 和 `reused_semantic_judgement` 明示来源。正在运行或实验定义不同的记录不会被复用，避免并发附着或跨实验污染。

在运行配置中设置 `budget.enabled=false`，取消普通任务的周期/任务级 Model Calls、Tool Calls、累计 Tokens、Cycles 和谱系 `max_actions_per_cycle` 配额；同时关闭预算预留、soft pressure 和预算强制收尾。旧配置及示例默认仍为 `true`。关闭时模型收到 `enabled=false`，剩余额度为 `null`（无限制），费用和调用计数仍记录，Web UI 显示“无限制”。这不改变任务的权限或完成模式。

无预算时不会因配额产生周期续跑；Agent 可持续执行到最终回答、无行动让出、错误或人工停止。Ctrl+C 请求停止后，当前模型请求及该轮工具操作结束，Runtime 保存 continuation checkpoint，重启后可续跑。历史已 yielded 的任务不会自动重跑。预算开关以重启后的配置为准，旧检查点只恢复用量，不恢复旧开关。

单次模型输出长度、上下文/观察裁剪、协议修复次数、命令超时、沙盒 CPU/内存/PID 及权限边界仍保留；外部实验/Benchmark 的独立保护不在此开关范围。无预算意味着不再有任务级费用上限，模型可能重复执行并持续计费。取消自动周期切分后，长任务也可能增加内存与上下文压力。

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

## v0.9.0-alpha.2.2 Observation Identity Correctness

`read_observation` 现在满足 `View(R, offset, limit)`，不再发生 `Store(Read(R))`。每个原始 Tool Result 最多创建一个 immutable canonical Observation；引用、payload、SHA-256 digest 与 kind 在会话内保持一对一稳定。重载始终返回传入的同一 `Rxxxx`，按 Unicode 字符 offset 分页，并提供 `returned_characters / next_offset / total_characters / digest`；`limit=0` 可只查询元数据。

当前模型轮能看到所请求的 transient chunk；下一轮上下文只保留 ref、范围、字符数和 digest。持久化 Evolution transcript 对 `read_observation` 同样只记录小型访问元数据，不保存正文。因此连续重载不会形成新 Observation、引用链、JSON envelope 嵌套或正文线性复制。该补丁没有修改 Reasoner、轮数、世界隔离、Candidate boundary 或外部门禁。

修复后的 Task 77 Run 43 机制回归确认：3 次 reload 均保留原引用（`R0002/R0004/R0004`），持久记录均无 `text`，相关源码从第 4 轮开始读取。总 Token 为 93,849，与修复前 93,809 基本持平；模型仍未运行诊断实验或提交终态。这不再归因于引用递归，且 Task 77 不计入能力评估。World、Goal、Observation identity、处置分离、Candidate boundary 与外部 Evaluator 隔离全部满足后，Open Evolution instrument 在 alpha.2.2 冻结，等待新的 Future Holdout。

## v0.9.0-alpha.2.1 Open Evolution World/Goal/Context Integrity

alpha.2.1 不增强 Reasoner，也不增加强制诊断合同。它只修复 Run 41 暴露的实验边界：Open Agent 只能看到 failure-time Runtime 白名单世界；`original_task` 是证据，`evolution_objective` 才是当前目标；旧 Tool Result 压缩为可寻址事实引用，模型可通过 `read_observation` 重载；模型意图与 Host 有效处置分别保存。

运行时必须显式声明 benchmark 角色。已知案例默认是机制回归；只有未见 holdout 且世界、来源与环境完整时，才允许进入能力评价：

```powershell
python -m aios --config config.json evolution runtime-open 77 `
  --benchmark-role mechanism_regression --max-rounds 12

python -m aios --config config.json evolution runtime-open NEW_TASK_ID `
  --benchmark-role capability_holdout --max-rounds 12
```

`experimental_validity.state` 使用 `valid / invalid_leakage / invalid_objective_binding / invalid_provenance / invalid_environment`。机制能运行不等于实验有效；无模型终态时保存 `model_intended_disposition=missing`，Host 可以安全回退为 `effective_host_disposition=NO_ACTION`，但不能把回退伪装成模型 abstention。

## v0.9.0-alpha.2 Open Evolution Agent

Structured Runtime Evolution remains unchanged and is the `H_structured` baseline. Open mode changes only the interaction pattern to an iterative Agent loop:

```text
Experience → inspect/search → diagnostic experiment → edit → test
           → revise/continue → submit candidate or NO_ACTION
```

Run one frozen regression case, evaluate a submitted candidate with the unchanged Host gate, and compare modes:

```powershell
$env:DEEPSEEK_API_KEY = (Get-Content api.key -Raw).Trim()
python -m aios --config config.json evolution runtime-open 77 `
  --benchmark-role mechanism_regression --max-rounds 12
python -m aios --config config.json evolution runtime-evaluate RTC_CANDIDATE_ID
python -m aios --config config.json evolution runtime-compare `
  --task-id 77 --task-id 79 --task-id 80 --task-id 83 --task-id 84
```

The Open Agent cannot edit production, external evaluators, benchmark annotations, credentials, or Root-of-Trust files. Diagnostic commands receive a networkless, read-only candidate mount; persistent edits occur only through the bounded candidate tools. Candidate self-tests never replace the task-specific immutable external gate.

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
