# AIOS v0.6 实现与测试报告

## v0.6.6.1 Runtime Hardening（2026-08-28）

Task 55 的失败链已定位为 Runtime 复合故障：`python ... | head` 由末端命令覆盖退出码；模型请求 180 秒却被旧的 60 秒配置上限静默截断；科学依赖在任务私有环境中重复安装；第六轮仅因 CycleBudget 耗尽就强制 `tool_choice=none`；XLSX 默认大预览与历史 Tool Result 持续堆入 HOT context。它不是“建模能力缺失”。

本版实现：

1. Docker Bash 固定使用 `bash -o pipefail -lc`。
2. `default_timeout_seconds=60` 与 `max_timeout_seconds=300` 分离，并兼容读取旧 `timeout_seconds`。
3. 显式引入 `CycleBudget` 与 `TaskBudget`；默认 Task 上限为 24 Model Calls、32 Tool Calls、300000 Tokens、6 Cycles。
4. 非终态周期耗尽写入 `budget_deferred` checkpoint，提交阶段性 Workspace，并投递 `TASK_CONTINUE`；continuation 不增加 attempts。
5. Controller 仅在 `budget.force_final=true` 的真实 TaskBudget 终点禁用工具，Cycle 最后一轮保持 Tool Calling。
6. Resource Observation 默认压缩为：text/PDF 12000 字符、CSV 5 行、XLSX 每 Sheet 3 行；显式 offset/limit 仍可分页读取。
7. Tool Result 全量进入 Trace，模型侧只保留受限 Observation；超过 HOT 窗口的旧结果降为摘要与 `trace:<id>` 引用。
8. 新增可复用 `scientific-py312-v1` 环境，固定 numpy、pandas、scipy、statsmodels、openpyxl、pypdf；首次准备后跨任务只读复用。任务私有 `/deps` 则跨 continuation 保留至真实终态。
9. 新增累计指标：Task Cycles、Model Calls、Tokens、Rounds To First Computation、Dependency Provision Latency。

确定性回归新增 5 项，覆盖 timeout 迁移、180 秒请求、pipefail 启动参数、checkpoint continuation/attempt 不增加、HOT context compaction；Docker 可用环境另执行真实 `python ... | head` 失败传播测试。Task 55 原题作为最终真实模型验收，不以单元模拟代替。

实际仓库验收结果：完整 86 项测试全部通过、无跳过。真实 Docker 探针 `python -c "import sys; sys.exit(7)" | head -5` 返回 `exit_code=7`，ToolExecutor 记录 `ok=false / Command exited with 7`。科学环境首次准备耗时 203725 ms（超过旧 60 秒阈值但在新 300 秒上限内完成），紧接着第二次加载命中缓存，仅 0.073 ms。

Task 55 已恢复为 queued，等待真实模型重跑。该验收会把 `workspace/MathModeling/题目分析.md` 与 `附件.xlsx` 的任务相关内容发送到用户配置的外部模型；由于这属于本地数据外发，需单独明确授权后才能执行，不能用“已允许联网/安装依赖”替代数据发送授权。

## v0.6.6 Resource Adapter & Harness Perception

### 迭代动机

任务 52、53 暴露的主要问题不是模型缺少任务求解知识，而是环境发现与数据接入成本过高：Agent 在真正阅读题目前，连续使用 `ls`、`find`、`file`、Python Import 探测和依赖安装，消耗了大部分 Model Round。任务 53 在第 5 轮成功安装 `pymupdf/openpyxl`，但第 6 轮已被强制收尾，最终只能进入 `degraded`。

本次迭代采用以下边界：

```text
Specialize Perception, not Cognition

模型可见：read / write / edit / bash
                    │
                    ▼
             Resource Adapter
       ┌────────┬─────┬─────┬─────┬─────┐
       ▼        ▼     ▼     ▼     ▼     ▼
   directory  text   CSV   ZIP   PDF   XLSX
```

Harness 负责稳定、重复的数据接入；模型仍然决定读取范围、分析方法、是否继续取样以及如何完成用户任务。系统没有加入 `read_pdf`、`read_excel` 等专用 Tool，也没有硬编码“数模题分析流程”。

### 实现内容

