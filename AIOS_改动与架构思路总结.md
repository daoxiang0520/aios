# AIOS 改动与架构思路总结

> 更新时间：2026-08-25  
> 当前实现版本：v0.4.0  
> 项目目录：`C:\Users\15959\Desktop\AIOS\aios`

> **目标调整决议**：评审后已正式将下一阶段定义为 **v0.5 — Capability Kernel Refactor**。暂停扩展新的专用 Tool Evolution，优先完成 Task Semantics、CapabilityRegistry、EvidenceContract 与 SandboxBroker。详细目标、非目标、验收标准和迁移顺序见 [`AIOS_v0.5_目标与路线.md`](./AIOS_v0.5_目标与路线.md)。

## 1. 项目目标

AIOS 的目标不是单纯封装一次模型调用，而是构建一个可持续运行的 Agent Runtime，统一管理：

- 事件与持久任务；
- 目标和意图仲裁；
- 模型与 Tool Calling；
- 工作区、记忆和上下文；
- 权限、预算与执行隔离；
- 结果验证、失败重试和死信；
- Trace、诊断和系统自我改进。

长期目标是形成：

```text
Observe
→ Diagnose
→ Propose
→ Generate
→ Evaluate
→ Promote
→ Observe Again
```

但实践表明，系统不应为了覆盖所有情况而不断增加专用工具和控制层。新的方向是借鉴 Pi Agent：用少量高能力根工具构成 Agent Harness，将安全、审计和持久化放在模型不可见的宿主层。

---

## 2. 已完成的版本演进

### 2.1 v0.1：最小事件驱动 Runtime

首版实现：

- SQLite 状态库；
- Event Queue；
- Goal Manager；
- Intent Arbiter；
- Runtime 常驻循环；
- Mock/OpenAI-compatible Controller；
- `echo`、`list_files`、`read_file`、`write_file`；
- SecurityKernel 工作区路径限制；
- Trace记录；
- CLI和基础测试。

这一阶段解决了“模型不会自行持续行动”的问题：模型外部有了事件、状态、循环和执行器。

### 2.2 v0.2：任务可靠性与受控进化骨架

新增：

- durable task；
- result inbox；
- checkpoint；
- 自动重试和人工重试；
- dead letter queue；
- Working/Episodic/Semantic/Procedural Memory类型；
- Context Composer；
- 确定性 Verifier；
- Trace聚合诊断；
- Harness Candidate；
- benchmark、人工 promote和 rollback。

此时的“进化”仅能修改：

```text
prompt_append
max_actions_per_cycle
memory_context_characters
```

它是受控配置实验，不是真正的自主工具开发。

### 2.3 v0.2.1～v0.3.1：多轮 Tool Calling 与验证修复

主要改动：

- Observe → Plan → Act 多轮执行；
- 原生 DeepSeek Tool Calling；
- 使用 `tool_call_id` 回传工具结果；
- 保留旧 JSON Plan兼容模式；
- 模型普通文本可作为最终回答；
- 修复 DeepSeek JSON/代码块响应解析；
- 修复输入文件被误判成目标产物；
- 成功写入目标文件可在最后一轮直接完成；
- 增加 `task reconcile`，可用新 Verifier重新核验历史任务。

典型问题与修复：

```text
任务要求读取game.py并生成code_review.md
旧逻辑：同时把game.py和code_review.md视为输出
新逻辑：只识别“生成/创建/写入”等动词之后的目标文件
```

### 2.4 v0.4.0：失败驱动的声明式工具进化

新增：

- `EvolutionConfig`；
- `EvolutionDaemon`式失败观察逻辑；
- 重复失败签名；
- `evolution_runs`审计表；
- 声明式 Tool Plugin Manifest；
- candidate/active/quarantine目录；
- Manifest权限检查；
- smoke test；
- 自动启用；
- Canary失败自动回滚；
- 同一进程刷新动态 Tool Schema；
- `append_file`长产物追加能力；
- 模型可见的剩余预算；
- 为最终产物保留工具调用；
- 超预算调用返回 `BudgetDeferred`，不再静默丢弃。

当前声明式进化允许生成：

```text
query_tasks
query_traces
query_dead_letters
search_files
```

当前允许的插件类型：

```text
state_query
workspace_search
```

明确禁止生成工具请求：

```text
任意网络
子进程
数据库写入
宿主机任意文件访问
修改SecurityKernel
```

v0.4.0 部署后测试结果：25项测试全部通过。

---

## 3. 当前系统组件

```text
AIOS Runtime
├── Event Queue
├── Task Store
├── Goal Manager
├── Intent Arbiter
├── LLM Controller
├── Tool Registry / Executor
├── SecurityKernel
├── Memory / Context Composer
├── Verifier
├── Trace / Checkpoint
├── Retry / Dead Letter
├── Evolution Engine
└── Plugin Manager
```

数据主要保存在：

```text
data/aios.db
```

其中：

- Trace在 `traces` 表，不是 workspace 日志文件；
- 死信在 `dead_letters` 表，不是 workspace 文件；
- 记忆在 `memories` 表；
- 任务在 `tasks` 表；
- 进化记录在 `evolution_runs` 和 `evolution_candidates` 表。

