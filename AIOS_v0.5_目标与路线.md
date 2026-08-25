# AIOS v0.5 目标与路线

> 版本主题：Capability Kernel Refactor  
> 性质：架构收缩版，不以增加功能数量为目标  
> 决策日期：2026-08-25

## 1. 目标调整结论

从 v0.5 开始，AIOS 不再通过持续增加模型可见 Tool Schema 获得能力。

正式采用以下边界：

```text
Host controls authority.
Agent controls strategy.
```

即：

- AIOS Host负责目标、状态、权限、证据、评估、沙盒和部署；
- Agent Harness负责在已授权能力内观察、推理、行动和组合策略；
- Root Capability由宿主授予，Agent不能创造；
- Agent可在沙盒中基于根能力创造脚本、Skill和Workflow；
- Tool只是能力接口，不等于系统能力清单。

## 2. v0.5 核心目标

v0.5 只解决三个地基问题：

### P0：Task Semantics

重新定义任务何时算完成，阻止“写了文件但没有完成真实目标”的假完成。

必须实现：

- `EvidenceContract`；
- `BLOCKED_CAPABILITY`；
- `DEGRADED`；
- `NEEDS_AUTHORITY`；
- 完成状态必须由证据判定，不能由模型单方面声明；
- 未通过证据验证的任务不能写入可信成功记忆。

### P1：Capability Architecture

建立能力层次，停止专用 Tool Schema膨胀。

正式定义：

```text
Root Capability
→ Primitive Tool
→ Skill
→ Workflow
→ Harness
```

其中：

- Root Capability：宿主提供的文件、进程、网络、状态、凭据和插件加载能力；
- Primitive Tool：模型直接调用的 `read/write/edit/bash`；
- Skill：Agent在沙盒中创造的可复用脚本或操作说明；
- Workflow：多个 Skill和推理步骤的组合；
- Harness：上下文、模型、循环、验证和调度策略。

### P2：Strong Sandbox

在开放 `bash` 前建立真正的执行隔离。

必须满足：

- Bash不在宿主机直接执行；
- 候选工作区使用临时副本；
- 不向沙盒传入 API Key和生产凭据；
- 生产数据库不以可写方式挂载；
- 不挂载用户主目录或 Docker Socket；
- CPU、内存、磁盘、进程数和执行时间受限；
- 网络默认关闭，开放时经过域名和请求策略；
- 沙盒销毁后只通过宿主 Broker提取允许的产物和证据。

## 3. 正式架构边界

```text
┌──────────────────────────────────────────────┐
│              TRUSTED AIOS HOST               │
│                                              │
│ Event / Task Store                           │
│ Scheduler                                    │
│ State / Memory                               │
│ Trace / Audit / Dead Letter                  │
│ Budget / Timeout                             │
│ CapabilityRegistry                           │
│ Evidence / Evaluation                        │
│ Credential / Network Broker                  │
│ SandboxBroker                                │
│ Promotion / Rollback                         │
│ SecurityKernel                               │
└──────────────────────┬───────────────────────┘
                       │ Capability Gateway
═══════════════════════╪════════════════════════
                       ↓
┌──────────────────────────────────────────────┐
│            SANDBOXED AGENT HARNESS           │
│                                              │
│ Observe → Think → Act → Observe              │
│                                              │
│ Agent-visible primitives:                    │
│ read / write / edit / bash                   │
│                                              │
│ Workspace Copy / Skills / Scripts            │
└──────────────────────────────────────────────┘
```

Agent不需要知道：

- Trace数据库表名；
- Dead Letter存储结构；
- Plugin Registry内部格式；
- Promotion事务如何执行；
- 凭据如何保存和注入；
- SecurityKernel内部规则。

## 4. 四个 Loop

AIOS正式拆分为四个独立循环：

### Goal Loop

```text
Event
→ Durable Task
→ Scheduler
→ Task Runtime
```

v0.5 中 `Goal = durable task` 即可。暂不扩展哲学化的长期动机、欲望和复杂价值仲裁。

### Agent Loop

```text
Observe
→ Model
→ read/write/edit/bash
→ Sandbox Result
→ Observe
```

### Evaluation Loop

```text
Trace
→ ExecutionVerifier
→ EvidenceVerifier
→ ArtifactVerifier
→ GoalVerifier
→ Task State
```

### Evolution Loop

```text
Verified Traces
→ Evolution Scheduler
→ Temporary Evolution Agent
→ Candidate Sandbox
→ Capability Test
→ Integration Test
→ Promote / Rollback
```