- 新增 `ResourceAdapter`，置于既有 `read` Primitive 下方；模型 Tool Schema 仍精确保持 `read/write/edit/bash` 四项。
- `read` 统一返回 `resource.path / type / metadata / representations` 结构：
  - Directory：条目类型、工作区相对路径、大小和修改时间；
  - Text/Code：字符总数、有界文本表示、Offset 与截断标记；
  - CSV：行列规模与最多 100 行的有界预览；
  - ZIP：最多 200 个归档条目及压缩前后大小；
  - PDF：页数、文本字符数、加密状态和有界文本表示；
  - XLSX：工作表名称、行列规模及每个 Sheet 的有界行预览。
- 旧版隐藏接口 `list_files/read_file` 继续通过兼容投影返回原有列表或文本，避免破坏 legacy Plugin。
- Capability Registry 新增 `resource.read=available`，并声明 `directory/text/csv/zip/pdf/xlsx` Adapter；它是环境接口能力，不是可自主晋升的任务 Skill。
- Runtime 首轮上下文新增有界 Workspace Inventory：最多 200 个文件的相对路径与大小，排除 `.aios` 内部路径，并明确要求模型不要对已知路径重复执行 `ls/find/file`。
- PDF/XLSX 通过固定、非模型生成的 Python Adapter 在 Docker 内解析：
  - 工作区与依赖目录只读挂载；
  - 丢弃全部 Linux Capabilities，启用 `no-new-privileges`；
  - 正式解析阶段强制 `--network none`；
  - 路径必须是工作区相对路径，拒绝绝对路径和 `..`；
  - Adapter 只输出结构化观察，不执行总结或任务策略。
- 任务级 `/deps` 在同一任务的多次 Docker 调用之间持久，任务结束时删除；缺少 PDF/XLSX 依赖时，仅安装固定版本 `pypdf==5.4.0`、`openpyxl==3.1.5`。它不污染宿主 Python，也不会进入 Workspace 提交结果。
- `capabilities.network_enabled=true` 时允许依赖安装使用 Docker Bridge；关闭时安装会明确失败，不回退宿主 Shell。依赖安装完成后的资源解析始终断网。
- 默认与示例 `max_model_calls_per_cycle` 从 4 调整为 6；Tool Call 上限仍为 8，最终回答仍保留一轮。
- 最终轮连续产生序列化 Tool Markup 时，一次无 Tools 修复后若仍失败，Controller 生成协议干净、携带最后工具证据的受限回答；Verifier 将其标记为 `degraded`，不再因为相同协议污染直接进入死信。
- 包版本升级为 `0.6.6`，README 同步记录 Resource Adapter 的职责与“不替代认知策略”边界。

### 测试与真实验证

- 完整回归共发现 81 项测试，81 项全部通过，无跳过。
- 新增/更新测试覆盖：
  - 模型可见 Tool 仍只有四项；
  - `/workspace` 路径语义与目录结构化返回；
  - Workspace Inventory 的 200 项上限、截断标记和 `.aios` 排除；
  - Text Offset/Limit、CSV Preview、ZIP Listing；
  - PDF/XLSX 固定路由到 Sandbox Adapter；
  - `/deps` 在两次独立真实 Docker 调用之间可见；
  - Docker Bridge 开关、依赖环境变量和只读边界；
  - 两次 DSML 最终回答失败转为协议干净的 `degraded` 结果。
- 使用真实工作区文件进行不调用模型的集成验证：
  - `MathModeling/C题.pdf` 成功识别为 `application/pdf`，页数为 2，并按 `limit=2000` 返回 2000 个文本字符；
  - `MathModeling/附件.xlsx` 成功识别为 OOXML Spreadsheet，发现 2 个工作表，并按每表 3 行生成预览；
  - 验证过程只输出页数、类型、Sheet 数和预览行数等元数据，没有把文档内容发送给外部模型。

### 当前限制

- PDF Adapter 当前只做文本层提取；扫描版 PDF 尚无 OCR，复杂表格和版面结构也未还原。
- XLSX 当前提供 Sheet 元数据和有界行预览，不直接生成 DataFrame、公式依赖图或全表统计；模型可按需继续分页读取。
- Image、音视频及非 ZIP Archive 尚未进入 Resource Adapter；未知二进制格式只返回元数据与“无已注册表示”说明。
- `/deps` 是任务级而非跨任务缓存，同一依赖在新任务中可能重新下载；这是隔离性与启动成本之间的当前取舍。
- 当前 Budget 仍以 Model Round 与 Tool Call 数量为主，尚未实现按 Execution Cost、Observation Cost 加权的成本模型。
- `resource.read` 已进入 Root Capability，但“运行中发现新格式→能力重评估→生成候选 Adapter/Skill”的动态进化闭环仍未实现。