---

## 4. 当前记忆能力

AIOS定义四类记忆：

| 类型 | 作用 | 当前实现 |
|---|---|---|
| Working | 当前任务临时状态 | 主要由上下文和观察结果承担 |
| Episodic | 过去发生的任务 | 成功任务自动写入摘要 |
| Semantic | 长期事实与规则 | 支持存储，主要人工添加 |
| Procedural | 做事方法与流程 | 支持存储，主要人工添加 |

当前检索使用关键词相关性与 importance 排序，不是向量检索。

主要风险：

```text
Verifier假完成
→ 错误结果写入Episodic Memory
→ 后续任务检索错误历史
→ 错误被重复强化
```

因此未来只有通过证据和质量验证的任务才能进入可信记忆；失败经验需要经过提炼、冲突检测和分级后再进入 Semantic/Procedural Memory。

---

## 5. 实验中暴露的关键问题

### 5.1 JSON截断不是解析器问题

长 Markdown被整体放入 `write_file.content`，输出达到 `max_tokens` 后工具参数成为不完整 JSON。

已采取：

- 提高合理输出预算；
- 增加 `append_file`；
- 提示模型分块生成长文件。

仍需增加：

- 检查 `finish_reason=length`；
- 针对截断进行分块重试，不要原样重试。

### 5.2 固定工具预算导致计划被静默裁剪

任务9曾出现：

```text
planned_actions=9
executed_actions=8
budget_truncated=true
```

根因不是简单的“8太小”，而是模型不知道剩余预算，Runtime又静默丢弃计划尾部。

v0.4已改为：

- 将剩余模型/工具预算放入上下文；
- 为产物写入预留调用；
- 被推迟的调用返回 `BudgetDeferred`；
- 要求模型停止宽泛调查并进入综合阶段。

### 5.3 失败触发进化不等于正确进化

任务10要求“在网络上查找AIOS信息”。诊断器因看到“查找”而生成了 `search_files`，但真实缺口是网络能力。

说明关键词式诊断会发生：

```text
网络查找
→ 错误识别为workspace搜索
```

系统确实触发了进化，但进化方向错误。

### 5.4 假完成

任务10和任务17都明确表示：

```text
无法真实联网
内容基于模型内置知识
建议换环境执行
```

但系统仍标记为 `completed`，因为：

```text
write_file成功
+ 文件存在
+ echo宣布完成
= Verifier通过
```

这是当前最严重的问题之一：形式完成不等于目标完成。

### 5.5 Trace与死信证据被错误理解

任务11要求查询任务、Trace和死信，但模型只使用：

```text
list_files
search_files
read_file
write_file
```

它没有在 workspace找到 Trace文件，于是错误宣称系统没有 Trace和死信。

真实情况：Trace和死信位于 SQLite。

### 5.6 Canary粒度错误

强制缺失文件测试触发了 `query_tasks/query_traces/query_dead_letters` 的自动生成。但原任务包含一个永久不存在的文件，重试仍失败，系统因此回滚了三个本身有效的查询工具。

结论：

```text
原任务继续失败
≠
新增工具无效
```

Canary必须测试候选能力本身，并将原任务中的其他失败原因隔离。

---

## 6. 执行前能力检查与证据来源验证

### 6.1 执行前能力检查

任务开始前回答：

```text
任务需要什么能力？
系统拥有什么能力？
缺失能力能否组合、开发或授权？
```

例如：

```text
“查询arXiv最新论文”
→ 需要外部网络和当前信息
→ 当前没有网络根能力
→ 不应让模型使用内置知识降级完成
→ 应开发能力或标记BLOCKED_CAPABILITY
```

### 6.2 证据来源验证

任务完成后检查模型是否真正使用了要求的数据源：

```text
声称查询Trace
→ 必须存在真实Trace查询证据

声称查询死信
→ 必须存在真实死信查询证据

声称获取最新网络信息
→ 必须存在成功网络请求及来源URL

声称运行测试
→ 必须存在测试命令和退出码
```

没有证据时，即使生成文件也不能完成。

### 6.3 后续需要的任务状态

除 completed/dead_letter 外，需要增加：

```text
BLOCKED_CAPABILITY
DEGRADED
NEEDS_AUTHORITY
```

其中“无法执行”“没有工具”“仅基于模型知识”“非实时”等内容应被识别为降级或阻塞，而不是成功。

---

## 7. 权限工具与系统接口

模型不能凭空获得真实能力。文件、网络、进程、数据库和凭据必须由系统提供可信接口。

关系类似：

```text
传统程序 → 系统调用 → OS内核
AI Agent → Tool Calling → AIOS Capability Kernel
```

但系统接口不应全部暴露为大量模型工具。应区分：

### 根能力

由宿主机预置或明确授权：

```text
文件访问
沙盒执行
网络出口
状态查询
凭据代理
插件加载
```

### 派生能力

由 Agent基于根能力自行开发：

```text
arxiv_search
GitHub分析
Trace诊断脚本
测试运行器
数据处理Skill
```

