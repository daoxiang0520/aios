# AIOS v0.6 实现与测试报告

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
- 在真实目标仓库中共 47 项单元/集成测试全部通过，无跳过。
- Docker Engine 可用，真实通过了只读 Skill Dispatcher、`workspace_search` 执行和直接源文件调用拒绝测试。
- 测试覆盖：四原语不增殖、Manifest 验证、确定性候选、Benchmark 门、人工晋升门、单调版本、回滚、废弃、权限交集、Agent 候选注册、Runtime 目录隔离和旧版回归。

## 边界与后续

- v0.6 不实现 Workflow Evolution；计划属于 v0.7。
- v0.6 不实现 Harness Evolution；计划属于 v0.8。
- 默认没有外部网络代理，因此声明 `network.external` 的 Skill 仍会进入 `needs_authority`。
- 当 Docker 不可用时，Skill Benchmark 和沙盒任务会明确失败/阻断，不回退到宿主 Shell。
- Python `compile()` 只是静态语法门，不代表安全证明；真实行为安全仍由 Docker 边界和 Capability 检查保证。