## v0.6.5 Task Capsule & Counterfactual Skill Evaluation

- 路径语义热修复：Primitive 文件工具现在把容器路径 `/workspace` 和 `/workspace/...` 安全映射到 Host 侧事务 Workspace；相对路径行为保持不变，`/workspace/..`、相似前缀、`/aios-state` 与其他绝对路径仍拒绝。任务 50 的失败调用 `read({"path":"/workspace"})` 已加入回归测试。
- 完全出网热修复：`capabilities.network_enabled=true` 现在真实传递到 Runtime、Skill Benchmark 与 `aiosctl`，Docker 从 `--network none` 切换为 `--network bridge`，Capability Registry 同步标记 `network.external=available` 和 `mode=unrestricted`。关闭时仍保持 `none/needs_authority`；`allowed_domains` 当前不执行白名单约束。
- 新增 `experiments/` 子系统：`ContentAddressedSnapshotStore`、`CapsuleManager`、`ExperimentOrchestrator`、`RuntimeVariantRunner`、`CounterfactualEvaluator` 与 `PairwiseSemanticJudge`。
- Capsule 以 Manifest + Immutable References 保存任务、执行前 Workspace、Active Skill 集、Capability、Harness、Model、Sandbox 镜像身份和外部依赖指纹；工作区与 Skill 文件按 SHA256 去重存储。
- 完整初始状态哈希同时覆盖 Task、Workspace、Active Skills、Capability、Harness、Model 与 Environment。Baseline/Candidate 在应用显式 Skill Mutation 前必须具有相同状态哈希。
- 每个变体默认独立恢复并真实重跑完整 Agent Loop 3 次；实验世界位于受管目录，运行后销毁，生产 Workspace 不会被 replay 修改。
- Run Evidence 分离 Skill 执行、Task Verifier 和最终 Task 状态，并记录模型调用、Token、延迟、安全违规、产物哈希与 Trace。
- Counterfactual Report 保留完成率、Verifier、调用数、Token、Median/P95 延迟等完整指标向量；Scalar Utility 不再是唯一判断依据。
- 晋升状态升级为 `REJECTED / INSUFFICIENT_EVIDENCE / NEEDS_REVIEW / PROMOTABLE`。正确率下降或安全违规直接拒绝；质量提高但成本增加进入人工复核；Partial Fidelity 只能进入复核。
- Semantic Judge 为可选 Tier-2 软证据，通过 A/B 顺序反转检测位置偏差；它不能覆盖 Tier-1 的安全、Verifier 和测量结果。
- v0.6.4 Historical Replay 保留，但 Agent 生成的 Skill 不再能仅凭历史代理证据晋升；必须先通过 Docker Benchmark 和同起点 Paired Counterfactual Replay，之后仍需人工批准。
- 新增 SQLite 表：`task_capsules`、`capsule_objects`、`experiments`、`experiment_variants`、`experiment_runs`、`counterfactual_reports`、`semantic_judgements`。
- 新增 CLI：`capsule capture/show/list/verify/fork/archive/delete`、`experiment run/show/compare`、`skill counterfactual-replay`。
- v0.6.5 专项 10 项测试全部通过，覆盖同起点、实验隔离、Host 不污染、单变量、3×2 重复运行、随机聚合、坏 Skill、质量成本冲突、Judge 位置偏差、Capsule 损坏和晋升证据门。
- 构建副本在受限测试环境中发现 76 项测试：75 项通过，1 项 Docker 实机用例显式跳过；真实仓库在宿主权限下 76 项全部通过。额外以 `python:3.12-slim`、只读根文件系统、Drop ALL Capabilities 和 Docker Bridge 实测访问 `https://example.com` 返回 HTTP 200。新增测试分别锁定“开启即 bridge/available/unrestricted”、“关闭仍 none/needs_authority”，以及成功的 Python `urllib.request` 调用可形成 Verifier 网络证据。