Agent可以创造如何使用权限的逻辑，但不能自行创造尚未授予的权限。

---

## 8. Pi Agent带来的架构启发

Pi默认只向模型提供四个工具：

```text
read
write
edit
bash
```

其能力强的关键不是工具多，而是 `bash` 是通用原语：

```text
ls/rg/find
Python
测试
Git
curl
包管理
脚本开发
```

AIOS当前不断增加：

```text
list_files
append_file
search_files
query_tasks
query_traces
query_dead_letters
未来的各种xxx_search
```

这会让 Tool Schema、诊断映射和权限规则持续膨胀。

新的建议是模型侧同样只保留：

```text
read
write
edit
bash
```

其中 `bash` 必须运行在隔离沙盒，而不是宿主机。

状态可以通过沙盒内 CLI读取：

```text
aiosctl tasks list
aiosctl traces list
aiosctl dead-letters list
aiosctl memory search
```

网络在沙盒策略允许时使用受控命令或Broker；具体工具以脚本、Skill或Extension存在，不继续增加底层模型工具。

---

## 9. 建议的精简目标架构

```text
AIOS Host（模型不可直接访问）
├── Event / Task Store
├── Goal / Intent
├── Memory
├── Trace / Dead Letter
├── Budget / Timeout
├── SecurityKernel
├── Sandbox Manager
├── Candidate Promotion / Rollback
└── Agent Loop
        │
        ↓
Agent可见工具
├── read
├── write
├── edit
└── bash（仅沙盒）
```

具体能力：

```text
脚本
Skill
CLI
Extension
```

不再默认成为新的 Tool Schema。

---

## 10. 沙盒权限原则

可以给 Agent沙盒内完整开发权限，但不能给宿主机完整权限。

沙盒内可允许：

- 任意修改候选源码副本；
- Shell/Python；
- 运行测试；
- 创建和删除沙盒文件；
- 安装沙盒依赖；
- 开发新脚本、Skill和Extension。

宿主机必须隔离：

- 不传入 API Key；
- 不挂载生产数据库为可写；
- 不挂载用户主目录；
- 不提供 Docker Socket；
- 不允许直接修改运行中的 SecurityKernel；
- 不允许候选自行部署到生产。

Windows环境优先使用 Docker Desktop容器或 Hyper-V/Windows Sandbox。普通 subprocess不属于强安全沙盒。

推荐流程：

```text
发现缺口
→ 创建候选源码快照
→ 启动临时沙盒
→ Agent使用read/write/edit/bash开发
→ 运行候选专属测试
→ 生成补丁、Skill和测试证据
→ 销毁沙盒
→ 宿主Broker检查
→ Canary
→ Promote或Rollback
```

---

## 11. 下一版建议实施顺序

### 第一阶段：纠正完成语义

1. 增加 `BLOCKED_CAPABILITY/DEGRADED` 状态；
2. 检测“无法、不能、非实时、基于内置知识”等降级回答；
3. 未真实满足目标的任务不得写入成功记忆；
4. 给任务建立最小证据契约。

### 第二阶段：极简四工具 Harness

1. 新增 `read/write/edit/bash`；
2. `bash`只连接临时沙盒；
3. 将 list/search/append/test/git 等能力转为命令；
4. 提供只读 `aiosctl`；
5. 逐步废弃专用查询 Tool Schema。

### 第三阶段：沙盒自开发

1. Candidate工作区快照；
2. Agent在沙盒中开发脚本/Skill；
3. 自动运行静态检查、单元测试和能力测试；
4. 测试证据随候选保存；
5. 候选能力单独 Canary，不再只看整个原任务。

### 第四阶段：受控网络根能力

1. 增加网络Broker或容器网络策略；
2. 默认无网络；
3. 支持域名白名单；
4. 凭据不进入模型和沙盒；
5. 先以 arXiv查询作为端到端样例。

### 第五阶段：经验学习

1. 从成功和失败中提炼候选经验；
2. 区分原始Trace和总结Memory；
3. 记忆冲突检测；
4. 错误记忆隔离与回滚；
5. 将稳定经验保存为 Procedural Skill，而不是不断增加 Prompt。

---

## 12. 当前结论

当前 AIOS 已经具备：

```text
持久任务
原生Tool Calling
工作区隔离
多轮执行
预算
Verifier
Trace/Dead Letter
基础记忆
声明式候选工具进化
自动测试、启用和回滚
```

但仍未达到真正通用自进化 Agent，主要缺口是：

```text
完成语义不可靠
证据质量不足
成功但降级不会触发进化
Canary归因粗糙
无强沙盒Shell
无受控网络根能力
专用工具数量开始膨胀
```

下一步不应继续堆叠专用工具，而应：

> 将模型侧收缩为 `read/write/edit/bash`，用强沙盒承载通用执行；将任务、记忆、安全、审计、权限和部署保留在宿主 AIOS 内核中；将具体能力沉淀为脚本、Skill和Extension。

这能同时保留 AIOS长期自治与可审计的目标，以及 Pi Agent极简、高组合性的优势。