执行任务的 Agent不能在任务中途自行决定修改生产 Harness。

## 5. 三个关键新抽象

### 5.1 CapabilityRegistry

不再只列工具名称，而是描述宿主真实能力：

```yaml
filesystem:
  read: true
  write: workspace_copy

process:
  sandbox_exec: true

network:
  enabled: false
  allowed_domains: []

state:
  task_read: true
  trace_read: true
  dead_letter_read: true
  memory_read: true

credentials:
  visible_to_agent: false
  brokered: true

deployment:
  candidate_create: true
  production_promote: false
```

能力检查的结果只能是：

```text
AVAILABLE
COMPOSABLE
MISSING
NEEDS_AUTHORITY
FORBIDDEN
```

### 5.2 EvidenceContract

任务开始时生成完成契约，而不是任务结束后只检查文件是否存在。

示例：

```yaml
goal: 查找arXiv最新AI论文并生成report.md

requirements:
  - id: current_information
    evidence:
      type: network_request
      min_success: 1

  - id: arxiv_source
    evidence:
      type: source_domain
      domain: arxiv.org

  - id: output
    evidence:
      type: file_exists
      path: report.md
```

最终完成条件：

```text
Completion = GoalSatisfied AND EvidenceSatisfied
```

### 5.3 SandboxBroker

SandboxBroker是宿主与通用 `bash` 之间唯一的执行通道，负责：

- 创建和销毁临时环境；
- 提供工作区副本；
- 注入非敏感任务输入；
- 执行命令并限制资源；
- 记录命令、退出码、stdout/stderr和产物摘要；
- 代理受控网络和凭据使用；
- 将执行证据返回 Evaluation Loop。

## 6. 模型可见工具

v0.5 的目标工具表面固定为：

```text
read
write
edit
bash
```

保留四个工具是为了模型操作稳定性，而不是因为它们代表全部系统能力。

- `read`：分页读取文本和受支持文件；
- `write`：创建或整体覆盖沙盒文件；
- `edit`：精确修改已有文件并返回差异；
- `bash`：在强沙盒中运行命令。

状态能力通过 `bash + aiosctl` 使用：

```text
aiosctl tasks list
aiosctl traces list
aiosctl dead-letters list
aiosctl memory search
aiosctl capabilities show
```

具体业务能力通过脚本和 Skill使用：

```text
python skills/search_arxiv/search.py
python skills/analyze_traces/run.py
python skills/test_project/run.py
```

## 7. 任务状态机

目标状态机：

```text
PENDING
  ↓
RUNNING
  ├── COMPLETED
  ├── DEGRADED
  ├── BLOCKED_CAPABILITY
  ├── NEEDS_AUTHORITY
  ├── RETRYABLE_FAILURE
  └── TERMINAL_FAILURE
```

定义：

- `COMPLETED`：真实目标满足，EvidenceContract全部通过；
- `DEGRADED`：产生了部分结果，但质量或时效不满足原请求；
- `BLOCKED_CAPABILITY`：缺少宿主根能力；
- `NEEDS_AUTHORITY`：能力存在，但需要新的用户或管理员授权；
- `RETRYABLE_FAILURE`：重试、重规划或修复后可能成功；
- `TERMINAL_FAILURE`：当前策略下不可恢复。

包含以下表达的结果不能直接成为 `COMPLETED`：

```text
无法执行
不具备工具
仅基于模型知识
未实际查询
不是实时信息
建议换环境
```

## 8. Verifier重构

当前单一 Verifier拆分为：

### ExecutionVerifier

验证计划动作是否真实执行、工具是否成功、预算和超时状态。

### EvidenceVerifier

验证任务要求的数据源和行为是否有 Trace证据。

### ArtifactVerifier

验证文件、结构、非空性、格式和目标路径。

### GoalVerifier

综合判断真实用户目标是否满足，不能只接受 Agent的 done声明。

## 9. Memory调整

正式采用：

```text
Raw Trace
→ Evidence Verified
→ Episode
→ Repeated Verified Evidence
→ Semantic / Procedural Memory
```

停止：

```text
任务被标记completed
→ 无条件写入成功Episodic Memory
```

错误、降级和阻塞任务可以形成诊断材料，但不能作为可信事实进入长期记忆。

## 10. Evolution暂时冻结范围

v0.5 不继续增加新的 Evolution Mutation 类型。

冻结：

