# AIOS v0.9-alpha.6 Component Lineage 设计与测试报告

## 目标

把 v0.7 已建立的统一 `ComponentRegistry` 接入 Autonomous Lineage，使实验谱系不再只遗传
Harness 参数，而是同时遗传一个版本化、内容寻址的 `ComponentSet`。本版不重写既有 Skill、
Plugin 或实验系统，也不扩大 Root of Trust。

## 统一模型

每个谱系现在持久化：

```text
SystemLineage
├── harness settings
└── component_set
    ├── primitive
    ├── skill
    ├── workflow
    ├── plugin
    ├── resource_adapter
    ├── environment_provider
    └── kernel_component
```

`component_set/v1` 的每个 member 保存 `component_id / version_id / kind / name / version /
content_digest / provides / source`，整个集合生成确定性的 `active_set_hash`。旧谱系在首次读取时
从父谱系继承；旧根谱系从当前 Component Registry 做一次兼容快照。

## 可执行进化边界

所有 Component kind 都进入统一合同和谱系清单，但只有 `skill` 当前开放：

- Skill 已有 Agent Candidate、Manifest 验证、沙盒 Benchmark、版本与运行器；
- `AUTHOR_COMPONENT_CANDIDATE` 可由谱系依据有界、脱敏的自身任务证据创作 `kind=skill` 候选；
- Host 强制写入 Agent 来源与任务谱系，完成 Manifest/源码校验后仅在 Docker 中执行 Benchmark；
- Authoring 只产生 Variation，不改变当前谱系、子谱系或生产状态；
- `ADOPT_COMPONENT_CANDIDATE` 只接受 Benchmark `passed=true` 的 Skill Candidate；
- Candidate 被加入隔离子谱系，不会 Promotion 到生产 SkillRegistry；
- 子谱系任务运行前物化独立 `/skills` 投影，同一任务的 continuation 继续使用相同集合；
- 全局 Active Skill、Plugin、Harness 和生产默认均不改变。

其余类型保持可见但关闭：

- Workflow：尚无统一可执行 Candidate lifecycle；
- Plugin：Host sidecar 与权限边界；
- Resource Adapter：证据接入边界；
- Environment Provider：执行环境边界；
- Primitive / Kernel Component：Kernel 与 Root of Trust。

这满足 `Component → provides Capability` 的统一表达，同时避免把“能登记”误写成“能自主执行”。

## Runtime 与可观察性

- `evolution_lineages` 新增 `component_set` 与 `source_component_candidate_id`，并提供旧数据库迁移；
- Root/Child Lineage 统一标记为 `kind=system`；
- Runtime 根据任务绑定谱系选择对应 Skill projection 和模型可见 Skill catalog；
- `lineage_bound` Trace 增加 `component_set_hash`；
- Web UI 谱系选项显示代际和 Component 数量，任务页显示集合数量与短哈希；
- 历史模型 `reason` 继续完整保存在审计记录，但不会再回灌给下一轮模型；
- 任务状态清单及语义继续作为权威事实提供给 Lineage Reasoner。

## 决策合同

Lineage Reasoner 当前动作：

```text
CONTINUE
ADOPT_CANDIDATE                 # Harness Candidate
FORK_MUTATION                   # 新 Harness Candidate
AUTHOR_COMPONENT_CANDIDATE      # 创作并 Benchmark 一个隔离 Skill Candidate，不采用
ADOPT_COMPONENT_CANDIDATE       # 已 Benchmark 的 Skill Component Candidate
RETURN
```

模型会看到每种 Component kind 的 `mutable / lineage_adoption / reason`，Host 只验证 Candidate
真实性、Benchmark、类型策略和继承关系，不判断该组件是否“更优”。

## 测试

专项测试覆盖：

1. 旧谱系自动获得 Component Set；
2. Harness fork 完整继承 Component Set；
3. 未 Benchmark 的 Skill Candidate 不能进入谱系；
4. 已 Benchmark Skill Candidate 只进入实验子谱系；
5. Candidate Skill 能物化为独立 Runtime projection 和 catalog；
6. 生产 Active Skill 不改变；
7. 七类 Component 的开放/关闭策略显式交付；
8. 历史 `reason` 不进入下一轮 Prompt；
9. 任务状态语义与 `CONTINUE` 效果保持明确；
10. Component、Skill、Lineage、Runtime 与 Web UI 兼容回归。
11. DeepSeek 出站证据的凭据和 Host 路径脱敏；
12. Authoring 后候选已 Benchmark，但谱系数量、生产 Skill 与生产默认均不改变。

本次专项 `test_v090_alpha5.py`：21 项通过，0 失败。完整套件运行 260 项，其中 255 项通过、
3 项因 Docker 在测试进程中不可用而跳过；2 项旧 v0.5 网络能力断言失败（期望静态网络可用，
当前实现按 Docker operational health 判为 unavailable），与本次 Candidate Authoring 路径无关。

## 结论

Alpha.6 完成的是：

```text
Harness Lineage
→ System Lineage(Harness + ComponentSet)
```

它第一次允许 AI 让一个实际可执行 Skill 成为自身实验后代的一部分，但仍不等于生产自动升级，
也不代表 Workflow/Plugin 等类型已经具备安全的自主 Candidate runner。下一阶段若开放新类型，应先为
该类型补齐 Candidate package、隔离执行、外部评价、版本解析和回退，而不是只切换 `mutable=true`。