## v0.6.4 Skill Utility & Replay Benchmark

- 新增 `SkillUtilityEvaluator`，按成功、真实完成、Model Calls、Tokens、延迟和失败计算 Utility，并持久化 `skill_replay_reports`。
- `skill replay` 在 Docker 中多次重放候选测试，记录成功率、中位/P95 延迟、输出确定性，并与 Manifest 明确列出的 `replay_task_ids` 真实任务指标比较。
- 新增 `skill compare` 查询最新 Replay 报告，`skill utility` 从实际 Skill Telemetry 计算晋升后的关联性 Utility 与 Negative Transfer Rate。
- Agent 候选的 Promote 门升级为 `Docker Benchmark Pass AND Replay Not Worse AND no Negative Transfer`；人工批准仍保留，且不自动晋升。
- 缺少历史基线、基线任务无结果或基线已使用同名 Skill 时，证据级别为 `insufficient_historical_baseline` 并阻止 Agent 候选晋升。
- 直接 Replay 无法观测端到端 Token，因此评分时将 Token 保持为基线值，不把未知值伪装为零；Skill-enabled Model Calls 明确标记为两次往返代理。
- 当前属于 Execution Proxy + Historical Baseline，不宣称严格因果 A/B。精确 A/B 需要后续保存任务执行前 Workspace Capsule，再分别运行 baseline/skill-enabled Harness。
- 修复能力预检误报：仅提及 Python 源文件不再要求 `process.sandbox_exec`；只有运行、执行、测试、Shell/Bash/命令等执行语义才要求 Docker 沙盒。任务 48 的原始提示词已加入回归测试。
- 构建副本与真实仓库均发现 61 项测试：60 项通过，1 项 Docker 实机用例因宿主 `com.docker.service` 停止且当前身份无启动权限而显式跳过。未将跳过计为实机通过；Docker 恢复后应单独复跑。

## v0.6.3 Skill Authoring Contract

- 修复 Task 42 暴露的 Skill 创作预算浪费：候选包完整前不执行 Bash 探查，至少为 `manifest.json` 与 `skill.py` 保留写入额度。
- 将候选目录、Manifest、输入 Schema、Capability、基础测试及 `--input-json` 入口要求作为确定性创作协议注入模型。
- Verifier 新增 `valid_skill_candidate_package` 检查；缺任一文件、目录与 Manifest 名称不一致、版本非法、无测试或源码不可编译都不能完成任务。
- “编写基础测试”表示在 Manifest 中声明候选测试，不再被错误解释为立即执行未注册候选；Benchmark 和 Promote 仍走宿主生命周期门。
- 协议修复失败现在记录 `finish_reason`、正文类型/长度与 reasoning 长度，不记录或泄露模型正文。
- 在真实目标仓库中 56 项测试全部通过，无跳过；包含 Docker Skill Dispatcher 实机边界测试，以及“能力询问不触发创作”的意图边界测试。

## v0.6.2 Skill Telemetry + Lineage

- 新增 `skill_usage` 持久表，记录 Skill/版本、任务、轮次、调用顺序、耗时、退出码、Capability 评估、Verifier 结果和任务结果。
- 为避免将用户输入复制到长期遥测中，只记录规范化输入 SHA-256 与字段名，不保存原始 Skill 输入。
- 每次调用统一产生 `SKILL_INVOKE`、`SKILL_CAPABILITY_CHECK`、`SKILL_RESULT` 三类 Trace。
- 新增 `aios skill telemetry` 与 `aiosctl skill-usage list` 查询入口，同时将最近遥测暴露为沙盒只读状态。
- Manifest 新增 `parent_version`、`mutation_reason`、`source_task_ids`、`source_trace_ids`、`hypothesis`、`benchmark_delta` Lineage 字段。
- 现有 Skill 的新版本在 Promote 前必须显式指向当前 Active 版本，防止无来源替换。
- Agent 生成的候选在未显式填写时，会自动继承来源 Task ID 和当前 Cycle Trace IDs。
- `state_query` 升级到 1.1.0，可查询 `skill-usage`。

## v0.6.1 协议与边界修复