- 新专用 Tool Schema自动生成；
- 更多关键词到工具名映射；
- Agent Graph Mutation；
- Workflow Mutation；
- Harness自动晋升；
- 后台自主优化。

保留：

- v0.4 Trace和候选记录，用于兼容和研究；
- 人工查看历史 evolution runs；
- 回滚能力；
- 当前声明式插件目录，但标记为 legacy/deprecated。

重新开启 Evolution的前提：

```text
Evidence semantics稳定
CapabilityRegistry稳定
SandboxBroker稳定
```

## 11. Canary调整

候选评价拆成两层：

### Unit Canary

只验证新增能力本身：

- 是否能运行；
- 是否满足权限；
- 是否返回正确结构；
- 是否通过候选自己的测试。

### Integration Canary

验证能力加入真实任务后是否改善结果。

原任务存在无关永久错误时，不能据此判定新能力无效。

## 12. v0.5 删除、降级与新增

### 删除或逐步废弃的模型工具

```text
list_files
append_file
search_files
query_tasks
query_traces
query_dead_letters
```

迁移期间可保留兼容适配器，但不再作为目标架构。

### 新增

```text
read
write
edit
sandbox_bash
aiosctl
CapabilityRegistry
EvidenceContract
SandboxBroker
新任务状态
分层Verifier
```

### 保留在Host

```text
Task Store
Scheduler
Trace / Audit
Dead Letter
Memory Storage
Budget / Timeout
SecurityKernel
Credential / Network Broker边界
Promotion / Rollback
```

## 13. v0.5 非目标

本版本明确不做：

- 多 Agent编排；
- 复杂长期 Goal哲学模型；
- 自动 Harness Mutation；
- 无限制互联网；
- 将宿主机Shell直接暴露给模型；
- Agent读取 API Key；
- 任意候选自动部署生产；
- 大规模向量记忆系统；
- 为每种业务创建新的 Function Tool。

## 14. v0.5 验收标准

### Task Semantics

- “查询最新arXiv论文”在没有网络根能力时不能成为 `COMPLETED`；
- 应进入 `BLOCKED_CAPABILITY` 或 `NEEDS_AUTHORITY`；
- 写一份“无法查询”的 Markdown不能满足原任务；
- EvidenceContract的失败原因可查询、可审计。

### Capability Architecture

- CapabilityRegistry能区分能力、工具和权限；
- Agent默认只看到 `read/write/edit/bash`；
- Trace、死信和任务查询通过 `aiosctl`，不需要专用 Tool Schema；
- 新业务逻辑优先实现为 Skill或脚本。

### Sandbox

- Bash不能访问宿主项目之外的路径；
- Bash不能读取宿主 API Key；
- Bash超时后进程被终止；
- 沙盒文件变更能够生成审计差异；
- 不允许沙盒直接修改生产数据库和 SecurityKernel。

### Evaluation与Memory

- Verifier至少拆分为 Execution/Evidence/Artifact/Goal四类检查；
- 未通过EvidenceContract的结果不能写入可信成功记忆；
- 降级答案可保留为任务结果，但状态不是 `COMPLETED`。

## 15. 迁移顺序

```text
1. 新任务状态与EvidenceContract
2. CapabilityRegistry
3. 分层Verifier
4. Memory写入门控
5. aiosctl只读状态接口
6. read/write/edit兼容层
7. SandboxBroker
8. sandbox_bash
9. 将专用查询工具迁移到aiosctl
10. 将声明式Tool Evolution标记legacy
```

迁移期间继续保证已有 CLI、数据库和任务记录可读取，不删除历史 Trace、死信、候选和产物。

## 16. 后续路线

```text
v0.4  Declarative Tool Evolution
  ↓ 发现Tool膨胀、假完成、错误归因和Canary问题
v0.5  Capability Kernel Refactor
  ↓
v0.6  Sandbox Skill Synthesis
  ↓
v0.7  Workflow Evolution
  ↓
v0.8  Harness Evolution
  ↓
v0.9  Background Autonomous Optimization
  ↓
v1.0  Self-Evolving AIOS
```

## 17. 最终目标定义

AIOS不再定义为“超级 Agent加大量工具”，而定义为：

```text
AIOS
= Trusted Host
+ Sandboxed Agent Runtime
+ Capability Kernel
+ Evaluation System
+ Evolution System
```

一句话目标：

> AIOS提供少量稳定根能力和可信边界；Agent在沙盒中用通用原语组合策略、开发Skill；Host负责状态、权限、证据、评估和部署。

