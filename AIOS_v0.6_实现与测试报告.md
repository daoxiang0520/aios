# AIOS v0.6 实现与测试报告

## v0.6.4 Skill Utility & Replay Benchmark

- 新增 `SkillUtilityEvaluator`，按成功、真实完成、Model Calls、Tokens、延迟和失败计算 Utility，并持久化 `skill_replay_reports`。
- `skill replay` 在 Docker 中多次重放候选测试，记录成功率、中位/P95 延迟、输出确定性，并与 Manifest 明确列出的 `replay_task_ids` 真实任务指标比较。
- 新增 `skill compare` 查询最新 Replay 报告，`skill utility` 从实际 Skill Telemetry 计算晋升后的关联性 Utility 与 Negative Transfer Rate。
- Agent 候选的 Promote 门升级为 `Docker Benchmark Pass AND Replay Not Worse AND no Negative Transfer`；人工批准仍保留，且不自动晋升。
- 缺少历史基线、基线任务无结果或基线已使用同名 Skill 时，证据级别为 `insufficient_historical_baseline` 并阻止 Agent 候选晋升。
- 直接 Replay 无法观测端到端 Token，因此评分时将 Token 保持为基线值，不把未知值伪装为零；Skill-enabled Model Calls 明确标记为两次往返代理。
- 当前属于 Execution Proxy + Historical Baseline，不宣称严格因果 A/B。精确 A/B 需要后续保存任务执行前 Workspace Capsule，再分别运行 baseline/skill-enabled Harness。
- 构建副本与真实仓库均发现 60 项测试：59 项通过，1 项 Docker 实机用例因宿主 `com.docker.service` 停止且当前身份无启动权限而显式跳过。未将跳过计为实机通过；Docker 恢复后应单独复跑。

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

本版只进化 Skill 层。模型可见的 Primitive Tool 仍精确为 `read`、`write`、`edit`、`bash`。

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
- v0.6.2 发布时在真实目标仓库中共 53 项单元/集成测试全部通过，无跳过。
- Docker Engine 可用，真实通过了只读 Skill Dispatcher、`workspace_search` 执行和直接源文件调用拒绝测试。
- 测试覆盖：四原语不增殖、Manifest 与 Lineage 验证、确定性候选、Benchmark 门、人工晋升门、单调版本、回滚、废弃、权限交集、Agent 候选注册、Skill 遥测与标准 Trace、模型调用/token 计数、Runtime 目录隔离和旧版回归。

## 边界与后续

- v0.6 不实现 Workflow Evolution；计划属于 v0.7。
- v0.6 不实现 Harness Evolution；计划属于 v0.8。
- 默认没有外部网络代理，因此声明 `network.external` 的 Skill 仍会进入 `needs_authority`。
- 当 Docker 不可用时，Skill Benchmark 和沙盒任务会明确失败/阻断，不回退到宿主 Shell。
- Python `compile()` 只是静态语法门，不代表安全证明；真实行为安全仍由 Docker 边界和 Capability 检查保证。