- 最终轮收到序列化 DSML 时，只进行一次不带 Tools 的最终答案修复，不重跑已执行动作。
- 修复仍失败时视为终止性协议失败，不再完整重试同一任务三次。
- Skill Dispatcher 调用改为 Shell token 级精确校验，禁止用 `;`、管道或重定向捆绑直接 `/skills` 访问。
- Windows CLI 将 stdout/stderr 统一配置为 UTF-8，避免中文、Emoji 和 Trace JSON 触发 GBK 编码异常。

## 版本目标

v0.6 将高阶能力的进化从“增加模型可见 Tool Schema”迁移到 Skill 层：

```text
Root Capability → Primitive Tool → Skill → Workflow → Harness
```

v0.6.0–v0.6.5 只进化 Skill 层；v0.6.6 新增的 Resource Adapter 是不可由 Agent 绕过治理门修改的基础环境感知层，不是任务 Workflow。模型可见的 Primitive Tool 仍精确为 `read`、`write`、`edit`、`bash`。

## 已实现

1. 新增 Skill Manifest，支持名称、SemVer、说明、输入 Schema、测试、来源和所需 Capability。
2. 新增 Skill Registry 及 `candidate / active / history / deprecated / reports` 生命周期。
3. 新增确定性候选 ID，相同 Manifest 和源码不会重复创建。
4. 新增 Docker-only Benchmark：候选 Skill 以只读方式挂载，禁网、只读根文件系统、丢弃 Linux capabilities，且没有宿主执行回退。
5. Promote 必须通过 Benchmark，默认还必须显式 `--approve`；新版本必须高于当前版本。
6. 支持 Rollback 与 Deprecated，两者默认同样需人工确认。
7. 新增只读 Skill Runtime 投影。Agent 只能看到 Dispatcher 和 Active Skill，不能看到候选、报告、历史或废弃代码。
8. Skill 只能经 `python /skills/skill.py run ...` 调用，沙盒拒绝直接执行 Active Skill 源文件。
9. Dispatcher 在执行前使用只读状态快照检查 Manifest 声明 Capability 与 Host 授权的交集。
10. Agent 可在 `workspace/skill_candidates/<name>/` 写入 `manifest.json` 和 `skill.py`。任务成功提交后宿主才注册候选；默认不自动测试或晋升。
11. 内置 `workspace_search`、`state_query`、`trace_failure_analyzer` 三个 Skill，用于代替过去增殖的专用查询 Tool。
12. 新增 `aios skill list/candidates/show/versions/propose/benchmark/promote/rollback/deprecate/bootstrap` CLI。
13. v0.4 生成插件继续作为 legacy 兼容层，不进入模型 Tool Schema。

## 验收结果

- Python 源码与测试编译检查通过。
- 当前 v0.6.6 在真实目标仓库中共 81 项单元/集成测试全部通过，无跳过；v0.6.2 发布时的 53 项记录保留在版本历史中。
- Docker Engine 可用，真实通过只读 Skill Dispatcher、`workspace_search`、直接源文件调用拒绝、任务级 `/deps` 跨调用持久性，以及真实 PDF/XLSX Resource Adapter 测试。
- 测试覆盖：四原语不增殖、结构化资源观察、Workspace Inventory、路径隔离、Manifest 与 Lineage、确定性候选、Benchmark/Counterfactual/人工晋升门、单调版本、回滚、废弃、权限交集、Agent 候选注册、Skill 遥测与标准 Trace、模型调用/Token 计数、Runtime 目录隔离和旧版回归。

## 边界与后续

- v0.6 不实现 Workflow Evolution；计划属于 v0.7。
- v0.6 不实现 Harness Evolution；计划属于 v0.8。
- 示例配置默认关闭网络；当前用户配置已显式启用完全出网，Docker 使用 unrestricted Bridge。系统尚无域名白名单代理，`allowed_domains` 目前不构成强制约束。
- 当 Docker 不可用时，Skill Benchmark 和沙盒任务会明确失败/阻断，不回退到宿主 Shell。
- PDF/XLSX Adapter 依赖 Docker；依赖尚未安装且网络关闭时会明确失败。已安装依赖后的解析过程固定断网。
- Resource Adapter 特化数据接入，不特化任务思考；数模分析、文献综述、仓库分析等仍属于 Model/Skill/Workflow 层。
- Python `compile()` 只是静态语法门，不代表安全证明；真实行为安全仍由 Docker 边界和 Capability 检查保证。
